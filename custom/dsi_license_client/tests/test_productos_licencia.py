"""Un registro de licencia por producto.

Antes había un solo registro por empresa, fijo para la Bóveda: una clave de
otro producto que se diera de alta en el servidor habría abierto la Bóveda.
Ahora cada registro dice de qué producto es y si vale para una empresa (la
Bóveda) o para toda la base (Manufactura). Aquí se usa un producto por base
que solo existe en las pruebas (common.catalogo_de_prueba).
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger, sql

from .common import (CLAVE_DE_PRUEBA, PRODUCTO_BASE, catalogo_de_prueba, token_de_prueba,
                     usar_llave_de_prueba)

ENVIO = 'odoo.addons.dsi_license_client.models.dsi_license_client_state.requests.post'
RFC = 'EKU9003173C9'


def _respuesta(status=200, **datos):
    return MagicMock(status_code=status, content=b'x', json=lambda: datos)


@tagged('post_install', '-at_install')
class TestProductosLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        catalogo_de_prueba(cls)
        cls.State = cls.env['dsi.license.client.state']
        cls.empresa = cls.env.company
        cls.empresa.partner_id.vat = RFC
        cls.otra = cls.env['res.company'].create({'name': "Otra empresa (licencias)"})
        cls.boveda = cls.State._get_or_create(cls.empresa)
        cls.boveda.write({'token': False, 'license_key': False})

    def _por_base(self, company=None):
        return self.State._get_product_license(PRODUCTO_BASE, company, create=True)

    # ----- Modelo -----

    def test_un_registro_sin_producto_es_de_la_boveda(self):
        registro = self.State.create({'company_id': self.otra.id})
        self.assertEqual(registro.product_code, 'cfdi_vault')
        self.assertEqual(registro.license_scope, 'company')
        self.assertEqual(registro.product_name, "Bóveda CFDI")

    def test_la_boveda_y_un_producto_por_base_conviven(self):
        por_base = self._por_base(self.empresa)
        self.assertNotEqual(por_base, self.boveda)
        self.assertEqual(por_base.license_scope, 'database')
        self.assertEqual(self.State._get_or_create(self.empresa), self.boveda)

    def test_no_hay_dos_registros_del_mismo_producto_en_una_empresa(self):
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self.State.create({'company_id': self.empresa.id})
            self.env.flush_all()

    def test_un_producto_por_base_tiene_un_solo_registro(self):
        self._por_base(self.empresa)
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self.State.create({'product_code': PRODUCTO_BASE, 'company_id': self.otra.id})
            self.env.flush_all()

    def test_el_producto_y_el_alcance_no_cambian(self):
        with self.assertRaises(UserError):
            self.boveda.write({'product_code': PRODUCTO_BASE})
        with self.assertRaises(UserError):
            self.boveda.write({'license_scope': 'database'})
        # Escribir el mismo valor no estorba (lo hace la actualización de datos).
        self.boveda.write({'product_code': 'cfdi_vault', 'license_scope': 'company'})

    def test_el_contexto_no_cambia_el_producto_ni_el_alcance(self):
        registro = self.State.with_context(
            default_product_code=PRODUCTO_BASE, default_license_scope='database',
        )._get_or_create(self.otra)
        self.assertEqual((registro.product_code, registro.license_scope), ('cfdi_vault', 'company'))
        # Y el alcance lo dicta el catálogo aunque venga otro valor.
        por_base = self.State.create({
            'product_code': PRODUCTO_BASE, 'company_id': self.otra.id, 'license_scope': 'company'})
        self.assertEqual(por_base.license_scope, 'database')

    def test_restriccion_e_indice_en_la_base(self):
        self.assertEqual(
            sql.constraint_definition(self.cr, 'dsi_license_client_state', 'dsi_license_client_state_company_uniq'),
            'unique(company_id, product_code)')
        # Con pg_indexes y no con sql.index_definition, que Odoo 18 no tiene.
        self.cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                        ['dsi_license_client_state_product_database_uniq'])
        fila = self.cr.fetchone()
        self.assertTrue(fila, "Falta el índice único parcial de los productos por base.")
        definicion = fila[0]
        self.assertIn('UNIQUE', definicion)
        self.assertIn("WHERE", definicion)
        self.assertIn("license_scope", definicion)

    # ----- Búsqueda y empresa titular -----

    def test_por_base_un_solo_registro_para_todas_las_empresas(self):
        self.assertFalse(self.State._get_product_license(PRODUCTO_BASE))
        self.assertFalse(self.State.search_count([('product_code', '=', PRODUCTO_BASE)]))
        primero = self._por_base(self.empresa)
        self.assertEqual(self._por_base(self.otra), primero)
        self.assertEqual(self.State._get_product_license(PRODUCTO_BASE, self.otra), primero)
        self.assertEqual(self.State.search_count([('product_code', '=', PRODUCTO_BASE)]), 1)

    def test_alta_de_los_productos_por_base(self):
        self.State._ensure_product_licenses()
        self.State._ensure_product_licenses()
        registros = self.State.search([('product_code', '=', PRODUCTO_BASE)])
        self.assertEqual(len(registros), 1)
        self.assertEqual(registros.license_scope, 'database')

    def test_cadena_de_rfc(self):
        principal = self.env.ref('base.main_company')
        raiz = self.env['res.company'].create({'name': "Raíz con RFC"})
        raiz.partner_id.vat = 'AAA010101AAA'
        sucursal = self.env['res.company'].create({'name': "Sucursal sin RFC", 'parent_id': raiz.id})
        sucursal.partner_id.vat = False
        # La empresa con RFC es ella misma; la sucursal sin RFC, su raíz.
        self.assertEqual(self.State._rfc_company(raiz), raiz)
        self.assertEqual(self.State._rfc_company(sucursal), raiz)
        # Una empresa suelta sin RFC: la principal.
        self.otra.partner_id.vat = False
        self.assertEqual(self.State._rfc_company(self.otra), principal)
        # Nadie con RFC: la del usuario, para que el error diga dónde capturarlo.
        principal.partner_id.vat = False
        self.assertEqual(self.State._rfc_company(self.otra), self.otra)

    def test_por_base_se_crea_con_la_empresa_titular(self):
        self.otra.partner_id.vat = False
        self.assertEqual(self._por_base(self.otra).company_id, self.env.ref('base.main_company'))

    # ----- Ajustes -----

    def test_ajustes_muestran_solo_productos_instalados(self):
        # El módulo de la Bóveda se fija aquí: el cliente no depende de
        # dsi_cfdi_vault, y en una base con solo el cliente (o con Manufactura)
        # la Bóveda sale, con razón, como no instalada.
        por_base = self._por_base()
        Clase = self.registry['dsi.license.client.state']
        original = Clase._product_catalog

        def boveda_en(modulo):
            def catalogo_con(modelo):
                catalogo = original(modelo)
                catalogo['cfdi_vault'] = dict(catalogo['cfdi_vault'], module=modulo)
                return catalogo
            return catalogo_con

        with patch.object(Clase, '_product_catalog', boveda_en('dsi_license_client')):
            instalados = self.State.search([('product_installed', '=', True)])
            self.assertIn(self.boveda, instalados)
            self.assertIn(por_base, instalados)

        with patch.object(Clase, '_product_catalog', boveda_en('modulo_que_no_existe')):
            instalados = self.State.search([('product_installed', '=', True)])
            self.assertNotIn(self.boveda, instalados)
            self.assertIn(por_base, instalados)
            self.assertIn(self.boveda, self.State.search([('product_installed', '=', False)]))

    def test_ajustes_abren_la_lista(self):
        accion = self.env['ir.actions.act_window']._for_xml_id(
            'dsi_license_client.action_dsi_license_client_state')
        self.assertFalse(accion.get('res_id'))
        self.assertTrue(accion['view_mode'].startswith('list'))

    def test_las_vistas_cargan(self):
        vistas = self.State.get_views([(False, 'list'), (False, 'form'), (False, 'search')])
        self.assertIn('product_name', vistas['views']['list']['arch'])
        self.env['dsi.license.client.notice'].get_views([(False, 'form')])

    def test_nombre_del_registro(self):
        self.assertIn("Bóveda CFDI", self.boveda.display_name)
        self.assertIn(self.empresa.name, self.boveda.display_name)
        self.assertIn("toda la base", self._por_base().display_name)

    # ----- Llamadas al servidor -----

    def test_activar_manda_el_producto(self):
        self.boveda.license_key = CLAVE_DE_PRUEBA
        with patch(ENVIO, return_value=_respuesta(token=token_de_prueba(self.env))) as envio:
            self.boveda.action_activate()
        self.assertEqual(envio.call_args.kwargs['json']['product_code'], 'cfdi_vault')
        self.assertEqual(self.boveda.state, 'active')

        por_base = self._por_base()
        por_base.license_key = CLAVE_DE_PRUEBA.lower()
        token = token_de_prueba(self.env, product_code=PRODUCTO_BASE)
        with patch(ENVIO, return_value=_respuesta(token=token)) as envio:
            por_base.action_activate()
        enviado = envio.call_args.kwargs['json']
        self.assertEqual(enviado['product_code'], PRODUCTO_BASE)
        self.assertEqual(enviado['license_key'], CLAVE_DE_PRUEBA)
        self.assertEqual(enviado['rfc'], RFC)
        self.assertEqual(por_base.state, 'active')
        self.assertEqual(por_base.license_key, CLAVE_DE_PRUEBA)

    def test_la_clave_queda_como_la_conoce_el_servidor(self):
        # El servidor busca la clave tal cual (distingue mayúsculas). Activar
        # con ' dsi-test-test-test ' funciona porque se manda normalizada; si
        # en el registro quedara como se capturó, la renovación, la
        # desactivación y la compra mandarían una clave que el servidor no
        # encuentra y la licencia se perdería días después sin aviso.
        self.boveda.license_key = f"  {CLAVE_DE_PRUEBA.lower()} "
        with patch(ENVIO, return_value=_respuesta(token=token_de_prueba(self.env))) as envio:
            self.boveda.action_activate()
        self.assertEqual(envio.call_args.kwargs['json']['license_key'], CLAVE_DE_PRUEBA)
        self.assertEqual(self.boveda.state, 'active')
        self.assertEqual(self.boveda.license_key, CLAVE_DE_PRUEBA)
        nuevo = token_de_prueba(self.env, dias=60)
        with patch(ENVIO, return_value=_respuesta(token=nuevo)) as envio:
            self.boveda._do_heartbeat()
        self.assertEqual(envio.call_args.kwargs['json']['license_key'], CLAVE_DE_PRUEBA)
        self.assertEqual(self.boveda.token, nuevo)

        # Una clave que ya estuviera guardada en minúsculas también sale bien.
        self.boveda.license_key = f" {CLAVE_DE_PRUEBA.lower()}"
        with patch(ENVIO, return_value=_respuesta(token=nuevo)) as envio:
            self.boveda._do_heartbeat()
        self.assertEqual(envio.call_args.kwargs['json']['license_key'], CLAVE_DE_PRUEBA)
        self.assertIn(f"clave={CLAVE_DE_PRUEBA}", self.boveda._purchase_url())
        with patch(ENVIO, return_value=_respuesta(ok=True)) as envio:
            self.boveda.action_deactivate()
        self.assertEqual(envio.call_args.kwargs['json']['license_key'], CLAVE_DE_PRUEBA)
        self.assertEqual(self.boveda.state, 'not_activated')

        # Solo espacios no es una clave.
        self.boveda.license_key = "   "
        with patch(ENVIO) as envio, self.assertRaises(UserError):
            self.boveda.action_activate()
        envio.assert_not_called()

    def test_desde_el_aviso_la_clave_se_guarda_normalizada(self):
        aviso = self.env['dsi.license.client.notice'].create({
            'product_code': PRODUCTO_BASE, 'license_key': f" {CLAVE_DE_PRUEBA.lower()} "})
        token = token_de_prueba(self.env, product_code=PRODUCTO_BASE)
        with patch(ENVIO, return_value=_respuesta(token=token)) as envio:
            aviso.action_activate()
        self.assertEqual(envio.call_args.kwargs['json']['license_key'], CLAVE_DE_PRUEBA)
        self.assertEqual(self._por_base().license_key, CLAVE_DE_PRUEBA)
        # «Comprar» con una clave capturada y sin clave en el registro.
        self.boveda.license_key = False
        aviso = self.env['dsi.license.client.notice'].create({
            'product_code': 'cfdi_vault', 'license_key': CLAVE_DE_PRUEBA.lower()})
        self.assertIn(f"clave={CLAVE_DE_PRUEBA}", aviso.action_buy()['url'])
        self.assertEqual(self.boveda.license_key, CLAVE_DE_PRUEBA)

    def test_desactivar_manda_el_producto(self):
        por_base = self._por_base()
        por_base.write({'license_key': CLAVE_DE_PRUEBA,
                        'token': token_de_prueba(self.env, product_code=PRODUCTO_BASE)})
        with patch(ENVIO, return_value=_respuesta(ok=True)) as envio:
            por_base.action_deactivate()
        self.assertEqual(envio.call_args.kwargs['json']['product_code'], PRODUCTO_BASE)
        self.assertEqual(por_base.state, 'not_activated')

    def test_comprar_lleva_el_producto(self):
        por_base = self._por_base()
        por_base.license_key = CLAVE_DE_PRUEBA
        url = por_base._purchase_url()
        self.assertIn(f"producto={PRODUCTO_BASE}", url)
        self.assertIn(f"clave={CLAVE_DE_PRUEBA}", url)
        self.assertIn("producto=cfdi_vault", self.boveda._purchase_url())

    def test_por_base_no_se_reemplaza_una_clave_vigente(self):
        por_base = self._por_base()
        token = token_de_prueba(self.env, product_code=PRODUCTO_BASE)
        por_base.write({'license_key': 'DSI-OTRA-OTRA-OTRA', 'token': token})
        with patch(ENVIO) as envio, self.assertRaises(UserError) as error:
            por_base.action_activate()
        envio.assert_not_called()
        self.assertIn("ya tiene una licencia vigente", str(error.exception))
        self.assertEqual(por_base.token, token)
        # Ni con la renovación fallida: la licencia sigue vigente.
        por_base.token = token_de_prueba(self.env, dias=-3, product_code=PRODUCTO_BASE)
        with patch(ENVIO) as envio, self.assertRaises(UserError):
            por_base.action_activate()
        envio.assert_not_called()

    def test_por_base_la_misma_clave_se_reactiva(self):
        por_base = self._por_base()
        por_base.write({'license_key': CLAVE_DE_PRUEBA,
                        'token': token_de_prueba(self.env, product_code=PRODUCTO_BASE)})
        nuevo = token_de_prueba(self.env, dias=60, product_code=PRODUCTO_BASE)
        with patch(ENVIO, return_value=_respuesta(token=nuevo)) as envio:
            por_base.action_activate()
        envio.assert_called_once()
        self.assertEqual(por_base.token, nuevo)

    def test_por_base_se_reemplaza_una_clave_vencida(self):
        por_base = self._por_base()
        hace_tres = (fields.Date.today() - timedelta(days=3)).isoformat()
        por_base.write({
            'license_key': 'DSI-NUEV-NUEV-NUEV',
            'token': token_de_prueba(self.env, dias=-3, license_end=hace_tres, product_code=PRODUCTO_BASE),
        })
        self.assertEqual(por_base.license_mode, 'read')
        nuevo = token_de_prueba(self.env, product_code=PRODUCTO_BASE, license_key='DSI-NUEV-NUEV-NUEV')
        with patch(ENVIO, return_value=_respuesta(token=nuevo)):
            por_base.action_activate()
        self.assertEqual(por_base.state, 'active')

    def test_la_boveda_si_deja_cambiar_la_clave(self):
        self.boveda.write({'license_key': 'DSI-OTRA-OTRA-OTRA', 'token': token_de_prueba(self.env)})
        nuevo = token_de_prueba(self.env, dias=90, license_key='DSI-OTRA-OTRA-OTRA')
        with patch(ENVIO, return_value=_respuesta(token=nuevo)) as envio:
            self.boveda.action_activate()
        envio.assert_called_once()
        self.assertEqual(self.boveda.token, nuevo)

    def test_por_base_se_activa_con_el_rfc_de_la_raiz(self):
        raiz = self.env['res.company'].create({'name': "Raíz que activa"})
        raiz.partner_id.vat = 'AAA010101AAA'
        sucursal = self.env['res.company'].create({'name': "Sucursal que activa", 'parent_id': raiz.id})
        sucursal.partner_id.vat = False
        por_base = self._por_base(self.empresa)
        por_base.license_key = CLAVE_DE_PRUEBA
        token = token_de_prueba(self.env, product_code=PRODUCTO_BASE)
        with patch(ENVIO, return_value=_respuesta(token=token)) as envio:
            por_base.with_company(sucursal).action_activate()
        self.assertEqual(envio.call_args.kwargs['json']['rfc'], 'AAA010101AAA')
        self.assertEqual(por_base.company_id, raiz)
        self.assertEqual(por_base.state, 'active')

    def test_un_token_de_otro_producto_no_se_guarda(self):
        por_base = self._por_base()
        por_base.license_key = CLAVE_DE_PRUEBA
        with patch(ENVIO, return_value=_respuesta(token=token_de_prueba(self.env))), \
                self.assertRaises(UserError) as error:
            por_base.action_activate()
        self.assertIn("otro producto", str(error.exception))
        self.assertFalse(por_base.token)

    def test_la_renovacion_manda_producto_y_version(self):
        por_base = self._por_base()
        por_base.write({'license_key': CLAVE_DE_PRUEBA,
                        'token': token_de_prueba(self.env, product_code=PRODUCTO_BASE)})
        nuevo = token_de_prueba(self.env, dias=60, product_code=PRODUCTO_BASE)
        with patch(ENVIO, return_value=_respuesta(token=nuevo)) as envio:
            por_base._do_heartbeat()
        enviado = envio.call_args.kwargs['json']
        self.assertEqual(enviado['product_code'], PRODUCTO_BASE)
        self.assertTrue(enviado['odoo_version'])
        self.assertEqual(enviado['module_version'], por_base._get_module_version())
        self.assertEqual(por_base.token, nuevo)

    def test_la_renovacion_no_guarda_un_token_de_otro_producto(self):
        por_base = self._por_base()
        token = token_de_prueba(self.env, product_code=PRODUCTO_BASE)
        por_base.write({'license_key': CLAVE_DE_PRUEBA, 'token': token})
        with patch(ENVIO, return_value=_respuesta(token=token_de_prueba(self.env))):
            por_base._do_heartbeat()
        self.assertEqual(por_base.token, token)
        self.assertIn("otro producto", por_base.last_error)

    def test_la_renovacion_guarda_cada_registro(self):
        # Una falla en un registro no se lleva lo de los demás: cada uno va en
        # su propio savepoint y se guarda al terminar.
        por_base = self._por_base()
        por_base.write({'license_key': CLAVE_DE_PRUEBA,
                        'token': token_de_prueba(self.env, product_code=PRODUCTO_BASE)})
        self.boveda.write({'license_key': CLAVE_DE_PRUEBA, 'token': token_de_prueba(self.env)})
        con_token = self.State.search_count([('token', '!=', False)])

        def renovar(registro):
            if registro.product_code == 'cfdi_vault':
                registro.last_error = "a medias"
                raise RuntimeError("se cayó la renovación")
            registro.last_error = "renovado"

        with patch.object(self.registry['dsi.license.client.state'], '_do_heartbeat', renovar), \
                patch.object(self.env.cr, 'commit') as commit, \
                mute_logger('odoo.addons.dsi_license_client.models.dsi_license_client_state'):
            self.State._cron_heartbeat()
        self.assertEqual(commit.call_count, con_token)
        self.assertEqual(por_base.last_error, "renovado")
        self.assertNotEqual(self.boveda.last_error, "a medias")
