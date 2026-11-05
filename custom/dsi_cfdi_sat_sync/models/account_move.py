"""«Actualizar SAT» automático en las ventas con solicitud de cancelación.

Una venta timbrada en Odoo cuya cancelación se pidió desde Odoo (con el PAC en
producción) queda con un documento CFDI «solicitud de cancelación» y el
asiento publicado hasta que alguien aprieta «Actualizar SAT»: Odoo revisa por
su cuenta solo las solicitudes de hasta 60 días. Cuando los metadatos del SAT o la
consulta pública de la Bóveda ya la dan cancelada, la Bóveda ejecuta ese mismo
botón (l10n_mx_edi_cfdi_try_sat), factura por factura, en su propia tarea:
consulta el SAT y, si confirma, Odoo registra la cancelación y cancela el
asiento (salvo una fecha de bloqueo).

Solo ventas con solicitud. A una compra nunca se le ejecuta (Odoo cancelaría
el asiento: lo decide el contador; ver _sincronizar_estado_sat de la Bóveda),
ni a una venta timbrada sin solicitud: su cancelación en el SAT queda como
«Estatus SAT distinto».
"""
import logging
import time
from datetime import timedelta

from odoo import api, fields, models, tools

from odoo.addons.dsi_cfdi_vault.models.cfdi_document import ERRORES_DE_CONCURRENCIA
from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import _puede_hacer_commit

_logger = logging.getLogger(__name__)

# Las ventas a las que se les ejecuta «Actualizar SAT».
TIPOS_DE_VENTA = ('out_invoice', 'out_refund')

# Una misma venta no se vuelve a consultar antes de esto, la dispare quien la
# dispare: la tarea diaria, los metadatos o la consulta pública de la Bóveda.
HORAS_ENTRE_INTENTOS = 20

# Lo que dura una corrida. Cada consulta al SAT tarda hasta 20 s (más el WSDL)
# si el servicio no responde. Lo que falta se programa de inmediato para la
# siguiente, como hace Odoo con su propia consulta (_fetch_and_update_sat_status).
SEGUNDOS_POR_CORRIDA = 60

# Dónde guarda la transacción el disparo que ya pidió para la tarea de
# «Actualizar SAT» (ver _dsi_programar_actualizar_sat).
DISPARO_DE_LA_TRANSACCION = 'dsi_cfdi_sat_sync.disparo_actualizar_sat'


