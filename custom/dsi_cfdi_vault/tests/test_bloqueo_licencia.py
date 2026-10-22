"""Qué bloquea la licencia en la Bóveda y qué no.

Antes solo la importación manual revisaba la licencia: sin activar nada se
podía convertir a factura y todo lo demás. Sin licencia vigente (o con la
prueba terminada) se bloquea importar y convertir a factura; consultar y
exportar lo que ya está guardado nunca se bloquea.
"""
from datetime import date
from unittest.mock import patch

from odoo.exceptions import RedirectWarning
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_license_client.tests.common import token_de_prueba, usar_llave_de_prueba


@tagged('post_install', '-at_install')
class TestBloqueoLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        cls.company = cls.env.company
        cls.licencia = cls.env['dsi.license.client.state']._get_or_create(cls.company)
        cls.licencia.token = False
        adjunto = cls.env['ir.attachment'].create({
            'name': 'bloqueo.xml', 'raw': b'<cfdi/>', 'mimetype': 'text/xml',
        })
        cls.doc = cls.env['cfdi.document'].create({
            'uuid': 'FFFFFFFF-0000-0000-0000-000000000001',
            'direction': 'received',
            'move_type': 'I',
            'invoice_date': date(2026, 9, 15),
            'amount_total': 1000.0,
            'partner_rfc_issuer': 'PRO010101AA1',
            'partner_rfc_receiver': 'EKU9003173C9',
            'xml_attachment_id': adjunto.id,
        })

    def test_sin_licencia_no_se_importa(self):
        wizard = self.env['cfdi.import.wizard'].create({'company_id': self.company.id})
        with self.assertRaises(RedirectWarning) as error:
            wizard.action_import()
        self.assertIn("No se puede importar CFDI", str(error.exception))

    def test_sin_licencia_no_se_convierte_a_factura(self):
        with self.assertRaises(RedirectWarning) as error:
            self.doc.action_crear_factura_proveedor()
        self.assertIn("convertir CFDI en facturas de proveedor", str(error.exception))
        self.assertFalse(self.doc.move_id)

    def test_prueba_terminada_bloquea_y_lo_dice(self):
        self.licencia.token = token_de_prueba(
            self.env, dias=-1, license_type='trial', grace_days=0,
            base_url=self.licencia._get_base_url())
        with self.assertRaises(RedirectWarning) as error:
            self.doc.action_crear_factura_proveedor()
        self.assertIn("La prueba terminó", str(error.exception))

    def test_entrar_sin_licencia_muestra_el_aviso(self):
        accion = self.env['cfdi.document']._vault_entry_action()
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        aviso = self.env['dsi.license.client.notice'].browse(accion['res_id'])
        self.assertEqual(aviso.next_action, 'dsi_cfdi_vault.action_cfdi_document')
        # «Seguir solo consultando» lleva a la lista de siempre.
        self.assertEqual(aviso.action_continue()['res_model'], 'cfdi.document')

    def test_entrar_con_licencia_va_directo_a_la_lista(self):
        self.licencia.token = token_de_prueba(self.env)
        self.assertEqual(self.env['cfdi.document']._vault_entry_action()['res_model'], 'cfdi.document')

    def test_el_aviso_de_fin_de_prueba_llega_a_los_usuarios_de_la_boveda(self):
        usuario = self.env['res.users'].create({'name': "Contadora", 'login': 'contadora-boveda@example.com'})
        campo = 'group_ids' if 'group_ids' in usuario._fields else 'groups_id'
        usuario.write({campo: [(4, self.env.ref('dsi_cfdi_vault.group_cfdi_vault_user').id)]})
        self.assertIn(usuario.partner_id, self.licencia._reminder_partners())

    def test_consultar_y_exportar_siempre(self):
        self.assertEqual(
            self.env['cfdi.document'].search_read([('id', '=', self.doc.id)], ['uuid'])[0]['uuid'],
            self.doc.uuid)
        self.assertEqual(self.doc.export_data(['uuid'])['datas'], [[self.doc.uuid]])
        # Verificar el estatus en el SAT tampoco es de lo que se bloquea.
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status',
                          lambda *args: {'value': 'valid'}):
            self.doc.action_verify_sat_status()
        self.assertEqual(self.doc.sat_status, 'valid')
