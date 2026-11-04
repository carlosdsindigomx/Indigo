"""Discrepancias guardadas: lo que la Bóveda encontró y quién lo revisó.

Antes las discrepancias eran listas calculadas al momento (CFDI cancelados que
siguen contabilizados, proveedores en la lista 69-B) o mensajes en el chatter
de la factura (avisos al convertir). No quedaba registro de cuándo apareció
cada una ni de quién la revisó, y una que ya se había revisado volvía a salir
igual al día siguiente.

Aquí solo se guardan las de estado: estatus SAT distinto, CFDI cancelado con
factura contabilizada, cancelación por actualizar, riesgo 69-B, avisos al
convertir, REP de una factura que no está en Odoo y compras PPD pagadas sin
el REP de ese pago (models/ppd_sin_rep.py). «CFDI sin factura»,
«Factura sin CFDI» y «Nómina» siguen como listas en vivo: son ausencias, y en
una base real serían miles de registros de golpe.
"""
import hashlib
import logging
from datetime import timedelta

import psycopg2

from odoo import _, api, fields, models, modules, tools
from odoo.exceptions import AccessError
from odoo.tools import SQL

from .cfdi_document import ERRORES_DE_CONCURRENCIA, _rfc
from .cfdi_efos_entry import EFOS_CLEARED_SITUATIONS

_logger = logging.getLogger(__name__)

# La severidad sale del tipo (y, en un solo caso, de la huella: ver
# HUELLA_VIGENTE_SIN_VERIFICAR); no la decide quien detecta.
SEVERIDAD_POR_TIPO = {
    'sat_distinto': 'critical',
    'cancelado_contabilizado': 'critical',
    'cancelacion_por_actualizar': 'critical',
    'riesgo_69b': 'critical',
    'aviso_conversion': 'warning',
    'rep_sin_factura': 'warning',
    'ppd_sin_rep': 'warning',
}

# Lo único que un Usuario de la Bóveda puede escribir directamente: su nota. El
# estado, quién revisó y cuándo los ponen las acciones «Marcar como revisado» y
# «Reabrir»; lo demás es la fotografía de lo que se encontró. Todo eso solo lo
# escribe el sistema (con sudo) o el gestor contable.
CAMPOS_DEL_USUARIO = {'review_note'}

# Mismo margen que la conversión usa para decir que el total no cuadra.
TOLERANCIA_TOTAL = 0.05

# Las compras que se revisan contra la lista 69-B, como el asistente.
TIPOS_DE_COMPRA = ('in_invoice', 'in_refund')

# Las discrepancias de estatus de una factura, de mayor a menor precedencia: una
# factura tiene a lo más una. Sin esto, una compra publicada y cancelada en el
# SAT saldría tres veces. La cancelación por actualizar (punto 4) la registra y
# la resuelve su propio flujo; las otras dos salen del Estado bóveda.
TIPOS_DE_ESTADO = ('cancelacion_por_actualizar', 'sat_distinto', 'cancelado_contabilizado')
TIPOS_DEL_ESTADO_BOVEDA = ('sat_distinto', 'cancelado_contabilizado')

# «Vigente en la Bóveda, cancelado en Odoo» casi siempre es que la Bóveda no ha
# vuelto a verificar el CFDI desde que Odoo lo canceló, y a uno de más de 30
# días le tocaría hasta dentro de un mes. Mientras no lo verifica, la
# discrepancia lleva esta huella y severidad de advertencia, y la verificación
# SAT automática toma ese CFDI antes que los demás. Si el SAT lo confirma
# vigente, queda la de abajo, crítica; si lo da cancelado, se resuelve sola.
HUELLA_VIGENTE_SIN_VERIFICAR = "boveda=valid|odoo=cancelled|sin_verificar"
HUELLA_VIGENTE_VERIFICADA = "boveda=valid|odoo=cancelled"

# Después de un choque con otra transacción, la revisión de discrepancias se
# vuelve a programar con este margen, para que la otra termine primero.
MARGEN_TRAS_UN_CHOQUE = timedelta(minutes=5)


def _clave_del_hallazgo(kind, cfdi=None, factura=None, linea=0):
    """Qué se encontró y sobre qué: el tipo y el CFDI, la factura o la línea de
    pago de la que se habla. Va en texto con ceros, y no en los Many2one, porque
    en PostgreSQL dos NULL no chocan en una restricción única."""
    return f"{kind}:{cfdi.id if cfdi else 0}:{factura.id if factura else 0}:{linea or 0}"


def _huella_de_texto(prefijo, texto):
    """Huella corta de un aviso en texto libre: el mismo aviso da la misma."""
    return f"{prefijo}:{hashlib.sha1((texto or '').encode()).hexdigest()[:16]}"


def _tipo_de_estado(factura):
    """La discrepancia de estatus que el Estado bóveda le encuentra a la
    factura, o False. «Estatus SAT distinto» gana; «Cancelado con factura
    contabilizada» es solo cuando Odoo también dice cancelado y aun así el
    asiento sigue publicado (normalmente, por una fecha de bloqueo)."""
    if factura.dsi_vault_sat_mismatch:
        return 'sat_distinto'
    if (factura.is_invoice(include_receipts=True) and factura.state == 'posted'
            and factura.dsi_vault_sat_state == 'cancelled'
            and factura.l10n_mx_edi_cfdi_sat_state == 'cancelled'):
        return 'cancelado_contabilizado'
    return False


def _puede_hacer_commit():
    """Como l10n_mx_edi.document._can_commit: nunca dentro de una prueba, donde
    un commit rompería el aislamiento entre pruebas."""
    return not tools.config['test_enable'] and not modules.module.current_test


