"""Licencias de prueba de autoservicio y vigencia de los tokens.

Existen por dos cosas:

  * El token duraba 30 días aunque la licencia terminara antes, así que una
    licencia (o una prueba) de 7 días funcionaba 30 más los de gracia.
  * La prueba la pide la base del cliente sin pasar por aquí. Lo único que
    impide que alguien la pida a nombre de otra dirección, o que la repita, es
    lo que se comprueba aquí: el reto contestado desde la URL declarada y una
    prueba por RFC, por base y por dirección.
"""
import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.dsi_license_server.crypto import decode_token_unsafe

DNS = 'odoo.addons.dsi_license_server.trial._direcciones'
DESCARGA = 'odoo.addons.dsi_license_server.trial.requests.get'
IP_PUBLICA = '93.184.216.34'
DATOS = {
    'product_code': 'cfdi_vault',
    'rfc': 'EKU9003173C9',
    'company_name': "Cliente de Prueba SA de CV",
    'db_uuid': 'uuid-cliente-1',
    'base_url': 'https://cliente.example.com',
    'odoo_version': '20.0+e',
    'module_version': '20.0.0.1.0',
    'challenge_id': 'reto-1',
    'challenge_secret': 'secreto-1',
}


def _respuesta_del_cliente(texto, status=200):
    respuesta = MagicMock(status_code=status)
    respuesta.__enter__.return_value = respuesta
    respuesta.raw.read.return_value = texto.encode()
    return respuesta


def _producto(env):
    # Como la busca el servidor: en producción la Bóveda es 'I-BCDFI' y solo
    # puede haber un producto de la Bóveda.
    producto = env['dsi.license.product'].with_context(active_test=False)._search_client_code('cfdi_vault')
    if not producto:
        producto = env['dsi.license.product'].create({'name': "Bóveda CFDI", 'code': 'cfdi_vault'})
    producto.write({'allow_trial': True, 'trial_days': 7})
    return producto


