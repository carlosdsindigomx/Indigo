"""La entrada a un producto con licencia y el aviso por producto.

  * Sin licencia, la entrada muestra el aviso del producto (sin «Seguir solo
    consultando» si el producto no lo permite) y, al activar o pulsar
    «Entrar», vuelve a la entrada.
  * Con licencia abre su destino tal cual; en solo consulta, sin crear,
    editar ni borrar.
  * El aviso seguía con ir.actions.act_window._for_xml_id, que truena con
    una acción cliente o de servidor: activar desde un aviso así revertía la
    activación entera.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from .common import (CLAVE_DE_PRUEBA, PRODUCTO_BASE, catalogo_de_prueba, token_de_prueba,
                     usar_llave_de_prueba)

ENVIO = 'odoo.addons.dsi_license_client.models.dsi_license_client_state.requests.post'
DESTINO = 'dsi_prueba.destino'
DESTINO_CON_UID = 'dsi_prueba.destino_con_uid'
CLIENTE = 'dsi_prueba.cliente'
ENTRADA = 'dsi_prueba.entrada'


@tagged('post_install', '-at_install')
class TestEntradaLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        catalogo_de_prueba(cls)
        cls.State = cls.env['dsi.license.client.state']
        cls.Aviso = cls.env['dsi.license.client.notice']
        cls.por_base = cls.State._get_product_license(PRODUCTO_BASE, create=True)
        cls.boveda = cls.State._get_or_create()
        cls.boveda.write({'token': False, 'license_key': False})
        cls.boveda.company_id.partner_id.vat = 'EKU9003173C9'
        modelo = cls.env['ir.model']._get('res.partner')
        acciones = {
            DESTINO: cls.env['ir.actions.act_window'].create({
                'name': "Destino", 'res_model': 'res.partner',
                'context': "{'search_default_type_company': 1}",
            }),
            DESTINO_CON_UID: cls.env['ir.actions.act_window'].create({
                'name': "Destino con uid", 'res_model': 'res.partner',
                'context': "{'default_user_id': uid, 'default_company_id': allowed_company_ids[0]}",
            }),
            CLIENTE: cls.env['ir.actions.client'].create({
                'name': "Pantalla completa", 'tag': 'dsi_prueba_cliente', 'target': 'fullscreen',
            }),
            ENTRADA: cls.env['ir.actions.server'].create({
                'name': "Entrada", 'model_id': modelo.id, 'state': 'code',
                'code': f"action = env['dsi.license.client.state']._entry_action("
                        f"'{PRODUCTO_BASE}', '{DESTINO}', '{ENTRADA}')",
            }),
        }
        for xmlid, accion in acciones.items():
            modulo, nombre = xmlid.split('.')
            cls.env['ir.model.data'].create({
                'module': modulo, 'name': nombre, 'model': accion._name, 'res_id': accion.id})

    def _activar(self, registro, **valores):
        if registro.product_code != 'cfdi_vault':
            valores.setdefault('product_code', registro.product_code)
        registro.write({'license_key': CLAVE_DE_PRUEBA, 'token': token_de_prueba(self.env, **valores)})
        self.env.invalidate_all()

    def _vencida(self):
        hace_tres = (fields.Date.today() - timedelta(days=3)).isoformat()
        self._activar(self.por_base, dias=-3, license_end=hace_tres)

    def _aviso(self, **valores):
        valores.setdefault('product_code', PRODUCTO_BASE)
        return self.Aviso.create(valores)

    # ----- Entrada -----

    def test_sin_licencia_la_entrada_muestra_el_aviso(self):
        accion = self.State._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        self.assertEqual(accion['target'], 'main')
        # Sin res_id: el aviso es temporal y recargar la pestaña no debe
        # buscar uno que Odoo ya borró.
        self.assertFalse(accion.get('res_id'))
        contexto = accion['context']
        self.assertEqual(contexto['dsi_license_product'], PRODUCTO_BASE)
        self.assertEqual(contexto['default_next_action'], ENTRADA)
        self.assertFalse(contexto['default_allow_continue'])
        aviso = self.Aviso.with_context(**contexto).create({})
        self.assertEqual(aviso.product_code, PRODUCTO_BASE)
        self.assertEqual(aviso.next_action, ENTRADA)
        self.assertEqual(aviso.target, 'main')
        self.assertFalse(aviso.allow_continue)
        self.assertEqual(aviso.license_mode, 'none')
        self.assertFalse(aviso.can_enter)
        self.assertIn("Producto de prueba no tiene una licencia activada", aviso.title)

    def test_con_licencia_la_entrada_abre_el_destino(self):
        self._activar(self.por_base)
        accion = self.State._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)
        self.assertEqual(accion['res_model'], 'res.partner')
        self.assertEqual(accion['context'], "{'search_default_type_company': 1}")
        cliente = self.State._entry_action(PRODUCTO_BASE, CLIENTE, ENTRADA)
        self.assertEqual((cliente['type'], cliente['target']), ('ir.actions.client', 'fullscreen'))

    def test_en_solo_consulta_no_se_crea_edita_ni_borra(self):
        self._vencida()
        accion = self.State._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)
        contexto = accion['context']
        self.assertIsInstance(contexto, dict)
        for clave in ('create', 'edit', 'delete', 'group_create', 'group_edit', 'group_delete'):
            self.assertIs(contexto[clave], False, clave)
        self.assertEqual(contexto['dsi_license_mode'], 'read')
        self.assertEqual(contexto['search_default_type_company'], 1)
        # Un contexto con variables se evalúa con las del usuario.
        contexto = self.State._entry_action(PRODUCTO_BASE, DESTINO_CON_UID, ENTRADA)['context']
        self.assertEqual(contexto['default_user_id'], self.env.uid)
        self.assertEqual(contexto['default_company_id'], self.env.company.id)
        self.assertIs(contexto['create'], False)
        # Las acciones cliente (kiosco, panel) también reciben el modo.
        cliente = self.State._entry_action(PRODUCTO_BASE, CLIENTE, ENTRADA)
        self.assertEqual(cliente['context']['dsi_license_mode'], 'read')

    def test_el_contexto_se_evalua_con_la_empresa_actual_primero(self):
        # Sin allowed_company_ids en el contexto (shell, cron, RPC externo),
        # en 18 y 19 env.companies sale en el orden de res.company (aquí,
        # «primera» antes que la actual); en 20, res.users._get_company_ids ya
        # pone primero la empresa del usuario. El cliente web pone la empresa
        # actual primero, y así debe evaluarse en cualquier versión.
        actual = self.env.company
        primera = self.env['res.company'].create({'name': "AAA primera (licencias)", 'sequence': 0})
        usuario = new_test_user(
            self.env, login='varias-empresas-licencia@example.com', groups='base.group_user',
            company_id=actual.id, company_ids=[(6, 0, [primera.id, actual.id])])
        State = self.State.with_user(usuario)
        self.assertEqual(State.env.company, actual)
        self.assertCountEqual(State.env.companies.ids, [primera.id, actual.id])
        contexto = State._action_context("{'empresa': allowed_company_ids[0], 'todas': allowed_company_ids}")
        self.assertEqual(contexto['empresa'], actual.id)
        self.assertCountEqual(contexto['todas'], [primera.id, actual.id])
        # Con allowed_company_ids (el cliente web) manda ese orden.
        con_web = State.with_context(allowed_company_ids=[primera.id, actual.id])
        self.assertEqual(con_web._action_context("{'empresa': allowed_company_ids[0]}")['empresa'], primera.id)
        # Y así llega a la entrada en solo consulta.
        self._vencida()
        contexto = State._entry_action(PRODUCTO_BASE, DESTINO_CON_UID, ENTRADA)['context']
        self.assertEqual(contexto['default_company_id'], actual.id)
        self.assertEqual(contexto['default_user_id'], usuario.id)

    def test_desde_el_aviso_el_destino_reemplaza_la_ruta(self):
        self._activar(self.por_base)
        desde_aviso = self.State.with_context(dsi_license_from_notice=True)
        self.assertEqual(desde_aviso._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)['target'], 'main')
        self.assertEqual(desde_aviso._entry_action(PRODUCTO_BASE, CLIENTE, ENTRADA)['target'], 'fullscreen')

    def test_la_accion_de_servidor_corre_la_entrada(self):
        accion = self.env.ref(ENTRADA).run()
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        self._activar(self.por_base)
        self.assertEqual(self.env.ref(ENTRADA).run()['res_model'], 'res.partner')

    def test_un_usuario_sin_ajustes_consulta_el_modo_y_entra(self):
        # Los operarios no tienen acceso a los registros de licencia: el
        # cliente los lee con sudo y el aviso lo crea el propio usuario.
        operario = new_test_user(self.env, login='operario-licencia@example.com', groups='base.group_user')
        State = self.State.with_user(operario)
        self.assertEqual(State._license_mode(PRODUCTO_BASE), 'none')
        self.assertIsNotNone(State._license_error(PRODUCTO_BASE, "registrar"))
        accion = State._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)
        aviso = self.Aviso.with_user(operario).with_context(**accion['context']).create({})
        self.assertEqual(aviso.create_uid, operario)
        self.assertEqual(aviso.license_mode, 'none')
        # Sin licencia el producto no deja seguir: vuelve al aviso, sin su id.
        accion = aviso.action_continue()
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        self.assertFalse(accion.get('res_id'))
        self._activar(self.por_base)
        self.assertEqual(State._license_mode(PRODUCTO_BASE), 'full')
        self.assertEqual(State._entry_action(PRODUCTO_BASE, DESTINO, ENTRADA)['res_model'], 'res.partner')
        # Como lo abre el menú «Licencia» de un producto.
        accion = State._get_product_license(PRODUCTO_BASE, create=True)._notice_action(target='new')
        self.assertEqual(self.Aviso.browse(accion['res_id']).create_uid, operario)

    # ----- Aviso -----

    def test_el_aviso_sigue_a_una_accion_cliente(self):
        aviso = self._aviso(next_action=CLIENTE, target='main')
        accion = aviso._next()
        self.assertEqual(accion['type'], 'ir.actions.client')
        self.assertEqual(accion['target'], 'fullscreen')

    def test_el_aviso_sigue_a_una_accion_de_servidor(self):
        accion = self._aviso(next_action=ENTRADA, target='main')._next()
        self.assertEqual(accion['type'], 'ir.actions.server')
        self.assertEqual(accion['context'], {'dsi_license_from_notice': True})

    def test_el_aviso_sigue_a_una_ventana_en_pantalla_principal(self):
        accion = self._aviso(next_action=DESTINO, target='main')._next()
        self.assertEqual((accion['res_model'], accion['target']), ('res.partner', 'main'))

    def test_accion_inexistente_cierra_el_aviso(self):
        aviso = self._aviso(next_action='dsi_prueba.no_existe', target='main')
        with mute_logger('odoo.addons.dsi_license_client.models.dsi_license_client_notice'):
            self.assertEqual(aviso._next(), {'type': 'ir.actions.act_window_close'})

    def test_activar_con_accion_cliente_no_se_revierte(self):
        aviso = self.Aviso.create({'product_code': 'cfdi_vault', 'next_action': CLIENTE, 'target': 'main',
                                   'license_key': CLAVE_DE_PRUEBA})
        respuesta = MagicMock(status_code=200, content=b'x',
                              json=lambda: {'token': token_de_prueba(self.env)})
        with patch(ENVIO, return_value=respuesta):
            accion = aviso.action_activate()
        self.assertEqual(accion['params']['next']['type'], 'ir.actions.client')
        self.assertEqual(self.boveda.state, 'active')

    def test_entrar_aparece_al_tener_licencia(self):
        aviso = self._aviso(next_action=ENTRADA, target='main', allow_continue=False)
        self.assertFalse(aviso.can_enter)
        self._activar(self.por_base)
        self.assertTrue(aviso.can_enter)
        self.assertEqual(aviso.action_continue()['type'], 'ir.actions.server')
        # Sin acción siguiente, o en una ventana emergente, no hay a dónde entrar.
        self.assertFalse(self._aviso(target='main').can_enter)
        self.assertFalse(self._aviso(next_action=ENTRADA, target='new').can_enter)

    def test_sin_seguir_consultando_vuelve_al_aviso(self):
        aviso = self._aviso(next_action=DESTINO, target='main', allow_continue=False)
        accion = aviso.action_continue()
        self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
        self.assertEqual(accion['context']['default_next_action'], DESTINO)
        self.assertFalse(accion['context']['default_allow_continue'])
        # La Bóveda sí deja seguir consultando.
        accion = self.boveda._notice_action(next_action=DESTINO, target='main')
        aviso = self.Aviso.browse(accion['res_id'])
        self.assertTrue(aviso.allow_continue)
        self.assertEqual(aviso.action_continue()['res_model'], 'res.partner')

    def test_los_botones_en_pantalla_completa_no_fijan_el_aviso(self):
        # Después de «Actualizar», «Iniciar prueba» o de volver al aviso, la
        # pantalla completa se abre otra vez sin res_id: con el id del
        # temporal en la URL, recargar horas después daría «registro
        # inexistente».
        otra = self.env['res.company'].create({'name': "Otra empresa (aviso)"})
        aviso = self._aviso(next_action=ENTRADA, target='main', allow_continue=False, company_id=otra.id)
        for accion in (aviso.action_refresh(), aviso.action_continue()):
            self.assertEqual(accion['res_model'], 'dsi.license.client.notice')
            self.assertFalse(accion.get('res_id'))
            self.assertEqual(accion['target'], 'main')
            otro = self.Aviso.with_context(**accion['context']).create({})
            self.assertEqual(
                (otro.product_code, otro.company_id, otro.next_action, otro.target, otro.allow_continue),
                (PRODUCTO_BASE, otra, ENTRADA, 'main', False))
        # La Bóveda en pantalla completa conserva «Seguir solo consultando».
        accion = self.boveda._notice_action(next_action=DESTINO, target='main')
        boveda = self.Aviso.browse(accion['res_id'])
        reabierto = boveda.action_refresh()
        self.assertFalse(reabierto.get('res_id'))
        self.assertTrue(reabierto['context']['default_allow_continue'])
        self.assertEqual(reabierto['context']['dsi_license_product'], 'cfdi_vault')
        # En una ventana emergente o dentro de la ruta, con su id, como siempre.
        for target in ('new', 'current'):
            aviso = self._aviso(target=target)
            accion = aviso.action_refresh()
            self.assertEqual((accion['res_id'], accion['target']), (aviso.id, target))
        # Iniciar la prueba desde la pantalla completa tampoco fija el aviso.
        cp = self.env['ir.config_parameter'].sudo()
        # Odoo 20 cambió set_param por accesores tipados.
        poner = cp.set_str if hasattr(cp, 'set_str') else cp.set_param
        poner('web.base.url', 'https://cliente.example.com')
        aviso = self._aviso(next_action=ENTRADA, target='main', allow_continue=False)
        accion = aviso.action_start_trial()
        self.assertFalse(accion.get('res_id'))
        self.assertEqual(self.por_base.trial_state, 'requested')

    def test_el_aviso_del_producto_toma_su_catalogo(self):
        accion = self.por_base._notice_action(target='new')
        aviso = self.Aviso.browse(accion['res_id'])
        self.assertEqual(aviso.product_code, PRODUCTO_BASE)
        self.assertFalse(aviso.allow_continue)
        self.assertEqual(accion['name'], "Licencia de Producto de prueba")
        self.assertIn("Para usar Producto de prueba", aviso.notice_text)
        # Por contexto, como lo abre la ventana de bloqueo.
        aviso = self.Aviso.with_context(
            dsi_license_product=PRODUCTO_BASE, dsi_license_company=self.boveda.company_id.id).create({})
        self.assertEqual(aviso.product_code, PRODUCTO_BASE)
        self.assertEqual(aviso.company_id, self.boveda.company_id)

    def test_textos_del_aviso_en_solo_consulta(self):
        self._vencida()
        aviso = self._aviso()
        self.assertEqual(aviso.license_mode, 'read')
        self.assertIn("solo consulta hasta el", aviso.title)
        self.assertTrue(aviso.read_text)
        self.assertFalse(aviso.notice_text)
        # Con la renovación fallida se trabaja normal: sin texto de bloqueo.
        self._activar(self.por_base, dias=-3)
        aviso = self._aviso()
        self.assertEqual(aviso.license_mode, 'full')
        self.assertIn("No se ha podido renovar", aviso.title)
        self.assertFalse(aviso.notice_text)
        self.assertFalse(aviso.read_text)

    def test_la_boveda_conserva_sus_textos(self):
        aviso = self.Aviso.create({'product_code': 'cfdi_vault'})
        self.assertIn("Tus CFDI siguen aquí", aviso.notice_text)
        self._activar(self.boveda, dias=-3)
        aviso = self.Aviso.create({'product_code': 'cfdi_vault'})
        self.assertEqual(aviso.title, "Tu licencia de Bóveda CFDI venció")
        self.assertIn("Tus CFDI siguen aquí", aviso.notice_text)
