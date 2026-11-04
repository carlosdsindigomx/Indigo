"""«Estado bóveda» de las facturas y las discrepancias de estatus.

Existe porque la Bóveda verifica en el SAT CFDI que Odoo ya no revisa (el cron
de l10n_mx_edi solo mira las compras de los últimos 7 días), y lo que
encontraba se quedaba en el CFDI: en la factura nadie veía que el SAT ya la
había cancelado. Ahora la factura lleva su Estado bóveda y la «Diferencia SAT /
bóveda» contra el estado SAT de Odoo, con filtros en clientes y proveedores, y
la diferencia queda como discrepancia guardada.

Lo que más cuidan estas pruebas:

  * en una compra la Bóveda nunca ejecuta «Actualizar SAT» ni toca el
    asiento: Odoo lo cancelaría. Solo la marca «Por revisar», que no se
    pierde al publicar la compra;
  * qué es diferencia y qué no (vigente contra «No definido» no lo es);
  * una sola discrepancia de estatus por factura, con precedencia, aunque la
    de menor precedencia ya estuviera revisada;
  * vigente en la Bóveda y cancelado en Odoo: se vuelve a verificar antes que
    los demás, y mientras no se verifica no es crítica;
  * la verificación SAT automática no pierde la tanda si un CFDI falla;
  * el estatus SAT del CFDI solo lo escribe la verificación (o el gestor
    contable): con él se resuelven las discrepancias;
  * lo que detecta el cron, sin idioma, va en el de la empresa;
  * el llenado por SQL al instalar da lo mismo que el cálculo.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_folio_fiscal.py: la conversión pasa por el import de CFDI de Odoo.
"""
from datetime import date, timedelta
from unittest.mock import patch

import psycopg2
from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import (
    HUELLA_VIGENTE_SIN_VERIFICAR,
    HUELLA_VIGENTE_VERIFICADA,
    TIPOS_DE_ESTADO,
    _clave_del_hallazgo,
)
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import RFC_EMISOR, RFC_RECEPTOR
from .test_factura_como_odoo import cfdi_xml
from .test_inconsistencias import mensajes_que_registran

GRUPOS_BOVEDA = 'base.group_user,dsi_cfdi_vault.group_cfdi_vault_user'
CLIENTES = 'account.action_move_out_invoice_type'
PROVEEDORES = 'account.action_move_in_invoice_type'