class CfdiInconsistency(models.Model):
    _name = 'dsi.cfdi.inconsistency'
    _description = "Discrepancia de la Bóveda CFDI"
    _inherit = ['mail.thread']
    # Un Selection se ordena por su clave, no por el orden de sus opciones:
    # ordenar por estado pondría «Resuelto» antes que «Revisado». El menú ya
    # filtra las pendientes y agrupa por tipo; dentro de cada grupo, lo más
    # reciente primero.
    _order = 'last_seen desc, id desc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, index=True, readonly=True)
    kind = fields.Selection([
        ('sat_distinto', "Estatus SAT distinto"),
        ('cancelado_contabilizado', "Cancelado con factura contabilizada"),
        ('cancelacion_por_actualizar', "Cancelación por actualizar"),
        ('riesgo_69b', "Riesgo 69-B"),
        ('aviso_conversion', "Aviso al convertir"),
        ('rep_sin_factura', "Pago (REP) de factura fuera de Odoo"),
        ('ppd_sin_rep', "Compra PPD pagada sin REP"),
    ], string="Tipo", required=True, index=True, readonly=True)
    severity = fields.Selection([
        ('critical', "Crítica"),
        ('warning', "Advertencia"),
        ('info', "Informativa"),
    ], string="Severidad", compute='_compute_severity', store=True)

    cfdi_document_id = fields.Many2one(
        'cfdi.document', string="CFDI", ondelete='cascade', index=True, readonly=True)
    move_id = fields.Many2one(
        'account.move', string="Factura", ondelete='cascade', index=True, readonly=True)
    payment_line_id = fields.Many2one(
        'dsi.cfdi.payment.line', string="Documento relacionado del REP", ondelete='cascade',
        index='btree_not_null', readonly=True)
    # Del pago de Odoo de la discrepancia «Compra PPD pagada sin REP»: set null
    # y no cascade, para no perder el historial si el pago se borra (la
    # siguiente revisión la resuelve: ese pago ya no está aplicado).
    account_payment_id = fields.Many2one(
        'account.payment', string="Pago", ondelete='set null', index='btree_not_null', readonly=True)
    payment_move_id = fields.Many2one(
        'account.move', string="Asiento del pago", ondelete='set null', index='btree_not_null',
        readonly=True,
        help="El asiento del pago o el del movimiento bancario conciliado con la compra.")
    partner_id = fields.Many2one(
        'res.partner', string="Contacto", ondelete='set null', readonly=True)

    # Fotografía: lo que se vio al detectarla, para que se entienda aunque el
    # CFDI o la factura cambien después.
    uuid = fields.Char(string="UUID", readonly=True)
    vault_value = fields.Char(string="Valor en la Bóveda", readonly=True)
    odoo_value = fields.Char(string="Valor en Odoo", readonly=True)
    observation = fields.Text(string="Observación", readonly=True)
    source = fields.Selection([
        ('sat_check', "Verificación SAT"),
        ('metadata', "Metadatos del SAT"),
        ('conversion', "Conversión a factura"),
        ('efos', "Lista 69-B"),
        ('download', "Descarga masiva"),
        ('cron', "Revisión diaria"),
        ('link', "Vinculación del CFDI"),
        ('rep', "Complemento de pago"),
        ('try_sat', "Actualizar SAT"),
        ('pago', "Pago en Odoo"),
    ], string="Origen", readonly=True,
        help="Qué la detectó la primera vez.")

    state = fields.Selection([
        ('open', "Pendiente"),
        ('reviewed', "Revisado"),
        ('resolved', "Resuelto"),
    ], string="Estado", default='open', required=True, index=True, tracking=True, readonly=True,
        help="Pendiente: nadie la ha revisado. Revisado: alguien la vio y la dejó así; aunque "
             "siga apareciendo, no vuelve a Pendiente. Resuelto: la causa ya no existe (estuviera "
             "pendiente o revisada); si vuelve a aparecer, se reabre como pendiente.")
    reviewed_by_id = fields.Many2one('res.users', string="Revisado por", readonly=True)
    reviewed_date = fields.Datetime(string="Revisado el", readonly=True)
    # Con seguimiento: quién cambió la nota y qué decía antes queda en el
    # historial. Una vez revisada, solo la cambia quien la revisó (ver write).
    review_note = fields.Text(string="Nota de revisión", tracking=True)
    first_seen = fields.Datetime(
        string="Vista por primera vez", default=fields.Datetime.now, readonly=True)
    last_seen = fields.Datetime(
        string="Vista por última vez", default=fields.Datetime.now, readonly=True)
    resolved_date = fields.Datetime(string="Resuelta el", readonly=True)

    # Para no duplicar: la misma clave con la misma huella es el mismo
    # hallazgo. Si cambian los valores (cambia la huella) nace otro registro y
    # el anterior se resuelve: así queda el historial.
    dedupe_key = fields.Char(string="Clave del hallazgo", required=True, index=True, readonly=True)
    fingerprint = fields.Char(string="Huella de los valores", required=True, readonly=True)

    _hallazgo_uniq = models.Constraint(
        'unique(company_id, dedupe_key, fingerprint)',
        "Esta discrepancia ya está registrada en la empresa.",
    )

    @api.depends('kind', 'fingerprint')
    def _compute_severity(self):
        for registro in self:
            if registro.fingerprint == HUELLA_VIGENTE_SIN_VERIFICAR:
                registro.severity = 'warning'
            else:
                registro.severity = SEVERIDAD_POR_TIPO.get(registro.kind, 'info')

    @api.depends('kind', 'uuid', 'move_id', 'account_payment_id', 'payment_move_id')
    def _compute_display_name(self):
        tipos = dict(self._fields['kind'].selection)
        for registro in self:
            referencia = registro.uuid or registro.move_id.name
            # Dos pagos sin REP de la misma compra no dan dos nombres iguales.
            # Con sudo: el pago puede ser de la matriz y la compra de una
            # sucursal, y el nombre no debe fallar por eso.
            pago = (registro.account_payment_id or registro.payment_move_id).sudo().display_name
            registro.display_name = " · ".join(filter(None, [tipos.get(registro.kind), referencia, pago]))

    def write(self, vals):
        # El Usuario de la Bóveda tiene escritura solo para revisar, y aun así
        # directamente solo escribe su nota. Sin esto podría, por RPC, poner a
        # otro como revisor, cualquier fecha, darla por resuelta a mano o
        # reescribir la fotografía de lo que se encontró.
        if vals and not self.env.su and not self.env.user.has_group('account.group_account_manager'):
            if set(vals) - CAMPOS_DEL_USUARIO:
                raise AccessError(_(
                    "Solo puedes escribir la nota de revisión. Para marcar la discrepancia como "
                    "revisada o reabrirla usa sus botones; lo demás lo registra la Bóveda."))
            # La nota de una revisión es de quien la hizo: si otro la
            # cambiara, la discrepancia diría «Revisado por» alguien con un
            # texto que no escribió. Para dejar la suya, la reabre.
            ajenas = self.filtered(lambda r: r.reviewed_by_id and r.reviewed_by_id != self.env.user)
            if ajenas:
                raise AccessError(_(
                    "La nota de una discrepancia revisada solo la cambia quien la revisó (%s). Para "
                    "dejar la tuya, reábrela y vuelve a marcarla como revisada.",
                    ", ".join(ajenas.reviewed_by_id.mapped('name'))))
        return super().write(vals)

    # -------------------------------------------------------------------
    # Revisión
    # -------------------------------------------------------------------

    def action_marcar_revisado(self):
        """«Marcar como revisado», en el formulario o en lote desde la lista.
        Solo toca las pendientes: una resuelta no necesita revisión y una
        revisada ya tiene quién y cuándo. Quién y cuándo los pone el
        servidor: el usuario que la marca, y ahora."""
        self.filtered(lambda r: r.state == 'open')._escribir_revision({
            'state': 'reviewed',
            'reviewed_by_id': self.env.uid,
            'reviewed_date': fields.Datetime.now(),
        })
        return True

    def action_reabrir(self):
        self.filtered(lambda r: r.state == 'reviewed')._escribir_revision({
            'state': 'open',
            'reviewed_by_id': False,
            'reviewed_date': False,
        })
        return True

    def _escribir_revision(self, valores):
        """Lo que escriben las dos acciones. Va con sudo porque el Usuario de
        la Bóveda no puede escribir esos campos directamente (ver write); antes
        se comprueba que pueda escribir estos registros: su grupo y la regla
        multiempresa. El mensaje del cambio de estado sigue saliendo a su
        nombre: sudo no cambia de usuario."""
        self.check_access('write')
        self.sudo().write(valores)

    @api.model
    def _accion_ver(self, dominio, pendientes=0):
        """Lo que abren los botones «Discrepancias» del CFDI y de la factura:
        todas las suyas, con historial, y filtradas en las pendientes si hay."""
        return {
            'type': 'ir.actions.act_window',
            'name': _("Discrepancias"),
            'res_model': self._name,
            'view_mode': 'list,form',
            'domain': dominio,
            'context': {'search_default_filter_open': 1} if pendientes else {},
        }

    # -------------------------------------------------------------------
    # Sincronización: el único camino para crear, reabrir y resolver
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar(self, kind, alcance, hallazgos, source, en_sitio=()):
        """Deja los registros de `kind` como dicen los `hallazgos`.

        :param alcance: dominio sobre este modelo con lo que quien llama
            revisó (por ejemplo, ciertos CFDI). Los registros pendientes o
            revisados de ese tipo dentro del alcance que ya no están en
            `hallazgos` se resuelven solos; lo que quedó fuera del alcance no
            se toca.
        :param hallazgos: lista de dicts con los valores de cada registro:
            company_id, dedupe_key y fingerprint (la identidad) y lo demás de
            la fotografía (CFDI, factura, UUID, valores, observación).
        :param source: qué los detectó.
        :param en_sitio: campos de texto de la fotografía que, en un registro
            que sigue apareciendo, se reescriben con los de ahora si
            cambiaron. Son pistas de presentación (qué hacer, desde cuándo
            cuenta un plazo), no parte del hallazgo: no cambian su estado, su
            revisión ni su historial. Por omisión ninguno, y la fotografía es
            la de cuando apareció.
        :return: los registros que siguen apareciendo.

        - Hallazgo nuevo: se crea pendiente.
        - Ya existe pendiente o revisado: solo se mueve «Vista por última
          vez» (y lo de `en_sitio`). Un revisado no se reabre: alguien ya
          decidió dejarlo así.
        - Ya existe resuelto: se reabre como pendiente, con un mensaje en su
          historial.
        - Con otra huella (cambiaron los valores): es otro registro, y el
          anterior se resuelve con ese motivo.
        - Lo pendiente o revisado del alcance que ya no aparece se resuelve.
          También lo revisado: «revisado» es que alguien lo vio y «resuelto»,
          que la causa ya no existe. El mensaje dice quién lo había revisado.
        """
        Inconsistencia = self.sudo().with_context(mail_create_nolog=True, mail_create_nosubscribe=True)
        ahora = fields.Datetime.now()
        etiqueta = dict(self._fields['source'].selection).get(source, source)

        por_identidad = {}
        for valores in hallazgos:
            valores = dict(valores, kind=kind, fingerprint=valores.get('fingerprint') or '-')
            por_identidad[(valores['company_id'], valores['dedupe_key'], valores['fingerprint'])] = valores

        existentes = {}
        if por_identidad:
            claves = list({identidad[1] for identidad in por_identidad})
            for registro in Inconsistencia.search([('kind', '=', kind), ('dedupe_key', 'in', claves)]):
                existentes[(registro.company_id.id, registro.dedupe_key, registro.fingerprint)] = registro

        vistos = Inconsistencia.browse([r.id for i, r in existentes.items() if i in por_identidad])
        nuevos = [
            dict(valores, source=source, first_seen=ahora, last_seen=ahora)
            for identidad, valores in por_identidad.items() if identidad not in existentes
        ]
        if nuevos:
            vistos |= Inconsistencia._crear_o_encontrar(nuevos)

        vistos.filtered(lambda r: r.state == 'resolved')._reabrir(source)
        if vistos:
            vistos.write({'last_seen': ahora})
        if en_sitio:
            vistos._actualizar_en_sitio(por_identidad, en_sitio)

        solas = Inconsistencia.search([
            ('kind', '=', kind), ('state', 'in', ('open', 'reviewed')), ('id', 'not in', vistos.ids),
        ] + list(alcance))
        claves_vistas = set(vistos.mapped('dedupe_key'))
        cambiaron = solas.filtered(lambda r: r.dedupe_key in claves_vistas)
        cambiaron._resolver_solas(source, motivo=_(
            "Cambiaron los valores (%s): la sustituye un registro nuevo con los de ahora.", etiqueta))
        (solas - cambiaron)._resolver_solas(source)
        return vistos

    def _actualizar_en_sitio(self, por_identidad, campos):
        """Reescribe en estos registros los `campos` que cambiaron respecto
        a su hallazgo en `por_identidad` ({(empresa, clave, huella): valores},
        como en _sincronizar). Solo escribe lo distinto: una revisión que no
        cambia nada no toca el registro."""
        for registro in self:
            valores = por_identidad.get((registro.company_id.id, registro.dedupe_key, registro.fingerprint))
            if not valores:
                continue
            cambios = {
                campo: valores[campo] for campo in campos
                if campo in valores and (registro[campo] or False) != (valores[campo] or False)
            }
            if cambios:
                registro.write(cambios)

    def _crear_o_encontrar(self, vals_list):
        """Crea los registros o, si alguno ya existe y esta transacción lo ve,
        se queda con el que ya está. El choque con la restricción única se
        deshace en su savepoint y la transacción sigue. Se silencia el log de
        PostgreSQL: aquí el choque es esperado, no un error.

        Si lo creó otra transacción después de que empezó esta (el cron y el
        gancho de un CFDI pueden detectar lo mismo a la vez), esta no lo ve
        (REPEATABLE READ) y no tiene con cuál quedarse: es un choque con otra
        transacción y se relanza. Quien sincroniza desde un gancho, o la
        revisión diaria, lo deshace, deja un aviso y vuelve a programar la
        revisión, que ya lo ve.

        Primero se intenta todo junto; solo si algo choca se crea uno por uno,
        para no pagar un savepoint por registro en el caso normal.
        """
        # Lo pendiente de quien llamó se escribe fuera del savepoint: si eso
        # choca, el error es suyo y no de la creación.
        self.env.cr.flush()
        try:
            with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                return self.create(vals_list)
        except psycopg2.errors.UniqueViolation:
            if len(vals_list) > 1:
                # Sin union(*...): Odoo 20 le cambió la firma (un solo
                # iterable) y avisa en el log con la de 18 y 19.
                ids = [registro.id for valores in vals_list for registro in self._crear_o_encontrar([valores])]
                return self.browse(list(dict.fromkeys(ids)))
            valores = vals_list[0]
            existente = self.search([
                ('company_id', '=', valores['company_id']),
                ('dedupe_key', '=', valores['dedupe_key']),
                ('fingerprint', '=', valores['fingerprint']),
            ], limit=1)
            if not existente:
                raise
            return existente

    def _reabrir(self, source):
        """Las resueltas que volvieron a aparecer: otra vez pendientes, con un
        mensaje en su historial. Quién las había revisado quedó en el mensaje
        con que se resolvieron."""
        if not self:
            return
        etiqueta = dict(self._fields['source'].selection).get(source, source)
        self._track_set_log_message(_("Volvió a aparecer (%s) después de haberse resuelto.", etiqueta))
        self.write({'state': 'open', 'resolved_date': False, 'reviewed_by_id': False, 'reviewed_date': False})

    def _resolver_solas(self, source, motivo=None):
        """Las que dejaron de aparecer: resueltas, con el motivo en el mismo
        mensaje que registra el cambio de estado. A una revisada se le agrega
        quién la revisó y cuándo, que al resolverse deja de verse como
        estado."""
        if not self:
            return
        etiqueta = dict(self._fields['source'].selection).get(source, source)
        motivo = motivo or _("Ya no aparece (%s): se resolvió sola.", etiqueta)
        for registro in self:
            mensaje = motivo
            if registro.state == 'reviewed':
                mensaje += " " + _(
                    "Estaba revisada por %(usuario)s el %(fecha)s.",
                    usuario=registro.reviewed_by_id.name or _("un usuario"),
                    fecha=tools.format_datetime(
                        self.env, registro.reviewed_date, tz=registro.reviewed_by_id.tz, dt_format='short'))
            registro._track_set_log_message(mensaje)
        self.write({'state': 'resolved', 'resolved_date': fields.Datetime.now()})

    # -------------------------------------------------------------------
    # Desde los ganchos de otros flujos
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar_desde_gancho(self, metodo, *args, **kwargs):
        """Corre `metodo` de este modelo (una sincronización) desde el gancho
        de otro flujo: vincular, verificar o convertir un CFDI, cargar la
        lista 69-B, el asistente de revisión del periodo.

        Va en su propio savepoint. Si choca con otra transacción que tocó los
        mismos registros (la revisión diaria corriendo a la vez), se deshace
        solo esta parte: el flujo que llamó sigue, queda un aviso en el log y
        la revisión de discrepancias se vuelve a programar para recogerlo. Se
        silencia el log de PostgreSQL, que registraría el choque como error.

        Lo que quien llamó tiene pendiente (el CFDI que vincula, el Estado
        bóveda recalculado de la factura) se escribe antes, fuera del
        savepoint. Si eso choca, el error es de quien llamó y le llega tal
        cual: en una petición web, Odoo la reintenta. Antes se tomaba por un
        choque de la sincronización, sin savepoint al cual volver, y quien
        llamó recibía «current transaction is aborted», que no se reintenta.
        """
        self.env.cr.flush()
        try:
            with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                getattr(self, metodo)(*args, **kwargs)
        except ERRORES_DE_CONCURRENCIA as error:
            _logger.warning(
                "Discrepancias: %s chocó con otra transacción (%s) y se deshizo; la siguiente "
                "revisión de discrepancias lo recoge.", metodo, type(error).__name__)
            self._programar_revision(MARGEN_TRAS_UN_CHOQUE)

    @api.model
    def _programar_revision(self, despues=None):
        """Pide que la revisión de discrepancias corra pronto, o dentro de
        `despues` (un timedelta), con ir.cron._trigger (igual en 18, 19 y
        20). Odoo no corre la misma tarea dos veces a la vez: si ya está
        corriendo, vuelve a correr al terminar."""
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(fields.Datetime.now() + despues if despues else None)

    # -------------------------------------------------------------------
    # Riesgo 69-B
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar_riesgo_69b(self, cfdis=None, facturas=None, source='efos'):
        """Registros de riesgo 69-B de los `cfdis` y las `facturas` revisados.

        Un CFDI en riesgo es el que ya marca is_efos_risk (emisor o receptor
        presunto o definitivo); los archivados no cuentan. Una factura de
        proveedor cuenta solo si su proveedor está en riesgo y ninguno de sus
        CFDI en la Bóveda ya lo señala, para no registrar lo mismo dos veces.
        La huella es RFC y situación: un presunto que pasa a definitivo deja el
        registro anterior resuelto y abre otro.
        """
        lista = self._lista_69b_en_riesgo(cfdis, facturas)
        situaciones = dict(self.env['dsi.cfdi.efos.entry']._fields['situation'].selection)

        if cfdis is not None:
            hallazgos = []
            for doc in cfdis.filtered(lambda d: d.active and d.is_efos_risk):
                roles = [(rol, rfc) for rol, rfc in (
                    (_("emisor"), doc.partner_rfc_issuer), (_("receptor"), doc.partner_rfc_receiver),
                ) if rfc in lista]
                if not roles:
                    continue
                rfcs = sorted({rfc for _rol, rfc in roles})
                hallazgos.append({
                    'company_id': doc.company_id.id,
                    'dedupe_key': _clave_del_hallazgo('riesgo_69b', cfdi=doc),
                    'fingerprint': ";".join(f"{rfc}={lista[rfc].situation}" for rfc in rfcs),
                    'cfdi_document_id': doc.id,
                    'uuid': doc.uuid,
                    'vault_value': ", ".join(
                        f"{rfc}: {situaciones.get(lista[rfc].situation)}" for rfc in rfcs),
                    # env._ y no _(): dentro de un generador _() no encuentra el idioma.
                    'observation': " ".join(self.env._(
                        "El %(rol)s %(rfc)s (%(nombre)s) aparece en la lista 69-B del SAT como %(situacion)s.",
                        rol=rol, rfc=rfc, nombre=lista[rfc].name or _("sin nombre"),
                        situacion=situaciones.get(lista[rfc].situation)) for rol, rfc in roles),
                })
            self._sincronizar('riesgo_69b', [('cfdi_document_id', 'in', cfdis.ids)], hallazgos, source)

        if facturas is not None:
            hallazgos = []
            for factura in facturas:
                rfc = _rfc(factura.commercial_partner_id.vat)
                if (rfc not in lista or factura.state == 'cancel' or factura.move_type not in TIPOS_DE_COMPRA
                        or factura.cfdi_document_ids.filtered('is_efos_risk')):
                    continue
                situacion = situaciones.get(lista[rfc].situation)
                hallazgos.append({
                    'company_id': factura.company_id.id,
                    'dedupe_key': _clave_del_hallazgo('riesgo_69b', factura=factura),
                    'fingerprint': f"{rfc}={lista[rfc].situation}",
                    'move_id': factura.id,
                    'partner_id': factura.commercial_partner_id.id,
                    'uuid': factura.l10n_mx_edi_cfdi_uuid,
                    'vault_value': f"{rfc}: {situacion}",
                    'observation': _(
                        "El proveedor %(rfc)s (%(nombre)s) de esta factura aparece en la lista 69-B "
                        "del SAT como %(situacion)s. La factura no tiene en la Bóveda un CFDI que ya "
                        "lo señale.", rfc=rfc, nombre=lista[rfc].name or factura.commercial_partner_id.name,
                        situacion=situacion),
                })
            self._sincronizar(
                'riesgo_69b', [('cfdi_document_id', '=', False), ('move_id', 'in', facturas.ids)],
                hallazgos, source)

    @api.model
    def _lista_69b_en_riesgo(self, cfdis, facturas):
        """{RFC: entrada de la lista 69-B} de los presuntos y definitivos que
        aparecen en estos CFDI y facturas. Los CFDI se comparan con el RFC tal
        como lo trae el XML, igual que is_efos_risk; las facturas, con el RFC
        del contacto normalizado, igual que el asistente de revisión."""
        rfcs = set()
        if cfdis is not None:
            rfcs |= set(cfdis.mapped('partner_rfc_issuer') + cfdis.mapped('partner_rfc_receiver'))
        if facturas is not None:
            rfcs |= {_rfc(vat) for vat in facturas.commercial_partner_id.mapped('vat')}
        rfcs.discard(False)
        rfcs.discard('')
        if not rfcs:
            return {}
        return {
            entrada.rfc: entrada
            for entrada in self.env['dsi.cfdi.efos.entry'].sudo().search([
                ('rfc', 'in', list(rfcs)),
                ('situation', 'not in', EFOS_CLEARED_SITUATIONS),
            ])
        }

    # -------------------------------------------------------------------
    # Estatus: lo que dice la Bóveda frente a lo que dice Odoo
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar_estado_sat(self, facturas, source):
        """Registros de estatus de las `facturas` revisadas, según su Estado
        bóveda: «Estatus SAT distinto» y «Cancelado con factura
        contabilizada», uno a lo más por factura (TIPOS_DE_ESTADO).

        La factura que ya tiene una cancelación por actualizar sin resolver no
        lleva ninguno de los dos: esa gana. Las de menor precedencia que la
        factura tuviera se resuelven, aunque alguien ya las hubiera revisado,
        con un mensaje que dice cuál las sustituye: nunca quedan dos vivas.

        Una compra que la Bóveda ve cancelada y Odoo no queda además «Por
        revisar», la primera vez que aparece (o si reaparece después de
        resolverse): si el contador la revisa y la deja así, no se le vuelve
        a marcar cada día. Al publicarla se le vuelve a poner si la
        discrepancia sigue pendiente (account.move._post). A la compra nunca
        se le ejecuta «Actualizar SAT»: Odoo cancelaría el asiento, y eso lo
        decide el contador. Odoo sí lo hace por su cuenta con su cron
        (compras de hasta 7 días) o con su botón; si cancela la factura, la
        revisión diaria resuelve el registro.
        """
        facturas = facturas.sudo().exists()
        if not facturas:
            return
        Inconsistencia = self.sudo()
        alcance = [('move_id', 'in', facturas.ids)]
        con_cancelacion = Inconsistencia.search([
            ('kind', '=', 'cancelacion_por_actualizar'), ('state', '!=', 'resolved'),
        ] + alcance).move_id

        # La discrepancia de estatus que gana en cada factura.
        gana = dict.fromkeys(con_cancelacion.ids, 'cancelacion_por_actualizar')
        hallazgos = {kind: [] for kind in TIPOS_DEL_ESTADO_BOVEDA}
        for factura in facturas - con_cancelacion:
            kind = _tipo_de_estado(factura)
            if kind:
                gana[factura.id] = kind
                hallazgos[kind].append(self._hallazgo_de_estado(kind, factura, source))

        antes = Inconsistencia.search([('kind', '=', 'sat_distinto'), ('state', '!=', 'resolved')] + alcance)
        sustituidas = Inconsistencia.search([
            ('kind', 'in', TIPOS_DEL_ESTADO_BOVEDA), ('state', '!=', 'resolved'),
        ] + alcance).filtered(lambda r: gana.get(r.move_id.id, r.kind) != r.kind)
        tipos = dict(self._fields['kind'].selection)
        for kind, registros in sustituidas.grouped(lambda r: gana[r.move_id.id]).items():
            registros._resolver_solas(source, motivo=_(
                "La factura ahora tiene «%s», que la sustituye: una factura lleva una sola "
                "discrepancia de estatus.", tipos[kind]))

        distintos = self._sincronizar('sat_distinto', alcance, hallazgos['sat_distinto'], source)
        self._sincronizar('cancelado_contabilizado', alcance, hallazgos['cancelado_contabilizado'], source)

        (distintos - antes).move_id.filtered(
            lambda f: f.is_purchase_document(include_receipts=True) and f.dsi_vault_sat_state == 'cancelled'
        )._dsi_marcar_por_revisar()

    @api.model
    def _hallazgo_de_estado(self, kind, factura, source=None):
        """La fotografía de una discrepancia de estatus. La huella son los dos
        lados del conflicto, no el valor exacto de Odoo: que su estado SAT pase
        de «No definido» a «Validado» sigue siendo la misma diferencia. En
        «vigente en la Bóveda, cancelado en Odoo» dice además si la Bóveda ya
        lo verificó (HUELLA_VIGENTE_SIN_VERIFICAR).

        Va en el idioma de quien la detecta o, si no lo trae (un cron), en el
        de la empresa de la factura. El texto se guarda tal cual: sin esto,
        lo que detectaba la revisión diaria quedaba con las etiquetas de
        Odoo en inglés («Validated», «Posted») para siempre."""
        env = self.with_context(lang=tools.get_lang(
            self.env, self.env.context.get('lang') or factura.company_id.partner_id.lang).code).env
        Factura = env['account.move']
        cfdi = factura._dsi_cfdi_de_la_boveda()
        boveda = factura.dsi_vault_sat_state
        sat_odoo = factura.l10n_mx_edi_cfdi_sat_state
        etiquetas_boveda = dict(Factura._fields['dsi_vault_sat_state']._description_selection(env))
        etiquetas_sat = dict(Factura._fields['l10n_mx_edi_cfdi_sat_state']._description_selection(env))
        etiquetas_factura = dict(Factura._fields['state']._description_selection(env))
        uuid = cfdi.uuid or factura.l10n_mx_edi_cfdi_uuid
        valores = {
            'uuid': uuid,
            'odoo': etiquetas_sat.get(sat_odoo) or env._("Sin estado SAT"),
            'factura': etiquetas_factura.get(factura.state),
        }

        if kind == 'cancelado_contabilizado':
            huella = "boveda=cancelled|odoo=cancelled|asiento=posted"
            observacion = env._(
                "El CFDI %(uuid)s está cancelado en el SAT (la Bóveda y Odoo coinciden), pero la "
                "factura sigue publicada. Odoo la cancela sola cuando ve la cancelación, salvo que una "
                "fecha de bloqueo no lo permita: revisa el periodo y cancélala o registra el ajuste.",
                **valores)
        elif boveda == 'cancelled':
            huella = "boveda=cancelled|odoo=no_cancelado"
            observacion = env._(
                "La Bóveda tiene el CFDI %(uuid)s cancelado en el SAT y Odoo no: estado SAT "
                "«%(odoo)s», factura «%(factura)s».", **valores)
            if factura.is_purchase_document(include_receipts=True):
                observacion += " " + env._(
                    "No se ejecutó «Actualizar SAT» para no cancelar el asiento: confirma la "
                    "cancelación con el proveedor y decide qué hacer con la factura.")
            elif factura.l10n_mx_edi_update_sat_needed:
                observacion += " " + env._(
                    "«Actualizar SAT» en la factura registra la cancelación en Odoo.")
            else:
                # Sin un CFDI timbrado en Odoo que consultar (se timbró fuera
                # de Odoo, por ejemplo), la factura no tiene ese botón.
                observacion += " " + env._(
                    "Odoo no puede registrarla por su cuenta: la factura no tiene un CFDI timbrado "
                    "en Odoo que consultar en el SAT (por ejemplo, si se timbró fuera de Odoo). "
                    "Confirma la cancelación y cancela la factura o registra el ajuste.")
        elif self._vigencia_verificada(cfdi, factura, source):
            huella = HUELLA_VIGENTE_VERIFICADA
            observacion = env._(
                "El SAT le confirmó a la Bóveda que el CFDI %(uuid)s sigue vigente, pero Odoo lo "
                "tiene cancelado: estado SAT «%(odoo)s», factura «%(factura)s». Revisa la "
                "cancelación: pudo no completarse o la pudo rechazar el receptor.", **valores)
        else:
            huella = HUELLA_VIGENTE_SIN_VERIFICAR
            observacion = env._(
                "Odoo tiene el CFDI %(uuid)s cancelado en el SAT, pero la última verificación de la "
                "Bóveda lo dio vigente. La verificación SAT automática de la Bóveda lo vuelve a "
                "consultar antes que los demás (o usa «Verificar estatus SAT» en el CFDI): si el "
                "SAT confirma la cancelación, esta discrepancia se resuelve sola.", **valores)

        return {
            'company_id': factura.company_id.id,
            'dedupe_key': _clave_del_hallazgo(kind, cfdi=cfdi, factura=factura),
            'fingerprint': huella,
            'cfdi_document_id': cfdi.id,
            'move_id': factura.id,
            'partner_id': factura.commercial_partner_id.id,
            'uuid': uuid,
            'vault_value': etiquetas_boveda.get(boveda),
            'odoo_value': f"{valores['odoo']} · {valores['factura']}",
            'observation': observacion,
        }

    @api.model
    def _vigencia_verificada(self, cfdi, factura, source):
        """Si la Bóveda ya verificó en el SAT, después de que Odoo lo canceló,
        que el CFDI sigue vigente. Lo está si la diferencia la encontró la
        verificación misma, si ya se registró como verificada, o si el CFDI
        se verificó después de que apareció la que esperaba verificación.

        Cuando la verificación automática la encuentra, la revisión de
        discrepancias corre después y no sabe que el estatus es de ese
        momento: la registra sin verificar y el CFDI se consulta una vez más.
        """
        if source == 'sat_check':
            return True
        registradas = self.sudo().search([
            ('company_id', '=', factura.company_id.id),
            ('dedupe_key', '=', _clave_del_hallazgo('sat_distinto', cfdi=cfdi, factura=factura)),
            ('fingerprint', 'in', (HUELLA_VIGENTE_VERIFICADA, HUELLA_VIGENTE_SIN_VERIFICAR)),
            ('state', '!=', 'resolved'),
        ])
        return any(
            registro.fingerprint == HUELLA_VIGENTE_VERIFICADA
            or (cfdi.sat_last_check and cfdi.sat_last_check >= registro.first_seen)
            for registro in registradas
        )

    @api.model
    def _cfdi_por_volver_a_verificar(self):
        """Los CFDI de una discrepancia «vigente en la Bóveda, cancelado en
        Odoo» que la Bóveda no ha verificado desde que apareció. La
        verificación SAT automática los toma antes que los demás."""
        esperando = self.sudo().search([
            ('kind', '=', 'sat_distinto'), ('fingerprint', '=', HUELLA_VIGENTE_SIN_VERIFICAR),
            ('state', '!=', 'resolved'), ('cfdi_document_id', '!=', False),
        ])
        return esperando.filtered(
            lambda r: r.cfdi_document_id.active and not (
                r.cfdi_document_id.sat_last_check and r.cfdi_document_id.sat_last_check >= r.first_seen)
        ).cfdi_document_id

    # -------------------------------------------------------------------
    # Pagos (REP) de facturas que no están en Odoo
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar_reps_sin_factura(self, lineas, source):
        """Registros «Pago (REP) de factura fuera de Odoo» de las `lineas`
        revisadas (documentos relacionados de un REP): uno por línea sin
        factura en Odoo, de un REP activo, no cancelado y con fecha desde el
        corte (dsi.cfdi.payment.line._corte_rep_sin_factura). Los anteriores
        al corte solo salen en la lista en vivo. Al ligarse la factura, o si
        el REP se cancela, el registro se resuelve solo.

        Una factura registrada en otra empresa del mismo RFC (la sucursal) no
        falta: el REP no se liga con ella, pero no es una ausencia. Lo dice la
        liga (invoice_in_other_company), igual que en la lista en vivo."""
        lineas = lineas.sudo().exists()
        if not lineas:
            return
        corte = self.env['dsi.cfdi.payment.line']._corte_rep_sin_factura()
        faltan = lineas.filtered(lambda linea: linea._falta_su_factura(corte))
        contrapartes = {}
        hallazgos = [self._hallazgo_de_rep(linea, contrapartes) for linea in faltan]
        self._sincronizar('rep_sin_factura', [('payment_line_id', 'in', lineas.ids)], hallazgos, source)

    @api.model
    def _hallazgo_de_rep(self, linea, contrapartes=None):
        """La fotografía de un REP cuya factura no está en Odoo, en el idioma
        de quien la detecta o, desde un cron, en el de la empresa. La huella
        es el folio fiscal que paga: la línea no cambia de factura.

        El UUID es el de la factura que falta, no el del REP (que va en el
        CFDI), y el contacto, el proveedor o cliente del REP: un REP que paga
        varias facturas fuera de Odoo daba filas idénticas en «Por revisar».
        `contrapartes` guarda los contactos ya buscados en la tanda."""
        pago = linea.payment_doc_id
        env = self.with_context(lang=tools.get_lang(
            self.env, self.env.context.get('lang') or pago.company_id.partner_id.lang).code).env
        valores = {
            'rep': pago.uuid,
            'fecha': tools.format_date(env, pago.invoice_date),
            'uuid': linea.id_documento,
            'pagado': tools.format_amount(env, linea.imp_pagado, linea.currency_id)
            if linea.currency_id else f"{linea.imp_pagado:,.2f} {linea.moneda_dr or ''}".strip(),
        }
        if linea.num_parcialidad:
            observacion = env._(
                "El pago (REP) %(rep)s del %(fecha)s abona %(pagado)s a la factura con folio fiscal "
                "%(uuid)s (parcialidad %(n)s) y esa factura no está en Odoo.", n=linea.num_parcialidad, **valores)
        else:
            observacion = env._(
                "El pago (REP) %(rep)s del %(fecha)s abona %(pagado)s a la factura con folio fiscal "
                "%(uuid)s y esa factura no está en Odoo.", **valores)
        if linea.related_cfdi_id:
            observacion += " " + env._(
                "La Bóveda sí tiene su CFDI: regístralo en Odoo (o vincúlalo a la factura que lo "
                "registra) y esto se resuelve solo.")
        else:
            observacion += " " + env._(
                "Tampoco está en la Bóveda: impórtala o descárgala del SAT y regístrala en Odoo; al "
                "ligarse, esto se resuelve solo.")
        return {
            'company_id': pago.company_id.id,
            'dedupe_key': _clave_del_hallazgo('rep_sin_factura', cfdi=pago, linea=linea.id),
            'fingerprint': linea.id_documento,
            'cfdi_document_id': pago.id,
            'payment_line_id': linea.id,
            'partner_id': self._contraparte_del_rep(pago, contrapartes if contrapartes is not None else {}).id,
            'uuid': linea.id_documento,
            'vault_value': env._("En la Bóveda") if linea.related_cfdi_id else env._("Fuera de la Bóveda"),
            'odoo_value': env._("Sin factura"),
            'observation': observacion,
        }

    @api.model
    def _contraparte_del_rep(self, pago, contrapartes):
        """El contacto del proveedor (REP recibido) o del cliente (emitido),
        por su RFC, de la empresa del REP o compartido; vacío si no está."""
        rfc = _rfc(pago.partner_rfc_issuer if pago.direction == 'received' else pago.partner_rfc_receiver)
        clave = (pago.company_id.id, rfc)
        if clave not in contrapartes:
            contacto = self.env['res.partner']
            if rfc:
                contacto = contacto.sudo().search([
                    ('vat', '=ilike', rfc), '|', ('company_id', '=', False), ('company_id', '=', pago.company_id.id),
                ], limit=1).commercial_partner_id
            contrapartes[clave] = contacto
        return contrapartes[clave]

    # -------------------------------------------------------------------
    # Cron diario: detectar lo nuevo y resolver lo que ya no aparece
    # -------------------------------------------------------------------

    @api.model
    def _detectores(self):
        """Los métodos que corre la revisión diaria, en orden. Cada fase que
        agrega un tipo agrega aquí su detector."""
        return [
            '_detectar_riesgo_69b', '_detectar_avisos_de_conversion', '_detectar_estado_sat',
            '_detectar_reps_sin_factura',
        ]

    @api.model
    def _cron_detectar_inconsistencias(self):
        """Revisión diaria. Va después de la verificación de estatus SAT y es
        la única que ve los cambios que Odoo hace por su cuenta (su propia
        consulta al SAT no pasa por el código de la Bóveda).

        Un detector que falla no detiene a los demás: se deshace su parte, se
        deja en el log y sigue el siguiente. Cada detector termina con commit,
        para que una falla posterior no se lleve lo ya hecho. Si choca con
        otra transacción que tocó lo mismo (un CFDI que se vincula o se
        verifica a la vez), no es una falla: queda un aviso y la revisión se
        vuelve a programar.
        """
        detectores = self._detectores()
        choco = False
        for hechos, nombre in enumerate(detectores, 1):
            # Lo pendiente de antes se escribe fuera del savepoint: si eso
            # falla, no es el detector el que falló.
            self.env.cr.flush()
            try:
                with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                    getattr(self, nombre)()
            except ERRORES_DE_CONCURRENCIA as error:
                choco = True
                _logger.warning(
                    "La revisión de discrepancias chocó con otra transacción en %s (%s) y se deshizo; "
                    "se vuelve a programar.", nombre, type(error).__name__)
            except Exception:  # pylint: disable=broad-except
                _logger.exception("La revisión diaria de discrepancias falló en %s", nombre)
            self._avance_del_cron(1, len(detectores) - hechos)
        if choco:
            self._programar_revision(MARGEN_TRAS_UN_CHOQUE)

    @api.model
    def _avance_del_cron(self, hechos, faltan):
        """Commit con avance. Odoo 19 y 20 lo hacen en un paso
        (_commit_progress); 18 solo registra el avance y el commit va aparte."""
        if not _puede_hacer_commit():
            return
        Cron = self.env['ir.cron']
        if hasattr(Cron, '_commit_progress'):
            Cron._commit_progress(hechos, remaining=faltan)
        else:
            Cron._notify_progress(done=hechos, remaining=faltan)
            self.env.cr.commit()

    @api.model
    def _detectar_riesgo_69b(self):
        """Los CFDI en riesgo hoy, más los que tenían un registro abierto (para
        resolverlo si ya no). Las facturas solo se vuelven a revisar si ya
        tenían registro: nuevas solo las agrega el asistente de revisión del
        periodo, que es quien decide qué periodo y qué empresa revisar."""
        Inconsistencia = self.sudo()
        abiertas = Inconsistencia.search([('kind', '=', 'riesgo_69b'), ('state', '!=', 'resolved')])
        cfdis = self.env['cfdi.document'].sudo().search([('is_efos_risk', '=', True)])
        cfdis |= abiertas.cfdi_document_id
        facturas = abiertas.filtered(lambda r: not r.cfdi_document_id).move_id
        Inconsistencia._sincronizar_riesgo_69b(cfdis=cfdis, facturas=facturas, source='cron')

    @api.model
    def _detectar_avisos_de_conversion(self):
        """Los avisos al convertir no se pueden volver a detectar: si el usuario
        ya capturó a mano el impuesto que faltaba, nada en la factura lo dice.
        Solo se resuelven solos cuando dejan de importar: la factura se canceló,
        o el aviso era de total y el total ya cuadra con el del CFDI.

        Y vuelven como cualquier resuelto que reaparece: la factura cancelada
        se vuelve a publicar, o el total deja de cuadrar. Se reabren con su
        mensaje y la compra vuelve a «Por revisar», como al convertirla. Antes
        solo se miraban los no resueltos, y uno resuelto ya no volvía."""
        avisos = self.sudo().search([
            ('kind', '=', 'aviso_conversion'),
            '|', ('state', '!=', 'resolved'), ('move_id.state', '!=', 'cancel'),
        ])
        vigentes = avisos.filtered(lambda r: r._aviso_sigue_vigente())
        vuelven = vigentes.filtered(lambda r: r.state == 'resolved')
        vuelven._reabrir('cron')
        if vigentes:
            vigentes.write({'last_seen': fields.Datetime.now()})
        (avisos - vigentes).filtered(lambda r: r.state != 'resolved')._resolver_solas('cron')
        vuelven.move_id._dsi_marcar_por_revisar()

    @api.model
    def _detectar_estado_sat(self):
        """Las facturas con discrepancia de estatus hoy, más las que tenían un
        registro sin resolver (para resolverlo si ya no).

        Es la única que ve los cambios que hace Odoo por su cuenta: su
        consulta al SAT (el cron de l10n_mx_edi o el botón «Actualizar SAT»)
        cambia el estado SAT y puede cancelar la factura sin pasar por la
        Bóveda. El Estado bóveda y la diferencia sí se recalculan solos al
        momento; lo que espera a esta revisión es el registro.
        """
        Inconsistencia = self.sudo()
        facturas = self.env['account.move'].sudo().search([
            '|', ('dsi_vault_sat_mismatch', '=', True),
            '&', '&', ('dsi_vault_sat_state', '=', 'cancelled'),
            ('l10n_mx_edi_cfdi_sat_state', '=', 'cancelled'), ('state', '=', 'posted'),
        ])
        facturas |= Inconsistencia.search([
            ('kind', 'in', TIPOS_DEL_ESTADO_BOVEDA), ('state', '!=', 'resolved'),
        ]).move_id
        Inconsistencia._sincronizar_estado_sat(facturas, 'cron')

    @api.model
    def _detectar_reps_sin_factura(self):
        """Los pagos (REP) sin factura en Odoo, más los que tenían un registro
        sin resolver y los que se ligaron a una factura que ya no es la suya.
        Se vuelven a ligar: es la única que ve las facturas que entran por el
        import de Odoo (subir el XML a una factura, o su descarga del SAT en
        20), que no pasan por la Bóveda. Al ligarse, su registro se resuelve
        solo."""
        Linea = self.env['dsi.cfdi.payment.line'].sudo()
        lineas = Linea.search([('invoice_id', '=', False)])
        lineas |= self.sudo().search([
            ('kind', '=', 'rep_sin_factura'), ('state', '!=', 'resolved'),
        ]).payment_line_id
        lineas |= Linea.browse(self._lineas_con_otra_factura())
        lineas.payment_doc_id.with_context(active_test=False)._sincronizar_reps('cron')

    @api.model
    def _lineas_con_otra_factura(self):
        """Ids de las líneas ligadas a una factura que quizá ya no es la suya,
        por cambios hechos fuera de la Bóveda: la factura se canceló (y pudo
        entrar otra con el mismo folio), es de otra empresa (el CFDI de Pago
        cambió de empresa), es del otro lado (el REP cambió de dirección: uno
        recibido solo paga compras y uno emitido, solo ventas) o ya no lleva
        ese folio fiscal (se cambió su XML). Las ligadas por el CFDI de la
        Bóveda, porque la factura no lleva el folio, siguen bien mientras el
        CFDI siga en esa factura. En SQL: son todas las ligadas, y solo se
        vuelven a ligar unas cuantas."""
        Factura = self.env['account.move']
        self.env['dsi.cfdi.payment.line'].flush_model(
            ['invoice_id', 'company_id', 'direction', 'id_documento', 'related_cfdi_id'])
        Factura.flush_model(['state', 'company_id', 'move_type', 'l10n_mx_edi_cfdi_uuid'])
        self.env['cfdi.document'].flush_model(['move_id'])
        self.env.cr.execute(SQL("""
            SELECT linea.id
              FROM dsi_cfdi_payment_line linea
              JOIN account_move factura ON factura.id = linea.invoice_id
         LEFT JOIN cfdi_document cfdi ON cfdi.id = linea.related_cfdi_id
             WHERE factura.state = 'cancel'
                OR factura.company_id != linea.company_id
                OR (linea.direction = 'received' AND factura.move_type NOT IN %(compras)s)
                OR (linea.direction = 'issued' AND factura.move_type NOT IN %(ventas)s)
                OR (upper(factura.l10n_mx_edi_cfdi_uuid) IS DISTINCT FROM linea.id_documento
                    AND cfdi.move_id IS DISTINCT FROM factura.id)
        """, compras=tuple(Factura.get_purchase_types(include_receipts=True)),
            ventas=tuple(Factura.get_sale_types(include_receipts=True))))
        return [fila[0] for fila in self.env.cr.fetchall()]

    def _aviso_sigue_vigente(self):
        self.ensure_one()
        if not self.move_id or self.move_id.state == 'cancel':
            return False
        if self.fingerprint.startswith('total:') and self.cfdi_document_id:
            return abs(self.move_id.amount_total - self.cfdi_document_id.amount_total) > TOLERANCIA_TOTAL
        return True
