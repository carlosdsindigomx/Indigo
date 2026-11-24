"""Los pasos de la Descarga SAT en la configuración inicial de la Bóveda: la
llave que cifra la e.firma, la e.firma y la descarga automática.

La llave era lo más difícil de explicar en la guía: en un servidor propio va
en el odoo.conf y en Odoo.sh, que no lo deja editar, había que generarla con
Python y capturarla a mano como parámetro del sistema.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_sat_sync import crypto
from odoo.addons.dsi_license_client.tests.common import token_de_prueba, usar_llave_de_prueba

RFC = 'EKU9003173C9'


@tagged('post_install', '-at_install')
class TestAsistenteSat(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        # Sin llave en ningún lado, salvo la que ponga cada prueba.
        cls.startClassPatcher(patch.object(crypto, 'config', {}))
        # crypto lee la variable de entorno de este diccionario y no del
        # os.environ real. Un patch.dict de clase sobre os.environ rompe
        # Odoo 18: revisa .target de cada parche activo al terminar cada prueba.
        cls.entorno = {}
        cls.startClassPatcher(patch.object(crypto, 'os', SimpleNamespace(environ=cls.entorno)))
        cls.env['ir.config_parameter'].sudo().search([('key', '=', crypto.MASTER_KEY_PARAM)]).unlink()

        pesos = cls.env.ref('base.MXN')
        pesos.active = True
        cls.empresa = cls.env['res.company'].create({
            'name': "Empresa SAT del asistente",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': pesos.id,
            'chart_template': 'mx',
            'vat': RFC,
        })
        cls.env.user.company_ids |= cls.empresa
        licencia = cls.env['dsi.license.client.state']._get_or_create(cls.empresa)
        licencia.token = token_de_prueba(cls.env)

    def asistente(self):
        return self.env['dsi.cfdi.vault.setup.wizard'].create({'company_id': self.empresa.id})

    def efirma(self, **valores):
        return self.env['sat.credential'].sudo().create({
            'company_id': self.empresa.id,
            'rfc': RFC,
            'date_valid_from': fields.Date.today() - timedelta(days=30),
            'date_valid_to': fields.Date.today() + timedelta(days=365),
            **valores,
        })

    # Llave ------------------------------------------------------------------

    def test_sin_llave_falta_y_ofrece_generarla(self):
        asistente = self.asistente()
        self.assertFalse(asistente.llave_origen)
        self.assertFalse(asistente.llave_ok)
        self.assertIn("no se puede guardar la e.firma", asistente.llave_texto)
        self.assertIn("--generar-llave-sat", asistente.llave_texto)

    def test_llave_en_odoo_conf(self):
        with patch.object(crypto, 'config', {crypto.MASTER_KEY_PARAM: Fernet.generate_key().decode()}):
            asistente = self.asistente()
            self.assertEqual(asistente.llave_origen, 'conf')
            self.assertTrue(asistente.llave_ok)

    def test_llave_en_variable_de_entorno(self):
        with patch.dict(self.entorno, {crypto.MASTER_KEY_ENV: Fernet.generate_key().decode()}):
            asistente = self.asistente()
            self.assertEqual(asistente.llave_origen, 'env')
            self.assertTrue(asistente.llave_ok)

    def test_una_llave_que_no_es_de_fernet_no_sirve(self):
        with patch.dict(self.entorno, {crypto.MASTER_KEY_ENV: 'no-es-una-llave'}):
            asistente = self.asistente()
            self.assertEqual(asistente.llave_origen, 'env')
            self.assertFalse(asistente.llave_ok)
            self.assertIn("no es válida", asistente.llave_texto)

    def test_generarla_la_guarda_como_parametro_y_sirve_para_cifrar(self):
        asistente = self.asistente()
        asistente.action_generar_llave()
        self.assertEqual(asistente.llave_origen, 'param')
        self.assertTrue(asistente.llave_ok)
        self.assertIn("respaldo", asistente.llave_texto)
        cifrado = crypto.encrypt_bytes(b'e.firma', env=self.env)
        self.assertEqual(crypto.decrypt_bytes(cifrado, env=self.env), b'e.firma')

    def test_nunca_reemplaza_una_llave_que_ya_existe(self):
        asistente = self.asistente()
        asistente.action_generar_llave()
        llave = crypto.master_key_source(self.env)[1]
        with self.assertRaises(UserError):
            asistente.action_generar_llave()
        self.assertEqual(crypto.master_key_source(self.env)[1], llave)
        with patch.dict(self.entorno, {crypto.MASTER_KEY_ENV: Fernet.generate_key().decode()}):
            with self.assertRaises(UserError):
                self.asistente().action_generar_llave()

    # e.firma ----------------------------------------------------------------

    def test_sin_efirma_falta(self):
        asistente = self.asistente()
        self.assertFalse(asistente.efirma_ok)
        self.assertIn("No hay e.firma", asistente.efirma_texto)

    def test_efirma_vigente(self):
        self.efirma()
        asistente = self.asistente()
        self.assertTrue(asistente.efirma_ok)
        self.assertIn(RFC, asistente.efirma_texto)

    def test_efirma_vencida_o_revocada(self):
        efirma = self.efirma(date_valid_to=fields.Date.today() - timedelta(days=1))
        self.assertIn("venció", self.asistente().efirma_texto)
        efirma.write({'state': 'revoked', 'date_valid_to': fields.Date.today() + timedelta(days=30)})
        asistente = self.asistente()
        self.assertFalse(asistente.efirma_ok)
        self.assertIn("revocada", asistente.efirma_texto)

    def test_efirma_de_otro_rfc(self):
        self.efirma(rfc='AAA010101AAA')
        asistente = self.asistente()
        self.assertFalse(asistente.efirma_ok)
        self.assertIn("AAA010101AAA", asistente.efirma_texto)

    def test_cargar_efirma_abre_su_asistente_para_esta_empresa(self):
        accion = self.asistente().action_cargar_efirma()
        self.assertEqual(accion['res_model'], 'sat.credential.import.wizard')
        self.assertEqual(accion['target'], 'new')
        self.assertEqual(accion['context']['default_company_id'], self.empresa.id)
        self.assertTrue(accion['context']['dsi_cfdi_vault_setup'])

    # Descarga automática y conjunto -----------------------------------------

    def test_la_descarga_automatica_es_opcional(self):
        asistente = self.asistente()
        asistente.action_generar_llave()
        self.efirma()
        asistente = self.asistente()
        self.assertFalse(asistente.descarga_ok)
        self.assertIn("Sin programar", asistente.descarga_texto)
        self.assertTrue(asistente.listo)

    def test_sin_llave_ni_efirma_no_esta_listo(self):
        self.assertFalse(self.asistente().listo)

    def test_programar_la_descarga_lleva_la_empresa_y_su_efirma(self):
        efirma = self.efirma()
        contexto = self.asistente().action_programar_descarga()['context']
        self.assertEqual(contexto['default_company_id'], self.empresa.id)
        self.assertEqual(contexto['default_credential_id'], efirma.id)
        self.env['sat.sync.schedule'].sudo().create({
            'company_id': self.empresa.id,
            'credential_id': efirma.id,
            'date_start': fields.Date.today(),
        })
        asistente = self.asistente()
        self.assertTrue(asistente.descarga_ok)