@tagged('post_install', '-at_install')
class TestEstadoBoveda(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
        ], order='sequence, id', limit=1)
        mx = cls.env.ref('base.mx')
        cls.proveedor = cls.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1, 'country_id': mx.id,
        })
        cls.cliente = cls.env['res.partner'].create({
            'name': "Cliente de Prueba", 'vat': 'CLI010101AA1', 'customer_rank': 1, 'country_id': mx.id,
        })
        cls.usuario = new_test_user(
            cls.env, login='boveda-estado', groups=GRUPOS_BOVEDA,
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return f"BBBBBBBB-4444-4000-8000-{self.contador:012d}"

    def _cfdi(self, uuid=None, direction='received', sat_status='valid', move_type='I'):
        """CFDI en la Bóveda: 1,000 + IVA = 1,160, como el XML."""
        uuid = uuid or self._uuid()
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'text/xml',
        })
        doc = self.env['cfdi.document'].create({
            'company_id': self.company.id,
            'direction': direction,
            'uuid': uuid,
            'move_type': move_type,
            'invoice_date': date(2026, 6, 15),
            'amount_total': 1160.0,
            'currency_id': self.company.currency_id.id,
            'partner_rfc_issuer': RFC_EMISOR,
            'partner_name_issuer': "Proveedor de Prueba",
            'partner_rfc_receiver': RFC_RECEPTOR,
            'sat_status': sat_status,
            'xml_attachment_id': adjunto.id,
        })
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _compra(self, sat_status='valid', publicar=True):
        """Factura de proveedor convertida desde la Bóveda: lleva el documento
        CFDI «Recibido» de Odoo con el estatus de la Bóveda."""
        doc = self._cfdi(sat_status=sat_status)
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        if publicar:
            factura.action_post()
        return doc, factura

    def _venta(self, publicar=True):
        factura = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'company_id': self.company.id,
            'partner_id': self.cliente.id,
            'invoice_date': date(2026, 6, 15),
            'invoice_line_ids': [Command.create({
                'name': "Servicio", 'quantity': 1, 'price_unit': 1000.0, 'tax_ids': [Command.clear()],
            })],
        })
        if publicar:
            factura.action_post()
        return factura

    def _documento_odoo(self, factura, estado, sat_state, uuid):
        """El documento CFDI de Odoo de la factura, como lo dejan el timbrado o
        «Actualizar SAT». De su XML sale el folio fiscal."""
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'application/xml',
            'res_model': 'account.move', 'res_id': factura.id,
        })
        Documento = self.env['l10n_mx_edi.document']
        valores = {
            'move_id': factura.id,
            'invoice_ids': [Command.set(factura.ids)],
            'state': estado,
            'sat_state': sat_state,
            'attachment_id': adjunto.id,
            'datetime': fields.Datetime.now(),
        }
        # Odoo 20 agregó la empresa al documento, obligatoria.
        if 'company_id' in Documento._fields:
            valores['company_id'] = factura.company_id.id
        return Documento.create(valores)

    def _documento_recibido(self, factura):
        return self.env['l10n_mx_edi.document'].search([
            ('move_id', '=', factura.id), ('state', '=', 'invoice_received')])

    def _sat_dice(self, docs, valor):
        """La verificación de la Bóveda, con la respuesta del SAT simulada."""
        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, _uuid):
            return {'value': valor}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            docs._fetch_and_store_sat_status()

    def _registros(self, facturas, kind=None):
        return self.Inconsistencia.search([
            ('kind', '=', kind) if kind else ('kind', 'in', TIPOS_DE_ESTADO),
            ('move_id', 'in', facturas.ids),
        ])

    def _por_revisar(self, factura):
        if 'review_state' in factura._fields:
            return factura.review_state in ('todo', 'anomaly')
        return not factura.checked

    def _revisar_factura(self, factura):
        """El contador marca la factura como revisada en su lista."""
        if 'review_state' in factura._fields:
            factura.review_state = 'reviewed'
        else:
            factura.checked = True

    def _revision_diaria(self):
        self.Inconsistencia._cron_detectar_inconsistencias()

    def _verificacion_automatica(self, valor, batch_size=200):
        """El cron de verificación SAT de la Bóveda, con la respuesta del SAT
        simulada. Devuelve los UUID que consultó, en orden."""
        consultados = []

        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, uuid):
            consultados.append(uuid)
            return {'value': valor}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            self.env['cfdi.document']._cron_verify_sat_status(batch_size=batch_size)
        return consultados

    def _revisiones_programadas(self):
        """Las veces que se pidió correr la revisión de discrepancias."""
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        return self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])

    def _venta_cancelada_con_boveda_vigente(self):
        """Venta que Odoo canceló (timbrada y cancelada en Odoo) y cuyo CFDI la
        Bóveda tiene vigente: lo que hace el flujo normal de Odoo antes de que
        la Bóveda vuelva a verificar."""
        uuid = self._uuid()
        factura = self._venta()
        factura.button_draft()
        factura.button_cancel()
        self._documento_odoo(factura, 'invoice_cancel', 'cancelled', uuid)
        return factura, self._cfdi(uuid=uuid, direction='issued')

    def _confirmar_historial(self):
        """Lo que hace el commit: escribir los mensajes de seguimiento."""
        self.env.flush_all()
        self.env.cr.precommit.run()

    def _mensajes(self, registro):
        return " ".join(str(m.body) for m in registro.message_ids)

    # -- La compra cancelada en el SAT -------------------------------------

    def test_compra_cancelada_en_el_sat_queda_por_revisar_sin_tocar_el_asiento(self):
        doc, factura = self._compra()
        self.assertEqual(factura.dsi_vault_sat_state, 'valid')
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self.assertFalse(self._por_revisar(factura))
        asiento = factura.line_ids.read(['account_id', 'debit', 'credit', 'tax_ids'])

        with patch.object(self.env.registry['account.move'], 'l10n_mx_edi_cfdi_try_sat',
                          autospec=True) as try_sat:
            self._sat_dice(doc, 'cancelled')
            self._revision_diaria()
        try_sat.assert_not_called()

        self.assertEqual(factura.dsi_vault_sat_state, 'cancelled')
        self.assertTrue(factura.dsi_vault_sat_mismatch)
        registro = self._registros(factura)
        self.assertEqual(len(registro), 1, "la revisión diaria no la duplica")
        self.assertEqual(registro.kind, 'sat_distinto')
        self.assertEqual(registro.state, 'open')
        self.assertEqual(registro.source, 'sat_check')
        self.assertEqual(registro.cfdi_document_id, doc)
        self.assertEqual(registro.partner_id, self.proveedor)
        self.assertEqual(registro.uuid, doc.uuid)
        self.assertEqual(registro.fingerprint, "boveda=cancelled|odoo=no_cancelado")
        self.assertEqual(registro.vault_value, "Cancelado")
        self.assertIn("Actualizar SAT", registro.observation)

        # El asiento y lo que Odoo sabe del CFDI, intactos.
        self.assertEqual(factura.state, 'posted')
        self.assertEqual(factura.line_ids.read(['account_id', 'debit', 'credit', 'tax_ids']), asiento)
        self.assertEqual(factura.l10n_mx_edi_cfdi_state, 'received')
        self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, 'valid')
        self.assertEqual(self._documento_recibido(factura).sat_state, 'valid')

        # Solo la marca «Por revisar».
        self.assertTrue(self._por_revisar(factura))
        # El contador la revisa y la deja así: no se le vuelve a marcar cada día.
        self._revisar_factura(factura)
        self._revision_diaria()
        self.assertFalse(self._por_revisar(factura))
        self.assertEqual(self._registros(factura), registro)

        # Los botones de la factura y del CFDI la cuentan.
        self.assertEqual(factura.dsi_inconsistency_count, 1)
        self.assertEqual(doc.dsi_inconsistency_count, 1)

    def test_venta_cancelada_en_odoo_con_boveda_vigente_es_diferencia(self):
        factura, doc = self._venta_cancelada_con_boveda_vigente()
        uuid = doc.uuid

        doc.move_id = factura

        self.assertEqual(factura.l10n_mx_edi_cfdi_uuid, uuid)
        self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, 'cancelled')
        self.assertEqual(factura.dsi_vault_sat_state, 'valid')
        self.assertTrue(factura.dsi_vault_sat_mismatch)
        registro = self._registros(factura)
        self.assertEqual(registro.kind, 'sat_distinto')
        # La Bóveda no lo ha verificado desde que Odoo lo canceló.
        self.assertEqual(registro.fingerprint, HUELLA_VIGENTE_SIN_VERIFICAR)
        self.assertEqual(registro.severity, 'warning')
        self.assertEqual(registro.source, 'link')
        self.assertEqual(registro.partner_id, self.cliente)
        self.assertIn("lo vuelve a consultar antes que los demás", registro.observation)

        # La Bóveda vuelve a verificar y el SAT confirma: se resuelve sola.
        self._sat_dice(doc, 'cancelled')
        self.assertEqual(factura.dsi_vault_sat_state, 'cancelled')
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self.assertEqual(registro.state, 'resolved')
        self.assertEqual(self._registros(factura), registro, "con la factura cancelada no hay otra")

    def test_vigente_contra_no_definido_no_es_diferencia(self):
        """Odoo aún no consulta: no es diferencia. «No encontrado» en la Bóveda
        tampoco, porque no dice qué pasó con el CFDI."""
        doc, factura = self._compra()
        documento = self._documento_recibido(factura)
        for sat_state in ('not_defined', 'error'):
            documento.sat_state = sat_state
            self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, sat_state)
            self.assertEqual(factura.dsi_vault_sat_state, 'valid')
            self.assertFalse(factura.dsi_vault_sat_mismatch)

        # Una venta sin documento CFDI de Odoo, sin estado SAT.
        venta = self._venta()
        self._cfdi(direction='issued').move_id = venta
        self.assertFalse(venta.l10n_mx_edi_cfdi_sat_state)
        self.assertEqual(venta.dsi_vault_sat_state, 'valid')
        self.assertFalse(venta.dsi_vault_sat_mismatch)

        self._sat_dice(doc, 'not_found')
        self.assertEqual(factura.dsi_vault_sat_state, 'not_found')
        self.assertFalse(factura.dsi_vault_sat_mismatch)

        self._revision_diaria()
        self.assertFalse(self._registros(factura | venta))
        self.assertFalse(self._por_revisar(factura))

    def test_asiento_cancelado_no_es_diferencia(self):
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        registro = self._registros(factura)
        self.assertEqual(registro.state, 'open')

        # El contador decide cancelar la factura: deja de haber diferencia y la
        # revisión diaria resuelve el registro.
        factura.button_draft()
        factura.button_cancel()
        self.assertEqual(factura.dsi_vault_sat_state, 'cancelled')
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self._revision_diaria()
        self.assertEqual(registro.state, 'resolved')
        self.assertEqual(self._registros(factura), registro)

        # Una compra ya cancelada cuando la Bóveda ve la cancelación: nada.
        otro_doc, otra = self._compra(publicar=False)
        otra.button_cancel()
        self._sat_dice(otro_doc, 'cancelled')
        self.assertEqual(otra.dsi_vault_sat_state, 'cancelled')
        self.assertFalse(otra.dsi_vault_sat_mismatch)
        self.assertFalse(self._registros(otra))

    def test_desvincular_el_cfdi_recalcula_y_resuelve(self):
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        registro = self._registros(factura)

        doc.action_unlink_move()
        self.assertFalse(factura.dsi_vault_sat_state)
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self.assertEqual(registro.state, 'resolved', "sin esperar a la revisión diaria")

        # Volver a ligarlo lo reabre: es el mismo hallazgo.
        doc.action_confirm_match(factura.id)
        self.assertTrue(factura.dsi_vault_sat_mismatch)
        self.assertEqual(self._registros(factura), registro)
        self.assertEqual(registro.state, 'open')

        # Archivar el CFDI es como desligarlo.
        doc.action_archive()
        self.assertFalse(factura.dsi_vault_sat_state)
        self.assertEqual(registro.state, 'resolved')

    def test_un_cfdi_ligado_de_otro_folio_no_da_el_estado_boveda(self):
        """La venta se canceló, se regresó a borrador y se volvió a timbrar:
        el CFDI anterior, cancelado, sigue ligado y el nuevo no está en la
        Bóveda. Tomaba el estatus del anterior («el primero que llegó») y
        quedaba un «Estatus SAT distinto» que nunca se resolvía y aconsejaba
        «Actualizar SAT». Sin folio fiscal en Odoo sí cuenta el ligado."""
        venta = self._venta()
        self._documento_odoo(venta, 'invoice_sent', 'valid', self._uuid())
        self._cfdi(direction='issued', sat_status='canceled').move_id = venta
        self.assertFalse(venta.dsi_vault_sat_state)
        self.assertFalse(venta.dsi_vault_sat_mismatch)
        self._revision_diaria()
        self.assertFalse(self._registros(venta))

        # Cuando llega el CFDI de su folio, ese manda.
        self._cfdi(uuid=venta.l10n_mx_edi_cfdi_uuid, direction='issued', sat_status='valid').move_id = venta
        self.assertEqual(venta.dsi_vault_sat_state, 'valid')

        sin_folio = self._venta()
        self._cfdi(direction='issued', sat_status='canceled').move_id = sin_folio
        self.assertEqual(sin_folio.dsi_vault_sat_state, 'cancelled')
        self.assertTrue(sin_folio.dsi_vault_sat_mismatch)

    def test_cancelado_contabilizado_con_fecha_de_bloqueo(self):
        """La Bóveda ve la cancelación primero. Después Odoo la ve por su
        cuenta, pero no puede cancelar el asiento por la fecha de bloqueo: ya no
        es «Estatus SAT distinto» sino «Cancelado con factura contabilizada»,
        y el cambio lo encuentra la revisión diaria."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        distinto = self._registros(factura)
        self.assertEqual(distinto.kind, 'sat_distinto')

        self.company.fiscalyear_lock_date = date(2026, 6, 30)
        self._documento_recibido(factura)._update_document_sat_state('cancelled')

        self.assertEqual(factura.l10n_mx_edi_cfdi_state, 'cancel', "Odoo registró la cancelación")
        self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, 'cancelled')
        self.assertEqual(factura.state, 'posted', "la fecha de bloqueo no deja cancelarla")
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self.assertEqual(distinto.state, 'open', "Odoo cambió por su cuenta; lo ve la revisión diaria")

        self._revision_diaria()
        self.assertEqual(distinto.state, 'resolved')
        contabilizado = self._registros(factura) - distinto
        self.assertEqual(contabilizado.kind, 'cancelado_contabilizado')
        self.assertEqual(contabilizado.state, 'open')
        self.assertEqual(contabilizado.severity, 'critical')
        self.assertIn("fecha de bloqueo", contabilizado.observation)

        # No es «Diferencia SAT / bóveda» (las dos dicen cancelado), pero sí
        # una discrepancia de estatus sin revisar: por eso el filtro ya no se
        # llama «Diferencia sin revisar».
        self.assertEqual(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_unreviewed'), factura)
        self.assertFalse(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_mismatch'))

    def test_la_cancelacion_por_actualizar_gana(self):
        """Una sola discrepancia de estatus por factura: la cancelación por
        actualizar (la registra su propio flujo) gana sobre las del Estado
        bóveda, que se resuelven aunque alguien ya las haya revisado."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        distinto = self._registros(factura)
        distinto.with_user(self.usuario).action_marcar_revisado()

        self.Inconsistencia._sincronizar('cancelacion_por_actualizar', [('move_id', '=', factura.id)], [{
            'company_id': self.company.id,
            'dedupe_key': _clave_del_hallazgo('cancelacion_por_actualizar', factura=factura),
            'fingerprint': 'boveda=cancelled',
            'move_id': factura.id,
        }], 'try_sat')
        self._revision_diaria()

        self._confirmar_historial()
        self.assertEqual(distinto.state, 'resolved')
        self.assertEqual(self._registros(factura).filtered(lambda r: r.state != 'resolved').kind,
                         'cancelacion_por_actualizar')
        mensajes = self._mensajes(distinto)
        self.assertIn("Cancelación por actualizar", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario.name}", mensajes)

    def test_revisada_y_despues_contabilizada_queda_una_sola(self):
        """La precedencia también resuelve las revisadas: un «Estatus SAT
        distinto» que alguien revisó y que después pasa a «Cancelado con
        factura contabilizada» se resuelve. Antes quedaban dos vivas."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        distinto = self._registros(factura)
        distinto.with_user(self.usuario).action_marcar_revisado()

        self.company.fiscalyear_lock_date = date(2026, 6, 30)
        self._documento_recibido(factura)._update_document_sat_state('cancelled')
        self._revision_diaria()
        self._confirmar_historial()

        vivas = self._registros(factura).filtered(lambda r: r.state != 'resolved')
        self.assertEqual(len(vivas), 1)
        self.assertEqual(vivas.kind, 'cancelado_contabilizado')
        self.assertEqual(distinto.state, 'resolved')
        mensajes = self._mensajes(distinto)
        self.assertIn("Cancelado con factura contabilizada", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario.name}", mensajes)

    # -- Revisado no es resuelto -------------------------------------------

    def test_una_discrepancia_revisada_se_resuelve_cuando_la_causa_desaparece(self):
        """Revisado es que alguien la vio; resuelto, que la causa ya no existe.
        El contador revisa la discrepancia y después cancela la factura: se
        resuelve, con un mensaje que dice quién la había revisado. Antes se
        quedaba revisada para siempre. Si la vuelve a publicar, la diferencia
        reaparece: se reabre pendiente y la compra vuelve a «Por revisar»."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        registro = self._registros(factura)
        registro.with_user(self.usuario).action_marcar_revisado()
        self._revisar_factura(factura)

        factura.button_draft()
        factura.button_cancel()
        self.assertFalse(factura.dsi_vault_sat_mismatch)
        self._revision_diaria()
        self._confirmar_historial()
        self.assertEqual(registro.state, 'resolved')
        self.assertIn(f"Estaba revisada por {self.usuario.name}", self._mensajes(registro))

        factura.button_draft()
        factura.action_post()
        self._revision_diaria()
        self.assertEqual(self._registros(factura), registro, "es el mismo hallazgo")
        self.assertEqual(registro.state, 'open')
        self.assertFalse(registro.reviewed_by_id)
        self.assertTrue(self._por_revisar(factura))

        # Igual en una venta (timbrada fuera de Odoo) que se cancela y se
        # vuelve a publicar.
        venta = self._venta()
        doc_venta = self._cfdi(direction='issued')
        doc_venta.move_id = venta
        self._sat_dice(doc_venta, 'cancelled')
        de_la_venta = self._registros(venta)
        de_la_venta.with_user(self.usuario).action_marcar_revisado()
        venta.button_draft()
        venta.button_cancel()
        self._revision_diaria()
        self.assertEqual(de_la_venta.state, 'resolved')
        venta.button_draft()
        venta.action_post()
        self._revision_diaria()
        self.assertEqual(self._registros(venta), de_la_venta)
        self.assertEqual(de_la_venta.state, 'open')

    # -- Solo la verificación escribe el estatus ---------------------------

    def test_el_usuario_de_la_boveda_no_escribe_el_estatus_sat_a_mano(self):
        """El estatus SAT y la última verificación los escribe la
        verificación. Antes el Usuario de la Bóveda podía, por RPC o
        importando, dar el CFDI por vigente: la discrepancia se resolvía «por
        la Verificación SAT» sin pasar por «Marcar como revisado». Con la
        fecha de verificación, una «vigente en la Bóveda, cancelado en Odoo»
        salía de la cola sin consultar al SAT."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        registro = self._registros(factura)
        como_usuario = doc.with_user(self.usuario).with_context(allowed_company_ids=self.company.ids)

        for valores in ({'sat_status': 'valid'}, {'sat_status': 'not_verified'},
                        {'sat_last_check': fields.Datetime.now()}):
            with self.subTest(valores=valores), self.assertRaises(AccessError):
                como_usuario.write(valores)
        with self.assertRaises(AccessError):
            self.env['cfdi.document'].with_user(self.usuario).with_context(
                allowed_company_ids=self.company.ids).create({
                    'company_id': self.company.id, 'direction': 'received', 'uuid': self._uuid(),
                    'currency_id': self.company.currency_id.id, 'sat_status': 'valid',
                    'xml_attachment_id': doc.xml_attachment_id.id,
                })
        self.assertEqual(doc.sat_status, 'canceled')
        self.assertEqual(registro.state, 'open')

        # Con «Verificar estatus SAT» sí, y queda a su nombre en el historial.
        otro, otra_factura = self._compra()
        self._sat_dice(otro.with_user(self.usuario).with_context(allowed_company_ids=self.company.ids),
                       'cancelled')
        self.assertEqual(otro.sat_status, 'canceled')
        self.assertEqual(self._registros(otra_factura).state, 'open')
        self._confirmar_historial()
        cambio = mensajes_que_registran(otro, 'sat_status')[:1]
        self.assertEqual(cambio.author_id, self.usuario.partner_id)

        # La cancelación es definitiva: la consulta ya no la regresa a vigente.
        self._sat_dice(como_usuario, 'valid')
        self.assertEqual(doc.sat_status, 'canceled')
        self.assertEqual(registro.state, 'open')
        self.assertIn("es definitiva", str(doc.message_ids[:1].body))

        # El gestor contable puede corregirlo a mano, y eso sí la resuelve.
        gestor = new_test_user(
            self.env, login='gestor-estatus', groups=GRUPOS_BOVEDA + ',account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        doc.with_user(gestor).with_context(allowed_company_ids=self.company.ids).sat_status = 'valid'
        self.assertEqual(factura.dsi_vault_sat_state, 'valid')
        self.assertEqual(registro.state, 'resolved')

    # -- Idioma de lo que se guarda ----------------------------------------

    def test_lo_que_detecta_el_cron_va_en_el_idioma_de_la_empresa(self):
        """El cron no lleva idioma en el contexto y las etiquetas de Odoo
        (estado SAT, estado de la factura) quedaban en inglés: «Validated ·
        Posted» en una base en español. Como el texto se guarda, nunca se
        corregía. Ahora, sin idioma en el contexto, va en el de la empresa de
        la factura. Las traducciones de Odoo no se cargan en la base de
        pruebas: se ponen a mano las dos etiquetas que se revisan."""
        self.env['res.lang']._activate_lang('es_MX')
        Seleccion = self.env['ir.model.fields.selection']
        for campo, valor, etiqueta in (('state', 'posted', "Registrado"),
                                       ('l10n_mx_edi_cfdi_sat_state', 'valid', "Validado")):
            Seleccion.search([
                ('field_id.model', '=', 'account.move'), ('field_id.name', '=', campo), ('value', '=', valor),
            ]).with_context(lang='es_MX').name = etiqueta
        self.company.partner_id.lang = 'es_MX'
        doc, factura = self._compra()

        # Como el cron: sin idioma en el contexto.
        cron = self.env(context={'lastcall': False, 'allowed_company_ids': self.company.ids})
        self.assertFalse(cron.context.get('lang'))
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status',
                          lambda *_args: {'value': 'cancelled'}):
            cron['cfdi.document']._cron_verify_sat_status()
        cron['dsi.cfdi.inconsistency']._cron_detectar_inconsistencias()

        registro = self._registros(factura)
        self.assertEqual(registro.source, 'cron')
        self.assertEqual(registro.odoo_value, "Validado · Registrado")
        self.assertIn("estado SAT «Validado», factura «Registrado»", registro.observation)

    # -- Por revisar al publicar -------------------------------------------

    def test_compra_en_borrador_cancelada_en_el_sat_sigue_por_revisar_al_publicarla(self):
        """La conversión deja la compra en borrador. Si el SAT la cancela antes
        de publicarla, al publicar Odoo la daba por revisada y la marca se
        perdía. Ahora se vuelve a marcar mientras la discrepancia siga
        pendiente; si el contador la revisa después, no se le vuelve a
        marcar. Tampoco si la discrepancia ya se revisó en la Bóveda."""
        doc, factura = self._compra(publicar=False)
        self._sat_dice(doc, 'cancelled')
        registro = self._registros(factura)
        self.assertEqual((registro.kind, registro.state), ('sat_distinto', 'open'))
        self.assertTrue(self._por_revisar(factura))

        factura.action_post()
        self.assertTrue(self._por_revisar(factura))
        self.assertEqual(self._registros(factura), registro)

        self._revisar_factura(factura)
        self._revision_diaria()
        self.assertFalse(self._por_revisar(factura), "la revisó el contador")

        otro_doc, otra = self._compra(publicar=False)
        self._sat_dice(otro_doc, 'cancelled')
        self._registros(otra).with_user(self.usuario).action_marcar_revisado()
        otra.action_post()
        self.assertFalse(self._por_revisar(otra))

    def test_no_se_pisa_una_anomalia_de_odoo_20(self):
        """En Odoo 20 el import de l10n_mx_edi deja 'anomaly' cuando los
        totales o impuestos del CFDI no cuadran: ya es «Por revisar», y la
        más grave. La Bóveda la cambiaba por 'todo' y se perdía. En 18 y 19
        la marca es checked y no hay anomalía."""
        if 'review_state' not in self.env['account.move']._fields:
            self.skipTest("Odoo 18 y 19 marcan «Por revisar» con checked, sin anomalía.")
        doc, factura = self._compra()
        factura.review_state = 'anomaly'
        self._sat_dice(doc, 'cancelled')
        self.assertEqual(self._registros(factura).kind, 'sat_distinto')
        self.assertEqual(factura.review_state, 'anomaly')

    # -- Vigente en la Bóveda, cancelado en Odoo ---------------------------

    def test_venta_cancelada_en_odoo_se_verifica_antes_que_los_demas(self):
        """Una venta de hace 60 días que Odoo canceló, con el CFDI verificado
        vigente por la Bóveda hace dos días: le tocaba la verificación
        mensual, y mientras tanto la discrepancia salía como crítica. Ahora es
        advertencia, la verificación automática toma ese CFDI antes que los
        demás y, si el SAT confirma la cancelación, se resuelve sola."""
        factura, doc = self._venta_cancelada_con_boveda_vigente()
        doc.write({
            'invoice_date': fields.Date.today() - timedelta(days=60),
            'sat_last_check': fields.Datetime.now() - timedelta(days=2),
        })
        doc.move_id = factura
        self._revision_diaria()
        registro = self._registros(factura)
        self.assertEqual(registro.fingerprint, HUELLA_VIGENTE_SIN_VERIFICAR)
        self.assertEqual(registro.severity, 'warning')
        self.assertEqual(self.Inconsistencia._cfdi_por_volver_a_verificar(), doc)

        # Otro CFDI sin verificar nunca: en una tanda de uno, va primero el que
        # pidió la discrepancia.
        self._cfdi()
        programadas = self._revisiones_programadas()
        self.assertEqual(self._verificacion_automatica('cancelled', batch_size=1), [doc.uuid])
        self.assertEqual(doc.sat_status, 'canceled')
        self.assertFalse(self.Inconsistencia._cfdi_por_volver_a_verificar())
        # La verificación automática no revisa discrepancias: programa la revisión.
        self.assertEqual(registro.state, 'open')
        self.assertEqual(self._revisiones_programadas(), programadas + 1)

        self._revision_diaria()
        self.assertEqual(registro.state, 'resolved')
        self.assertFalse(factura.dsi_vault_sat_mismatch)

    def test_si_el_sat_la_confirma_vigente_la_diferencia_es_critica(self):
        """Verificada otra vez y el SAT la sigue dando vigente: ya no es que
        la Bóveda esté atrasada. La «sin verificar» se resuelve (la sustituye
        otra) y queda una crítica, que no se vuelve a pedir verificar."""
        factura, doc = self._venta_cancelada_con_boveda_vigente()
        doc.move_id = factura
        pendiente = self._registros(factura)
        self.assertEqual(pendiente.fingerprint, HUELLA_VIGENTE_SIN_VERIFICAR)

        self.assertEqual(self._verificacion_automatica('valid'), [doc.uuid])
        self._revision_diaria()
        self._confirmar_historial()

        self.assertEqual(pendiente.state, 'resolved')
        self.assertIn("Cambiaron los valores", self._mensajes(pendiente))
        verificada = self._registros(factura) - pendiente
        self.assertEqual(verificada.fingerprint, HUELLA_VIGENTE_VERIFICADA)
        self.assertEqual((verificada.state, verificada.severity), ('open', 'critical'))
        self.assertIn("El SAT le confirmó a la Bóveda", verificada.observation)
        self.assertFalse(self.Inconsistencia._cfdi_por_volver_a_verificar())

        self._revision_diaria()
        self.assertEqual(self._registros(factura).filtered(lambda r: r.state != 'resolved'), verificada)

        # Con «Verificar estatus SAT» en el CFDI es crítica de una vez: la
        # verificación misma la encontró.
        otra, otro_doc = self._venta_cancelada_con_boveda_vigente()
        otro_doc.move_id = otra
        self._sat_dice(otro_doc, 'valid')
        self.assertEqual(
            self._registros(otra).filtered(lambda r: r.state != 'resolved').fingerprint, HUELLA_VIGENTE_VERIFICADA)

    def test_la_observacion_de_una_venta_depende_de_si_tiene_actualizar_sat(self):
        """«Actualizar SAT» solo aparece en la factura si Odoo tiene su CFDI
        timbrado. En una venta timbrada fuera de Odoo la observación ya no lo
        recomienda."""
        sin_documento = self._venta()
        doc = self._cfdi(direction='issued')
        doc.move_id = sin_documento
        self._sat_dice(doc, 'cancelled')
        self.assertFalse(sin_documento.l10n_mx_edi_update_sat_needed)
        observacion = self._registros(sin_documento).observation
        self.assertIn("no tiene un CFDI timbrado en Odoo", observacion)
        self.assertNotIn("«Actualizar SAT» en la factura", observacion)

        uuid = self._uuid()
        con_documento = self._venta()
        self._documento_odoo(con_documento, 'invoice_sent', 'valid', uuid)
        doc = self._cfdi(uuid=uuid, direction='issued')
        doc.move_id = con_documento
        self._sat_dice(doc, 'cancelled')
        self.assertTrue(con_documento.l10n_mx_edi_update_sat_needed)
        self.assertIn("«Actualizar SAT» en la factura registra la cancelación",
                      self._registros(con_documento).observation)

    # -- Verificación automática -------------------------------------------

    def test_la_verificacion_automatica_no_pierde_la_tanda(self):
        """Cada CFDI se verifica en su savepoint: el que falla, aunque deje la
        transacción abortada (un error de SQL) o choque con otra transacción,
        no se lleva lo ya verificado de los demás. Antes, un choque con la
        revisión diaria dejaba la tanda entera sin guardar. El choque queda
        como aviso; el error, con su traceback."""
        docs = [self._cfdi() for _i in range(4)]
        falla_sql, choca = docs[1].uuid, docs[2].uuid
        cr = self.env.cr

        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, uuid):
            if uuid == falla_sql:
                cr.execute("SELECT 1/0")
            if uuid == choca:
                raise psycopg2.errors.SerializationFailure("could not serialize access due to concurrent update")
            return {'value': 'cancelled'}

        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_document', level='WARNING') as log:
            self.env['cfdi.document']._cron_verify_sat_status()

        self.assertEqual([d.sat_status for d in docs], ['canceled', 'valid', 'valid', 'canceled'])
        self.assertEqual([bool(d.sat_last_check) for d in docs], [True, False, False, True])
        self.assertEqual(sorted(r.levelname for r in log.records), ['ERROR', 'WARNING'])
        aviso = next(r for r in log.records if r.levelname == 'WARNING')
        self.assertIn("SerializationFailure", aviso.getMessage())
        self.assertIsNone(aviso.exc_info, "sin traceback")

    # -- Filtros ---------------------------------------------------------

    def _filtrar(self, accion, filtro):
        """Lo que da el filtro `filtro` en la lista de la acción `accion`, como
        el Usuario de la Bóveda."""
        accion = self.env.ref(accion)
        Factura = self.env['account.move'].with_user(self.usuario).with_context(
            allowed_company_ids=self.company.ids)
        arch = Factura.get_views([(accion.search_view_id.id, 'search')])['views']['search']['arch']
        nodo = etree.fromstring(arch).xpath(f"//filter[@name='{filtro}']")
        self.assertEqual(len(nodo), 1, f"el filtro {filtro} está en la búsqueda de {accion.name}")
        if nodo[0].get('context'):
            return safe_eval(nodo[0].get('context'))
        return Factura.search(safe_eval(accion.domain) + safe_eval(nodo[0].get('domain')))

    def test_los_filtros_de_clientes_y_proveedores(self):
        doc_dif, compra_dif = self._compra()
        self._sat_dice(doc_dif, 'cancelled')
        _doc, compra_ok = self._compra()
        uuid = self._uuid()
        venta_dif = self._venta()
        self._documento_odoo(venta_dif, 'invoice_sent', 'valid', uuid)
        doc_venta = self._cfdi(uuid=uuid, direction='issued')
        doc_venta.move_id = venta_dif
        self._sat_dice(doc_venta, 'cancelled')
        self._venta()

        self.assertEqual(self._filtrar(CLIENTES, 'filter_dsi_vault_sat_mismatch'), venta_dif)
        self.assertEqual(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_mismatch'), compra_dif)
        self.assertEqual(self._filtrar(CLIENTES, 'filter_dsi_vault_cancelled'), venta_dif)
        self.assertEqual(self._filtrar(PROVEEDORES, 'filter_dsi_vault_cancelled'), compra_dif)
        self.assertEqual(self._filtrar(CLIENTES, 'filter_dsi_vault_sat_unreviewed'), venta_dif)
        self.assertEqual(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_unreviewed'), compra_dif)

        # Revisada la discrepancia, sale de «sin revisar» pero sigue siendo diferencia.
        self._registros(compra_dif).with_user(self.usuario).action_marcar_revisado()
        self.assertFalse(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_unreviewed'))
        self.assertEqual(self._filtrar(PROVEEDORES, 'filter_dsi_vault_sat_mismatch'), compra_dif)

        # Agrupar por Estado bóveda, en las dos listas.
        for accion in (CLIENTES, PROVEEDORES):
            self.assertEqual(self._filtrar(accion, 'group_by_dsi_vault_sat_state'),
                             {'group_by': 'dsi_vault_sat_state'})
        grupos = dict(self.env['account.move']._read_group(
            [('move_type', '=', 'in_invoice'), ('company_id', '=', self.company.id)],
            ['dsi_vault_sat_state'], ['__count']))
        self.assertEqual(grupos, {'cancelled': 1, 'valid': 1})
        self.assertIn(compra_ok, self._filtrar(PROVEEDORES, 'posted'))

    def test_un_usuario_de_facturacion_sin_la_boveda_abre_la_factura(self):
        """Ve el Estado bóveda y la diferencia, que son de la factura, pero no
        los CFDI ni las discrepancias de la Bóveda. Antes ni siquiera podía
        abrir la factura: el botón «CFDI» pedía los CFDI al cargarla."""
        doc, factura = self._compra()
        self._sat_dice(doc, 'cancelled')
        facturacion = new_test_user(
            self.env, login='solo-facturacion', groups='base.group_user,account.group_account_invoice',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        Factura = factura.with_user(facturacion).with_context(allowed_company_ids=self.company.ids)

        vistas = Factura.get_views([
            (False, 'form'), (self.env.ref('account.view_account_invoice_filter').id, 'search')])
        campos = vistas['models']['account.move']
        campos = campos.get('fields', campos)
        self.assertNotIn('cfdi_document_count', campos)
        self.assertNotIn('dsi_inconsistency_count', campos)
        valores = Factura.read(list(campos))[0]
        self.assertEqual(valores['dsi_vault_sat_state'], 'cancelled')
        self.assertTrue(valores['dsi_vault_sat_mismatch'])

        busqueda = etree.fromstring(vistas['views']['search']['arch'])
        self.assertTrue(busqueda.xpath("//filter[@name='filter_dsi_vault_sat_mismatch']"))
        self.assertFalse(busqueda.xpath("//filter[@name='filter_dsi_vault_sat_unreviewed']"))
        self.assertFalse(busqueda.xpath("//filter[@name='filter_no_cfdi_document']"))

    # -- Instalación -----------------------------------------------------

    def test_el_llenado_por_sql_da_lo_mismo_que_el_calculo(self):
        """Al instalar, las columnas se llenan con SQL para no recalcular todas
        las facturas de la base en Python. Tiene que dar lo mismo que el
        cálculo en cada caso que el cálculo distingue."""
        Factura = self.env['account.move']
        facturas = Factura
        for sat_status in ('valid', 'canceled', 'not_found', 'not_verified'):
            facturas |= self._compra(sat_status='valid')[1]
            facturas[-1:].cfdi_document_ids.sat_status = sat_status
        # Venta cancelada en Odoo, vigente en la Bóveda.
        uuid = self._uuid()
        venta = self._venta()
        self._documento_odoo(venta, 'invoice_sent', 'cancelled', uuid)
        self._cfdi(uuid=uuid, direction='issued').move_id = venta
        # Venta en borrador sin estado SAT y cancelada en la Bóveda.
        borrador = self._venta(publicar=False)
        self._cfdi(direction='issued', sat_status='canceled').move_id = borrador
        # Venta con el asiento cancelado y cancelada en la Bóveda.
        cancelada = self._venta(publicar=False)
        cancelada.button_cancel()
        self._cfdi(direction='issued', sat_status='canceled').move_id = cancelada
        # Dos CFDI de ingreso: gana el del folio fiscal, aunque llegó después.
        uuid = self._uuid()
        dos = self._venta()
        self._documento_odoo(dos, 'invoice_sent', 'valid', uuid)
        self._cfdi(direction='issued', sat_status='canceled').move_id = dos
        self._cfdi(uuid=uuid, direction='issued', sat_status='valid').move_id = dos
        # Con folio fiscal, uno ligado de otro folio no cuenta (el anterior de
        # una venta que se volvió a timbrar).
        refacturada = self._venta()
        self._documento_odoo(refacturada, 'invoice_sent', 'valid', self._uuid())
        self._cfdi(direction='issued', sat_status='canceled').move_id = refacturada
        # Un CFDI de Pago o uno archivado no cuentan.
        con_pago = self._venta()
        self._cfdi(direction='issued', sat_status='canceled', move_type='P').move_id = con_pago
        archivado = self._venta()
        doc_archivado = self._cfdi(direction='issued', sat_status='canceled')
        doc_archivado.move_id = archivado
        doc_archivado.action_archive()
        # Una póliza tiene Estado bóveda, pero no diferencia: no es factura.
        poliza = Factura.create({'move_type': 'entry', 'company_id': self.company.id})
        self._cfdi(sat_status='canceled').move_id = poliza
        facturas |= venta | borrador | cancelada | dos | refacturada | con_pago | archivado | poliza

        def valores(registros):
            return {f.id: (f.dsi_vault_sat_state, f.dsi_vault_sat_mismatch) for f in registros}

        esperado = valores(facturas)
        self.assertEqual(
            [esperado[f.id] for f in facturas],
            [('valid', False), ('cancelled', True), ('not_found', False), (False, False),
             ('valid', True), ('cancelled', True), ('cancelled', False), ('valid', False),
             (False, False), (False, False), (False, False), ('cancelled', False)],
            "el cálculo distingue cada caso")
        todas = Factura.search([])
        antes = valores(todas)

        self.env.flush_all()
        self.env.cr.execute("UPDATE account_move SET dsi_vault_sat_state = NULL, dsi_vault_sat_mismatch = NULL")
        self.env.invalidate_all()
        self.assertEqual(set(valores(facturas).values()), {(False, False)})

        with self.assertLogs('odoo.addons.dsi_cfdi_vault.models.account_move', level='INFO'):
            Factura._dsi_llenar_estado_boveda_por_sql()
        self.env.invalidate_all()

        self.assertEqual(valores(facturas), esperado)
        self.assertEqual(valores(todas), antes)
