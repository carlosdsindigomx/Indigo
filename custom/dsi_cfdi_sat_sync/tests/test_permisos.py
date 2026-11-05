"""Pruebas de cómo se presentan los permisos de la Bóveda en la ficha de usuario.

Existen porque el primer intento de agruparlos metió los dos grupos en un mismo
privilegio sin relación entre ellos. Odoo pinta cada privilegio como UN solo
desplegable, así que elegir «Custodio de e.firma» le quitaba al usuario
«Usuario», y con él el menú raíz de la Bóveda del que cuelga la Descarga
Masiva SAT. Ninguna prueba funcional lo detectaba: solo se veía al editar los
permisos desde la ficha del usuario.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPermisosBoveda(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usuario = cls.env.ref('dsi_cfdi_vault.group_cfdi_vault_user')
        cls.custodio = cls.env.ref('dsi_cfdi_sat_sync.group_sat_credential_manager')
        cls.privilegio = cls.env.ref('dsi_cfdi_vault.res_groups_privilege_cfdi_vault')

    def test_un_solo_desplegable_escalonado(self):
        self.assertEqual(self.usuario.privilege_id, self.privilegio)
        self.assertEqual(self.custodio.privilege_id, self.privilegio)
        jerarquia = self.env['res.groups']._get_view_group_hierarchy()
        # El desplegable muestra el último grupo marcado en este orden, así que
        # el custodio tiene que ir arriba del usuario.
        self.assertEqual(
            jerarquia['privileges'][self.privilegio.id]['group_ids'],
            [self.usuario.id, self.custodio.id])

    def test_elegir_custodio_conserva_la_app(self):
        # Es lo que manda la ficha de usuario al elegir «Custodio de e.firma»:
        # solo ese grupo del privilegio, nunca los dos.
        user = self.env['res.users'].create({
            'name': "Custodia de prueba",
            'login': 'custodia-prueba@example.com',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id, self.custodio.id])],
        })
        self.assertTrue(user.has_group('dsi_cfdi_vault.group_cfdi_vault_user'))
        self.assertTrue(user.has_group('dsi_cfdi_sat_sync.group_sat_credential_manager'))

    def test_el_usuario_no_recibe_la_efirma(self):
        self.assertNotIn(self.custodio, self.usuario.all_implied_ids)

    def test_nombres_completos(self):
        self.assertEqual(self.usuario.full_name, "Bóveda CFDI / Usuario")
        self.assertEqual(self.custodio.full_name, "Bóveda CFDI / Custodio de e.firma")