class AccountMove(models.Model):
    _inherit = 'account.move'

    dsi_vault_sat_state = fields.Selection(
        help="Estatus en el SAT del CFDI de esta factura según la Bóveda: el de su CFDI de ingreso o "
             "egreso en la Bóveda (su última verificación o los metadatos de la descarga masiva) o, si "
             "la Bóveda no lo tiene ligado, el de los metadatos de su folio fiscal. Vacío si no hay "
             "ninguno de los dos, o si todavía no se verifica.")

    # Solo para la Bóveda, como los CFDI: los metadatos del SAT con el folio
    # fiscal de esta factura.
    dsi_sat_metadata_ids = fields.One2many(
        'dsi.cfdi.sat.metadata', 'move_id', string="Metadatos del SAT",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    dsi_try_sat_last = fields.Datetime(
        string="Última «Actualizar SAT» de la Bóveda", readonly=True, copy=False,
        groups='dsi_cfdi_vault.group_cfdi_vault_user',
        help="Cuándo la Bóveda ejecutó por última vez «Actualizar SAT» en esta venta porque el SAT "
             "ya canceló el CFDI de una solicitud de cancelación. No lo repite antes de 20 horas.")

    # El decorador de un método que se sobrescribe reemplaza al del original:
    # van todas sus dependencias más las de los metadatos. La columna ya
    # existe cuando se instala este módulo, así que no se recalcula nada.
    @api.depends('cfdi_document_ids.sat_status', 'cfdi_document_ids.uuid', 'cfdi_document_ids.move_type',
                 'cfdi_document_ids.active', 'l10n_mx_edi_cfdi_uuid',
                 'dsi_sat_metadata_ids.estatus', 'dsi_sat_metadata_ids.efecto', 'dsi_sat_metadata_ids.uuid')
    def _compute_dsi_vault_sat_state(self):
        """Una factura sin su CFDI de ingreso o egreso en la Bóveda toma su
        Estado bóveda de los metadatos del SAT. Pasa con los cancelados (el
        SAT no deja descargar su XML) y con lo que no se descarga en XML. Si
        el CFDI está en la Bóveda, manda el CFDI: los metadatos ya le pasaron
        su estatus."""
        super()._compute_dsi_vault_sat_state()
        for move in self:
            if move._dsi_cfdi_de_la_boveda():
                continue
            metadato = move._dsi_metadato_del_sat()
            if metadato:
                move.dsi_vault_sat_state = 'cancelled' if metadato.estatus == '0' else 'valid'

    def _dsi_metadato_del_sat(self):
        """Los metadatos de ingreso o egreso del folio fiscal de esta factura.
        Solo los de su folio: se ligan por folio, y uno ligado con otro es de
        un CFDI que la factura ya no lleva (se volvió a timbrar) mientras la
        revisión diaria no lo desliga. Con él, una venta refacturada tomaba el
        estatus cancelado del CFDI anterior."""
        self.ensure_one()
        folio = (self.l10n_mx_edi_cfdi_uuid or '').upper()
        if not folio:
            return self.env['dsi.cfdi.sat.metadata']
        return self.sudo().dsi_sat_metadata_ids._de_factura().filtered(lambda m: m.uuid == folio)[:1]

    def _dsi_hay_diferencia_sat(self):
        """Sin su CFDI ligado en la Bóveda, un «Vigente» de los metadatos no
        es diferencia contra Odoo cancelado: la consulta de Odoo es la más
        reciente (los metadatos son de cuando se generó el paquete), la
        cancelación es definitiva y no hay un CFDI que la Bóveda pueda volver
        a verificar. El refresco de metadatos pide lo emitido en los últimos
        90 días: si se canceló después, el cancelado no volvería a llegar y la
        diferencia quedaba abierta para siempre."""
        if (self.dsi_vault_sat_state == 'valid' and self.l10n_mx_edi_cfdi_sat_state == 'cancelled'
                and not self.sudo()._dsi_cfdi_de_la_boveda()):
            return False
        return super()._dsi_hay_diferencia_sat()

    def _dsi_sin_historial_al_llenar_vigentes(self):
        """Que el Estado bóveda de estas facturas pase de vacío a «Vigente»
        no deja mensaje en su historial, como no lo deja en el CFDI: la
        primera descarga de 90 días serían miles, y crearlos al confirmar la
        transacción alargaba la descarga. Cualquier otro cambio, una
        cancelación sobre todo, sí lo deja.

        El seguimiento de un campo calculado guarda el valor de antes al
        recalcularlo (mail.thread._compute_field_value) y escribe el mensaje
        al confirmar la transacción comparándolo con el de ese momento; un
        campo sin valor de antes no se compara. Aquí se recalcula y se quita
        el de antes si era vacío y ahora es «Vigente». Igual en 18, 19 y 20."""
        estados = dict(zip(self.ids, self.mapped('dsi_vault_sat_state')))
        iniciales = self.env.cr.precommit.data.get(f'mail.tracking.{self._name}', {})
        for factura_id, estado in estados.items():
            antes = iniciales.get(factura_id)
            if estado == 'valid' and antes and 'dsi_vault_sat_state' in antes and not antes['dsi_vault_sat_state']:
                del antes['dsi_vault_sat_state']

    # -------------------------------------------------------------------
    # Ventas con una cancelación por actualizar
    # -------------------------------------------------------------------

    @api.model
    def _dsi_ventas_con_solicitud_de_cancelacion(self, dominio=None):
        """Las ventas con un documento CFDI «solicitud de cancelación» que Odoo
        todavía no da por cancelado. No se filtra por el estado CFDI
        «Cancelación solicitada»: si una consulta del SAT dio la solicitud por
        vigente (el cliente la rechazó, o el SAT aún no la reflejaba), Odoo
        vuelve a mostrar la factura «Timbrada» aunque la solicitud siga ahí,
        y «Actualizar SAT» todavía la consulta. La solicitud puede ser de un
        CFDI anterior de la venta: eso lo decide _dsi_solicitud_de_cancelacion."""
        return self.sudo().search([
            ('move_type', 'in', TIPOS_DE_VENTA),
            ('l10n_mx_edi_invoice_document_ids', 'any', [('state', '=', 'invoice_cancel_requested')]),
            ('l10n_mx_edi_cfdi_state', '!=', 'cancel'),
        ] + (dominio or []), order='id')

    def _dsi_solicitud_de_cancelacion(self):
        """El documento CFDI de la solicitud de cancelación más reciente del
        CFDI que la venta lleva hoy (su folio fiscal).

        Una solicitud que el SAT rechazó se queda para siempre (Odoo solo
        borra las que no se han verificado). Si después la venta se canceló,
        se regresó a borrador y se volvió a timbrar, esa solicitud es del CFDI
        anterior: con ella, «Actualizar SAT» consultaba el CFDI anterior
        (cancelado), Odoo registraba esa cancelación como la más reciente y
        cancelaba la venta con su CFDI nuevo vigente."""
        self.ensure_one()
        folio = (self.l10n_mx_edi_cfdi_uuid or '').upper()
        return self.sudo().l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_cancel_requested' and folio and (d.attachment_uuid or '').upper() == folio
        ).sorted()[:1]

    def _dsi_avisos_de_cancelacion(self):
        """{id de factura: (metadato, cfdi)} de estas facturas que el SAT ya
        da por canceladas según la Bóveda: el metadato cancelado de su folio
        fiscal, o el CFDI emitido de la Bóveda con su folio (ligado o no)
        cancelado por la consulta pública, los metadatos o el gestor contable.
        Solo de su folio, como _dsi_cfdi_de_la_boveda: un CFDI ligado con otro
        es uno anterior de la factura. Sin folio no hay aviso. Siempre dentro
        de la empresa de la factura. Las que no tienen aviso no aparecen."""
        Metadato = self.env['dsi.cfdi.sat.metadata'].sudo()
        Documento = self.env['cfdi.document'].sudo()
        avisos = {}
        for empresa, facturas in self.sudo().grouped('company_id').items():
            folios = {factura.id: (factura.l10n_mx_edi_cfdi_uuid or '').upper() for factura in facturas}
            uuids = {folio for folio in folios.values() if folio}
            metadatos, cfdis = {}, {}
            if uuids:
                metadatos = {m.uuid: m for m in Metadato.search([
                    ('company_id', '=', empresa.id), ('uuid', 'in', list(uuids)), ('estatus', '=', '0'),
                ])}
                for cfdi in Documento.search([
                    ('company_id', '=', empresa.id), ('direction', '=', 'issued'), ('sat_status', '=', 'canceled'),
                    ('uuid', 'in', list(uuids | {uuid.lower() for uuid in uuids})),
                ], order='id'):
                    cfdis.setdefault(cfdi.uuid.upper(), cfdi)
            for factura in facturas:
                folio = folios[factura.id]
                metadato = metadatos.get(folio, Metadato)
                # El ligado con su folio, aunque no esté marcado como emitido.
                cfdi = cfdis.get(folio) or factura._dsi_cfdi_de_la_boveda().filtered(
                    lambda d: folio and d.sat_status == 'canceled')
                if metadato or cfdi:
                    avisos[factura.id] = (metadato, cfdi)
        return avisos

    def _dsi_con_cancelacion_por_actualizar(self):
        """De estas facturas, las que tienen una cancelación por actualizar:
        ventas con solicitud de cancelación que Odoo no da por canceladas y
        que el SAT ya canceló según la Bóveda (_dsi_avisos_de_cancelacion).

        :return: (facturas, avisos), con los avisos de cada una.
        """
        ventas = self.sudo().filtered(
            lambda f: f.move_type in TIPOS_DE_VENTA and f.l10n_mx_edi_cfdi_state != 'cancel'
            and f._dsi_solicitud_de_cancelacion())
        avisos = ventas._dsi_avisos_de_cancelacion()
        return ventas.filtered(lambda f: f.id in avisos), avisos

    def _dsi_actualizar_sat_no_aplica(self):
        """Si «Actualizar SAT» no hace nada en esta factura. En 19 y 20 Odoo
        solo consulta el SAT en empresas de México con moneda MXN
        (_l10n_mx_edi_is_cfdi_invoice); en 18 no existe esa revisión y consulta
        siempre."""
        self.ensure_one()
        return hasattr(self, '_l10n_mx_edi_is_cfdi_invoice') and not self._l10n_mx_edi_is_cfdi_invoice()

    def _dsi_toca_actualizar_sat(self, ahora=None):
        """Si ya pasaron HORAS_ENTRE_INTENTOS desde la última vez."""
        self.ensure_one()
        ahora = ahora or fields.Datetime.now()
        ultima = self.sudo().dsi_try_sat_last
        return not ultima or ultima <= ahora - timedelta(hours=HORAS_ENTRE_INTENTOS)

    @api.model
    def _dsi_empresas_con_licencia(self, empresas):
        """De estas empresas, las que tienen la Bóveda con licencia."""
        Licencia = self.env['dsi.license.client.state']
        return empresas.filtered(lambda empresa: Licencia._get_or_create(empresa).is_feature_enabled('vault'))

    # -------------------------------------------------------------------
    # La tarea de «Actualizar SAT»
    # -------------------------------------------------------------------

    @api.model
    def _dsi_programar_actualizar_sat(self):
        """Pide que la tarea de «Actualizar SAT» corra pronto (ir.cron._trigger,
        igual en 18, 19 y 20), una vez por transacción: la consulta pública
        verifica cientos de CFDI en una pasada y cada cancelado emitido lo
        pediría otra vez. Si un savepoint deshizo el disparo, se pide otro.

        No se busca si ya hay un disparo pendiente: mientras la tarea corre,
        el que la inició sigue a la vista de las demás transacciones hasta que
        termina (Odoo lo borra en la transacción de la corrida). Lo que se
        pedía durante la corrida se perdía y esperaba a la del día siguiente."""
        cron = self.env.ref('dsi_cfdi_sat_sync.ir_cron_actualizar_cancelaciones_emitidas', raise_if_not_found=False)
        if not cron:
            return
        datos = self.env.cr.precommit.data
        if self.env['ir.cron.trigger'].sudo().browse(datos.get(DISPARO_DE_LA_TRANSACCION, [])).exists():
            return
        datos[DISPARO_DE_LA_TRANSACCION] = cron.sudo()._trigger().ids

    def _dsi_le_toca_actualizar_sat(self, con_licencia):
        """De estas ventas con una cancelación por actualizar, las que la
        tarea consultaría ahora: de una empresa con licencia (`con_licencia`),
        donde «Actualizar SAT» sí consulta el SAT y sin intentarlo en las
        últimas HORAS_ENTRE_INTENTOS."""
        ahora = fields.Datetime.now()
        return self.filtered(
            lambda f: f.company_id in con_licencia and not f._dsi_actualizar_sat_no_aplica()
            and f._dsi_toca_actualizar_sat(ahora))

    @api.model
    def _dsi_por_actualizar_en_el_sat(self):
        """Las ventas a las que la tarea les ejecuta «Actualizar SAT»: con una
        cancelación por actualizar y a las que ya les toca
        (_dsi_le_toca_actualizar_sat)."""
        limite = fields.Datetime.now() - timedelta(hours=HORAS_ENTRE_INTENTOS)
        facturas = self._dsi_ventas_con_solicitud_de_cancelacion([
            '|', ('dsi_try_sat_last', '=', False), ('dsi_try_sat_last', '<=', limite),
        ])
        facturas, _avisos = facturas._dsi_con_cancelacion_por_actualizar()
        return facturas._dsi_le_toca_actualizar_sat(self._dsi_empresas_con_licencia(facturas.company_id))

    @api.model
    def _cron_actualizar_cancelaciones_emitidas(self, segundos=SEGUNDOS_POR_CORRIDA):
        """Ejecuta «Actualizar SAT» en las ventas con solicitud de cancelación
        que el SAT ya canceló según los metadatos o la consulta pública de la
        Bóveda (_dsi_por_actualizar_en_el_sat), una por una.

        Corre a diario y, además, cuando los metadatos o la consulta pública
        encuentran una (_dsi_programar_actualizar_sat). Después de cada una
        revisa su discrepancia: si Odoo registró la cancelación, la
        «Cancelación por actualizar» se resuelve; si no pudo cancelar el
        asiento por una fecha de bloqueo, queda «Cancelado con factura
        contabilizada»; si el SAT aún no la confirma, sigue abierta y se
        vuelve a intentar al día siguiente.

        Con presupuesto de tiempo (`segundos`): lo que no alcanza se programa
        para la siguiente corrida con un disparo nuevo, como hace Odoo en
        _fetch_and_update_sat_status. Una factura que falla no detiene a las
        demás.
        """
        limite = time.monotonic() + segundos
        facturas = self._dsi_por_actualizar_en_el_sat()
        Inconsistencia = self.env['dsi.cfdi.inconsistency']
        for hechas, factura in enumerate(facturas):
            if time.monotonic() > limite:
                _logger.info(
                    "«Actualizar SAT» automático: se acabó el tiempo de la corrida; quedan %s ventas para "
                    "la siguiente.", len(facturas) - hechas)
                self.env.ref('dsi_cfdi_sat_sync.ir_cron_actualizar_cancelaciones_emitidas').sudo()._trigger()
                break
            factura._dsi_actualizar_sat()
            Inconsistencia._sincronizar_desde_gancho('_sincronizar_estado_sat', factura, 'try_sat')
            Inconsistencia._avance_del_cron(1, 0)

    def _dsi_actualizar_sat(self):
        """«Actualizar SAT» en esta venta, el mismo método del botón de Odoo.

        La hora del intento se guarda antes, con commit: si la consulta tumba
        la tarea (un tiempo de espera, por ejemplo), la venta no se vuelve a
        intentar en cada corrida. La consulta va sin savepoint: Odoo hace
        commit por cada documento que actualiza. Si falla, se deshace lo que
        quedó a medias, queda en el log y se sigue con la siguiente; se vuelve
        a intentar en HORAS_ENTRE_INTENTOS. Si chocó con otra transacción que
        tocaba la misma factura, no es una falla: queda un aviso y se intenta
        en otra corrida (en la siguiente, si el choque fue antes de guardar la
        hora del intento).

        Después de guardar la hora, si su CFDI está en la Bóveda sin ligar, se
        concilia (_dsi_conciliar_su_cfdi): así la venta tiene Estado bóveda y,
        si una fecha de bloqueo no deja cancelar el asiento, queda «Cancelado
        con factura contabilizada». Iba antes de guardar la hora: si fallaba,
        la venta quedaba sin intento, su discrepancia volvía a pedir la tarea
        y la tarea volvía a fallar, sin fin.

        Devuelve False si no se pudo.
        """
        self.ensure_one()
        try:
            self.sudo().with_context(skip_is_manually_modified=True).write({
                'dsi_try_sat_last': fields.Datetime.now(),
            })
            if _puede_hacer_commit():
                self.env.cr.commit()
            self._dsi_conciliar_su_cfdi()
            self.with_company(self.company_id).l10n_mx_edi_cfdi_try_sat()
        except ERRORES_DE_CONCURRENCIA as error:
            _logger.warning(
                "«Actualizar SAT» automático de la factura %s (id %s) chocó con otra transacción (%s); se "
                "vuelve a intentar en otra corrida.", self.name, self.id, type(error).__name__)
            if _puede_hacer_commit():
                self.env.cr.rollback()
            return False
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "«Actualizar SAT» automático falló en la factura %s (id %s); se vuelve a intentar en %s horas.",
                self.name, self.id, HORAS_ENTRE_INTENTOS)
            if _puede_hacer_commit():
                self.env.cr.rollback()
            return False
        return True

    def _dsi_conciliar_su_cfdi(self):
        """Concilia con esta venta su CFDI de la Bóveda (el de su folio, en su
        empresa) si no está ligado a ninguna factura: por UUID exacto, como la
        descarga y el botón «Conciliar automáticamente». Pasa si el XML entró
        a la Bóveda y la conciliación no lo ligó (un choque, por ejemplo). Sin
        él la venta no tiene Estado bóveda: el aviso de la consulta pública
        llegaba por el CFDI suelto y, si una fecha de bloqueo no dejaba
        cancelar el asiento, no quedaba ninguna discrepancia.

        Solo si la conciliación por UUID lo liga con esta venta: así no cae en
        la conciliación por RFC, total y fecha con otra factura.

        En su propio savepoint y a prueba de fallos: es lo de menos. Si choca
        con otra transacción o falla, se deshace solo esto, queda en el log y
        «Actualizar SAT» se ejecuta igual (lo que importa es que Odoo
        registre la cancelación). El CFDI se puede conciliar después con su
        botón «Conciliar automáticamente»."""
        self.ensure_one()
        folio = (self.l10n_mx_edi_cfdi_uuid or '').upper()
        if not folio:
            return
        cfdis = self.env['cfdi.document'].sudo().search([
            ('company_id', '=', self.company_id.id), ('uuid', 'in', [folio, folio.lower()]), ('move_id', '=', False),
        ])
        if not cfdis or cfdis._facturas_por_folio_fiscal().get((self.company_id.id, folio)) != self:
            return
        try:
            with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                cfdis.action_match_automatically()
        except ERRORES_DE_CONCURRENCIA as error:
            _logger.warning(
                "«Actualizar SAT» automático: conciliar el CFDI %s con la factura %s (id %s) chocó con otra "
                "transacción (%s); se sigue sin conciliarlo.", folio, self.name, self.id, type(error).__name__)
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "«Actualizar SAT» automático: falló conciliar el CFDI %s con la factura %s (id %s); se sigue "
                "sin conciliarlo.", folio, self.name, self.id)
