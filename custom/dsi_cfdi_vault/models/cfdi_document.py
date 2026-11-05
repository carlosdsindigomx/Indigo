import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta

import psycopg2
from lxml import etree

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError

from .cfdi_efos_entry import EFOS_CLEARED_SITUATIONS

_logger = logging.getLogger(__name__)

# Tolerancia para la conciliación automática de nivel 2 (RFC + total + fecha).
MATCH_AMOUNT_TOLERANCE = 0.05
MATCH_DATE_TOLERANCE_DAYS = 3

# Un CFDI de Pago ampara el pago y uno de Nómina, la póliza del periodo: ninguno
# se liga por RFC, total y fecha. Una N emitida tiene como contraparte el RFC
# del empleado y podía ligarse a un reembolso o una factura suya.
TIPOS_SIN_LIGA_POR_IMPORTE = ('P', 'N')

# Nivel 3: coincidencia parcial con score (ventana amplia de búsqueda de candidatos,
# el score decide qué tan buena es cada sugerencia — no se vincula solo).
SUGGEST_DATE_WINDOW_DAYS = 30
SUGGEST_AMOUNT_WINDOW_RATIO = 0.30
SUGGEST_MAX_CANDIDATES = 5

# Cron de verificación de estatus SAT: periodicidad decreciente.
SAT_CHECK_RECENT_WINDOW_DAYS = 30   # CFDI de los últimos 30 días
SAT_CHECK_RECENT_INTERVAL_DAYS = 1  # ... se revisan a diario
SAT_CHECK_OLD_INTERVAL_DAYS = 30    # CFDI más antiguos ... se revisan mensualmente

# Errores de PostgreSQL cuando otra transacción tocó lo mismo a la vez: la
# verificación SAT, la revisión de discrepancias y un CFDI que se vincula
# pueden coincidir. No son fallas: se deshace esa parte y la recoge la
# siguiente corrida. Odoo reintenta una petición web por los tres primeros;
# dentro de un cron o de un savepoint nadie los reintenta.
ERRORES_DE_CONCURRENCIA = (
    psycopg2.errors.SerializationFailure,
    psycopg2.errors.DeadlockDetected,
    psycopg2.errors.LockNotAvailable,
    psycopg2.errors.UniqueViolation,
)

# Respuesta de l10n_mx_edi.document._fetch_sat_status -> sat_status. Cualquier
# otra ('error', 'not_defined') deja el estatus como estaba.
SAT_STATUS_BY_RESULT = {
    'valid': 'valid',
    'cancelled': 'canceled',
    'not_found': 'not_found',
}

# Lo que cambia el Estado bóveda de una factura: a qué factura está ligado el
# CFDI, qué dijo el SAT y si el CFDI sigue activo. Al cambiar, se revisan las
# discrepancias de estatus de la factura de antes y la de después.
CAMPOS_DEL_ESTADO_BOVEDA = {'move_id', 'sat_status', 'active'}

# Lo que decide a qué facturas se liga un pago (REP): al cambiar, se vuelven a
# ligar. La factura del CFDI de ingreso o egreso (la liga de respaldo), la
# empresa (solo se liga dentro de ella) y la dirección, que se puede corregir
# en el formulario (un REP recibido solo paga compras y uno emitido, ventas).
CAMPOS_DE_LA_LIGA = {'move_id', 'company_id', 'direction'}

# Lo que escribe la verificación SAT, y nadie más: de ahí sale el Estado
# bóveda de la factura, con ellos se resuelven sus discrepancias de estatus y
# la última verificación dice si una «vigente en la Bóveda, cancelado en Odoo»
# ya se volvió a consultar. El Usuario de la Bóveda verifica con su botón; a
# mano solo los corrige el gestor contable.
CAMPOS_DE_LA_VERIFICACION = {'sat_status', 'sat_last_check'}

# Verificación en lote desde la lista: cada CFDI es una consulta al servicio
# público del SAT (~0.6 s, hasta 20 s si no responde). Se corta por tiempo y no
# por cantidad para no rebasar el límite de una petición web; lo que no alcanzó
# se verifica al volver a ejecutar la acción.
BATCH_VERIFY_SECONDS = 60


def _local(tag):
    """Strip the XML namespace from a tag, e.g. '{http://...}Comprobante' -> 'Comprobante'.

    Los comentarios y las instrucciones de proceso también salen al recorrer
    el XML (root.iter() y los hijos de un nodo), y su tag no es texto sino una
    función de lxml: para ellos, ''. Antes un comentario antes del timbre, o
    en la Addenda de una nómina, tumbaba la lectura con un TypeError."""
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _numero(valor):
    try:
        return float(valor or 0.0)
    except ValueError:
        return 0.0


def _fecha(valor):
    """La fecha de un atributo AAAA-MM-DDTHH:MM:SS del CFDI, o False si no
    viene o no es una fecha: un dato mal formado no debe impedir guardar el
    CFDI."""
    try:
        return fields.Date.to_date((valor or '')[:10]) or False
    except ValueError:
        return False


def _rfc(valor):
    """RFC comparable: sin espacios ni guiones, en mayúsculas y sin el prefijo
    de país que a veces se captura en el RFC de la empresa (MXEKU9003173C9)."""
    rfc = re.sub(r'[\s-]', '', valor or '').upper()
    if rfc.startswith('MX') and len(rfc) in (14, 15):
        rfc = rfc[2:]
    return rfc


def _empresa_con_rfc(empresa):
    """La empresa cuyo RFC usa `empresa` para recibir CFDI.

    Es la regla del import de Odoo 20: la más cercana con RFC, ella misma o su
    matriz. Una sucursal sin RFC propio recibe con el de su matriz. Si ninguna
    tiene RFC, la propia empresa.
    """
    empresa = empresa.sudo()
    return empresa.parent_ids[::-1].filtered('partner_id.vat')[:1] or empresa


def _rfc_receptor_de(empresa):
    """El RFC, comparable, con el que recibe CFDI `empresa`; vacío si no tiene."""
    return _rfc(_empresa_con_rfc(empresa).partner_id.vat)


