from odoo import api, models, tools

from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import _clave_del_hallazgo
from odoo.addons.dsi_cfdi_vault.models.cfdi_payment_line import ZONA_DEL_CFDI

from .account_move import TIPOS_DE_VENTA

# Por qué la Bóveda no ha registrado (o no puede registrar sola) una
# cancelación por actualizar: va en la huella, porque cambia lo que dice la
# observación. De mayor a menor: «Actualizar SAT» no hace nada en la empresa,
# no hay licencia, ya lo ejecutó desde la solicitud, o le toca ejecutarlo.
NO_APLICA, SIN_LICENCIA, INTENTADA, POR_INTENTAR = 'no_aplica', 'sin_licencia', 'intentada', 'por_intentar'


class CfdiInconsistency(models.Model):
    _inherit = 'dsi.cfdi.inconsistency'

    @api.model
    def _detectores(self):
        """Los metadatos van antes que el estatus: lo que les falte reflejar
        cambia el Estado bóveda que esa revisión mira."""
        detectores = list(super()._detectores())
        detectores.insert(detectores.index('_detectar_estado_sat'), '_detectar_metadatos_del_sat')
        return detectores

    @api.model
    def _detectar_metadatos_del_sat(self):
        """Lo que los metadatos dicen y la Bóveda o las facturas aún no
        reflejan (dsi.cfdi.sat.metadata._pendientes_de_aplicar): la factura o
        el CFDI que entraron después por fuera de la descarga (el import de
        Odoo, por ejemplo), una liga con una factura que ya no lleva ese folio
        y lo que una aplicación que chocó con otra transacción dejó sin
        escribir."""
        Metadato = self.env['dsi.cfdi.sat.metadata'].sudo()
        Metadato._pendientes_de_aplicar()._aplicar('cron', cambiados=Metadato)

    @api.model
    def _detectar_estado_sat(self):
        """Además, las ventas con solicitud de cancelación (su aviso puede
        venir de un CFDI de la Bóveda que no está ligado a la factura, sin
        Estado bóveda ni diferencia) y las que tenían una cancelación por
        actualizar sin resolver: Odoo pudo registrar la cancelación por su
        cuenta, con su cron o con el botón."""
        super()._detectar_estado_sat()
        Inconsistencia = self.sudo()
        facturas = self.env['account.move']._dsi_ventas_con_solicitud_de_cancelacion()
        facturas |= Inconsistencia.search([
            ('kind', '=', 'cancelacion_por_actualizar'), ('state', '!=', 'resolved'),
        ]).move_id
        Inconsistencia._sincronizar_estado_sat(facturas, 'cron')

    # -------------------------------------------------------------------
    # Cancelación por actualizar (punto 4)
    # -------------------------------------------------------------------

    @api.model
    def _sincronizar_estado_sat(self, facturas, source):
        """Antes que las del Estado bóveda, la cancelación por actualizar de
        las ventas: la de mayor precedencia (TIPOS_DE_ESTADO). Así la crean y
        la resuelven igual todos los que revisan el estatus de una factura:
        los metadatos, la consulta pública, vincular un CFDI, la revisión
        diaria y la tarea de «Actualizar SAT».

        Si encuentra una a la que ya le toca «Actualizar SAT», pide que la
        tarea corra pronto."""
        facturas = facturas.sudo().exists()
        if facturas:
            self._sincronizar_cancelaciones_por_actualizar(facturas, source)
        return super()._sincronizar_estado_sat(facturas, source)

    @api.model
    def _sincronizar_cancelaciones_por_actualizar(self, facturas, source):
        """Registros «Cancelación por actualizar» de las ventas entre
        `facturas`: una por venta con solicitud de cancelación que el SAT ya
        canceló según la Bóveda y que Odoo todavía no registra
        (account.move._dsi_con_cancelacion_por_actualizar). Las que ya no lo
        están (Odoo la canceló, se quitó la solicitud) se resuelven solas.

        Solo se revisan ventas: a una compra nunca se le ejecuta «Actualizar
        SAT», y su cancelación en el SAT es «Estatus SAT distinto»."""
        ventas = facturas.filtered(lambda f: f.move_type in TIPOS_DE_VENTA)
        if not ventas:
            return
        por_actualizar, avisos = ventas._dsi_con_cancelacion_por_actualizar()
        con_licencia = self.env['account.move']._dsi_empresas_con_licencia(por_actualizar.company_id)
        hallazgos = [
            self._hallazgo_de_cancelacion(factura, avisos[factura.id], factura.company_id in con_licencia)
            for factura in por_actualizar
        ]
        self._sincronizar('cancelacion_por_actualizar', [('move_id', 'in', ventas.ids)], hallazgos, source)
        if por_actualizar._dsi_le_toca_actualizar_sat(con_licencia):
            self.env['account.move']._dsi_programar_actualizar_sat()

    @api.model
    def _hallazgo_de_cancelacion(self, factura, aviso, con_licencia):
        """La fotografía de una cancelación por actualizar: qué dice la
        Bóveda (los metadatos o su consulta pública), qué respondió la
        consulta pública a la solicitud de Odoo y qué sigue. La huella cambia
        cuando cambia algo de eso: la respuesta del SAT a la solicitud y si la
        Bóveda ya ejecutó «Actualizar SAT», no puede (sin licencia) o no sirve
        de nada (empresa que no es de México en pesos).

        En el idioma de quien la detecta o, desde un cron, en el de la empresa."""
        env = self.with_context(lang=tools.get_lang(
            self.env, self.env.context.get('lang') or factura.company_id.partner_id.lang).code).env
        metadato, cfdi = aviso
        solicitud = factura._dsi_solicitud_de_cancelacion()
        consulta = solicitud.sat_state or 'not_defined'
        if factura._dsi_actualizar_sat_no_aplica():
            boveda = NO_APLICA
        elif not con_licencia:
            boveda = SIN_LICENCIA
        elif factura.sudo().dsi_try_sat_last and factura.sudo().dsi_try_sat_last >= solicitud.datetime:
            boveda = INTENTADA
        else:
            boveda = POR_INTENTAR

        def fecha(valor):
            return tools.format_datetime(env, valor, tz=ZONA_DEL_CFDI.key, dt_format='short')

        if metadato:
            senal = env._("metadatos: cancelada el %s", fecha(metadato.fecha_cancelacion)) \
                if metadato.fecha_cancelacion else env._("metadatos: cancelada")
        elif cfdi.sat_last_check:
            senal = env._("Bóveda: cancelada según su consulta pública del %s", fecha(cfdi.sat_last_check))
        else:
            senal = env._("Bóveda: cancelada")
        Factura = env['account.move']
        etiquetas_sat = dict(env['l10n_mx_edi.document']._fields['sat_state']._description_selection(env))
        etiquetas_cfdi = dict(Factura._fields['l10n_mx_edi_cfdi_state']._description_selection(env))
        etiquetas_factura = dict(Factura._fields['state']._description_selection(env))
        etiquetas_boveda = dict(Factura._fields['dsi_vault_sat_state']._description_selection(env))
        uuid = factura.l10n_mx_edi_cfdi_uuid or cfdi.uuid or metadato.uuid
        observacion = env._(
            "La factura tiene una solicitud de cancelación del CFDI %(uuid)s y el SAT ya lo canceló, pero "
            "Odoo todavía no registra la cancelación (%(senal)s; consulta pública: %(consulta)s).",
            uuid=uuid, senal=senal, consulta=etiquetas_sat.get(consulta, consulta))
        if boveda == NO_APLICA:
            observacion += " " + env._(
                "«Actualizar SAT» no hace nada en esta empresa: Odoo solo consulta el SAT en empresas de "
                "México con moneda MXN, y esta es de %(pais)s con %(moneda)s. Registra la cancelación a mano.",
                pais=factura.company_id.country_id.name or env._("sin país"),
                moneda=factura.company_id.currency_id.name)
        elif boveda == SIN_LICENCIA:
            observacion += " " + env._(
                "Sin una licencia vigente de la Bóveda, «Actualizar SAT» no se ejecuta solo: usa el botón "
                "«Actualizar SAT» de la factura.")
        elif boveda == INTENTADA:
            observacion += " " + env._(
                "La Bóveda ya ejecutó «Actualizar SAT» y la consulta pública todavía no confirma la "
                "cancelación; lo vuelve a intentar cada día.")
            if consulta == 'valid':
                observacion += " " + env._(
                    "Si sigue vigente, el cliente pudo rechazar la cancelación: confírmalo con él.")
        else:
            observacion += " " + env._(
                "La Bóveda ejecuta «Actualizar SAT» en las próximas horas: si el SAT confirma la cancelación, "
                "Odoo cancela la factura y esto se resuelve solo.")
        if consulta == 'error' and solicitud.message:
            observacion += " " + env._("La consulta respondió: %s", solicitud.message[:300])

        return {
            'company_id': factura.company_id.id,
            'dedupe_key': _clave_del_hallazgo('cancelacion_por_actualizar', factura=factura),
            'fingerprint': f"consulta={consulta}|{boveda}",
            'cfdi_document_id': (factura._dsi_cfdi_de_la_boveda() or cfdi).id,
            'move_id': factura.id,
            'partner_id': factura.commercial_partner_id.id,
            'uuid': uuid,
            'vault_value': etiquetas_boveda.get('cancelled'),
            'odoo_value': f"{etiquetas_cfdi.get(factura.l10n_mx_edi_cfdi_state) or '-'} · "
                          f"{etiquetas_factura.get(factura.state)}",
            'observation': observacion,
        }

    @api.model
    def _hallazgo_de_estado(self, kind, factura, source=None):
        """Si el estatus de la Bóveda salió de los metadatos de la descarga
        masiva, la observación lo dice, con la fecha de cancelación si se
        conoce: la consulta pública no da esa fecha. La huella no cambia."""
        hallazgo = super()._hallazgo_de_estado(kind, factura, source)
        nota = self._nota_de_los_metadatos(factura)
        if nota:
            hallazgo['observation'] = f"{hallazgo['observation']} {nota}"
        return hallazgo

    @api.model
    def _nota_de_los_metadatos(self, factura):
        env = self.with_context(lang=tools.get_lang(
            self.env, self.env.context.get('lang') or factura.company_id.partner_id.lang).code).env
        cfdi = factura._dsi_cfdi_de_la_boveda()
        if cfdi:
            if cfdi.sat_status_source != 'metadatos':
                return ''
            fecha = cfdi.sat_cancel_date if cfdi.sat_status == 'canceled' else False
            nota = env._("El estatus de la Bóveda salió de los metadatos de la descarga masiva del SAT.")
        else:
            metadato = factura._dsi_metadato_del_sat()
            if not metadato:
                return ''
            fecha = metadato.fecha_cancelacion if metadato.estatus == '0' else False
            nota = env._(
                "La Bóveda no tiene el XML de este CFDI: el estatus salió de los metadatos de la "
                "descarga masiva del SAT.")
        if fecha:
            nota += " " + env._("Cancelado el %s.", tools.format_datetime(
                env, fecha, tz=ZONA_DEL_CFDI.key, dt_format='short'))
        return nota
