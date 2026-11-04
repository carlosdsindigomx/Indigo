"""Bloque 04 del plan: los CFDI de Pago (REP) ligados a las facturas que pagan.

Antes la Bóveda guardaba el REP solo con su monto: no leía qué facturas pagaba
y en la factura no se veía. Ahora cada DoctoRelacionado es una línea propia,
la fuente de verdad, ligada a la factura de Odoo por su folio fiscal (o, si la
factura no lo lleva, por la factura del CFDI de la Bóveda). En cada factura
queda una copia del XML del REP.

Un REP recibido además sale en la pestaña CFDI de las compras que paga, como un
documento de Odoo con el estado propio «Pago recibido»: no tiene botones de
cancelar, reintentar ni imprimir, Odoo no lo consulta en el SAT (lo hace la
Bóveda) y no cambia el folio ni el estado CFDI de la factura. Un REP emitido no
lleva documento: en una venta timbrada, «Actualizar pagos» de Odoo borra los
documentos de pago que no timbró él.

Odoo no importa REP en ninguna versión: aquí no hay método nativo que
reutilizar, salvo `_create_update_document` para el documento.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from lxml import etree

from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param
from odoo import Command, _, api, fields, models
from odoo.tools import split_every

from .account_move import TIPOS_DE_CFDI_DE_FACTURA
from .cfdi_conceptos import _bytes_de_adjunto
from .cfdi_document import _doctos_relacionados, _empresas_del_mismo_rfc, _local, _rfc_receptor_de

_logger = logging.getLogger(__name__)

# Estatus SAT del REP en la Bóveda -> sat_state del documento «Pago recibido».
# Sin verificar es «No definido», como deja Odoo los suyos hasta consultarlos.
SAT_STATE_DEL_DOCUMENTO = {
    'valid': 'valid',
    'canceled': 'cancelled',
    'not_found': 'not_found',
}

# Desde qué fecha de REP se guarda la discrepancia «Pago (REP) de factura fuera
# de Odoo». Los anteriores salen solo en la lista en vivo: al instalar, casi
# todos los REP ya guardados pagan facturas que nunca pasaron por Odoo y
# serían cientos de registros de golpe. Por omisión es la fecha en que se
# instaló o actualizó a 0.2.0, y se fija una sola vez.
PARAMETRO_CORTE = 'dsi_cfdi_vault.rep_sin_factura_desde'

# REP por tanda al ligar: cada uno trae pocas líneas, y las búsquedas por folio
# fiscal se arman con todos los de la tanda.
LOTE_DE_REPS = 200

# CFDI de Pago por tanda al llenar las líneas de los ya guardados.
LOTE_DEL_LLENADO = 500

# El CFDI trae sus fechas en la hora del centro de México y sin zona; Odoo
# guarda las suyas en UTC.
ZONA_DEL_CFDI = ZoneInfo('America/Mexico_City')


def _del_lado(factura, direccion):
    """Si `factura` es del lado de un REP con esa dirección: uno recibido solo
    paga compras y uno emitido, solo ventas."""
    if direccion == 'received':
        return factura.is_purchase_document(include_receipts=True)
    return factura.is_sale_document(include_receipts=True)


def _fecha_del_timbrado(contenido):
    """La fecha de timbrado del CFDI en UTC, como Odoo guarda la fecha de sus
    documentos; si no la trae, la del comprobante. None si no se puede leer."""
    try:
        raiz = etree.fromstring(contenido)
    except (etree.XMLSyntaxError, ValueError):
        return None
    timbre = next((el for el in raiz.iter() if _local(el.tag) == 'TimbreFiscalDigital'), None)
    valor = (timbre.get('FechaTimbrado') if timbre is not None else None) or raiz.get('Fecha')
    try:
        fecha = datetime.strptime((valor or '')[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    return fecha.replace(tzinfo=ZONA_DEL_CFDI).astimezone(timezone.utc).replace(tzinfo=None)


class CfdiPaymentLine(models.Model):
    _name = 'dsi.cfdi.payment.line'
    _description = "Documento relacionado de un CFDI de Pago (REP)"
    _rec_name = 'id_documento'
    _order = 'payment_date desc, id'

    payment_doc_id = fields.Many2one(
        'cfdi.document', string="CFDI de Pago", required=True, ondelete='cascade', index=True,
        readonly=True)
    company_id = fields.Many2one(
        related='payment_doc_id.company_id', store=True, index=True, string="Empresa")
    direction = fields.Selection(related='payment_doc_id.direction', store=True, string="Dirección")
    payment_date = fields.Date(
        related='payment_doc_id.invoice_date', store=True, string="Fecha del REP")
    payment_sat_status = fields.Selection(related='payment_doc_id.sat_status', string="Estatus SAT del REP")

    # Del nodo Pago.
    pago_idx = fields.Integer(string="Pago n.º", readonly=True)
    fecha_pago = fields.Date(string="Fecha de pago", readonly=True)
    forma_pago = fields.Char(string="Forma de pago", readonly=True)
    moneda_p = fields.Char(string="Moneda del pago", readonly=True)
    currency_p_id = fields.Many2one('res.currency', string="Moneda del pago (Odoo)", readonly=True)
    tipo_cambio_p = fields.Float(string="Tipo de cambio del pago", digits=(16, 6), readonly=True)
    monto_pago = fields.Monetary(string="Monto del pago", currency_field='currency_p_id', readonly=True)

    # Del DoctoRelacionado. Los importes van en la moneda de la factura.
    docto_idx = fields.Integer(string="Documento n.º", readonly=True)
    id_documento = fields.Char(
        string="Folio fiscal de la factura", required=True, index=True, readonly=True,
        help="IdDocumento del REP, en mayúsculas.")
    serie = fields.Char(string="Serie", readonly=True)
    folio = fields.Char(string="Folio", readonly=True)
    moneda_dr = fields.Char(string="Moneda de la factura", readonly=True)
    currency_id = fields.Many2one('res.currency', string="Moneda de la factura (Odoo)", readonly=True)
    # Con los diez decimales que permite el SAT: redondeada a seis, convertir
    # lo pagado a la moneda del pago ya no cuadra por centavos.
    equivalencia_dr = fields.Float(
        string="Equivalencia", digits=(16, 10), readonly=True,
        help="Unidades de la moneda de la factura por una de la moneda del pago (EquivalenciaDR en "
             "Pagos 2.0, TipoCambioDR en 1.0).")
    num_parcialidad = fields.Integer(
        string="Parcialidad", readonly=True, help="NumParcialidad; vacío si el REP no lo trae.")
    imp_saldo_ant = fields.Monetary(string="Saldo anterior", readonly=True)
    imp_pagado = fields.Monetary(string="Importe pagado", readonly=True)
    imp_saldo_insoluto = fields.Monetary(string="Saldo insoluto", readonly=True)

    # Lo que se liga. Solo lo escribe la Bóveda (ver _sincronizar_lote_de_reps).
    related_cfdi_id = fields.Many2one(
        'cfdi.document', string="CFDI de la factura", ondelete='set null', index='btree_not_null',
        readonly=True, help="El CFDI de ingreso o egreso de la Bóveda con este folio fiscal.")
    invoice_id = fields.Many2one(
        'account.move', string="Factura", ondelete='set null', index='btree_not_null', readonly=True,
        help="La factura de Odoo que paga: la de su folio fiscal o, si no lo lleva, la del CFDI "
             "de la Bóveda. Un REP recibido solo se liga a compras y uno emitido, a ventas.")
    invoice_attachment_id = fields.Many2one(
        'ir.attachment', string="Copia del XML en la factura", ondelete='set null', readonly=True)
    # Guardado y no calculado: depende de facturas de otras empresas, que el
    # ORM no relaciona con la línea. Lo escribe la liga, como invoice_id.
    invoice_in_other_company = fields.Boolean(
        string="Factura en otra empresa", readonly=True,
        help="La factura está en Odoo, pero en otra empresa que recibe con el mismo RFC (la matriz o "
             "una sucursal). El REP solo se liga a facturas de su empresa; la factura no falta.")
    link_state = fields.Selection([
        ('linked', "Con factura"),
        ('other_company', "En otra empresa del mismo RFC"),
        ('in_vault', "En la Bóveda, sin factura"),
        ('missing', "Fuera de la Bóveda"),
    ], string="Liga", compute='_compute_link_state', store=True)

    _docto_uniq = models.Constraint(
        'unique(payment_doc_id, pago_idx, docto_idx)',
        "Ese documento relacionado ya está registrado en el CFDI de Pago.",
    )

    # Con el campo de la factura y no solo el Many2one: al borrar la factura la
    # base vacía invoice_id sin pasar por el ORM, y así la liga se recalcula.
    @api.depends('invoice_id', 'invoice_id.move_type', 'invoice_in_other_company',
                 'related_cfdi_id', 'related_cfdi_id.move_type')
    def _compute_link_state(self):
        for linea in self:
            if linea.invoice_id:
                linea.link_state = 'linked'
            elif linea.invoice_in_other_company:
                linea.link_state = 'other_company'
            elif linea.related_cfdi_id:
                linea.link_state = 'in_vault'
            else:
                linea.link_state = 'missing'

    @api.depends('id_documento', 'num_parcialidad')
    def _compute_display_name(self):
        for linea in self:
            if linea.num_parcialidad:
                linea.display_name = _("%(uuid)s (parcialidad %(n)s)", uuid=linea.id_documento, n=linea.num_parcialidad)
            else:
                linea.display_name = linea.id_documento

    @api.model
    def _corte_rep_sin_factura(self):
        """La fecha desde la que un REP sin factura en Odoo se guarda como
        discrepancia (PARAMETRO_CORTE). Si el parámetro no existe, se fija hoy:
        corre al instalar o actualizar (data/cfdi_payment_lines.xml), así que
        queda la fecha de 0.2.0. Un valor que no es fecha no se pisa: se usa
        hoy y se avisa en el log.

        «Hoy» es el de México, como la Fecha del CFDI con la que se compara:
        al actualizar corre como el sistema, sin zona horaria, y en UTC una
        actualización de la tarde dejaba el corte al día siguiente."""
        hoy = fields.Date.context_today(self.with_context(tz=ZONA_DEL_CFDI.key))
        valor = _param(self.env, PARAMETRO_CORTE)
        if not valor:
            Parametros = self.env['ir.config_parameter'].sudo()
            # Odoo 20 sustituyó set_param por accesores tipados.
            poner = Parametros.set_str if hasattr(Parametros, 'set_str') else Parametros.set_param
            poner(PARAMETRO_CORTE, fields.Date.to_string(hoy))
            return hoy
        try:
            return fields.Date.to_date(valor)
        except ValueError:
            _logger.warning(
                "El parámetro %s tiene «%s», que no es una fecha (AAAA-MM-DD): se usa hoy.", PARAMETRO_CORTE, valor)
            return hoy

    def _falta_su_factura(self, corte):
        """Si esta línea es un «Pago (REP) de factura fuera de Odoo» que se
        guarda: sin factura ni en su empresa ni en otra del mismo RFC, de un
        REP activo, no cancelado y con fecha desde el corte. Es la misma regla
        que la lista en vivo, salvo el corte."""
        self.ensure_one()
        pago = self.payment_doc_id
        return bool(
            not self.invoice_id and not self.invoice_in_other_company
            and pago.active and pago.sat_status != 'canceled'
            and pago.invoice_date and pago.invoice_date >= corte)

    def _con_factura_en_otra_empresa(self):
        """Las de estas líneas cuya factura sí está en Odoo, pero en otra
        empresa que recibe con el mismo RFC (la matriz y sus sucursales). La
        liga es solo dentro de la empresa del REP, así que se quedan sin
        factura; pero la factura no falta, y decir que no está en Odoo sería
        falso: la conversión desde la Bóveda incluso la rechazaría por
        duplicada. La liga lo guarda en invoice_in_other_company."""
        if not self:
            return self
        empresas = _empresas_del_mismo_rfc(self.company_id)
        if empresas == self.company_id:
            return self.browse()
        rfc_de = {empresa: _rfc_receptor_de(empresa) for empresa in empresas}
        folios = set(self.mapped('id_documento'))
        por_folio = defaultdict(lambda: self.env['account.move'])
        for factura in self.env['account.move'].sudo().search([
                ('company_id', 'in', empresas.ids),
                ('l10n_mx_edi_cfdi_uuid', 'in', list({f.upper() for f in folios} | {f.lower() for f in folios}))]):
            por_folio[factura.l10n_mx_edi_cfdi_uuid.upper()] |= factura
        return self.filtered(lambda linea: rfc_de.get(linea.company_id) and any(
            factura.company_id != linea.company_id and rfc_de[factura.company_id] == rfc_de[linea.company_id]
            and _del_lado(factura, linea.direction)
            for factura in por_folio[linea.id_documento]))

    def action_open_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cfdi.document',
            'view_mode': 'form',
            'res_id': self.payment_doc_id.id,
        }


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    dsi_payment_line_ids = fields.One2many(
        'dsi.cfdi.payment.line', 'payment_doc_id', string="Documentos relacionados",
        help="En un CFDI de Pago, las facturas que paga: un renglón por DoctoRelacionado.")
    # Sin los REP cancelados: un REP que el emisor cancela y vuelve a emitir
    # sumaría el pago dos veces.
    dsi_rep_line_ids = fields.One2many(
        'dsi.cfdi.payment.line', 'related_cfdi_id', string="Pagos (REP)",
        domain=[('payment_doc_id.sat_status', '!=', 'canceled')],
        help="En un CFDI de ingreso o egreso, los pagos (REP) que lo abonan. Los cancelados en el "
             "SAT no cuentan.")
    dsi_edi_document_id = fields.Many2one(
        'l10n_mx_edi.document', string="Documento «Pago recibido»", ondelete='set null', readonly=True,
        copy=False, help="El documento de Odoo con el que este REP recibido sale en la pestaña CFDI "
                         "de las compras que paga.")

    # -------------------------------------------------------------------
    # Las líneas: una por DoctoRelacionado
    # -------------------------------------------------------------------

    def _crear_lineas_de_pago(self):
        """Las líneas de los CFDI de Pago que todavía no las tienen. Las
        escribe la Bóveda (sudo): el Usuario de la Bóveda solo las lee. Un XML
        que no se puede leer queda en el log y no detiene a los demás."""
        monedas = {}

        def moneda(codigo):
            if codigo and codigo not in monedas:
                monedas[codigo] = self.env['res.currency'].with_context(active_test=False).search(
                    [('name', '=', codigo)], limit=1).id
            return monedas.get(codigo) or False

        valores = []
        for pago in self.sudo().filtered(lambda d: d.move_type == 'P' and not d.dsi_payment_line_ids):
            try:
                doctos = _doctos_relacionados(etree.fromstring(_bytes_de_adjunto(pago.xml_attachment_id)))
            except Exception:  # pylint: disable=broad-except
                _logger.exception("No se pudieron leer los documentos relacionados del CFDI de pago %s", pago.uuid)
                continue
            valores.extend(
                dict(docto, payment_doc_id=pago.id, currency_id=moneda(docto['moneda_dr']),
                     currency_p_id=moneda(docto['moneda_p']))
                for docto in doctos if docto['id_documento'])
        return self.env['dsi.cfdi.payment.line'].sudo().create(valores)

    @api.model
    def _backfill_lineas_de_pago(self):
        """Las líneas de los CFDI de Pago que ya estaban guardados, y su liga
        con las facturas. Corre al instalar o actualizar el módulo
        (data/cfdi_payment_lines.xml).

        Solo toma los P que aún no tienen líneas, así que correrla otra vez no
        hace nada. En tandas con flush y sin commit: la actualización es una
        sola transacción y si algo falla se deshace completa. Incluye los
        archivados: también son datos de la Bóveda.
        """
        pagos = self.sudo().with_context(active_test=False).search([
            ('move_type', '=', 'P'), ('dsi_payment_line_ids', '=', False)])
        lineas = 0
        for ids in split_every(LOTE_DEL_LLENADO, pagos.ids):
            lineas += len(self.sudo().browse(ids)._crear_lineas_de_pago())
            self.env.flush_all()
        pagos._sincronizar_reps('rep')
        self._desligar_pagos_de_las_facturas_que_pagan(pagos)
        self.env.flush_all()
        if pagos:
            _logger.info(
                "Pagos (REP): %s CFDI de pago con %s documentos relacionados; %s ligados a su factura.",
                len(pagos), lineas, len(pagos.dsi_payment_line_ids.filtered('invoice_id')))

    @api.model
    def _desligar_pagos_de_las_facturas_que_pagan(self, pagos):
        """Antes no había dónde poner la factura que paga un REP, y alguno se
        vinculó a mano a ella (move_id). En un P, move_id es el asiento del
        pago: si la factura vinculada es una de las que ya liga el REP, se
        desvincula y se deja la nota. Si no, se deja como está."""
        for pago in pagos.filtered(lambda p: p.move_id and p.move_id.is_invoice(include_receipts=True)):
            factura = pago.move_id
            if factura not in pago.dsi_payment_line_ids.invoice_id:
                continue
            pago.write({'move_id': False, 'match_state': 'pending', 'match_method': False})
            pago.message_post(body=_(
                "Estaba vinculado a la factura %s, que es una de las que paga. En un CFDI de Pago el "
                "vínculo es para el asiento del pago; las facturas que paga están en «Documentos "
                "relacionados».", factura.display_name))

    # -------------------------------------------------------------------
    # La liga con las facturas
    # -------------------------------------------------------------------

    def _ligar_reps(self, source='rep'):
        """Liga con sus facturas los REP de estos CFDI: los de Pago y los que
        pagan a los de ingreso o egreso. Se llama al importar, al descargar,
        al vincular un CFDI con su factura, al convertirlo y en la revisión
        diaria. Siempre llega al mismo resultado, así que no importa en qué
        orden lleguen el REP y su factura."""
        docs = self.sudo().with_context(active_test=False).exists()
        pagos = docs.filtered(lambda d: d.move_type == 'P')
        facturables = docs.filtered(lambda d: d.move_type in TIPOS_DE_CFDI_DE_FACTURA)
        if facturables:
            # También los de las otras empresas del mismo RFC: si la factura
            # entra en la sucursal, al REP de la matriz ya no le falta.
            empresas = _empresas_del_mismo_rfc(facturables.company_id)
            pagos |= self.env['dsi.cfdi.payment.line'].sudo().search([
                '|', ('related_cfdi_id', 'in', facturables.ids),
                '&', ('company_id', 'in', empresas.ids),
                ('id_documento', 'in', [uuid.upper() for uuid in facturables.mapped('uuid') if uuid]),
            ]).payment_doc_id.with_context(active_test=False)
        pagos._sincronizar_reps(source)

    def _sincronizar_reps(self, source):
        for ids in split_every(LOTE_DE_REPS, self.ids):
            self.browse(ids)._sincronizar_lote_de_reps(source)

    def _sincronizar_lote_de_reps(self, source):
        """Deja como deben estar las líneas de estos CFDI de Pago: su CFDI en
        la Bóveda, su factura (o si está en otra empresa del mismo RFC), la
        copia del XML en cada factura, el documento «Pago recibido» de los
        recibidos, la discrepancia de los que no tienen factura y la liga con
        el pago de Odoo.

        La liga se recalcula entera cada vez: si la factura ya no lleva el
        folio o el CFDI se desvinculó, la línea se queda sin factura. La copia
        del XML que ya se puso en una factura no se borra (en una empresa con
        bitácora de auditoría Odoo no lo permite) y se reutiliza si la liga
        vuelve. El documento «Pago recibido» tampoco se borra: se queda con
        las facturas que siga pagando.
        """
        pagos = self.sudo().with_context(active_test=False)
        lineas = pagos.dsi_payment_line_ids
        if not lineas:
            return
        facturas_antes = lineas.invoice_id
        deseado = self._ligas_de(lineas)
        # Las que se quedan sin factura porque la suya está en otra empresa
        # del mismo RFC: no salen como «fuera de Odoo».
        sin_factura = lineas.filtered(lambda linea: not deseado[linea][1])
        en_otra_empresa = set(sin_factura._con_factura_en_otra_empresa().ids)
        cambios = defaultdict(lambda: self.env['dsi.cfdi.payment.line'].sudo())
        for linea in lineas:
            relacionado, factura = deseado[linea]
            otra = linea.id in en_otra_empresa
            if linea.related_cfdi_id != relacionado or linea.invoice_id != factura \
                    or linea.invoice_in_other_company != otra:
                cambios[(relacionado.id, factura.id, otra)] |= linea
        for (relacionado_id, factura_id, otra), grupo in cambios.items():
            grupo.write({
                'related_cfdi_id': relacionado_id or False,
                'invoice_id': factura_id or False,
                'invoice_in_other_company': otra,
            })

        for pago in pagos:
            pago._copiar_rep_a_sus_facturas()
            # También el de un REP que ya tenía documento y dejó de ser
            # recibido (se corrigió su dirección): se queda sin compras.
            if pago.direction == 'received' or pago.dsi_edi_document_id:
                pago._documento_pago_recibido()
        # La liga de cada documento relacionado con su pago de Odoo (y la copia
        # del XML en el pago), en su propio savepoint. También con las facturas
        # de antes: si una línea cambió de factura, el pago que tenía queda
        # libre para las demás líneas de la factura anterior.
        self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
            '_ligar_reps_con_pagos', lineas, source, facturas=facturas_antes)
        self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
            '_sincronizar_reps_sin_factura', lineas, source)

    @api.model
    def _ligas_de(self, lineas):
        """{línea: (CFDI de la Bóveda, factura)} como deben quedar.

        - El CFDI: el de ingreso o egreso de la misma empresa con ese folio
          fiscal, archivado o no.
        - La factura: la de la misma empresa que lleva ese folio fiscal; si
          ninguna, la del CFDI de la Bóveda. Un REP recibido solo se liga a
          compras y uno emitido, solo a ventas. Si hay varias con el folio,
          gana la que no está cancelada.

        Los folios se buscan en mayúsculas y en minúsculas, y no con ilike,
        para que use el índice: hay PAC que los timbran en minúsculas.
        """
        empresas = lineas.company_id
        folios = set(lineas.mapped('id_documento'))
        variantes = list({folio.upper() for folio in folios} | {folio.lower() for folio in folios})
        relacionados = {}
        for doc in self.sudo().with_context(active_test=False).search([
                ('company_id', 'in', empresas.ids), ('uuid', 'in', variantes),
                ('move_type', 'in', TIPOS_DE_CFDI_DE_FACTURA)], order='id'):
            relacionados.setdefault((doc.company_id.id, doc.uuid.upper()), doc)
        por_folio = defaultdict(list)
        for factura in self.env['account.move'].sudo().search([
                ('company_id', 'in', empresas.ids), ('l10n_mx_edi_cfdi_uuid', 'in', variantes)], order='id'):
            por_folio[(factura.company_id.id, factura.l10n_mx_edi_cfdi_uuid.upper())].append(factura)

        Factura = self.env['account.move'].sudo()
        resultado = {}
        for linea in lineas:
            clave = (linea.company_id.id, linea.id_documento.upper())
            relacionado = relacionados.get(clave, self.browse())
            candidatas = [f for f in por_folio.get(clave, []) if _del_lado(f, linea.direction)]
            factura = next((f for f in candidatas if f.state != 'cancel'), candidatas[0] if candidatas else Factura)
            respaldo = relacionado.move_id
            if not factura and respaldo and respaldo.company_id == linea.company_id \
                    and _del_lado(respaldo, linea.direction):
                factura = respaldo
            resultado[linea] = (relacionado, factura)
        return resultado

    def _copiar_rep_a_sus_facturas(self):
        """Una copia del XML del REP en cada factura que paga, como adjunto
        (sin mensaje en el chatter: con cientos de REP al mes sería ruido). Si
        la factura ya tiene un adjunto con el mismo contenido (la copia de una
        liga anterior o el XML que alguien subió) se usa ese.

        Con `no_document`, por lo mismo: con Documents instalado, cada XML que
        se adjunta a una factura entra a la carpeta de contabilidad, y un REP
        que paga diez facturas serían diez documentos iguales. Sin Documents
        no hace nada."""
        self.ensure_one()
        Adjunto = self.env['ir.attachment'].sudo().with_context(no_document=True)
        contenido = None
        for factura, grupo in self.dsi_payment_line_ids.filtered('invoice_id').grouped('invoice_id').items():
            copia = grupo.invoice_attachment_id.filtered(
                lambda a, f=factura: a.res_model == 'account.move' and a.res_id == f.id)[:1]
            if not copia:
                contenido = contenido or _bytes_de_adjunto(self.xml_attachment_id)
                if not contenido:
                    continue
                copia = Adjunto.search([
                    ('res_model', '=', 'account.move'), ('res_id', '=', factura.id),
                    ('checksum', '=', Adjunto._compute_checksum(contenido)),
                ], limit=1) or Adjunto.create({
                    'name': f"REP-{self.uuid}.xml",
                    'raw': contenido,
                    'mimetype': 'application/xml',
                    'res_model': 'account.move',
                    'res_id': factura.id,
                    'company_id': factura.company_id.id,
                })
            grupo.filtered(lambda linea, c=copia: linea.invoice_attachment_id != c).write(
                {'invoice_attachment_id': copia.id})
        self.dsi_payment_line_ids.filtered(lambda linea: not linea.invoice_id and linea.invoice_attachment_id).write(
            {'invoice_attachment_id': False})

    def _documento_pago_recibido(self):
        """El documento «Pago recibido» de este REP recibido: uno por REP, con
        todas las compras que paga en invoice_ids, como Odoo hace con los
        pagos que timbra. Lo crea o actualiza `_create_update_document` de
        Odoo. Sin facturas no se crea; si ya existía, se queda sin ellas (así
        queda también el de un REP que dejó de ser recibido).

        Lleva su propia copia del XML: «Descargar» en la pestaña CFDI funciona
        sin permisos de la Bóveda y no depende de que exista una factura. Se
        pasa como id, así Odoo no la reformatea. Sin move_id: el asiento del
        pago de proveedor no es un pago que Odoo timbre, y con él Odoo lo
        trataría como uno de sus pagos.

        Un REP que cambió de empresa no reutiliza el documento de la otra: lo
        deja sin compras y usa otro de la nueva (_documento_de_otra_empresa).
        """
        self.ensure_one()
        facturas = self.env['account.move']
        if self.direction == 'received':
            facturas = self.dsi_payment_line_ids.invoice_id
        existente = self.dsi_edi_document_id.sudo()
        if existente and self._documento_de_otra_empresa(existente):
            if existente.invoice_ids:
                existente.write({'invoice_ids': [Command.clear()]})
            existente = existente.browse()
        if not existente and facturas:
            existente = self._documento_huerfano()
        sat_state = SAT_STATE_DEL_DOCUMENTO.get(self.sat_status, 'not_defined')
        documento = existente
        if not facturas:
            if existente.invoice_ids:
                existente.write({'invoice_ids': [Command.clear()]})
        elif not (existente.state == 'payment_received' and existente.invoice_ids == facturas
                  and existente.sat_state == sat_state):
            documento = self._escribir_documento_pago_recibido(existente, facturas, sat_state)
        if documento and documento != self.dsi_edi_document_id:
            self.sudo().with_context(dsi_sin_sync=True).write({'dsi_edi_document_id': documento.id})
        return documento

    def _escribir_documento_pago_recibido(self, existente, facturas, sat_state):
        """Crea o actualiza el documento con `_create_update_document`.

        Su fecha, la de la pestaña CFDI, es la del timbrado del REP y no
        cambia: `_create_update_document` pone «ahora» cada vez que lo toca, y
        la fecha se movía cada que el REP ganaba o perdía una compra.
        """
        Documento = self.env['l10n_mx_edi.document'].sudo()
        valores = {
            'state': 'payment_received',
            'invoice_ids': [Command.set(facturas.ids)],
            'sat_state': sat_state,
        }
        fecha = existente.datetime
        if not existente.attachment_id:
            contenido = _bytes_de_adjunto(self.xml_attachment_id)
            if not contenido:
                return existente
            fecha = fecha or _fecha_del_timbrado(contenido)
            valores['attachment_id'] = self.env['ir.attachment'].sudo().create({
                'name': f"REP-{self.uuid}.xml",
                'raw': contenido,
                'mimetype': 'application/xml',
                'res_model': Documento._name,
                'company_id': self.company_id.id,
            }).id
        # Odoo 20 agregó la empresa al documento, obligatoria, y la saca de
        # los valores al actualizar uno existente: hay que pasarla siempre.
        if 'company_id' in Documento._fields:
            valores['company_id'] = self.company_id.id
        documento = existente._create_update_document(
            facturas, valores, lambda doc: doc.state == 'payment_received')
        if fecha and documento.datetime != fecha:
            documento.datetime = fecha
        adjunto = documento.attachment_id.sudo()
        if adjunto.res_model == Documento._name and not adjunto.res_id:
            adjunto.res_id = documento.id
        return documento

    def _documento_huerfano(self):
        """El «Pago recibido» que dejó este mismo REP cuando su CFDI de Pago
        se borró de la Bóveda: el documento se queda en las compras. Si el REP
        vuelve a entrar se reutiliza; si no, las compras tendrían dos.

        Solo uno que ningún otro CFDI de Pago tenga, de la misma empresa y que
        no esté en compras de otra: el mismo REP puede estar en la Bóveda de
        dos.
        """
        self.ensure_one()
        Documento = self.env['l10n_mx_edi.document'].sudo()
        if not self.uuid:
            return Documento
        dominio = [
            ('state', '=', 'payment_received'),
            ('attachment_uuid', 'in', list({self.uuid.upper(), self.uuid.lower()})),
        ]
        if 'company_id' in Documento._fields:
            dominio.append(('company_id', '=', self.company_id.id))
        candidatos = Documento.search(dominio, order='id')
        if candidatos:
            candidatos -= self.sudo().with_context(active_test=False).search([
                ('dsi_edi_document_id', 'in', candidatos.ids)]).dsi_edi_document_id
        return candidatos.filtered(
            lambda doc: not self._documento_de_otra_empresa(doc) and doc.invoice_ids.company_id <= self.company_id)[:1]

    def _documento_de_otra_empresa(self, documento):
        """Si el documento «Pago recibido» es de otra empresa que este REP.

        En Odoo 20 el documento lleva empresa y sus compras tienen que ser de
        ella (check_company); al actualizarlo, `_create_update_document` saca
        la empresa de los valores, así que no se le puede cambiar. En 18 y 19
        no la lleva y vale la de su copia del XML, que se crea con la del REP:
        las tres versiones hacen lo mismo."""
        self.ensure_one()
        if 'company_id' in documento._fields:
            empresa = documento.company_id
        else:
            empresa = documento.attachment_id.sudo().company_id
        return bool(empresa) and empresa != self.company_id

    def _actualizar_estado_de_sus_reps(self):
        """El estatus SAT del documento «Pago recibido» sigue al del REP en la
        Bóveda: Odoo no lo consulta. Un REP cancelado deja su documento
        cancelado; nunca se borra."""
        for pago in self.sudo().filtered(lambda d: d.move_type == 'P' and d.dsi_edi_document_id):
            sat_state = SAT_STATE_DEL_DOCUMENTO.get(pago.sat_status, 'not_defined')
            if pago.dsi_edi_document_id.sat_state != sat_state:
                pago.dsi_edi_document_id.sat_state = sat_state

    def _revisar_reps_sin_factura(self, source):
        """Las discrepancias «Pago (REP) de factura fuera de Odoo» de estos REP
        (su estatus o si están archivados cambió), en su propio savepoint,
        como _revisar_estado_de_sus_facturas, y su liga con los pagos: un REP
        que se cancela suelta su pago y el que lo sustituye lo toma en el
        momento. Con `dsi_deteccion_diferida` las recoge la revisión diaria."""
        lineas = self.sudo().filtered(lambda d: d.move_type == 'P').dsi_payment_line_ids
        if lineas and not self.env.context.get('dsi_deteccion_diferida'):
            self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
                '_sincronizar_reps_sin_factura', lineas, source)
            self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho('_ligar_reps_con_pagos', lineas, source)
