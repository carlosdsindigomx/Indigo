"""Estado de la licencia en el cliente.

Existen por tres defectos reales:

  * El estado era un campo guardado que solo se recalculaba al llegar un token
    nuevo. Una licencia vencida o revocada ya no recibe tokens, así que se
    quedaba «Vigente» para siempre.
  * Se decidía con fechas y funciones guardadas en el registro, que cualquiera
    con acceso técnico puede editar, en vez de con el token firmado.
  * El token no se comprobaba contra la base: copiado a otra base, valía igual.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import RedirectWarning, UserError
from odoo.tests.common import TransactionCase, tagged

from .common import token_de_prueba, usar_llave_de_prueba


@tagged('post_install', '-at_install')
class TestEstadoLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        cls.estado = cls.env['dsi.license.client.state']._get_or_create()
        cls.base_url = cls.estado._get_base_url()

    def _con_token(self, **valores):
        self.estado.token = token_de_prueba(self.env, **valores)
        return self.estado

    def test_sin_token_no_esta_activada(self):
        self.estado.token = False
        self.assertEqual(self.estado.state, 'not_activated')
        self.assertFalse(self.estado.is_feature_enabled('vault'))

    def test_vigente(self):
        estado = self._con_token(dias=10)
        self.assertEqual(estado.state, 'active')
        self.assertTrue(estado.is_feature_enabled('vault'))

    def test_vencido_entra_en_gracia_y_despues_se_restringe(self):
        self.assertEqual(self._con_token(dias=-3).state, 'grace')
        self.assertTrue(self.estado.is_feature_enabled('vault'))
        self.assertEqual(self._con_token(dias=-16).state, 'restricted')
        self.assertFalse(self.estado.is_feature_enabled('vault'))

    def test_la_gracia_la_dicta_el_token(self):
        self.assertEqual(self._con_token(dias=-3, grace_days=5).state, 'grace')
        self.assertEqual(self._con_token(dias=-6, grace_days=5).state, 'restricted')

    def test_vence_la_licencia_aunque_el_token_siga_vigente(self):
        # Tokens emitidos antes de que el servidor los recortara a la licencia.
        ayer = (fields.Date.today() - timedelta(days=1)).isoformat()
        estado = self._con_token(dias=25, license_end=ayer, grace_days=0)
        self.assertEqual(estado.state, 'restricted')

    def test_prueba_vigente_y_terminada_sin_gracia(self):
        prueba = {'license_type': 'trial', 'grace_days': 0, 'base_url': self.base_url}
        estado = self._con_token(dias=6, **prueba)
        self.assertEqual(estado.state, 'trial')
        self.assertTrue(estado.is_feature_enabled('vault'))
        estado = self._con_token(dias=-1, **prueba)
        self.assertEqual(estado.state, 'restricted')
        self.assertIn("La prueba terminó", estado.status_message)

    def test_token_de_otra_base_no_vale(self):
        estado = self._con_token(db_uuid='otra-base')
        self.assertEqual(estado.state, 'restricted')
        self.assertIn("otra base", estado.status_message)

    def test_prueba_amarrada_a_la_url(self):
        estado = self._con_token(license_type='trial', grace_days=0, base_url='https://otro.example.com')
        self.assertEqual(estado.state, 'restricted')
        self.assertIn('otro.example.com', estado.status_message)

    def test_licencia_pagada_no_se_amarra_a_la_url(self):
        # Sin base_url en el token: en Odoo.sh cada build de staging cambia de URL.
        self.assertEqual(self._con_token(dias=10).state, 'active')

    def test_firma_alterada_no_vale(self):
        estado = self._con_token(dias=10)
        contenido, _punto, firma = estado.token.partition('.')
        estado.token = f"{contenido}.{firma[::-1]}"
        self.assertEqual(estado.state, 'restricted')
        self.assertFalse(estado.is_feature_enabled('vault'))

    def test_editar_los_campos_guardados_no_habilita_nada(self):
        estado = self._con_token(dias=-30, grace_days=0)
        estado.write({
            'token_expires': fields.Date.today() + timedelta(days=365),
            'features': 'vault,reconcile',
        })
        self.assertEqual(estado.state, 'restricted')
        self.assertFalse(estado.is_feature_enabled('vault'))

    def test_funcion_que_el_token_no_incluye(self):
        estado = self._con_token(dias=10, features=['reconcile'])
        with self.assertRaises(UserError) as error:
            self.env['dsi.license.client.state']._check_feature('vault', "importar CFDI")
        self.assertIn("no incluye", str(error.exception))
        self.assertEqual(estado.state, 'active')

    def test_la_renovacion_no_modifica_su_propia_tarea(self):
        # Odoo no deja escribir en una tarea programada mientras se ejecuta: la
        # renovación reprogramaba su propia hora y fallaba siempre.
        self.estado.token = False
        with patch.object(self.env.registry['ir.cron'], 'write',
                          side_effect=AssertionError("escribió en su propia tarea")):
            self.env['dsi.license.client.state']._cron_heartbeat()

    def test_mensaje_de_bloqueo(self):
        self.estado.token = False
        with self.assertRaises(RedirectWarning) as error:
            self.env['dsi.license.client.state']._check_feature('vault', "importar CFDI")
        self.assertIn("No se puede importar CFDI", str(error.exception))
        self.assertIn("consultando y exportando", str(error.exception))
        self.assertEqual(error.exception.args[1], 'dsi_license_client.action_license_notice_dialog')
