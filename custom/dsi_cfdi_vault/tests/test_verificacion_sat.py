"""Verificación de estatus SAT de varios CFDI a la vez, desde la lista.

El botón del formulario lanza un error si algún CFDI falla, y ese error deshace
la transacción completa: usado sobre una selección, un solo CFDI que el SAT no
contestara tiraba también lo que sí se había verificado. La versión en lote
informa en vez de abortar, y se corta por tiempo para no rebasar el límite de
una petición web; estas pruebas cuidan las dos cosas.
"""
from datetime import date, datetime
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

RELOJ = 'odoo.addons.dsi_cfdi_vault.models.cfdi_document.time'


@tagged('post_install', '-at_install')
class TestVerificacionSatLote(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contador = 0

    def _documento(self, **valores):
        type(self).contador += 1
        uuid = f"DDDDDDDD-0000-0000-0000-{self.contador:012d}"
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': b'<cfdi/>', 'mimetype': 'text/xml',
        })
        return self.env['cfdi.document'].create({
            'uuid': uuid,
            'direction': 'received',
            'move_type': 'I',
            'invoice_date': date(2026, 6, 15),
            'amount_total': 1000.0,
            'partner_rfc_issuer': 'PRO010101AA1',
            'partner_rfc_receiver': 'EKU9003173C9',
            'xml_attachment_id': adjunto.id,
            **valores,
        })

    def _sat_responde(self, respuestas):
        """Sustituye la consulta al SAT: `respuestas` va de UUID a lo que
        devuelve l10n_mx_edi.document._fetch_sat_status."""
        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, uuid):
            return respuestas[uuid]
        return patch.object(
            self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta)

    def test_un_error_no_deshace_los_que_si_respondieron(self):
        vigente, fallido, cancelado = self._documento(), self._documento(), self._documento()
        poliza = self.env['account.move'].create({'move_type': 'entry'})
        cancelado.move_id = poliza
        respuestas = {
            vigente.uuid: {'value': 'valid'},
            fallido.uuid: {'value': 'error', 'error': "el SAT no respondió"},
            cancelado.uuid: {'value': 'cancelled'},
        }
        with self._sat_responde(respuestas):
            accion = (vigente | fallido | cancelado).action_verify_sat_status_batch()

        self.assertEqual(vigente.sat_status, 'valid')
        self.assertEqual(cancelado.sat_status, 'canceled')
        self.assertTrue(cancelado.is_discrepancy)
        self.assertEqual(fallido.sat_status, 'not_verified')
        self.assertFalse(fallido.sat_last_check)

        params = accion['params']
        self.assertEqual(accion['tag'], 'display_notification')
        self.assertEqual(params['type'], 'warning')
        self.assertTrue(params['sticky'])
        self.assertIn("CFDI verificados: 2", params['message'])
        self.assertIn("Cancelados que siguen contabilizados: 1", params['message'])
        self.assertIn("Sin poder verificar: 1", params['message'])
        self.assertIn(fallido.uuid, params['message'])
        # La lista se recarga para que se vean los estatus nuevos.
        self.assertEqual(params['next']['tag'], 'soft_reload')

    def test_se_corta_por_tiempo_y_sigue_donde_se_quedo(self):
        revisado = self._documento(sat_status='valid', sat_last_check=datetime(2026, 1, 1))
        nunca = self._documento()
        respuestas = {revisado.uuid: {'value': 'valid'}, nunca.uuid: {'value': 'valid'}}

        # Primera corrida: el reloj se agota después del primer CFDI. Va primero
        # el que nunca se ha verificado, aunque venga segundo en la selección.
        with self._sat_responde(respuestas), patch(RELOJ) as reloj:
            reloj.monotonic.side_effect = [0, 0, 999]
            accion = (revisado | nunca).action_verify_sat_status_batch()
        self.assertEqual(nunca.sat_status, 'valid')
        self.assertTrue(nunca.sat_last_check)
        self.assertEqual(revisado.sat_last_check, datetime(2026, 1, 1))
        self.assertIn("Pendientes: 1", accion['params']['message'])

        # Segunda corrida, con la misma selección: ahora va primero el pendiente.
        with self._sat_responde(respuestas):
            accion = (revisado | nunca).action_verify_sat_status_batch()
        self.assertNotEqual(revisado.sat_last_check, datetime(2026, 1, 1))
        self.assertEqual(accion['params']['type'], 'success')
        self.assertFalse(accion['params']['sticky'])

    def test_el_boton_del_formulario_sigue_avisando_el_error(self):
        doc = self._documento()
        with self._sat_responde({doc.uuid: {'value': 'not_defined'}}):
            with self.assertRaises(UserError):
                doc.action_verify_sat_status()
