"""Solicitud de la prueba desde el Odoo del cliente.

El botón solo deja la solicitud lista con un reto; la tarea programada la manda
y el servidor de licencias, antes de dar la prueba, le pide el reto a la URL
declarada. Estas pruebas cuidan que el reto solo lo conteste una solicitud en
curso y vigente, y que la base solo pida prueba si es accesible desde internet.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from .common import CLAVE_DE_PRUEBA, token_de_prueba, usar_llave_de_prueba

ENVIO = 'odoo.addons.dsi_license_client.models.dsi_license_client_state.requests.post'
URL_PUBLICA = 'https://cliente.example.com'


def _poner_parametro(env, clave, valor):
    cp = env['ir.config_parameter'].sudo()
    if hasattr(cp, 'set_str'):
        cp.set_str(clave, valor)
    else:
        cp.set_param(clave, valor)


@tagged('post_install', '-at_install')
class TestPruebaCliente(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        cls.estado = cls.env['dsi.license.client.state']._get_or_create()
        cls.estado.token = False
        cls.estado.company_id.partner_id.vat = 'EKU9003173C9'
        _poner_parametro(cls.env, 'web.base.url', URL_PUBLICA)

    def _respuesta(self, status=200, **datos):
        return MagicMock(status_code=status, content=b'x', json=lambda: datos)

    def test_el_boton_deja_la_solicitud_lista(self):
        self.estado.action_request_trial()
        self.assertEqual(self.estado.trial_state, 'requested')
        self.assertTrue(self.estado.trial_challenge_id)
        secreto = self.env['dsi.license.client.state']._trial_challenge_secret(
            self.estado.trial_challenge_id)
        self.assertEqual(secreto, self.estado.trial_challenge_secret)
        self.assertIsNone(self.env['dsi.license.client.state']._trial_challenge_secret('otro'))

    def test_solo_desde_bases_accesibles_por_internet(self):
        for url in ('http://cliente.example.com', 'https://localhost:8069', 'https://192.168.1.20',
                    'https://odoo.local'):
            _poner_parametro(self.env, 'web.base.url', url)
            with self.assertRaises(UserError):
                self.estado.action_request_trial()
        self.assertEqual(self.estado.trial_state, 'none')

    def test_sin_rfc_no_se_pide(self):
        self.estado.company_id.partner_id.vat = False
        with self.assertRaises(UserError):
            self.estado.action_request_trial()

    def test_con_licencia_no_se_pide(self):
        self.estado.token = token_de_prueba(self.env)
        with self.assertRaises(UserError):
            self.estado.action_request_trial()

    def test_la_solicitud_completa_deja_la_prueba_activa(self):
        self.estado.action_request_trial()
        reto = (self.estado.trial_challenge_id, self.estado.trial_challenge_secret)
        token = token_de_prueba(self.env, dias=7, license_type='trial', grace_days=0,
                                base_url=URL_PUBLICA)
        with patch(ENVIO, return_value=self._respuesta(token=token, license_key=CLAVE_DE_PRUEBA)) as envio:
            self.estado._do_trial_request()

        enviado = envio.call_args.kwargs['json']
        self.assertTrue(envio.call_args.args[0].endswith('/dsi/license/v1/trial'))
        self.assertEqual((enviado['challenge_id'], enviado['challenge_secret']), reto)
        self.assertEqual(enviado['base_url'], URL_PUBLICA)
        self.assertEqual(enviado['rfc'], 'EKU9003173C9')
        self.assertEqual(self.estado.state, 'trial')
        self.assertEqual(self.estado.license_key, CLAVE_DE_PRUEBA)
        self.assertEqual(self.estado.trial_state, 'none')
        # El reto es de un solo uso.
        self.assertFalse(self.estado.trial_challenge_secret)

    def test_rechazo_del_servidor(self):
        self.estado.action_request_trial()
        with patch(ENVIO, return_value=self._respuesta(status=400, error="Ya se usó la prueba")):
            self.estado._do_trial_request()
        self.assertEqual(self.estado.trial_state, 'failed')
        self.assertEqual(self.estado.trial_error, "Ya se usó la prueba")
        self.assertEqual(self.estado.state, 'not_activated')

    def test_reto_vencido(self):
        self.estado.action_request_trial()
        self.estado.trial_requested_at = fields.Datetime.now() - timedelta(minutes=20)
        self.assertIsNone(self.env['dsi.license.client.state']._trial_challenge_secret(
            self.estado.trial_challenge_id))
        with patch(ENVIO) as envio:
            self.estado._do_trial_request()
        envio.assert_not_called()
        self.assertEqual(self.estado.trial_state, 'failed')


@tagged('post_install', '-at_install')
class TestRetoPorHttp(HttpCase):

    def test_la_url_contesta_el_reto_solo_de_una_solicitud_en_curso(self):
        estado = self.env['dsi.license.client.state']._get_or_create()
        estado.write({
            'token': False,
            'trial_state': 'requested',
            'trial_requested_at': fields.Datetime.now(),
            'trial_challenge_id': 'reto-de-prueba',
            'trial_challenge_secret': 'secreto-de-prueba',
        })
        respuesta = self.url_open('/dsi/license/v1/challenge?id=reto-de-prueba')
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.text, 'secreto-de-prueba')
        self.assertEqual(self.url_open('/dsi/license/v1/challenge?id=otro').status_code, 404)
