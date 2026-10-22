"""Lo que ve el usuario cuando la licencia no está vigente o la prueba se acaba.

Antes, al terminar la prueba solo aparecía un error que mandaba a un
administrador a Ajustes, y además ofrecía «iniciar la prueba» aunque ya no se
podía. Ahora la ventana lleva al aviso de licencia, donde cualquier usuario
compra o activa, y antes de que termine la prueba llegan avisos.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import RedirectWarning
from odoo.tests.common import TransactionCase, tagged

from .common import CLAVE_DE_PRUEBA, token_de_prueba, usar_llave_de_prueba

ENVIO = 'odoo.addons.dsi_license_client.models.dsi_license_client_state.requests.post'


@tagged('post_install', '-at_install')
class TestAvisoLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        cls.licencia = cls.env['dsi.license.client.state']._get_or_create()
        cls.licencia.write({'token': False, 'license_key': False})
        cls.licencia.company_id.partner_id.vat = 'EKU9003173C9'
        cls.contador = cls.env['res.users'].create({
            'name': "Contador", 'login': 'contador-licencia@example.com',
        })
        cls.base_url = cls.licencia._get_base_url()

    def _prueba(self, dias):
        self.licencia.write({
            'license_key': CLAVE_DE_PRUEBA,
            'token': token_de_prueba(self.env, dias=dias, license_type='trial', grace_days=0,
                                     base_url=self.base_url),
        })

    def _aviso(self, usuario=None):
        Aviso = self.env['dsi.license.client.notice']
        if usuario:
            Aviso = Aviso.with_user(usuario)
        return Aviso.create({'company_id': self.licencia.company_id.id})

    def test_bloqueo_ofrece_comprar_o_activar(self):
        with self.assertRaises(RedirectWarning) as error:
            self.env['dsi.license.client.state']._check_feature('vault', "importar CFDI")
        mensaje, accion, boton = error.exception.args[:3]
        self.assertEqual(accion, 'dsi_license_client.action_license_notice_dialog')
        self.assertEqual(boton, "Comprar o activar licencia")
        self.assertIn("Inicia la prueba de 7 días", mensaje)

    def test_prueba_terminada_no_ofrece_otra_prueba(self):
        self._prueba(dias=-1)
        with self.assertRaises(RedirectWarning) as error:
            self.env['dsi.license.client.state']._check_feature('vault', "importar CFDI")
        mensaje = error.exception.args[0]
        self.assertIn("La prueba terminó", mensaje)
        self.assertNotIn("prueba de 7 días", mensaje)
        self.assertIn("Compra o activa tu licencia", mensaje)

    def test_titulos_del_aviso(self):
        self.assertIn("no tiene una licencia activada", self._aviso().title)
        self._prueba(dias=3)
        self.assertIn("Estás usando la prueba", self._aviso().title)
        self._prueba(dias=-1)
        aviso = self._aviso()
        self.assertEqual(aviso.state, 'restricted')
        self.assertIn("Tu prueba de Bóveda CFDI terminó", aviso.title)

    def test_comprar_lleva_al_servidor_con_la_clave(self):
        self._prueba(dias=-1)
        accion = self._aviso().action_buy()
        self.assertEqual(accion['type'], 'ir.actions.act_url')
        self.assertEqual(accion['target'], 'new')
        self.assertTrue(accion['url'].startswith(
            f"{self.licencia.activation_url}/dsi/license/v1/comprar?"))
        self.assertIn(f"clave={CLAVE_DE_PRUEBA}", accion['url'])

    def test_cualquier_usuario_activa_desde_el_aviso(self):
        self._prueba(dias=-1)
        aviso = self._aviso(self.contador)
        # La clave de la prueba ya viene puesta: si DS Indigo la convirtió en
        # suscripción, basta con pulsar Activar.
        self.assertEqual(aviso.license_key, CLAVE_DE_PRUEBA)
        respuesta = MagicMock(status_code=200, content=b'x',
                              json=lambda: {'token': token_de_prueba(self.env, dias=365)})
        with patch(ENVIO, return_value=respuesta):
            accion = aviso.action_activate()
        self.assertEqual(accion['tag'], 'display_notification')
        self.assertEqual(self.licencia.state, 'active')

    def test_el_aviso_lo_crea_el_usuario_que_lo_ve(self):
        # Cada usuario solo puede abrir los registros temporales que él creó.
        accion = self.env['dsi.license.client.state'].with_user(self.contador)._get_or_create() \
            ._notice_action(next_action='base.action_partner_form')
        aviso = self.env['dsi.license.client.notice'].browse(accion['res_id'])
        self.assertEqual(aviso.create_uid, self.contador)
        self.assertEqual(aviso.next_action, 'base.action_partner_form')
        self.assertEqual(aviso.with_user(self.contador).action_continue()['res_model'], 'res.partner')

    def _avisos_enviados(self):
        return self.env['mail.message'].search([('subject', 'like', 'Tu prueba de Bóveda CFDI termina')])

    def test_avisos_a_3_dias_y_a_1_dia(self):
        admin = self.env.ref('base.user_admin')
        self._prueba(dias=3)
        self.licencia._send_trial_reminders()
        self.licencia._send_trial_reminders()  # la tarea puede correr dos veces el mismo día
        avisos = self._avisos_enviados()
        self.assertEqual(len(avisos), 1)
        self.assertIn("en 3 días", avisos.subject)
        self.assertIn(admin.partner_id, avisos.partner_ids)

        self._prueba(dias=1)
        self.licencia._send_trial_reminders()
        avisos = self._avisos_enviados()
        self.assertEqual(len(avisos), 2)
        self.assertIn("mañana", avisos[0].subject)

    def test_sin_aviso_lejos_del_fin_ni_en_licencias_pagadas(self):
        self._prueba(dias=6)
        self.licencia._send_trial_reminders()
        self.licencia.token = token_de_prueba(self.env, dias=1)
        self.licencia._send_trial_reminders()
        self.assertFalse(self._avisos_enviados())
