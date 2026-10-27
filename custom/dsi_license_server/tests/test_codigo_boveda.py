"""La Bóveda dada de alta con otro código, como en producción.

En el servidor de producción el producto de la Bóveda se dio de alta a mano
como 'I-BCDFI'. El servidor 19.0.0.2.0 tomaba 'cfdi_vault' como el único
código de la Bóveda: rechazaba la renovación y la activación de todas las
Bóvedas instaladas (no mandan product_code, que se entiende 'cfdi_vault') y
le quitaba al token 'vault' y 'reconcile'. Los clientes, viejos y nuevos, solo
conocen 'cfdi_vault': así deben seguir pidiéndola y recibiéndola.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.dsi_license_server.crypto import decode_token_unsafe
from odoo.addons.dsi_license_server.models.dsi_license_product import (
    CFDI_VAULT_CODE, MFG_PRODUCT_CODE, client_code)
from odoo.addons.dsi_license_server.tests.test_productos import (
    RFC, _hoy, _instancia, _licencia, _manufactura, _usar_llave_de_prueba)
from odoo.addons.dsi_license_server.tests.test_prueba_servidor import (
    DATOS, DESCARGA, DNS, IP_PUBLICA, _producto, _respuesta_del_cliente)

CODIGO_PRODUCCION = 'I-BCDFI'


def _como_en_produccion(env, producto):
    """La Bóveda con el código de producción. Por SQL, como quedó allá: el
    ORM ya no deja cambiar el código de un producto con licencias."""
    env.flush_all()
    env.cr.execute("UPDATE dsi_license_product SET code = %s WHERE id = %s",
                   [CODIGO_PRODUCCION, producto.id])
    env.invalidate_all()


@tagged('post_install', '-at_install')
class TestBovedaConOtroCodigo(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.boveda = _producto(cls.env)
        _como_en_produccion(cls.env, cls.boveda)
        cls.manufactura = _manufactura(cls.env)
        cls.Product = cls.env['dsi.license.product']
        cls.License = cls.env['dsi.license']

    def test_es_la_boveda(self):
        self.assertEqual(self.boveda.code, CODIGO_PRODUCCION)
        self.assertTrue(self.boveda.is_cfdi_vault)
        self.assertFalse(self.manufactura.is_cfdi_vault)
        self.assertEqual(self.boveda._client_code(), CFDI_VAULT_CODE)
        self.assertEqual(self.manufactura._client_code(), MFG_PRODUCT_CODE)
        self.assertEqual(self.Product.search([('is_cfdi_vault', '=', True)]), self.boveda)
        sin_boveda = self.Product.search([('is_cfdi_vault', '=', False)])
        self.assertIn(self.manufactura, sin_boveda)
        self.assertNotIn(self.boveda, sin_boveda)

    def test_client_code(self):
        for codigo, esperado in ((CODIGO_PRODUCCION, CFDI_VAULT_CODE), (CFDI_VAULT_CODE, CFDI_VAULT_CODE),
                                 (f' {CODIGO_PRODUCCION} ', CFDI_VAULT_CODE),
                                 (MFG_PRODUCT_CODE, MFG_PRODUCT_CODE), (None, ''), ('', '')):
            self.assertEqual(client_code(codigo), esperado, codigo)

    def test_se_encuentra_con_el_codigo_que_mandan_los_clientes(self):
        for codigo in (CFDI_VAULT_CODE, CODIGO_PRODUCCION):
            self.assertEqual(self.Product._search_client_code(codigo), self.boveda)
        self.assertEqual(self.Product._search_client_code(MFG_PRODUCT_CODE), self.manufactura)
        for codigo in (None, '', 'inexistente'):
            self.assertFalse(self.Product._search_client_code(codigo))

    def test_funciones_fijas_en_codigo(self):
        self.assertEqual(self.boveda._features_for('pro'), ['vault', 'reconcile'])
        self.assertEqual(self.boveda._features_for('basic'), ['vault'])
        with self.assertRaises(ValidationError):
            self.boveda.features_pro = 'vault,reconcile'

    def test_el_token_dice_cfdi_vault(self):
        licencia = _licencia(self.env, self.boveda, date_end=_hoy() + timedelta(days=100))
        payload = licencia._build_token_payload(_instancia(licencia))
        self.assertEqual(payload['product_code'], CFDI_VAULT_CODE)
        self.assertEqual(payload['features'], ['vault', 'reconcile'])
        self.assertEqual(licencia.token_features, 'vault, reconcile')
        basica = _licencia(self.env, self.boveda, rfc='BBB010101BBB', edition='basic')
        self.assertEqual(basica._build_token_payload()['features'], ['vault'])

    def test_check_product(self):
        boveda = _licencia(self.env, self.boveda)
        manufactura = _licencia(self.env, self.manufactura)
        for codigo in (None, '', CFDI_VAULT_CODE, CODIGO_PRODUCCION):
            boveda._check_product(codigo)
        manufactura._check_product(MFG_PRODUCT_CODE)
        for licencia, codigo in ((boveda, MFG_PRODUCT_CODE), (manufactura, None),
                                 (manufactura, CFDI_VAULT_CODE), (manufactura, CODIGO_PRODUCCION)):
            with self.assertRaisesRegex(UserError, "no sirve para"):
                licencia._check_product(codigo)

    def test_solo_un_producto_de_la_boveda(self):
        with self.assertRaisesRegex(ValidationError, "solo puede haber uno"):
            self.Product.create({'name': "Otra Bóveda", 'code': CFDI_VAULT_CODE})

    def test_un_codigo_con_espacios_no_es_la_boveda(self):
        # Por API se puede guardar un código con espacios; no es la Bóveda y
        # su token no puede decir 'cfdi_vault'.
        otro = self.Product.create({'name': "Raro", 'code': f'{CFDI_VAULT_CODE} ',
                                    'features_basic': 'otra', 'features_pro': 'otra'})
        self.assertFalse(otro.is_cfdi_vault)
        self.assertNotEqual(otro._client_code(), CFDI_VAULT_CODE)
        licencia = _licencia(self.env, otro)
        self.assertEqual(licencia._build_token_payload()['features'], ['otra'])
        with self.assertRaisesRegex(UserError, "no sirve para"):
            licencia._check_product(None)

    def test_prueba_de_la_boveda(self):
        self.boveda.write({'allow_trial': True, 'supported_versions': '19.0'})
        with patch(DNS, return_value={IP_PUBLICA}), \
                patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])):
            # Como la piden los clientes: con 'cfdi_vault', desde Odoo 20.
            licencia, instancia = self.License._start_trial(dict(DATOS))
        self.assertEqual(licencia.product_id, self.boveda)
        payload = licencia._build_token_payload(instancia)
        self.assertEqual((payload['product_code'], payload['features']),
                         (CFDI_VAULT_CODE, ['vault', 'reconcile']))


@tagged('post_install', '-at_install')
class TestEndpointsBovedaConOtroCodigo(HttpCase):

    def setUp(self):
        super().setUp()
        _usar_llave_de_prueba(self.env)
        self.boveda = _producto(self.env)
        _como_en_produccion(self.env, self.boveda)
        self.manufactura = _manufactura(self.env)
        self.lic_boveda = _licencia(self.env, self.boveda)
        self.lic_mfg = _licencia(self.env, self.manufactura)

    def _post(self, ruta, datos):
        datos = {k: v for k, v in datos.items() if v is not None}
        respuesta = self.url_open(ruta, data=json.dumps(datos), headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return respuesta

    def _activar(self, licencia, **cambios):
        return self._post('/dsi/license/v1/activate', {
            'license_key': licencia.key, 'rfc': RFC, 'db_uuid': 'uuid-base-1', 'base_url': 'https://cliente.example.com',
            'odoo_version': '19.0+e', 'module_version': '19.0.0.1.0', **cambios})

    def _heartbeat(self, licencia, **cambios):
        return self._post('/dsi/license/v1/heartbeat', {
            'license_key': licencia.key, 'db_uuid': 'uuid-base-1', **cambios})

    def _token(self, respuesta):
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        return decode_token_unsafe(respuesta.json()['token'])

    def test_bovedas_instaladas_activan_y_renuevan(self):
        """Lo que fallaba en producción: sin product_code, 400."""
        payload = self._token(self._activar(self.lic_boveda))
        self.assertEqual((payload['product_code'], payload['features']),
                         (CFDI_VAULT_CODE, ['vault', 'reconcile']))
        payload = self._token(self._heartbeat(self.lic_boveda))
        self.assertEqual((payload['product_code'], payload['features']),
                         (CFDI_VAULT_CODE, ['vault', 'reconcile']))
        self.assertTrue(self.lic_boveda.instance_ids.date_last_heartbeat)

    def test_bovedas_nuevas_activan_y_renuevan(self):
        self._token(self._activar(self.lic_boveda, product_code=CFDI_VAULT_CODE))
        payload = self._token(self._heartbeat(self.lic_boveda, product_code=CFDI_VAULT_CODE,
                                              odoo_version='20.0+e', module_version='20.0.0.2.0'))
        self.assertEqual(payload['product_code'], CFDI_VAULT_CODE)

    def test_las_claves_no_se_cruzan(self):
        for licencia, codigo in ((self.lic_mfg, None), (self.lic_mfg, CFDI_VAULT_CODE),
                                 (self.lic_mfg, CODIGO_PRODUCCION), (self.lic_boveda, MFG_PRODUCT_CODE)):
            respuesta = self._activar(licencia, product_code=codigo)
            self.assertEqual(respuesta.status_code, 400, (licencia.product_id.code, codigo))
            self.assertIn("no sirve para", respuesta.json()['error'])
            self.assertFalse(licencia.instance_ids)
        payload = self._token(self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE))
        self.assertEqual((payload['product_code'], payload['features']),
                         (MFG_PRODUCT_CODE, ['mrp_manufactura']))

    def test_prueba_por_el_endpoint(self):
        self.boveda.allow_trial = True
        with patch(DNS, return_value={IP_PUBLICA}), \
                patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])):
            payload = self._token(self._post('/dsi/license/v1/trial', dict(DATOS)))
        self.assertEqual((payload['product_code'], payload['features']),
                         (CFDI_VAULT_CODE, ['vault', 'reconcile']))

    def test_comprar(self):
        self.boveda.purchase_url = 'https://tienda.example.com/boveda'
        self.manufactura.purchase_url = 'https://tienda.example.com/manufactura'
        respuesta = self.url_open(f'/dsi/license/v1/comprar?producto={CFDI_VAULT_CODE}&clave={self.lic_boveda.key}',
                                  allow_redirects=False)
        self.assertEqual(respuesta.headers['Location'],
                         f'https://tienda.example.com/boveda?clave={self.lic_boveda.key}')
        # Sin producto ni clave: la Bóveda, como antes.
        respuesta = self.url_open('/dsi/license/v1/comprar', allow_redirects=False)
        self.assertEqual(respuesta.headers['Location'], 'https://tienda.example.com/boveda')
        # La clave de la Bóveda en el botón de Manufactura se descarta.
        respuesta = self.url_open(f'/dsi/license/v1/comprar?producto={MFG_PRODUCT_CODE}&clave={self.lic_boveda.key}',
                                  allow_redirects=False)
        self.assertEqual(respuesta.headers['Location'], 'https://tienda.example.com/manufactura')