def _empresas_del_mismo_rfc(empresas):
    """Las empresas que reciben con el mismo RFC que alguna de `empresas`:
    matriz y sucursales, o dos empresas capturadas para el mismo
    contribuyente. Un CFDI registrado en una de ellas ya está registrado para
    ese RFC. Una empresa sin RFC solo se tiene a sí misma."""
    empresas = empresas.sudo()
    rfcs = {_rfc_receptor_de(empresa) for empresa in empresas} - {''}
    if not rfcs:
        return empresas
    return empresas | empresas.search([]).filtered(lambda c: _rfc_receptor_de(c) in rfcs)


def _monto_de_pagos(root):
    """(monto, código de moneda) de un CFDI de Pago, leídos del complemento.

    En un CFDI de Pago el SAT exige Total="0" y Moneda="XXX" en el comprobante:
    lo pagado va en el complemento de pagos. Leer el Total dejaba todos los
    pagos en 0. Pagos 2.0 trae el total en pesos en Totales/@MontoTotalPagos;
    Pagos 1.0 (CFDI 3.3) no tiene Totales y hay que sumar cada Pago/@Monto en
    su MonedaP.
    """
    pagos = next((el for el in root.iter() if _local(el.tag) == 'Pagos'), None)
    if pagos is None:
        return 0.0, False
    totales = next((el for el in pagos if _local(el.tag) == 'Totales'), None)
    if totales is not None and totales.get('MontoTotalPagos'):
        return _numero(totales.get('MontoTotalPagos')), 'MXN'
    lista = [el for el in pagos if _local(el.tag) == 'Pago']
    monedas = {el.get('MonedaP') or 'MXN' for el in lista}
    if len(monedas) == 1:
        return sum(_numero(el.get('Monto')) for el in lista), monedas.pop()
    # Pagos en varias monedas: se suman en pesos con el tipo de cambio de cada uno.
    return sum(_numero(el.get('Monto')) * _numero(el.get('TipoCambioP') or 1) for el in lista), 'MXN'


def _doctos_relacionados(root):
    """Los DoctoRelacionado de un CFDI de Pago, uno por dict, con los datos del
    nodo Pago al que pertenecen. Pagos 1.0 y 2.0, todo con .get(): hay REP sin
    NumParcialidad, y lo que no viene queda vacío o en cero.

    - El folio fiscal (IdDocumento) va en mayúsculas: hay PAC que lo timbran en
      minúsculas y la Bóveda guarda los UUID en mayúsculas.
    - La equivalencia es EquivalenciaDR en Pagos 2.0 y TipoCambioDR en 1.0; en
      los dos, unidades de la moneda de la factura por una de la del pago. Si
      no viene, las dos monedas son la misma: 1.
    - Pagos 1.0 permite omitir ImpPagado cuando el pago trae un solo documento:
      entonces es el monto del pago, en la moneda de la factura.
    """
    pagos = next((el for el in root.iter() if _local(el.tag) == 'Pagos'), None)
    if pagos is None:
        return []
    resultado = []
    for pago_idx, pago in enumerate((el for el in pagos if _local(el.tag) == 'Pago'), 1):
        doctos = [el for el in pago if _local(el.tag) == 'DoctoRelacionado']
        datos_del_pago = {
            'pago_idx': pago_idx,
            'fecha_pago': _fecha(pago.get('FechaPago')),
            'forma_pago': pago.get('FormaDePagoP') or False,
            'moneda_p': pago.get('MonedaP') or False,
            'tipo_cambio_p': _numero(pago.get('TipoCambioP') or 1),
            'monto_pago': _numero(pago.get('Monto')),
        }
        for docto_idx, docto in enumerate(doctos, 1):
            equivalencia = _numero(docto.get('EquivalenciaDR') or docto.get('TipoCambioDR') or 1) or 1.0
            pagado = docto.get('ImpPagado')
            if pagado is None and len(doctos) == 1:
                pagado = datos_del_pago['monto_pago'] * equivalencia
            resultado.append({
                **datos_del_pago,
                'docto_idx': docto_idx,
                'id_documento': (docto.get('IdDocumento') or '').strip().upper(),
                'serie': docto.get('Serie') or False,
                'folio': docto.get('Folio') or False,
                'moneda_dr': docto.get('MonedaDR') or False,
                'equivalencia_dr': equivalencia,
                'num_parcialidad': int(_numero(docto.get('NumParcialidad'))),
                'imp_saldo_ant': _numero(docto.get('ImpSaldoAnt')),
                'imp_pagado': _numero(pagado),
                'imp_saldo_insoluto': _numero(docto.get('ImpSaldoInsoluto')),
            })
    return resultado


def _datos_de_nomina(root):
    """Lo que sirve del complemento de nómina (nomina12) para encontrar la
    póliza: el tipo, la fecha de pago y el periodo. La Fecha del comprobante
    (invoice_date) es la de emisión, que llega días o semanas después del
    pago: con ella sola no se encuentra la póliza, y dos quincenas timbradas
    el mismo día no se distinguen.

    Un CFDI puede traer más de un nodo Nomina (ordinaria y extraordinaria):
    la fecha de pago más temprana, el periodo que los cubre a todos y 'O' si
    alguno es ordinario. Solo se leen atributos del nodo Nomina, nunca de sus
    hijos: nomina12:Receptor y cfdi:Receptor tienen el mismo nombre local.
    Sin complemento devuelve {}; un dato que falta o no es fecha queda en
    False."""
    nominas = [el for el in root.iter() if _local(el.tag) == 'Nomina']
    if not nominas:
        return {}

    def fechas(atributo):
        return [fecha for fecha in (_fecha(nodo.get(atributo)) for nodo in nominas) if fecha]

    pagos, iniciales, finales = fechas('FechaPago'), fechas('FechaInicialPago'), fechas('FechaFinalPago')
    tipos = {nodo.get('TipoNomina') for nodo in nominas}
    return {
        'dsi_nomina_tipo': 'O' if 'O' in tipos else ('E' if 'E' in tipos else False),
        'dsi_nomina_fecha_pago': min(pagos) if pagos else False,
        'dsi_nomina_fecha_inicial': min(iniciales) if iniciales else False,
        'dsi_nomina_fecha_final': max(finales) if finales else False,
    }