@tagged('post_install', '-at_install')
class TestPruebaServidor(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.producto = _producto(cls.env)
        cls.License = cls.env['dsi.license']
        cls.hoy = fields.Date.today()

    def _pedir(self, contesta=None, ip=IP_PUBLICA, **cambios):
        datos = {**DATOS, **cambios}
        respuesta = _respuesta_del_cliente(datos['challenge_secret'] if contesta is None else contesta)
        with patch(DNS, return_value={ip}), \
                patch(DESCARGA, return_value=respuesta) as descarga:
            licencia, instancia = self.License._start_trial(datos)
        return licencia, instancia, descarga

    def test_crea_la_prueba_amarrada_a_la_base_y_la_url(self):
        licencia, instancia, descarga = self._pedir()

        self.assertEqual(licencia.license_type, 'trial')
        self.assertEqual(licencia.state, 'active')
        self.assertEqual(licencia.date_end, self.hoy + timedelta(days=7))
        self.assertEqual(licencia.max_instances, 1)
        self.assertEqual(licencia.odoo_versions, '20.0')
        self.assertEqual(licencia.partner_id.vat, 'EKU9003173C9')
        self.assertEqual(instancia.base_url, DATOS['base_url'])
        # Se pidió el reto a la dirección declarada, sin seguir redirecciones.
        self.assertEqual(descarga.call_args.args[0], 'https://cliente.example.com/dsi/license/v1/challenge')
        self.assertEqual(descarga.call_args.kwargs['params'], {'id': 'reto-1'})
        self.assertFalse(descarga.call_args.kwargs['allow_redirects'])

        payload = licencia._build_token_payload(instancia)
        self.assertEqual(payload['license_type'], 'trial')
        self.assertEqual(payload['grace_days'], 0)
        self.assertEqual(payload['token_expires'], licencia.date_end.isoformat())
        self.assertEqual(payload['base_url'], DATOS['base_url'])
        self.assertEqual(payload['db_uuid'], DATOS['db_uuid'])

    def test_una_prueba_por_rfc(self):
        self._pedir()
        with self.assertRaises(UserError):
            self._pedir(db_uuid='otra-base', base_url='https://otro.example.com')

    def test_una_prueba_por_base(self):
        self._pedir()
        with self.assertRaises(UserError):
            self._pedir(rfc='AAA010101AAA', base_url='https://otro.example.com')

    def test_una_prueba_por_direccion(self):
        self._pedir()
        with self.assertRaises(UserError):
            self._pedir(rfc='AAA010101AAA', db_uuid='otra-base')

    def test_si_la_url_no_contesta_el_reto_no_hay_prueba(self):
        with self.assertRaises(UserError):
            self._pedir(contesta='otro secreto')
        self.assertFalse(self.License.search([('rfc_authorized', '=', DATOS['rfc'])]))

    def test_solo_direcciones_publicas_con_https(self):
        for cambios, ip in (
            ({'base_url': 'http://cliente.example.com'}, IP_PUBLICA),
            ({'base_url': 'https://cliente.example.com:8443'}, IP_PUBLICA),
            ({}, '10.0.0.5'),
            ({}, '127.0.0.1'),
            ({}, '169.254.169.254'),
        ):
            with self.assertRaises(UserError):
                self._pedir(ip=ip, **cambios)

    def test_producto_sin_prueba(self):
        self.producto.allow_trial = False
        with self.assertRaises(UserError):
            self._pedir()

    def test_la_url_de_compra_debe_ser_web(self):
        with self.assertRaises(ValidationError):
            self.producto.purchase_url = 'javascript:alert(1)'

    def test_rfc_invalido(self):
        with self.assertRaises(UserError):
            self._pedir(rfc='NO-ES-RFC')

    def test_la_prueba_no_se_lleva_a_otra_base(self):
        licencia, instancia, _descarga = self._pedir()
        licencia._check_trial_activation(DATOS['db_uuid'], DATOS['base_url'])
        with self.assertRaises(UserError):
            licencia._check_trial_activation('otra-base', DATOS['base_url'])
        with self.assertRaises(UserError):
            licencia._check_trial_activation(DATOS['db_uuid'], 'https://otro.example.com')

    def test_convertir_en_suscripcion(self):
        licencia, instancia, _descarga = self._pedir()
        licencia.action_convert_to_subscription()
        self.assertEqual(licencia.license_type, 'subscription')
        self.assertEqual(licencia.max_instances, 2)
        self.assertGreater(licencia.date_end, self.hoy + timedelta(days=300))
        payload = licencia._build_token_payload(instancia)
        self.assertEqual(payload['grace_days'], 15)
        self.assertNotIn('base_url', payload)

    def test_el_token_no_va_mas_alla_de_la_licencia(self):
        partner = self.env['res.partner'].create({'name': "Cliente pagado"})
        licencia = self.License.create({
            'partner_id': partner.id, 'product_id': self.producto.id,
            'rfc_authorized': 'AAA010101AAA', 'date_end': self.hoy + timedelta(days=5),
        })
        payload = licencia._build_token_payload()
        self.assertEqual(payload['token_expires'], (self.hoy + timedelta(days=5)).isoformat())
        self.assertEqual(payload['grace_days'], 15)
        licencia.date_end = False
        self.assertEqual(licencia._build_token_payload()['token_expires'],
                         (self.hoy + timedelta(days=30)).isoformat())


@tagged('post_install', '-at_install')
class TestEndpointPrueba(HttpCase):

    def setUp(self):
        super().setUp()
        _producto(self.env)
        llave = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        self.env['ir.config_parameter'].sudo().set_param(
            'dsi_license_private_key', base64.b64encode(llave).decode())

    def _post(self):
        return self.url_open('/dsi/license/v1/trial', data=json.dumps(DATOS),
                             headers={'Content-Type': 'application/json'})

    def test_entrega_token_y_clave_y_no_guarda_el_secreto(self):
        with patch(DNS, return_value={IP_PUBLICA}), \
                patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])):
            respuesta = self._post()
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        datos = respuesta.json()
        self.assertTrue(datos['license_key'].startswith('DSI-'))
        payload = decode_token_unsafe(datos['token'])
        self.assertEqual(payload['license_type'], 'trial')
        self.assertEqual(payload['base_url'], DATOS['base_url'])
        bitacora = self.env['dsi.license.log'].search([('endpoint', '=', 'trial')], limit=1)
        self.assertTrue(bitacora.success)
        self.assertNotIn(DATOS['challenge_secret'], bitacora.request_data)

    def _crear_prueba(self):
        with patch(DNS, return_value={IP_PUBLICA}), \
                patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])):
            return self.env['dsi.license']._start_trial(dict(DATOS))

    def _post_json(self, ruta, datos):
        return self.url_open(ruta, data=json.dumps(datos), headers={'Content-Type': 'application/json'})

    def test_renovar_una_prueba_vencida_la_marca_expirada(self):
        # Antes esto tronaba: vencer la licencia escribe un campo con
        # seguimiento y la ruta no tenía usuario que firmara el mensaje.
        licencia, _instancia = self._crear_prueba()
        licencia.date_end = fields.Date.today() - timedelta(days=1)
        respuesta = self._post_json('/dsi/license/v1/heartbeat', {
            'license_key': licencia.key, 'db_uuid': DATOS['db_uuid']})
        self.assertEqual(respuesta.status_code, 400, respuesta.text)
        self.assertIn("expiró", respuesta.json()['error'])
        licencia.invalidate_recordset()
        self.assertEqual(licencia.state, 'expired')

    def test_la_clave_de_prueba_no_se_activa_en_otra_base(self):
        licencia, _instancia = self._crear_prueba()
        respuesta = self._post_json('/dsi/license/v1/activate', {
            'license_key': licencia.key, 'rfc': DATOS['rfc'], 'db_uuid': 'otra-base',
            'base_url': 'https://otra.example.com', 'odoo_version': '20.0'})
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("solo se puede usar en la base que la pidió", respuesta.json()['error'])

    def test_comprar_redirige_a_la_tienda_con_la_clave(self):
        licencia, _instancia = self._crear_prueba()
        licencia.product_id.purchase_url = 'https://tienda.example.com/boveda'
        respuesta = self.url_open(f'/dsi/license/v1/comprar?clave={licencia.key}', allow_redirects=False)
        self.assertIn(respuesta.status_code, (302, 303))
        self.assertEqual(respuesta.headers['Location'], f'https://tienda.example.com/boveda?clave={licencia.key}')
        clic = self.env['dsi.license.log'].search([('endpoint', '=', 'purchase')], limit=1)
        self.assertEqual(clic.license_id, licencia)

    def test_comprar_sin_tienda_definida(self):
        _producto(self.env).purchase_url = False
        respuesta = self.url_open('/dsi/license/v1/comprar?producto=cfdi_vault')
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("todavía no está disponible", respuesta.text)

    def test_limite_de_solicitudes_por_ip(self):
        self.env['dsi.license.log'].create([
            {'endpoint': 'trial', 'ip_address': '127.0.0.1', 'success': False} for _i in range(10)
        ])
        respuesta = self._post()
        self.assertEqual(respuesta.status_code, 429)
