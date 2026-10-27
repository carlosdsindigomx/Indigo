"""Licencias por producto: la Bóveda CFDI y Manufactura en el mismo servidor.

Existen por tres cosas:

  * Antes el servidor emitía siempre 'vault' y el token no decía de qué
    producto era: una clave de cualquier otro producto habría abierto la
    Bóveda. Ahora las funciones salen del producto, la Bóveda las tiene fijas
    en código y ningún otro producto puede emitir 'vault' ni 'reconcile'.
  * Las Bóvedas ya instaladas (18, 19 y 20) no mandan product_code y solo
    buscan 'vault' en el token: su payload debe seguir idéntico salvo por
    product_code, y sin product_code se entiende que piden la Bóveda.
  * Ajustes a la Bóveda decididos con Anthony: el heartbeat no renueva
    instancias liberadas, reactivar una instancia respeta el cupo y extender
    date_end reactiva sola una licencia ya marcada como vencida. Con eso,
    revocar ya no libera las bases (si no, «Activar» no las devolvería), el
    operador puede reactivar una base liberada y una liberada por la propia
    base con «Desactivar» sigue renovando para quien la comparte si cabe.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.dsi_license_server.controllers.main import GENERIC_ERROR
from odoo.addons.dsi_license_server.crypto import _b64u_decode, decode_token_unsafe, sign_token
from odoo.addons.dsi_license_server.models.dsi_license_product import (
    CFDI_VAULT_CODE, MFG_PRODUCT_CODE, normalize_version)
from odoo.addons.dsi_license_server.tests.test_prueba_servidor import (
    DATOS, DESCARGA, DNS, IP_PUBLICA, _producto, _respuesta_del_cliente)

RFC = 'AAA010101AAA'
URL = 'https://cliente.example.com'


def _hoy():
    # El mismo «hoy» que usan el payload y _check_usable: UTC.
    return datetime.now(timezone.utc).date()


def _usar_llave_de_prueba(env):
    llave = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    env['ir.config_parameter'].sudo().set_param('dsi_license_private_key', base64.b64encode(llave).decode())


def _manufactura(env, **vals):
    """El producto que dio de alta el -u (o el -i); si alguien lo borró de
    la base de desarrollo, la misma función lo vuelve a crear."""
    Product = env['dsi.license.product'].with_context(active_test=False)
    producto = Product.search([('code', '=', MFG_PRODUCT_CODE)], limit=1)
    if not producto:
        Product._ensure_default_products()
        producto = Product.search([('code', '=', MFG_PRODUCT_CODE)], limit=1)
    producto.write({'active': True, **vals})
    return producto


def _licencia(env, producto, rfc=RFC, **vals):
    partner = env['res.partner'].create({'name': f"Cliente {producto.code}"})
    return env['dsi.license'].create({
        'partner_id': partner.id,
        'product_id': producto.id,
        'rfc_authorized': rfc,
        'odoo_versions': '18.0,19.0,20.0',
        'date_end': _hoy() + timedelta(days=365),
        'max_instances': 2,
        'state': 'active',
        **vals,
    })


def _instancia(licencia, db_uuid='uuid-x', base_url=URL, odoo_version='19.0+e'):
    return licencia._find_or_create_instance(
        db_uuid=db_uuid, base_url=base_url, odoo_version=odoo_version,
        module_version='19.0.0.1.0', is_production=True)


@tagged('post_install', '-at_install')
class TestFuncionesPorProducto(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.boveda = _producto(cls.env)
        cls.manufactura = _manufactura(cls.env)
        cls.License = cls.env['dsi.license']
        cls.hoy = _hoy()

    # --- Funciones del producto -------------------------------------------

    def test_funciones_de_la_boveda_fijas_en_codigo(self):
        self.assertEqual(self.boveda._features_for('pro'), ['vault', 'reconcile'])
        self.assertEqual(self.boveda._features_for('basic'), ['vault'])
        with self.assertRaises(ValidationError):
            self.boveda.features_pro = 'vault,reconcile,otra'
        with self.assertRaises(ValidationError):
            self.boveda.features_basic = 'vault'

    def test_ningun_otro_producto_puede_usar_vault_ni_reconcile(self):
        with self.assertRaises(ValidationError):
            self.manufactura.features_pro = 'mrp_manufactura,vault'
        with self.assertRaises(ValidationError):
            self.manufactura.features_basic = 'reconcile'
        with self.assertRaises(ValidationError):
            self.env['dsi.license.product'].create({
                'name': "Otro", 'code': 'otro_producto',
                'features_basic': 'vault', 'features_pro': 'vault'})

    def test_aunque_se_cuelen_por_sql_no_se_emiten(self):
        # Defensa adicional: _features_for nunca devuelve las de la Bóveda
        # para otro producto, aunque alguien las meta sin pasar por el ORM.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE dsi_license_product SET features_pro = %s, features_basic = %s WHERE id = %s",
            ['mrp_manufactura,vault,reconcile', 'vault', self.manufactura.id])
        self.env.invalidate_all()
        self.assertEqual(self.manufactura._features_for('pro'), ['mrp_manufactura'])
        self.assertEqual(self.manufactura._features_for('basic'), [])
        licencia = _licencia(self.env, self.manufactura)
        self.assertNotIn('vault', licencia._build_token_payload()['features'])

    def test_funciones_vacias_o_mal_formadas(self):
        Product = self.env['dsi.license.product']
        with self.assertRaises(ValidationError):
            Product.create({'name': "Sin funciones", 'code': 'sin_funciones', 'features_pro': 'x'})
        with self.assertRaises(ValidationError):
            Product.create({'name': "Mal", 'code': 'mal', 'features_basic': 'Mrp Manufactura',
                            'features_pro': 'mrp_manufactura'})
        producto = Product.create({'name': "Bien", 'code': 'bien',
                                   'features_basic': 'uno', 'features_pro': 'uno, dos'})
        self.assertEqual(producto._features_for('pro'), ['uno', 'dos'])
        self.assertEqual(producto.features_summary, "Basic: uno · Pro: uno, dos")

    def test_el_codigo_no_cambia_si_hay_licencias(self):
        producto = self.env['dsi.license.product'].create({
            'name': "Producto X", 'code': 'producto_x', 'features_basic': 'x', 'features_pro': 'x'})
        producto.code = 'producto_y'
        _licencia(self.env, producto)
        self.assertEqual(producto.license_count, 1)
        with self.assertRaises(UserError):
            producto.code = 'producto_z'
        # Escribir el mismo código no es un cambio.
        producto.write({'code': 'producto_y', 'name': "Producto Y"})

    def test_normalize_version(self):
        self.assertEqual(normalize_version('18.0+e'), '18.0')
        self.assertEqual(normalize_version('20.0+e'), '20.0')
        self.assertEqual(normalize_version('19.0-20260101'), '19.0')
        self.assertEqual(normalize_version(' 19.0 '), '19.0')
        self.assertEqual(normalize_version(None), '')
        self.assertTrue(self.manufactura._supports_version('20.0+e'))
        self.assertFalse(self.manufactura._supports_version('18.0+e'))

    # --- Token ---------------------------------------------------------------

    def test_payload_de_la_boveda_no_cambia(self):
        """Regresión: lo que reciben las Bóvedas instaladas es lo de siempre
        más product_code, byte por byte (sign_token serializa así)."""
        licencia = _licencia(self.env, self.boveda, date_end=self.hoy + timedelta(days=100))
        instancia = _instancia(licencia)
        payload = licencia._build_token_payload(instancia)
        nonce = payload['nonce']
        self.assertRegex(nonce, r'^[0-9a-f]{32}$')
        fin = (self.hoy + timedelta(days=100)).isoformat()
        vence = (self.hoy + timedelta(days=30)).isoformat()
        anterior = (
            '{"db_uuid":"uuid-x","edition":"pro","features":["vault","reconcile"],"grace_days":15,'
            f'"license_end":"{fin}","license_key":"{licencia.key}","license_type":"subscription",'
            f'"nonce":"{nonce}","rfc":"AAA010101AAA","token_expires":"{vence}"}}'
        )
        actual = (
            '{"db_uuid":"uuid-x","edition":"pro","features":["vault","reconcile"],"grace_days":15,'
            f'"license_end":"{fin}","license_key":"{licencia.key}","license_type":"subscription",'
            f'"nonce":"{nonce}","product_code":"cfdi_vault","rfc":"AAA010101AAA","token_expires":"{vence}"}}'
        )
        serializar = lambda p: json.dumps(p, separators=(',', ':'), sort_keys=True)  # noqa: E731
        self.assertEqual(serializar({k: v for k, v in payload.items() if k != 'product_code'}), anterior)
        self.assertEqual(serializar(payload), actual)
        # Y lo que va firmado en el token es exactamente eso.
        _usar_llave_de_prueba(self.env)
        token = sign_token(payload, env=self.env)
        self.assertEqual(_b64u_decode(token.partition('.')[0]), actual.encode())

    def test_payload_de_la_boveda_variantes(self):
        basica = _licencia(self.env, self.boveda, edition='basic')
        self.assertEqual(basica._build_token_payload()['features'], ['vault'])
        self.assertEqual(basica._build_token_payload()['product_code'], CFDI_VAULT_CODE)

        basica.date_end = False
        perpetua = basica._build_token_payload()
        self.assertIsNone(perpetua['license_end'])
        self.assertEqual(perpetua['token_expires'], (self.hoy + timedelta(days=30)).isoformat())
        self.assertEqual(perpetua['grace_days'], 15)
        self.assertNotIn('db_uuid', perpetua)

        prueba = _licencia(self.env, self.boveda, rfc='BBB010101BBB', license_type='trial',
                           max_instances=1, date_end=self.hoy + timedelta(days=7))
        payload = prueba._build_token_payload(_instancia(prueba))
        self.assertEqual(payload['features'], ['vault', 'reconcile'])
        self.assertEqual(payload['grace_days'], 0)
        self.assertEqual(payload['base_url'], URL)
        self.assertEqual(payload['token_expires'], (self.hoy + timedelta(days=7)).isoformat())

    def test_payload_de_manufactura(self):
        self.manufactura.write({'features_basic': 'mrp_manufactura',
                                'features_pro': 'mrp_manufactura,mrp_extra'})
        for edicion, funciones in (('basic', ['mrp_manufactura']),
                                   ('pro', ['mrp_manufactura', 'mrp_extra'])):
            licencia = _licencia(self.env, self.manufactura, rfc=f'{edicion[:3].upper()}010101AAA',
                                 edition=edicion)
            payload = licencia._build_token_payload(_instancia(licencia))
            self.assertEqual(payload['product_code'], MFG_PRODUCT_CODE)
            self.assertEqual(payload['features'], funciones)
            self.assertNotIn('vault', payload['features'])
            self.assertNotIn('reconcile', payload['features'])
            self.assertEqual(licencia.token_features, ', '.join(funciones))

    # --- Producto y versión que pide el cliente ------------------------------

    def test_check_product_vacio_es_la_boveda(self):
        boveda = _licencia(self.env, self.boveda)
        manufactura = _licencia(self.env, self.manufactura)
        for codigo in (None, '', '  ', 'cfdi_vault', ' cfdi_vault '):
            boveda._check_product(codigo)
        manufactura._check_product('mrp_manufactura')
        for licencia, codigo in ((boveda, 'mrp_manufactura'), (boveda, 'inexistente'),
                                 (manufactura, None), (manufactura, ''),
                                 (manufactura, 'cfdi_vault'), (manufactura, 'inexistente')):
            with self.assertRaisesRegex(UserError, "no sirve para"):
                licencia._check_product(codigo)

    def test_versiones_18_y_20_enterprise(self):
        licencia = _licencia(self.env, self.boveda)
        for version in ('18.0+e', '19.0+e', '20.0+e', '19.0-20260101', '20.0'):
            licencia._check_usable(version)
        licencia.odoo_versions = '19.0'
        with self.assertRaisesRegex(UserError, "no cubre"):
            licencia._check_usable('20.0+e')
        # La lista configurada también se normaliza.
        licencia.odoo_versions = '19.0+e, 20.0'
        licencia._check_usable('19.0')

    def test_licencia_vendida_a_mano_toma_las_versiones_del_producto(self):
        partner = self.env['res.partner'].create({'name': "Cliente"})
        licencia = self.License.new({'partner_id': partner.id, 'rfc_authorized': RFC})
        licencia.product_id = self.manufactura
        licencia._onchange_product_id_versions()
        self.assertEqual(licencia.odoo_versions, '19.0,20.0')

    def test_versiones_del_producto_en_el_formulario(self):
        """Al crearla se proponen las del producto; lo que escribe el operador
        no se pisa, y en una licencia que ya existe nunca se tocan: son las
        que deciden si sus bases renuevan."""
        partner = self.env['res.partner'].create({'name': "Cliente"})
        with Form(self.License) as nueva:
            nueva.partner_id = partner
            nueva.rfc_authorized = 'CCC010101CCC'
            self.assertEqual(nueva.odoo_versions, '19.0')
            nueva.product_id = self.manufactura
            self.assertEqual(nueva.odoo_versions, '19.0,20.0')
            nueva.product_id = self.boveda
            self.assertEqual(nueva.odoo_versions, self.boveda.supported_versions)
            nueva.odoo_versions = '18.0'
            nueva.product_id = self.manufactura
            self.assertEqual(nueva.odoo_versions, '18.0', "Lo que escribió el operador se queda")

        existente = _licencia(self.env, self.boveda, rfc='DDD010101DDD')
        with Form(existente) as formulario:
            formulario.product_id = self.manufactura
            self.assertEqual(formulario.odoo_versions, '18.0,19.0,20.0')
            formulario.product_id = self.boveda
        self.assertEqual(existente.odoo_versions, '18.0,19.0,20.0')

    # --- Cupo y vigencia -------------------------------------------------------

    def test_reactivar_respeta_el_cupo(self):
        licencia = _licencia(self.env, self.boveda, max_instances=1)
        a = _instancia(licencia, 'uuid-a')
        # Volver a activar la que ya está activa no ocupa otro lugar.
        self.assertEqual(_instancia(licencia, 'uuid-a'), a)
        a.action_release()
        b = _instancia(licencia, 'uuid-b')
        with self.assertRaisesRegex(UserError, "cupo"):
            _instancia(licencia, 'uuid-a')
        self.assertEqual(a.state, 'inactive')
        b.action_release()
        self.assertEqual(_instancia(licencia, 'uuid-a'), a)
        self.assertEqual(a.state, 'active')

    def test_reactivar_una_instancia_desde_el_servidor(self):
        licencia = _licencia(self.env, self.boveda, max_instances=1)
        a = _instancia(licencia, 'uuid-a')
        a.action_release()
        self.assertEqual((a.state, a.released_by), ('inactive', 'operator'))
        b = _instancia(licencia, 'uuid-b')
        # Respeta el cupo, como reactivarla desde la base.
        with self.assertRaisesRegex(UserError, "cupo"):
            a.action_reactivate()
        self.assertEqual(a.state, 'inactive')
        b.action_release()
        a.action_reactivate()
        self.assertEqual((a.state, a.released_by), ('active', False))
        # Reactivar una que ya está activa no hace nada.
        a.action_reactivate()
        self.assertEqual(licencia.active_instance_count, 1)

    def test_revocar_no_libera_las_instancias(self):
        """Revocar ya corta la renovación (_check_usable); si además liberara
        las bases, «Activar» no las devolvería."""
        licencia = _licencia(self.env, self.boveda)
        a = _instancia(licencia, 'uuid-a')
        licencia.action_revoke()
        self.assertEqual(licencia.state, 'revoked')
        self.assertEqual(a.state, 'active')
        with self.assertRaisesRegex(UserError, "no está activa"):
            licencia._check_usable('19.0')
        licencia.action_activate()
        licencia._check_usable('19.0')
        self.assertEqual(a.state, 'active')

    def test_extender_la_fecha_reactiva_una_licencia_vencida(self):
        licencia = _licencia(self.env, self.manufactura, date_end=self.hoy - timedelta(days=10))
        try:
            licencia._check_usable('19.0')
        except UserError:
            pass
        else:
            self.fail("Una licencia vencida no debe pasar _check_usable")
        self.assertEqual(licencia.state, 'expired')

        licencia.date_end = self.hoy - timedelta(days=1)
        self.assertEqual(licencia.state, 'expired', "Extenderla a una fecha pasada no la reactiva")

        licencia.date_end = self.hoy
        self.assertEqual(licencia.state, 'active', "Vigente hasta hoy: se reactiva")
        licencia._check_usable('19.0')
        self.assertTrue(licencia.message_ids.filtered(lambda m: "reactivada" in str(m.body or '')))

        # Sin fecha (perpetua) también es vigente.
        licencia.write({'state': 'expired'})
        licencia.date_end = False
        self.assertEqual(licencia.state, 'active')

    def test_extender_la_fecha_no_toca_otros_estados(self):
        for estado in ('suspended', 'revoked', 'draft'):
            licencia = _licencia(self.env, self.boveda, rfc=f'{estado[:3].upper()}010101AAA',
                                 state=estado, date_end=self.hoy - timedelta(days=5))
            licencia.date_end = self.hoy + timedelta(days=365)
            self.assertEqual(licencia.state, estado)
        # Si se escribe el estado a la vez, manda el que se escribió.
        licencia = _licencia(self.env, self.boveda, state='expired', date_end=self.hoy - timedelta(days=5))
        licencia.write({'date_end': self.hoy + timedelta(days=365), 'state': 'expired'})
        self.assertEqual(licencia.state, 'expired')

    # --- Prueba de autoservicio por producto -----------------------------------

    def _pedir(self, **cambios):
        datos = {**DATOS, **cambios}
        with patch(DNS, return_value={IP_PUBLICA}), \
                patch(DESCARGA, return_value=_respuesta_del_cliente(datos['challenge_secret'])):
            return self.License._start_trial(datos)

    def test_prueba_de_manufactura_valida_versiones(self):
        self.manufactura.allow_trial = True
        with patch(DNS, return_value={IP_PUBLICA}) as dns, \
                patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])) as descarga:
            with self.assertRaisesRegex(UserError, "no está disponible para Odoo 18.0"):
                self.License._start_trial({**DATOS, 'product_code': MFG_PRODUCT_CODE, 'odoo_version': '18.0+e'})
        # Se rechaza antes de salir a la red.
        dns.assert_not_called()
        descarga.assert_not_called()
        self.assertFalse(self.License.search([('product_id', '=', self.manufactura.id),
                                              ('rfc_authorized', '=', DATOS['rfc'])]))

    def test_prueba_de_la_boveda_no_valida_versiones(self):
        # En producción cfdi_vault tiene '19.0' y la piden bases 18 y 20.
        self.boveda.supported_versions = '19.0'
        licencia, _instancia_ = self._pedir(odoo_version='18.0+e')
        self.assertEqual(licencia.odoo_versions, '18.0')
        self.assertEqual(licencia.edition, 'pro')

    def test_prueba_usa_la_edicion_del_producto(self):
        self.manufactura.write({'allow_trial': True, 'trial_edition': 'basic',
                                'features_basic': 'mrp_manufactura',
                                'features_pro': 'mrp_manufactura,mrp_extra'})
        licencia, instancia = self._pedir(product_code=MFG_PRODUCT_CODE, odoo_version='19.0+e')
        self.assertEqual(licencia.edition, 'basic')
        self.assertEqual(licencia.odoo_versions, '19.0')
        self.assertEqual(licencia._build_token_payload(instancia)['features'], ['mrp_manufactura'])

    def test_prueba_de_boveda_y_de_manufactura_conviven(self):
        """Mismo RFC, misma base y misma dirección: cada producto da su prueba."""
        self.manufactura.allow_trial = True
        boveda, inst_boveda = self._pedir()
        manufactura, inst_mfg = self._pedir(product_code=MFG_PRODUCT_CODE)
        self.assertNotEqual(boveda, manufactura)
        self.assertEqual((boveda.product_id, manufactura.product_id), (self.boveda, self.manufactura))
        self.assertEqual({boveda.license_type, manufactura.license_type}, {'trial'})
        self.assertEqual(boveda.partner_id, manufactura.partner_id)
        self.assertEqual(inst_boveda.database_uuid, inst_mfg.database_uuid)

        p_boveda = boveda._build_token_payload(inst_boveda)
        p_mfg = manufactura._build_token_payload(inst_mfg)
        self.assertEqual((p_boveda['product_code'], p_boveda['features']), (CFDI_VAULT_CODE, ['vault', 'reconcile']))
        self.assertEqual((p_mfg['product_code'], p_mfg['features']), (MFG_PRODUCT_CODE, ['mrp_manufactura']))
        self.assertEqual(p_mfg['grace_days'], 0)
        self.assertEqual(p_mfg['base_url'], DATOS['base_url'])

        # Sigue habiendo una sola prueba por producto.
        with self.assertRaises(UserError):
            self._pedir(product_code=MFG_PRODUCT_CODE)

    # --- Alta del producto ------------------------------------------------------

    def test_alta_idempotente_de_manufactura(self):
        Product = self.env['dsi.license.product'].with_context(active_test=False)
        # La creó el <function> del -u (o del -i).
        self.assertEqual(Product.search_count([('code', '=', MFG_PRODUCT_CODE)]), 1)

        # Como si no existiera: se le cambia el código por SQL, sin el ORM.
        self.env.flush_all()
        self.env.cr.execute("UPDATE dsi_license_product SET code = code || '_previo' WHERE id = %s",
                            [self.manufactura.id])
        self.env.invalidate_all()
        Product._ensure_default_products()
        nuevo = Product.search([('code', '=', MFG_PRODUCT_CODE)])
        self.assertEqual(len(nuevo), 1)
        self.assertNotEqual(nuevo, self.manufactura)
        self.assertRecordValues(nuevo, [{
            'name': "Manufactura · Kiosco y Control de Producción",
            'supported_versions': '19.0,20.0',
            'allow_trial': False,
            'trial_days': 7,
            'trial_edition': 'pro',
            'features_basic': 'mrp_manufactura',
            'features_pro': 'mrp_manufactura',
            'purchase_url': False,
            'active': True,
        }])

        Product._ensure_default_products()
        self.assertEqual(Product.search_count([('code', '=', MFG_PRODUCT_CODE)]), 1)

        # No pisa lo que se edita aquí.
        nuevo.write({'purchase_url': 'https://tienda.example.com/manufactura', 'allow_trial': True,
                     'supported_versions': '19.0', 'trial_days': 14})
        Product._ensure_default_products()
        self.assertRecordValues(nuevo, [{
            'purchase_url': 'https://tienda.example.com/manufactura', 'allow_trial': True,
            'supported_versions': '19.0', 'trial_days': 14}])

        # Ni revive uno archivado ni crea otro.
        nuevo.active = False
        Product._ensure_default_products()
        self.assertEqual(Product.search_count([('code', '=', MFG_PRODUCT_CODE)]), 1)
        self.assertFalse(nuevo.active)


@tagged('post_install', '-at_install')
class TestEndpointsPorProducto(HttpCase):

    def setUp(self):
        super().setUp()
        _usar_llave_de_prueba(self.env)
        self.boveda = _producto(self.env)
        self.manufactura = _manufactura(self.env)
        # Mismo RFC en los dos productos: unique(product_id, rfc) lo permite.
        self.lic_boveda = _licencia(self.env, self.boveda)
        self.lic_mfg = _licencia(self.env, self.manufactura)

    def _post(self, ruta, datos):
        datos = {k: v for k, v in datos.items() if v is not None}
        respuesta = self.url_open(ruta, data=json.dumps(datos), headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return respuesta

    def _activar(self, licencia, **cambios):
        return self._post('/dsi/license/v1/activate', {
            'license_key': licencia.key, 'rfc': RFC, 'db_uuid': 'uuid-base-1', 'base_url': URL,
            'odoo_version': '19.0+e', 'module_version': '19.0.0.1.0', **cambios})

    def _heartbeat(self, licencia, **cambios):
        return self._post('/dsi/license/v1/heartbeat', {
            'license_key': licencia.key, 'db_uuid': 'uuid-base-1', **cambios})

    def _ultimo_log(self, endpoint):
        return self.env['dsi.license.log'].search([('endpoint', '=', endpoint)], order='id desc', limit=1)

    # --- /activate --------------------------------------------------------------

    def test_activar_la_boveda_sin_product_code(self):
        """Así activan las Bóvedas ya instaladas."""
        respuesta = self._activar(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        payload = decode_token_unsafe(respuesta.json()['token'])
        self.assertEqual(payload['product_code'], CFDI_VAULT_CODE)
        self.assertEqual(payload['features'], ['vault', 'reconcile'])
        self.assertEqual(payload['db_uuid'], 'uuid-base-1')
        self.assertEqual(len(self.lic_boveda.instance_ids), 1)

        respuesta = self._activar(self.lic_boveda, product_code=CFDI_VAULT_CODE)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)

    def test_activar_manufactura_con_su_producto(self):
        respuesta = self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        payload = decode_token_unsafe(respuesta.json()['token'])
        self.assertEqual(payload['product_code'], MFG_PRODUCT_CODE)
        self.assertEqual(payload['features'], ['mrp_manufactura'])
        self.assertEqual(self.lic_mfg.instance_ids.database_uuid, 'uuid-base-1')

    def test_producto_equivocado_no_crea_instancia(self):
        for licencia, codigo in ((self.lic_mfg, None), (self.lic_mfg, CFDI_VAULT_CODE),
                                 (self.lic_mfg, 'inexistente'), (self.lic_boveda, MFG_PRODUCT_CODE),
                                 (self.lic_boveda, 'inexistente')):
            respuesta = self._activar(licencia, product_code=codigo)
            self.assertEqual(respuesta.status_code, 400, (licencia.product_id.code, codigo))
            self.assertIn("no sirve para", respuesta.json()['error'])
            self.assertNotIn('token', respuesta.json())
            self.assertFalse(licencia.instance_ids, (licencia.product_id.code, codigo))
            log = self._ultimo_log('activate')
            self.assertEqual(log.license_id, licencia)
            self.assertFalse(log.success)

    def test_el_producto_se_revisa_antes_que_todo_lo_demas(self):
        # Vencida y sin db_uuid: el error es el del producto y la licencia no
        # se marca 'expired' (_check_usable escribe).
        self.lic_mfg.date_end = _hoy() - timedelta(days=1)
        respuesta = self._activar(self.lic_mfg, db_uuid=None)
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("no sirve para", respuesta.json()['error'])
        self.assertEqual(self.lic_mfg.state, 'active')
        # Con su producto sí llega a _check_usable.
        respuesta = self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        self.assertIn("expiró", respuesta.json()['error'])
        self.assertEqual(self.lic_mfg.state, 'expired')
        self.assertFalse(self.lic_mfg.instance_ids)

    def test_activar_desde_18_y_20_enterprise(self):
        for db_uuid, version in (('uuid-18', '18.0+e'), ('uuid-20', '20.0+e')):
            respuesta = self._activar(self.lic_boveda, db_uuid=db_uuid, odoo_version=version)
            self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual(sorted(self.lic_boveda.instance_ids.mapped('odoo_version')), ['18.0+e', '20.0+e'])

        self.lic_mfg.odoo_versions = '19.0'
        respuesta = self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE, odoo_version='20.0+e')
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("no cubre", respuesta.json()['error'])

    def test_reactivar_respeta_el_cupo(self):
        self.lic_boveda.max_instances = 1
        self.assertEqual(self._activar(self.lic_boveda, db_uuid='uuid-a').status_code, 200)
        self.lic_boveda.instance_ids.action_release()
        self.assertEqual(self._activar(self.lic_boveda, db_uuid='uuid-b').status_code, 200)
        respuesta = self._activar(self.lic_boveda, db_uuid='uuid-a')
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("cupo", respuesta.json()['error'])
        self.assertEqual(self.lic_boveda.active_instance_count, 1)

    # --- /heartbeat -------------------------------------------------------------

    def test_heartbeat_de_una_boveda_ya_instalada(self):
        self._activar(self.lic_boveda)
        instancia = self.lic_boveda.instance_ids
        self.assertFalse(instancia.date_last_heartbeat)
        # Sin product_code ni versiones: usa lo guardado.
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        payload = decode_token_unsafe(respuesta.json()['token'])
        self.assertEqual((payload['product_code'], payload['features']), (CFDI_VAULT_CODE, ['vault', 'reconcile']))
        self.assertTrue(instancia.date_last_heartbeat)
        self.assertEqual((instancia.odoo_version, instancia.module_version), ('19.0+e', '19.0.0.1.0'))

    def test_heartbeat_actualiza_las_versiones(self):
        self._activar(self.lic_boveda)
        instancia = self.lic_boveda.instance_ids
        respuesta = self._heartbeat(self.lic_boveda, odoo_version='20.0+e', module_version='20.0.0.2.0')
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual((instancia.odoo_version, instancia.module_version), ('20.0+e', '20.0.0.2.0'))
        # Una versión que la licencia no cubre se rechaza y no se guarda.
        respuesta = self._heartbeat(self.lic_boveda, odoo_version='17.0', module_version='17.0.0.2.0')
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("no cubre", respuesta.json()['error'])
        self.assertEqual((instancia.odoo_version, instancia.module_version), ('20.0+e', '20.0.0.2.0'))

    def test_heartbeat_con_producto_equivocado(self):
        self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        instancia = self.lic_mfg.instance_ids
        for codigo in (None, CFDI_VAULT_CODE):
            respuesta = self._heartbeat(self.lic_mfg, product_code=codigo)
            self.assertEqual(respuesta.status_code, 400)
            self.assertIn("no sirve para", respuesta.json()['error'])
            self.assertFalse(instancia.date_last_heartbeat)
        respuesta = self._heartbeat(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual(decode_token_unsafe(respuesta.json()['token'])['product_code'], MFG_PRODUCT_CODE)
        self.assertTrue(instancia.date_last_heartbeat)

    def test_heartbeat_revisa_el_producto_antes_que_la_vigencia(self):
        # Una Bóveda vieja (sin product_code) con una clave de Manufactura
        # vencida: el rechazo es por el producto y la licencia no se marca
        # 'expired' (_check_usable escribe).
        self._activar(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        self.lic_mfg.date_end = _hoy() - timedelta(days=1)
        respuesta = self._heartbeat(self.lic_mfg)
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("no sirve para", respuesta.json()['error'])
        self.assertEqual(self.lic_mfg.state, 'active')
        respuesta = self._heartbeat(self.lic_mfg, product_code=MFG_PRODUCT_CODE)
        self.assertIn("expiró", respuesta.json()['error'])
        self.assertEqual(self.lic_mfg.state, 'expired')

    def test_activar_con_rfc_equivocado_no_revela_el_producto(self):
        # El producto se revisa después del RFC: quien solo tiene la clave no
        # sabe de qué producto es.
        respuesta = self._activar(self.lic_mfg, rfc='XXX010101XXX')
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()['error'], GENERIC_ERROR)
        self.assertFalse(self.lic_mfg.instance_ids)

    def test_heartbeat_no_renueva_una_instancia_liberada(self):
        self._activar(self.lic_boveda)
        instancia = self.lic_boveda.instance_ids
        instancia.action_release()
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("liberada", respuesta.json()['error'])
        self.assertNotIn('token', respuesta.json())
        self.assertFalse(instancia.date_last_heartbeat)
        # Volver a activarla (hay cupo) la deja renovar otra vez.
        self.assertEqual(self._activar(self.lic_boveda).status_code, 200)
        self.assertEqual(self._heartbeat(self.lic_boveda).status_code, 200)

    def test_reactivar_desde_el_servidor_devuelve_la_renovacion(self):
        self._activar(self.lic_boveda)
        instancia = self.lic_boveda.instance_ids
        instancia.action_release()
        self.assertIn("liberada", self._heartbeat(self.lic_boveda).json()['error'])
        instancia.action_reactivate()
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)

    def test_revocar_y_volver_a_activar(self):
        """El operador revoca y luego pulsa «Activar»: las bases vuelven a
        renovar sin hacer nada en ellas."""
        self._activar(self.lic_boveda)
        self._activar(self.lic_boveda, db_uuid='uuid-base-2')
        self.lic_boveda.action_revoke()
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("no está activa", respuesta.json()['error'])
        self.lic_boveda.action_activate()
        for db_uuid in ('uuid-base-1', 'uuid-base-2'):
            respuesta = self._heartbeat(self.lic_boveda, db_uuid=db_uuid)
            self.assertEqual(respuesta.status_code, 200, respuesta.text)

    def test_desactivar_desde_la_base_no_corta_a_quien_la_comparte(self):
        """Dos empresas de la misma base (mismo RFC y clave) comparten la
        instancia. La que pulsa «Desactivar» borra su token y deja de
        renovar; la otra sigue renovando mientras quepa en el cupo."""
        self._activar(self.lic_boveda)
        instancia = self.lic_boveda.instance_ids
        respuesta = self._post('/dsi/license/v1/deactivate',
                               {'license_key': self.lic_boveda.key, 'db_uuid': 'uuid-base-1'})
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual((instancia.state, instancia.released_by), ('inactive', 'client'))

        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual((instancia.state, instancia.released_by), ('active', False))

        # Si su lugar ya lo tomó otra base, no se renueva.
        self.lic_boveda.max_instances = 1
        self._post('/dsi/license/v1/deactivate',
                   {'license_key': self.lic_boveda.key, 'db_uuid': 'uuid-base-1'})
        self.assertEqual(self._activar(self.lic_boveda, db_uuid='uuid-otra').status_code, 200)
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("liberada", respuesta.json()['error'])
        self.assertEqual(instancia.state, 'inactive')
        self.assertEqual(self.lic_boveda.active_instance_count, 1)

    def test_heartbeat_despues_de_extender_la_fecha(self):
        self._activar(self.lic_boveda)
        self.lic_boveda.date_end = _hoy() - timedelta(days=1)
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self.lic_boveda.state, 'expired')
        # Se renueva extendiendo la fecha, sin pulsar «Activar».
        self.lic_boveda.date_end = _hoy() + timedelta(days=365)
        self.assertEqual(self.lic_boveda.state, 'active')
        respuesta = self._heartbeat(self.lic_boveda)
        self.assertEqual(respuesta.status_code, 200, respuesta.text)

    # --- /trial -----------------------------------------------------------------

    def test_prueba_de_boveda_y_de_manufactura_por_el_endpoint(self):
        self.manufactura.allow_trial = True
        tokens = {}
        for codigo in (CFDI_VAULT_CODE, MFG_PRODUCT_CODE):
            with patch(DNS, return_value={IP_PUBLICA}), \
                    patch(DESCARGA, return_value=_respuesta_del_cliente(DATOS['challenge_secret'])):
                respuesta = self._post('/dsi/license/v1/trial', {**DATOS, 'product_code': codigo})
            self.assertEqual(respuesta.status_code, 200, respuesta.text)
            tokens[codigo] = decode_token_unsafe(respuesta.json()['token'])
        self.assertEqual(tokens[CFDI_VAULT_CODE]['features'], ['vault', 'reconcile'])
        self.assertEqual(tokens[MFG_PRODUCT_CODE]['features'], ['mrp_manufactura'])
        self.assertEqual(tokens[MFG_PRODUCT_CODE]['product_code'], MFG_PRODUCT_CODE)
        self.assertEqual(tokens[MFG_PRODUCT_CODE]['db_uuid'], tokens[CFDI_VAULT_CODE]['db_uuid'])
        self.assertNotEqual(tokens[MFG_PRODUCT_CODE]['license_key'], tokens[CFDI_VAULT_CODE]['license_key'])

    # --- /comprar ---------------------------------------------------------------

    def _comprar(self, consulta):
        respuesta = self.url_open(f'/dsi/license/v1/comprar?{consulta}', allow_redirects=False)
        self.env.invalidate_all()
        return respuesta

    def test_comprar_manda_el_producto_pedido(self):
        self.boveda.purchase_url = 'https://tienda.example.com/boveda'
        self.manufactura.purchase_url = 'https://tienda.example.com/manufactura'

        # Clave de la Bóveda en el botón de Manufactura: se descarta.
        respuesta = self._comprar(f'producto={MFG_PRODUCT_CODE}&clave={self.lic_boveda.key}')
        self.assertIn(respuesta.status_code, (302, 303))
        self.assertEqual(respuesta.headers['Location'], 'https://tienda.example.com/manufactura')
        self.assertFalse(self._ultimo_log('purchase').license_id)

        respuesta = self._comprar(f'producto={MFG_PRODUCT_CODE}&clave={self.lic_mfg.key}')
        self.assertEqual(respuesta.headers['Location'],
                         f'https://tienda.example.com/manufactura?clave={self.lic_mfg.key}')
        self.assertEqual(self._ultimo_log('purchase').license_id, self.lic_mfg)

        # Las Bóvedas instaladas mandan producto=cfdi_vault: igual que antes.
        respuesta = self._comprar(f'producto={CFDI_VAULT_CODE}&clave={self.lic_boveda.key}')
        self.assertEqual(respuesta.headers['Location'],
                         f'https://tienda.example.com/boveda?clave={self.lic_boveda.key}')

        # Sin producto, el de la clave.
        respuesta = self._comprar(f'clave={self.lic_mfg.key}')
        self.assertEqual(respuesta.headers['Location'],
                         f'https://tienda.example.com/manufactura?clave={self.lic_mfg.key}')

    def test_comprar_producto_inexistente(self):
        self.boveda.write({'name': "Bóveda CFDI", 'purchase_url': 'https://tienda.example.com/boveda'})
        respuesta = self._comprar(f'producto=inexistente&clave={self.lic_boveda.key}')
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("todavía no está disponible", respuesta.text)
        self.assertIn("DS Indigo", respuesta.text)
        self.assertNotIn("Bóveda", respuesta.text)
        self.assertFalse(self._ultimo_log('purchase').license_id)