class CfdiDocument(models.Model):
    _name = 'cfdi.document'
    _description = "CFDI"
    _inherit = ['mail.thread']
    _rec_name = 'uuid'
    _order = 'invoice_date desc, id desc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, index=True,
        default=lambda self: self.env.company)
    uuid = fields.Char(string="UUID", required=True, tracking=True)
    move_id = fields.Many2one(
        'account.move', string="Factura", ondelete='set null', tracking=True,
        help="Factura de Odoo vinculada a este CFDI. Vacío si el CFDI aún no está contabilizado.")
    direction = fields.Selection([
        ('issued', "Emitido"),
        ('received', "Recibido"),
    ], string="Dirección", required=True)
    move_type = fields.Selection([
        ('I', "Ingreso"),
        ('E', "Egreso"),
        ('T', "Traslado"),
        ('N', "Nómina"),
        ('P', "Pago"),
    ], string="Tipo de comprobante")
    partner_rfc_issuer = fields.Char(string="RFC emisor")
    partner_name_issuer = fields.Char(string="Nombre emisor")
    partner_rfc_receiver = fields.Char(string="RFC receptor")
    partner_name_receiver = fields.Char(string="Nombre receptor")
    invoice_date = fields.Date(string="Fecha del comprobante")
    amount_total = fields.Monetary(string="Total")
    currency_id = fields.Many2one(
        'res.currency', string="Moneda", required=True,
        default=lambda self: self.env.company.currency_id)
    payment_method = fields.Char(string="Método de pago")
    payment_way = fields.Char(string="Forma de pago")
    sat_status = fields.Selection([
        ('not_verified', "Sin verificar"),
        ('valid', "Vigente"),
        ('canceled', "Cancelado"),
        ('not_found', "No encontrado"),
    ], string="Estatus SAT", default='not_verified', tracking=True, required=True, readonly=True)
    sat_last_check = fields.Datetime(string="Última verificación SAT", readonly=True)
    match_state = fields.Selection([
        ('pending', "Pendiente"),
        ('matched', "Vinculado automáticamente"),
        ('manual', "Vinculado manualmente"),
    ], string="Conciliación", default='pending', tracking=True, required=True)
    match_method = fields.Selection([
        ('uuid_exact', "UUID exacto"),
        ('rfc_amount_date', "RFC + total + fecha"),
        ('manual', "Manual"),
    ], string="Método de vinculación", tracking=True,
        help="Cómo se vinculó este CFDI a su factura: UUID exacto (nivel 1, alta confianza), "
             "RFC + total + fecha con tolerancia (nivel 2), o asignación manual del usuario.")
    source = fields.Selection([
        ('manual_upload', "Carga manual"),
    ], string="Origen", default='manual_upload', required=True)
    xml_attachment_id = fields.Many2one(
        'ir.attachment', string="XML", required=True, ondelete='restrict')
    pdf_attachment_id = fields.Many2one(
        'ir.attachment', string="PDF", ondelete='set null')
    parse_state = fields.Selection([
        ('ok', "OK"),
        ('partial', "Parcial"),
        ('error', "Error"),
    ], string="Resultado del parseo", default='ok', required=True)
    parse_error = fields.Text(string="Detalle del error")
    is_discrepancy = fields.Boolean(
        string="Discrepancia crítica", compute='_compute_is_discrepancy', store=True,
        help="CFDI cancelado en el SAT que sigue vinculado a una factura.")
    is_efos_risk = fields.Boolean(
        string="Riesgo 69-B", compute='_compute_efos_risk', store=True,
        help="El RFC emisor o el RFC receptor de este CFDI aparece en la lista 69-B "
             "(Empresas que Facturan Operaciones Simuladas) del SAT como presunto o "
             "definitivo. Los que desvirtuaron o tienen sentencia favorable no cuentan.")
    dsi_inconsistency_ids = fields.One2many(
        'dsi.cfdi.inconsistency', 'cfdi_document_id', string="Discrepancias")
    dsi_inconsistency_count = fields.Integer(
        string="Discrepancias pendientes", compute='_compute_dsi_inconsistency_count')
    dsi_inconsistency_total = fields.Integer(
        string="Total de discrepancias", compute='_compute_dsi_inconsistency_count')
    active = fields.Boolean(default=True)

    _uuid_company_uniq = models.Constraint(
        'unique(company_id, uuid)',
        "Ya existe un CFDI con este UUID en esta empresa.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Un CFDI nuevo llega sin verificar; si trae estatus, que lo ponga
        # quien puede (CAMPOS_DE_LA_VERIFICACION).
        if any(vals.get('sat_status', 'not_verified') != 'not_verified' or vals.get('sat_last_check')
               for vals in vals_list):
            self._solo_la_verificacion()
        docs = super().create(vals_list)
        # Los documentos relacionados de un CFDI de Pago se leen al guardarlo,
        # venga de donde venga (importación, descarga, pruebas). Ligarlos con
        # sus facturas le toca a quien guarda, una vez por tanda.
        docs._crear_lineas_de_pago()
        if not self.env.context.get('dsi_sin_sync') and any(vals.get('move_id') for vals in vals_list):
            docs._revisar_estado_de_sus_facturas(docs.move_id, 'link')
            docs.filtered('move_id')._ligar_reps('link')
        return docs

    def write(self, vals):
        """Al ligar, desligar, archivar o verificar un CFDI, su factura cambia
        de Estado bóveda y se revisan sus discrepancias de estatus: la de antes
        (la que se desliga se resuelve) y la de después. Al ligar o desligar un
        CFDI de ingreso o egreso, o al cambiarle la empresa o la dirección,
        sus pagos (REP) se vuelven a ligar (CAMPOS_DE_LA_LIGA).
        Los flujos en lote pasan `dsi_sin_sync` y revisan una sola vez al
        final. El estatus SAT solo lo escribe la verificación
        (_solo_la_verificacion)."""
        if CAMPOS_DE_LA_VERIFICACION & set(vals):
            self._solo_la_verificacion()
        en_lote = self.env.context.get('dsi_sin_sync')
        sincronizar = not en_lote and CAMPOS_DEL_ESTADO_BOVEDA & set(vals)
        religar = not en_lote and CAMPOS_DE_LA_LIGA & set(vals)
        facturas = self.sudo().move_id if sincronizar else None
        resultado = super().write(vals)
        # Siempre, también en los flujos en lote: es copiar un valor, no una
        # revisión.
        if 'sat_status' in vals:
            self._actualizar_estado_de_sus_reps()
        source = 'sat_check' if 'sat_status' in vals else 'link'
        if sincronizar:
            facturas |= self.sudo().move_id
            self._revisar_estado_de_sus_facturas(facturas, source)
            if not religar:
                self._revisar_reps_sin_factura(source)
        # Volver a ligar ya revisa sus «Pago (REP) de factura fuera de Odoo».
        if religar:
            self._ligar_reps(source)
        return resultado

    def _solo_la_verificacion(self):
        """El estatus SAT y la última verificación los escribe la verificación
        (con sudo) o el gestor contable. Sin esto el Usuario de la Bóveda
        podía, por RPC o importando, dar un CFDI por vigente: la discrepancia
        se resolvía como si la hubiera verificado el SAT, sin pasar por
        «Marcar como revisado»; o, con una fecha de verificación, sacar un CFDI
        de los que esperan volver a verificarse sin consultar al SAT."""
        if not self.env.su and not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_(
                "El estatus SAT y la fecha de verificación los registra la verificación: usa "
                "«Verificar estatus SAT»."))

    def _revisar_estado_de_sus_facturas(self, facturas, source):
        """Las discrepancias de estatus de `facturas`, en su propio savepoint:
        un choque con la revisión diaria no tumba lo que estaba haciendo quien
        llamó. La verificación SAT automática pasa `dsi_deteccion_diferida`:
        aquí no se revisa nada y ella programa la revisión al terminar."""
        if facturas and not self.env.context.get('dsi_deteccion_diferida'):
            self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
                '_sincronizar_estado_sat', facturas, source)

    @api.depends('sat_status', 'move_id')
    def _compute_is_discrepancy(self):
        for doc in self:
            doc.is_discrepancy = doc.sat_status == 'canceled' and bool(doc.move_id)

    @api.depends('partner_rfc_issuer', 'partner_rfc_receiver')
    def _compute_efos_risk(self):
        rfcs = {rfc for rfc in self.mapped('partner_rfc_issuer') + self.mapped('partner_rfc_receiver') if rfc}
        flagged = set()
        if rfcs:
            flagged = set(self.env['dsi.cfdi.efos.entry'].sudo().search([
                ('rfc', 'in', list(rfcs)),
                ('situation', 'not in', EFOS_CLEARED_SITUATIONS),
            ]).mapped('rfc'))
        for doc in self:
            doc.is_efos_risk = bool(
                (doc.partner_rfc_issuer and doc.partner_rfc_issuer in flagged)
                or (doc.partner_rfc_receiver and doc.partner_rfc_receiver in flagged)
            )

    @api.depends('dsi_inconsistency_ids.state')
    def _compute_dsi_inconsistency_count(self):
        for doc in self:
            doc.dsi_inconsistency_total = len(doc.dsi_inconsistency_ids)
            doc.dsi_inconsistency_count = len(doc.dsi_inconsistency_ids.filtered(lambda r: r.state == 'open'))

    def action_view_inconsistencies(self):
        """Botón «Discrepancias»: las de este CFDI, con su historial. Si hay
        pendientes, abre filtrado en ellas, que es por lo que se entra."""
        self.ensure_one()
        return self.env['dsi.cfdi.inconsistency']._accion_ver(
            [('cfdi_document_id', '=', self.id)], pendientes=self.dsi_inconsistency_count)

    @api.model
    def _vault_entry_action(self):
        """Lo que abre el menú CFDI. Sin licencia vigente (prueba terminada,
        licencia vencida o nunca activada) primero el aviso de licencia, con
        «Seguir solo consultando» hacia la lista; si no, la lista directa."""
        licencia = self.env['dsi.license.client.state']._get_or_create()
        if licencia.state in ('not_activated', 'restricted'):
            return licencia._notice_action(next_action='dsi_cfdi_vault.action_cfdi_document', target='main')
        return self.env['ir.actions.act_window']._for_xml_id('dsi_cfdi_vault.action_cfdi_document')

    @api.model
    def _recompute_efos_risk_all(self):
        """Llamado tras cargar/refrescar la lista 69-B y al actualizar el
        módulo (data/cfdi_efos_recompute.xml): los CFDI existentes no se
        recalculan solos con @api.depends porque el cambio ocurre en OTRO
        modelo (dsi.cfdi.efos.entry), o en la regla misma, no en los campos de
        cfdi.document. Incluye los archivados, que también pueden reactivarse.

        Al final deja las discrepancias de riesgo 69-B como dice la lista: las
        nuevas pendientes y las de un RFC que se desvirtuó, resueltas.

        Con sudo: la lista es una sola para todas las empresas, y el gestor
        contable que la carga solo ve los CFDI de las que tiene abiertas."""
        docs = self.sudo().with_context(active_test=False).search([])
        docs._compute_efos_risk()
        self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
            '_sincronizar_riesgo_69b', cfdis=docs, source='efos')

    @api.onchange('move_id')
    def _onchange_move_id(self):
        for doc in self:
            if doc.move_id and doc.match_state == 'pending':
                doc.match_state = 'manual'
                doc.match_method = 'manual'
            elif not doc.move_id:
                doc.match_state = 'pending'
                doc.match_method = False

    def action_unlink_move(self):
        self.write({'move_id': False, 'match_state': 'pending', 'match_method': False})

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    # -------------------------------------------------------------------
    # Parseo de XML
    # -------------------------------------------------------------------

    @api.model
    def _direccion_para(self, empresa, rfc_emisor):
        """'issued' si el CFDI lo emitió `empresa` y 'received' si no, con el
        RFC comparable (_rfc) y el de la matriz en una sucursal sin RFC propio
        (_rfc_receptor_de). Antes se comparaba el texto tal cual: con el RFC
        de la empresa capturado como MXEKU9003173C9 (bases que vienen de 18)
        o en una sucursal, todo quedaba como recibido, y un REP emitido nunca
        se ligaba a su venta."""
        rfc_empresa = _rfc_receptor_de(empresa)
        return 'issued' if rfc_empresa and _rfc(rfc_emisor) == rfc_empresa else 'received'

    @api.model
    def _corregir_direcciones(self):
        """Corrige la dirección de los CFDI ya guardados con la regla de
        _direccion_para. Corre al instalar o actualizar el módulo, antes de
        llenar las líneas de los REP (data/cfdi_payment_lines.xml): la
        dirección decide si un REP se liga a compras o a ventas.

        Agrupa por empresa, RFC emisor y dirección, así que no recorre la
        tabla en Python; solo escribe lo que cambia, y correrla otra vez no
        hace nada. Los CFDI corregidos vuelven a ligar sus REP."""
        corregidos = self.browse()
        Docs = self.sudo().with_context(active_test=False)
        for empresa, emisor, direccion in Docs._read_group(
                [], ['company_id', 'partner_rfc_issuer', 'direction']):
            correcta = self._direccion_para(empresa, emisor)
            if correcta != direccion:
                grupo = Docs.search([
                    ('company_id', '=', empresa.id), ('direction', '=', direccion),
                    ('partner_rfc_issuer', '=', emisor),
                ])
                grupo.with_context(dsi_sin_sync=True).write({'direction': correcta})
                corregidos |= grupo
        if corregidos:
            _logger.info("Dirección corregida en %s CFDI con el RFC de su empresa.", len(corregidos))
            corregidos._ligar_reps('rep')

    @api.model
    def _parse_cfdi_xml(self, xml_bytes):
        """Extrae los datos relevantes de un XML de CFDI (3.3 o 4.0).

        :return: dict con los valores para crear/leer un cfdi.document, incluyendo
                 siempre 'parse_state' ('ok' o 'error') y, en caso de error, 'parse_error'.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            return {'parse_state': 'error', 'parse_error': str(e)}

        if _local(root.tag) != 'Comprobante':
            return {
                'parse_state': 'error',
                'parse_error': _("El archivo no es un CFDI válido: no se encontró el nodo Comprobante."),
            }

        attrs = root.attrib
        tfd = next((el for el in root.iter() if _local(el.tag) == 'TimbreFiscalDigital'), None)
        if tfd is None or not tfd.get('UUID'):
            return {
                'parse_state': 'error',
                'parse_error': _("El CFDI no tiene Timbre Fiscal Digital (UUID)."),
            }

        emisor = next((el for el in root if _local(el.tag) == 'Emisor'), None)
        receptor = next((el for el in root if _local(el.tag) == 'Receptor'), None)

        amount_total = _numero(attrs.get('Total'))
        currency_code = attrs.get('Moneda')
        if attrs.get('TipoDeComprobante') == 'P':
            amount_total, moneda_pagos = _monto_de_pagos(root)
            currency_code = moneda_pagos or currency_code

        currency = self.env['res.currency']
        if currency_code:
            # active_test=False a propósito: en una base de Odoo la mayoría de
            # las monedas vienen desactivadas. Sin esto, un CFDI en USD (con USD
            # inactiva) caía al respaldo de la moneda de la compañía y quedaba
            # etiquetado como MXN — el importe se leería mal. Es preferible
            # referenciar la moneda correcta aunque esté inactiva.
            currency = self.env['res.currency'].with_context(active_test=False).search(
                [('name', '=', currency_code)], limit=1)

        valores = {
            'parse_state': 'ok',
            'uuid': tfd.get('UUID', '').upper(),
            'move_type': attrs.get('TipoDeComprobante') or False,
            'invoice_date': (attrs.get('Fecha') or '')[:10] or False,
            'amount_total': amount_total,
            'currency_id': currency.id if currency else False,
            'payment_method': attrs.get('MetodoPago') or '',
            'payment_way': attrs.get('FormaPago') or '',
            'partner_rfc_issuer': emisor.get('Rfc') if emisor is not None else False,
            'partner_name_issuer': emisor.get('Nombre') if emisor is not None else False,
            'partner_rfc_receiver': receptor.get('Rfc') if receptor is not None else False,
            'partner_name_receiver': receptor.get('Nombre') if receptor is not None else False,
        }
        # En un CFDI de Nómina, la fecha de pago y el periodo del complemento:
        # con ellos se busca la póliza (_datos_de_nomina). Solo sirven para
        # eso: si no se pueden leer, el CFDI se guarda sin ellos (el asistente
        # usa la Fecha del comprobante) en vez de tumbar la importación del
        # ZIP o el paquete de la descarga del SAT.
        if attrs.get('TipoDeComprobante') == 'N':
            try:
                valores.update(_datos_de_nomina(root))
            except Exception as error:  # pylint: disable=broad-except
                _logger.info("No se pudo leer el complemento de nómina del CFDI %s: %s", valores['uuid'], error)
        return valores

    # -------------------------------------------------------------------
    # Conciliación automática (REQ-002)
    # -------------------------------------------------------------------

    def action_match_automatically(self):
        """Conciliación automática en cascada:

        1. UUID exacto contra account.move (alta confianza).
        2. RFC de la contraparte + total + fecha con tolerancia, solo cuando
           hay un único candidato sin ambigüedad (nivel 2 del plan de conciliación).

        Los CFDI que no encuentran coincidencia en ninguno de los dos niveles
        quedan 'pending' para asignación manual (nivel 3/4 del plan) — nunca
        se adivina cuando hay más de un candidato posible.
        """
        docs = self.with_context(dsi_sin_sync=True).filtered(lambda d: not d.move_id)
        if not docs:
            return
        self._match_by_uuid(docs)
        remaining = docs.filtered(lambda d: not d.move_id)
        if remaining:
            self._match_by_rfc_amount_date(remaining)
        # Las discrepancias de estatus de las facturas ligadas, todas juntas.
        self._revisar_estado_de_sus_facturas(docs.move_id, 'link')
        # Y los pagos (REP) de los que encontraron su factura.
        con_factura = docs.filtered('move_id')
        if con_factura:
            con_factura._ligar_reps('link')

    def _match_by_uuid(self, docs):
        move_by_key = docs._facturas_por_folio_fiscal()
        for doc in docs:
            move = move_by_key.get((doc.company_id.id, (doc.uuid or '').upper()))
            # Un CFDI de Nómina solo va con una póliza vigente: si una factura
            # o una póliza cancelada llevaran su folio, ligarlo tumbaría la
            # importación (_check_poliza_de_nomina, _check_poliza_no_cancelada).
            # La póliza de la nómina de Odoo sí lleva el folio de un recibo.
            if move and (doc.move_type != 'N' or (move.move_type == 'entry' and move.state != 'cancel')):
                doc.write({'move_id': move.id, 'match_state': 'matched', 'match_method': 'uuid_exact'})

    def _facturas_por_folio_fiscal(self):
        """{(empresa, UUID en mayúsculas): factura} de las facturas de Odoo que
        ya llevan el folio fiscal de alguno de estos CFDI.

        El folio se compara sin distinguir mayúsculas: la Bóveda lo guarda en
        mayúsculas, pero Odoo lo toma tal como viene en el XML y hay PAC que lo
        timbran en minúsculas. Antes la comparación era exacta y esas facturas
        nunca se encontraban. Se buscan las dos formas, y no con ilike, para
        que la búsqueda siga usando el índice del campo.

        También trae las de las empresas que reciben con el mismo RFC (matriz
        y sucursales), con sudo porque el usuario puede no tenerlas abiertas:
        quien vincula solo usa las de la empresa del CFDI, pero una factura de
        la matriz con este folio también es un duplicado.

        Si una empresa tiene varias facturas con el mismo folio, gana la que
        no está cancelada: se recorren al final para que sobrescriban. Antes
        ganaba cualquiera, y el CFDI se vinculaba a la cancelada aunque
        hubiera una vigente.
        """
        uuids = {uuid for uuid in self.mapped('uuid') if uuid}
        if not uuids:
            return {}
        moves = self.env['account.move'].sudo().search([
            ('l10n_mx_edi_cfdi_uuid', 'in', list({u.upper() for u in uuids} | {u.lower() for u in uuids})),
            ('company_id', 'in', _empresas_del_mismo_rfc(self.company_id).ids),
        ])
        return {
            (m.company_id.id, m.l10n_mx_edi_cfdi_uuid.upper()): m
            for m in moves.sorted(lambda m: m.state != 'cancel')
        }

    def _match_by_rfc_amount_date(self, docs):
        # Excluir facturas que ya están vinculadas a OTRO cfdi.document (no solo
        # a los que se están procesando en este batch), para no robarle la
        # factura a un CFDI ya conciliado.
        already_linked = self.env['cfdi.document'].search([('move_id', '!=', False)])
        used_move_ids = set(already_linked.mapped('move_id').ids)
        for doc in docs:
            counterparty_rfc = (
                doc.partner_rfc_receiver if doc.direction == 'issued' else doc.partner_rfc_issuer
            )
            if not counterparty_rfc or not doc.invoice_date:
                continue
            # Un CFDI de Pago ampara el pago, no la factura que paga: su monto
            # coincide con el de esa factura, y ligarlos por importe sería un
            # error. Uno de Nómina va con la póliza del periodo, y por RFC (el
            # del empleado), total y fecha se ligaba a un reembolso o a una
            # factura suya. Se concilian solo por UUID o a mano (la nómina,
            # con «Relacionar con póliza»).
            if doc.move_type in TIPOS_SIN_LIGA_POR_IMPORTE:
                continue
            date_from = doc.invoice_date - timedelta(days=MATCH_DATE_TOLERANCE_DAYS)
            date_to = doc.invoice_date + timedelta(days=MATCH_DATE_TOLERANCE_DAYS)
            candidates = self.env['account.move'].search([
                ('company_id', '=', doc.company_id.id),
                ('state', '=', 'posted'),
                ('partner_id.vat', '=ilike', counterparty_rfc),
                ('invoice_date', '>=', date_from),
                ('invoice_date', '<=', date_to),
                ('amount_total', '>=', doc.amount_total - MATCH_AMOUNT_TOLERANCE),
                ('amount_total', '<=', doc.amount_total + MATCH_AMOUNT_TOLERANCE),
                ('id', 'not in', list(used_move_ids)),
            ])
            # Solo se auto-vincula si hay exactamente un candidato: con más de
            # uno, la elección automática sería una adivinanza contable.
            if len(candidates) == 1:
                move = candidates
                used_move_ids.add(move.id)
                doc.write({'move_id': move.id, 'match_state': 'matched', 'match_method': 'rfc_amount_date'})

    # -------------------------------------------------------------------
    # Nivel 3: sugerencias con score (coincidencia parcial, requiere
    # confirmación humana — nunca se vincula sola).
    # -------------------------------------------------------------------

    def action_open_match_suggestions(self):
        self.ensure_one()
        candidates = self._find_match_candidates()
        wizard = self.env['cfdi.match.suggestion.wizard'].create({
            'document_id': self.id,
            'line_ids': [(0, 0, {
                'move_id': c['move'].id,
                'score': c['score'],
                'rfc_match': c['rfc_match'],
                'amount_diff': c['amount_diff'],
                'date_diff_days': c['date_diff_days'],
            }) for c in candidates],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Sugerencias de conciliación"),
            'res_model': 'cfdi.match.suggestion.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    def _find_match_candidates(self, limit=SUGGEST_MAX_CANDIDATES):
        """Busca facturas candidatas en una ventana amplia y devuelve una lista
        de dicts (move, score, rfc_match, amount_diff, date_diff_days)
        ordenada de mayor a menor score. No vincula nada — es responsabilidad
        del asistente (nivel 4) confirmar."""
        self.ensure_one()
        # Proponer facturas no tiene sentido en un Pago ni en una Nómina (ver
        # TIPOS_SIN_LIGA_POR_IMPORTE). La vista ya oculta el botón; esto cubre
        # la llamada directa.
        if self.move_type in TIPOS_SIN_LIGA_POR_IMPORTE:
            return []
        already_linked = self.env['cfdi.document'].search([('move_id', '!=', False)])
        used_move_ids = set(already_linked.mapped('move_id').ids)
        counterparty_rfc = (
            self.partner_rfc_receiver if self.direction == 'issued' else self.partner_rfc_issuer
        )
        if not self.invoice_date:
            return []

        date_from = self.invoice_date - timedelta(days=SUGGEST_DATE_WINDOW_DAYS)
        date_to = self.invoice_date + timedelta(days=SUGGEST_DATE_WINDOW_DAYS)
        amount_low = self.amount_total * (1 - SUGGEST_AMOUNT_WINDOW_RATIO)
        amount_high = self.amount_total * (1 + SUGGEST_AMOUNT_WINDOW_RATIO)
        candidates = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('amount_total', '>=', amount_low),
            ('amount_total', '<=', amount_high),
            ('id', 'not in', list(used_move_ids)),
        ])

        results = []
        for move in candidates:
            rfc_match = bool(counterparty_rfc) and (move.partner_id.vat or '').strip().upper() == counterparty_rfc.strip().upper()
            amount_diff = abs(move.amount_total - self.amount_total)
            amount_ratio = amount_diff / self.amount_total if self.amount_total else 1.0
            date_diff_days = abs((move.invoice_date - self.invoice_date).days) if move.invoice_date else SUGGEST_DATE_WINDOW_DAYS

            rfc_score = 50 if rfc_match else 0
            amount_score = max(0, 30 * (1 - amount_ratio / SUGGEST_AMOUNT_WINDOW_RATIO)) if self.amount_total else 0
            date_score = max(0, 20 * (1 - date_diff_days / SUGGEST_DATE_WINDOW_DAYS))
            score = round(rfc_score + amount_score + date_score, 1)

            results.append({
                'move': move,
                'score': score,
                'rfc_match': rfc_match,
                'amount_diff': amount_diff,
                'date_diff_days': date_diff_days,
            })

        results.sort(key=lambda r: r['score'], reverse=True)
        return results[:limit]

    def action_confirm_match(self, move_id):
        """Confirma una sugerencia de nivel 3 elegida por el usuario (nunca
        automático — siempre viene de una acción explícita del asistente)."""
        self.ensure_one()
        self.write({'move_id': move_id, 'match_state': 'manual', 'match_method': 'manual'})

    # -------------------------------------------------------------------
    # Verificación de estatus SAT bajo demanda (REQ-004)
    # -------------------------------------------------------------------

    def _total_del_comprobante(self):
        """El Total tal como viene en el comprobante, que es lo que pide la
        consulta de estatus del SAT. En un CFDI de Pago es 0 aunque amount_total
        traiga lo pagado: con otro valor el SAT contesta «No encontrado»."""
        self.ensure_one()
        return 0.0 if self.move_type == 'P' else self.amount_total

    @api.model
    def _recompute_payment_amounts(self):
        """Corrige el monto de los CFDI de Pago que ya estaban guardados con 0
        (antes se leía el Total del comprobante). Corre al instalar o actualizar
        el módulo (data/cfdi_payment_amounts.xml); solo escribe si cambió."""
        from .cfdi_conceptos import _bytes_de_adjunto
        pagos = self.with_context(active_test=False).search([('move_type', '=', 'P')])
        for doc in pagos:
            try:
                vals = self._parse_cfdi_xml(_bytes_de_adjunto(doc.xml_attachment_id))
            except Exception:  # pylint: disable=broad-except
                _logger.exception("No se pudo releer el CFDI de pago %s", doc.uuid)
                continue
            if vals.get('parse_state') != 'ok':
                continue
            nuevos = {'amount_total': vals['amount_total']}
            if vals.get('currency_id'):
                nuevos['currency_id'] = vals['currency_id']
            if doc.amount_total != nuevos['amount_total'] or (
                    'currency_id' in nuevos and doc.currency_id.id != nuevos['currency_id']):
                doc.write(nuevos)

    def _fetch_and_store_sat_status(self, deadline=None):
        """Consulta al SAT el estatus de cada CFDI y guarda el que responda.

        Nunca lanza excepción: devuelve (verificados, errores), donde `errores`
        es una lista de (cfdi, mensaje). Un CFDI con error conserva su estatus
        anterior. Con `deadline` (un valor de time.monotonic()) deja de
        consultar al rebasarlo; los que no alcanzó no quedan en ninguna de las
        dos listas.

        Al final revisa una sola vez las discrepancias de estatus de las
        facturas de los que respondieron. Solo en la Bóveda: el documento CFDI
        de Odoo y la factura no se tocan (en una compra, «Actualizar SAT»
        cancelaría el asiento).

        La cancelación es definitiva (_valores_de_la_consulta): un CFDI que la
        Bóveda ya tiene cancelado no vuelve a vigente aunque la consulta lo
        diga; queda una nota en su historial.
        """
        sat_doc_model = self.env['l10n_mx_edi.document']
        verified = self.env['cfdi.document']
        errors = []
        for doc in self:
            if deadline is not None and time.monotonic() > deadline:
                break
            result = sat_doc_model._fetch_sat_status(
                doc.partner_rfc_issuer, doc.partner_rfc_receiver, doc._total_del_comprobante(), doc.uuid,
            )
            status = SAT_STATUS_BY_RESULT.get(result.get('value'))
            if status:
                # Con sudo: solo la verificación escribe estos campos (ver
                # _solo_la_verificacion). El seguimiento sale a nombre de quien
                # verificó: sudo no cambia de usuario.
                valores = doc._valores_de_la_consulta(status)
                doc.sudo().with_context(dsi_sin_sync=True).write(valores)
                if 'sat_status' not in valores:
                    etiquetas = dict(self._fields['sat_status']._description_selection(self.env))
                    doc.sudo().message_post(body=_(
                        "La consulta pública del SAT respondió «%s», pero este CFDI ya está cancelado: "
                        "una cancelación en el SAT es definitiva y el estatus no se regresa.",
                        etiquetas.get(status, status)))
                verified |= doc
            else:
                errors.append((doc, result.get('error') or _("estatus no definido")))
        self._revisar_estado_de_sus_facturas(verified.sudo().move_id, 'sat_check')
        # Un REP cancelado ya no cuenta como pago de una factura fuera de Odoo.
        verified._revisar_reps_sin_factura('sat_check')
        return verified, errors

    def _valores_de_la_consulta(self, status):
        """Lo que la verificación escribe en este CFDI cuando la consulta
        pública responde `status` (ya traducido a sat_status).

        La cancelación en el SAT es definitiva: un CFDI cancelado no vuelve a
        vigente ni a «No encontrado». Si la Bóveda ya lo tiene cancelado (por
        la consulta o por los metadatos de la descarga masiva) solo se anota
        la fecha de la consulta. Sin esto una consulta que todavía no ve la
        cancelación (el servicio de consulta y el de metadatos no se
        actualizan a la vez) la deshacía. Corregirlo a mano le toca al gestor
        contable. dsi_cfdi_sat_sync agrega de dónde salió el estatus."""
        self.ensure_one()
        valores = {'sat_last_check': fields.Datetime.now()}
        if self.sat_status != 'canceled' or status == 'canceled':
            valores['sat_status'] = status
        return valores

    def action_verify_sat_status(self):
        _verified, errors = self._fetch_and_store_sat_status()
        if errors:
            raise UserError(_(
                "No se pudo verificar el estatus SAT de %(count)s CFDI. El estatus anterior no se modificó.\n\n%(errors)s",
                count=len(errors), errors="\n".join("%s: %s" % (doc.uuid, msg) for doc, msg in errors),
            ))

    def action_verify_sat_status_batch(self):
        """«Verificar estatus SAT» desde el menú Acciones de la lista.

        A diferencia del botón del formulario, un CFDI que falla no deshace los
        que sí respondieron: el resultado se informa en una notificación y la
        lista se recarga. Van primero los que nunca se han verificado y luego
        los de verificación más antigua, así que volver a ejecutarla sobre la
        misma selección sigue donde se quedó la anterior.
        """
        pending = self.sorted(lambda d: d.sat_last_check or datetime.min)
        verified, errors = pending._fetch_and_store_sat_status(
            deadline=time.monotonic() + BATCH_VERIFY_SECONDS)
        missing = len(self) - len(verified) - len(errors)

        messages = []
        # Cancelados que siguen vinculados a una factura: es justo lo que la
        # verificación busca, así que se señala aparte.
        in_books = verified.filtered(lambda d: d.sat_status == 'canceled' and d.move_id)
        if verified:
            labels = dict(self._fields['sat_status'].selection)
            counts = Counter(verified.mapped('sat_status'))
            messages.append(_(
                "CFDI verificados: %(count)s (%(detail)s).",
                count=len(verified),
                detail=", ".join("%s: %s" % (labels[status], n) for status, n in sorted(counts.items())),
            ))
        if in_books:
            messages.append(_(
                "Cancelados que siguen contabilizados: %(count)s. Revísalos en Discrepancias.",
                count=len(in_books)))
        if errors:
            for doc, msg in errors:
                _logger.warning("Verificación SAT en lote falló para %s: %s", doc.uuid, msg)
            messages.append(_(
                "Sin poder verificar: %(count)s; conservan su estatus anterior (%(uuids)s).",
                count=len(errors), uuids=", ".join(doc.uuid for doc, _msg in errors[:3])
                + (", …" if len(errors) > 3 else "")))
        if missing:
            messages.append(_(
                "Pendientes: %(count)s. La verificación se corta al minuto para no rebasar el "
                "tiempo de la petición; vuelve a ejecutarla con la misma selección para continuar.",
                count=missing))

        if not verified:
            kind = 'danger'
        elif errors or missing or in_books:
            kind = 'warning'
        else:
            kind = 'success'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Verificación de estatus SAT"),
                'message': " ".join(messages),
                'type': kind,
                'sticky': kind != 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # -------------------------------------------------------------------
    # Verificación de estatus SAT automática (cron, periodicidad decreciente)
    # -------------------------------------------------------------------

    @api.model
    def _cron_verify_sat_status(self, batch_size=200):
        """Revisa estatus SAT con periodicidad decreciente: a diario para CFDI
        de los últimos 30 días, mensual para los más antiguos. Antes que
        todos, los que una discrepancia pidió volver a verificar (vigentes en
        la Bóveda y cancelados en Odoo): no esperan su turno, que en uno
        antiguo sería hasta dentro de un mes. Nunca lanza excepción — un CFDI
        que falla no debe tumbar el resto del batch."""
        today = fields.Date.today()
        recent_date_cutoff = today - timedelta(days=SAT_CHECK_RECENT_WINDOW_DAYS)
        recent_check_cutoff = fields.Datetime.now() - timedelta(days=SAT_CHECK_RECENT_INTERVAL_DAYS)
        old_check_cutoff = fields.Datetime.now() - timedelta(days=SAT_CHECK_OLD_INTERVAL_DAYS)

        urgent_docs = self.env['dsi.cfdi.inconsistency']._cfdi_por_volver_a_verificar()[:batch_size]
        recent_docs = self.env['cfdi.document']
        if batch_size > len(urgent_docs):
            recent_docs = self.search([
                ('id', 'not in', urgent_docs.ids),
                ('sat_status', '!=', 'canceled'),
                ('invoice_date', '>=', recent_date_cutoff),
                '|', ('sat_last_check', '=', False), ('sat_last_check', '<', recent_check_cutoff),
            ], limit=batch_size - len(urgent_docs))

        remaining = batch_size - len(urgent_docs) - len(recent_docs)
        old_docs = self.env['cfdi.document']
        if remaining > 0:
            old_docs = self.search([
                ('id', 'not in', urgent_docs.ids),
                ('sat_status', '!=', 'canceled'),
                ('invoice_date', '<', recent_date_cutoff),
                '|', ('sat_last_check', '=', False), ('sat_last_check', '<', old_check_cutoff),
            ], limit=remaining)

        # Las discrepancias no se revisan aquí con cada CFDI sino una vez, al
        # final (dsi_deteccion_diferida): si la revisión diaria corriera a la
        # vez, las dos tocarían los mismos registros, chocarían y la tanda
        # perdería lo ya verificado. Cada CFDI va en su savepoint, para que el
        # que falla no deje la transacción abortada para los demás.
        verificar = self.with_context(dsi_deteccion_diferida=True)
        for doc in verificar.browse((urgent_docs | recent_docs | old_docs).ids):
            try:
                with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                    doc.action_verify_sat_status()
            except ERRORES_DE_CONCURRENCIA as error:
                _logger.warning(
                    "Verificación SAT automática de cfdi.document id=%s chocó con otra transacción (%s); "
                    "se reintenta en la siguiente corrida.", doc.id, type(error).__name__)
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Verificación SAT automática falló para cfdi.document id=%s", doc.id)

        # La revisión de discrepancias va justo después, con los estatus recién
        # consultados. Tiene además su propio horario diario.
        self.env['dsi.cfdi.inconsistency']._programar_revision()
