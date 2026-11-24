"""Configuración inicial de la Bóveda (dsi.cfdi.vault.setup.wizard).

La piden los clientes que instalan solos: después de copiar los módulos,
todo lo demás (empresa mexicana, RFC, licencia, lista 69-B) estaba en una guía
de texto. El asistente dice qué falta en cada paso y tiene el botón que lo
resuelve; se abre solo al instalar desde Aplicaciones.
"""
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_vault.wizards.cfdi_vault_setup_wizard import RFC_RE, _rfc_limpio
from odoo.addons.dsi_license_client.tests.common import token_de_prueba, usar_llave_de_prueba

DESCARGA = 'odoo.addons.dsi_cfdi_vault.models.cfdi_efos_entry.requests.get'


@tagged('post_install', '-at_install')
class TestAsistenteConfiguracion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        cls.mexico = cls.env.ref('base.mx')
        cls.pesos = cls.env.ref('base.MXN')
        cls.pesos.active = True
        cls.empresa = cls.env['res.company'].create({
            'name': "Empresa del asistente",
            'country_id': cls.mexico.id,
            'currency_id': cls.pesos.id,
            'chart_template': 'mx',
            'vat': 'EKU9003173C9',
        })
        cls.env.user.company_ids |= cls.empresa
        cls.env['dsi.cfdi.efos.entry'].sudo().search([]).unlink()

    def asistente(self, empresa=None):
        return self.env['dsi.cfdi.vault.setup.wizard'].create({'company_id': (empresa or self.empresa).id})

    def activar_licencia(self, empresa=None):
        licencia = self.env['dsi.license.client.state']._get_or_create(empresa or self.empresa)
        licencia.token = token_de_prueba(self.env)
        return licencia

    # Empresa ----------------------------------------------------------------

    def test_empresa_mexicana_lista(self):
        self.assertTrue(self.asistente().empresa_ok)

    def test_empresa_sin_mexico_ni_mxn_dice_que_falta(self):
        otra = self.env['res.company'].create({
            'name': "Empresa en dólares",
            'country_id': self.env.ref('base.us').id,
            'currency_id': self.env.ref('base.USD').id,
        })
        asistente = self.asistente(otra)
        self.assertFalse(asistente.empresa_ok)
        self.assertIn("no tiene país México", asistente.empresa_texto)
        self.assertIn("no tiene moneda MXN", asistente.empresa_texto)
        self.assertIn("catálogo de cuentas no es el mexicano", asistente.empresa_texto)

    def test_una_sucursal_fuera_de_mexico_tambien_cuenta(self):
        self.env['res.company'].create({
            'name': "Sucursal en dólares",
            'parent_id': self.empresa.id,
            'country_id': self.env.ref('base.us').id,
        })
        asistente = self.asistente()
        self.assertFalse(asistente.empresa_ok)
        self.assertIn("Sucursal en dólares no tiene país México", asistente.empresa_texto)

    # RFC --------------------------------------------------------------------

    def test_rfc_de_la_empresa(self):
        asistente = self.asistente()
        self.assertEqual(asistente.rfc, 'EKU9003173C9')
        self.assertTrue(asistente.rfc_ok)
        self.assertFalse(asistente.rfc_cambiado)

    def test_rfc_con_prefijo_de_pais_cuenta_como_rfc(self):
        # Así lo tienen algunas bases que vienen de Odoo 18; hoy Odoo ya no
        # deja capturarlo, por eso se prueba la limpieza directo.
        self.assertEqual(_rfc_limpio('MXEKU9003173C9'), 'EKU9003173C9')
        self.assertEqual(_rfc_limpio(' eku-900317 3c9 '), 'EKU9003173C9')
        self.assertTrue(RFC_RE.match(_rfc_limpio('MXEKU9003173C9')))

    def test_guardar_rfc_lo_limpia_y_lo_pone_en_la_empresa(self):
        self.empresa.vat = False
        asistente = self.asistente()
        self.assertFalse(asistente.rfc_ok)
        self.assertIn("no tiene RFC", asistente.rfc_texto)
        asistente.rfc = ' eku9003173c9 '
        self.assertTrue(asistente.rfc_cambiado)
        asistente.action_guardar_rfc()
        self.assertEqual(self.empresa.vat, 'EKU9003173C9')
        self.assertTrue(asistente.rfc_ok)
        self.assertFalse(asistente.rfc_cambiado)

    def test_un_rfc_mal_formado_no_se_guarda(self):
        asistente = self.asistente()
        asistente.rfc = 'ABC123'
        with self.assertRaises(UserError):
            asistente.action_guardar_rfc()
        self.assertEqual(self.empresa.vat, 'EKU9003173C9')

    # Licencia ---------------------------------------------------------------

    def test_sin_licencia_falta(self):
        asistente = self.asistente()
        self.assertFalse(asistente.licencia_ok)
        self.assertIn("Sin activar", asistente.licencia_texto)

    def test_con_licencia_vigente_esta_lista(self):
        self.activar_licencia()
        asistente = self.asistente()
        self.assertTrue(asistente.licencia_ok)
        self.assertTrue(asistente.licencia_texto)

    def test_la_licencia_se_activa_en_el_aviso_de_siempre_en_un_dialogo(self):
        accion = self.asistente().action_licencia()
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        self.assertEqual(accion['target'], 'new')
        aviso = self.env['dsi.license.client.notice'].browse(accion['res_id'])
        self.assertEqual(aviso.company_id, self.empresa)
        self.assertFalse(aviso.allow_continue)
        # Al activar, el aviso se cierra y el asistente se recarga.
        self.assertEqual(aviso._next(), {'type': 'ir.actions.act_window_close'})

    # Lista 69-B -------------------------------------------------------------

    def test_lista_69b_vacia_es_recomendada_no_obligatoria(self):
        asistente = self.asistente()
        self.assertFalse(asistente.efos_ok)
        self.assertIn("Vacía", asistente.efos_texto)
        # Los obligatorios de la Bóveda son empresa, RFC y licencia; la 69-B no.
        self.assertEqual(asistente._pasos_obligatorios()[:3],
                         [asistente.empresa_ok, asistente.rfc_ok, asistente.licencia_ok])

    def test_lista_69b_cargada(self):
        self.env['dsi.cfdi.efos.entry'].sudo().create({'rfc': 'AAA010101AAA', 'situation': 'definitivo'})
        asistente = self.asistente()
        self.assertTrue(asistente.efos_ok)
        self.assertIn("1 contribuyentes", asistente.efos_texto)

    def test_si_el_sat_no_responde_lo_dice_y_ofrece_el_csv(self):
        with patch(DESCARGA, side_effect=requests.ConnectionError("sin red")):
            with self.assertRaises(UserError) as error:
                self.asistente().action_descargar_69b()
        self.assertIn("Cargar CSV", str(error.exception))

    def test_cargar_csv_abre_el_asistente_de_la_lista(self):
        accion = self.asistente().action_cargar_69b()
        self.assertEqual(accion['res_model'], 'dsi.cfdi.efos.import.wizard')

    # Conjunto ---------------------------------------------------------------

    def test_listo_con_empresa_rfc_y_licencia(self):
        asistente = self.asistente()
        self.assertFalse(asistente.listo)
        self.activar_licencia()
        asistente = self.asistente()
        if 'llave_ok' in asistente._fields:
            # Con la Descarga SAT instalada también cuentan la llave y la
            # e.firma (sus pruebas, en dsi_cfdi_sat_sync).
            self.assertFalse(asistente.listo)
        else:
            self.assertTrue(asistente.listo)
        self.assertEqual(asistente.action_ir_a_la_boveda()['res_model'], 'cfdi.document')

    def test_se_abre_solo_al_instalar_y_abrirlo_a_mano_lo_da_por_visto(self):
        todo = self.env.ref('dsi_cfdi_vault.todo_cfdi_vault_setup')
        self.assertEqual(todo.action_id.id, self.env.ref('dsi_cfdi_vault.action_cfdi_vault_setup_wizard').id)
        todo.state = 'open'
        self.env['dsi.cfdi.vault.setup.wizard'].default_get(['company_id'])
        self.assertEqual(todo.state, 'done')

    def test_solo_para_administradores(self):
        menu = self.env.ref('dsi_cfdi_vault.menu_cfdi_vault_setup')
        campo = 'group_ids' if 'group_ids' in menu._fields else 'groups_id'
        self.assertEqual(menu[campo], self.env.ref('base.group_system'))
