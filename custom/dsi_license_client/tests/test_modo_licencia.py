"""Qué deja hacer la licencia de cada producto: 'full', 'read' o 'none'.

Reglas (decisión 4 del plan de Manufactura):

  * Prueba vigente: completa. Prueba terminada: nada, aunque el token traiga
    días de gracia (las pruebas no tienen gracia).
  * Licencia vigente: completa.
  * En la gracia porque solo falló la renovación (licencia perpetua o que
    todavía no vence): completa.
  * En la gracia con la licencia ya vencida: solo consulta en los productos
    que lo piden (read_only_grace); la Bóveda sigue completa.
  * Un token de otro producto, de otra base o sin la función: nada.

Además, una clave de un producto no abre otro: el token trae product_code y
uno sin product_code es de la Bóveda (los de antes).
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import RedirectWarning, UserError
from odoo.tests.common import TransactionCase, tagged

from .common import PRODUCTO_BASE, catalogo_de_prueba, token_de_prueba, usar_llave_de_prueba


def _dia(dias):
    return (fields.Date.today() + timedelta(days=dias)).isoformat()


@tagged('post_install', '-at_install')
class TestModoLicencia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        catalogo_de_prueba(cls)
        cls.State = cls.env['dsi.license.client.state']
        cls.boveda = cls.State._get_or_create()
        cls.boveda.token = False
        cls.por_base = cls.State._get_product_license(PRODUCTO_BASE, create=True)
        cls.base_url = cls.boveda._get_base_url()

    def _token(self, registro, **valores):
        if registro.product_code != 'cfdi_vault':
            valores.setdefault('product_code', registro.product_code)
        registro.token = token_de_prueba(self.env, **valores)
        self.env.invalidate_all()
        return registro

    def _modo(self, registro, **valores):
        self._token(registro, **valores)
        return self.State._license_mode(registro.product_code)

    # ----- Producto del token -----

    def test_un_token_sin_producto_es_de_la_boveda(self):
        self.assertEqual(self._token(self.boveda).state, 'active')
        self.assertEqual(self.State._license_mode('cfdi_vault'), 'full')

    def test_la_clave_de_la_boveda_no_abre_otro_producto(self):
        self.por_base.token = token_de_prueba(self.env)
        self.assertEqual(self.por_base.state, 'restricted')
        self.assertIn("otro producto", self.por_base.status_message)
        self.assertEqual(self.State._license_mode(PRODUCTO_BASE), 'none')

    def test_la_clave_de_otro_producto_no_abre_la_boveda(self):
        # Aunque traiga la función de la Bóveda (como lo haría el servidor viejo).
        self.boveda.token = token_de_prueba(self.env, product_code=PRODUCTO_BASE, features=['vault'])
        self.assertEqual(self.boveda.state, 'restricted')
        self.assertFalse(self.boveda.is_feature_enabled('vault'))
        self.assertEqual(self.State._license_mode('cfdi_vault'), 'none')
        with self.assertRaises(RedirectWarning):
            self.State._check_feature('vault', "importar CFDI")

    def test_aplicar_un_token_de_otro_producto_no_lo_guarda(self):
        anterior = self._token(self.por_base).token
        with self.assertRaises(UserError):
            self.por_base._apply_token(token_de_prueba(self.env))
        self.assertEqual(self.por_base.token, anterior)

    # ----- Tipo de gracia -----

    def test_tipo_de_gracia(self):
        casos = [
            # (token_expires, license_end, tipo)
            (-3, None, 'renewal'),      # perpetua: solo falló la renovación
            (-3, 20, 'renewal'),        # la licencia sigue vigente
            (-3, -3, 'expired'),        # la licencia ya venció
            (-12, -1, 'expired'),       # vence lo primero: el token
        ]
        for dias, fin_licencia, tipo in casos:
            with self.subTest(dias=dias, license_end=fin_licencia):
                self._token(self.por_base, dias=dias,
                            license_end=_dia(fin_licencia) if fin_licencia is not None else None)
                detalle = self.por_base._evaluate_detail()
                self.assertEqual(detalle['state'], 'grace')
                self.assertEqual(detalle['kind'], tipo)
        # El límite cuenta desde lo que venza primero.
        self.assertEqual(detalle['limite'], fields.Date.today() + timedelta(days=-12 + 15))

    def test_mensajes_de_gracia(self):
        self._token(self.por_base, dias=-3, license_end=_dia(-3))
        self.assertIn("solo se puede consultar", self.por_base.status_message)
        self._token(self.por_base, dias=-3)
        self.assertIn("No se ha podido renovar", self.por_base.status_message)
        # La Bóveda conserva su texto de siempre.
        self._token(self.boveda, dias=-3)
        self.assertIn("sigue funcionando hasta el", self.boveda.status_message)
        self.assertIn("La licencia venció el", self.boveda.status_message)

    # ----- Modo -----

    def test_sin_registro_o_sin_token_no_hay_licencia(self):
        self.assertEqual(self.State._license_mode(PRODUCTO_BASE), 'none')
        self.por_base.unlink()
        self.assertEqual(self.State._license_mode(PRODUCTO_BASE), 'none')
        self.assertEqual(self.State._license_mode('producto_que_no_existe'), 'none')

    def test_matriz_de_modos(self):
        prueba = {'license_type': 'trial', 'grace_days': 0, 'base_url': self.base_url}
        casos = [
            ("vigente", {'dias': 30}, 'full'),
            ("prueba vigente", dict(prueba, dias=3), 'full'),
            # Las pruebas no tienen gracia aunque el token diga otra cosa.
            ("prueba terminada con gracia forjada", dict(prueba, dias=-1, grace_days=15), 'none'),
            ("renovación fallida, perpetua", {'dias': -3}, 'full'),
            ("renovación fallida, vigente", {'dias': -3, 'license_end': _dia(20)}, 'full'),
            ("licencia vencida", {'dias': -3, 'license_end': _dia(-3)}, 'read'),
            ("fuera de la gracia", {'dias': -20, 'license_end': _dia(-20)}, 'none'),
            ("sin la función", {'dias': 30, 'features': ['otra']}, 'none'),
            ("de otra base", {'dias': 30, 'db_uuid': 'otra-base'}, 'none'),
        ]
        for nombre, valores, modo in casos:
            with self.subTest(nombre):
                self.assertEqual(self._modo(self.por_base, **valores), modo)

    def test_la_boveda_no_tiene_gracia_de_solo_consulta(self):
        self.assertEqual(self._modo(self.boveda, dias=-3, license_end=_dia(-3)), 'full')
        self.assertEqual(self._modo(self.boveda, dias=-20, license_end=_dia(-20)), 'none')

    def test_por_base_el_modo_vale_en_cualquier_empresa(self):
        otra = self.env['res.company'].create({'name': "Otra empresa (modo)"})
        self._token(self.por_base)
        self.assertEqual(self.State._license_mode(PRODUCTO_BASE, otra), 'full')
        self.assertEqual(self.State.with_company(otra)._license_mode(PRODUCTO_BASE), 'full')
        # La Bóveda, en cambio, es por empresa.
        self._token(self.boveda)
        self.assertEqual(self.State._license_mode('cfdi_vault', otra), 'none')

    def test_calcular_el_modo_no_escribe(self):
        otra = self.env['res.company'].create({'name': "Otra empresa (sin escribir)"})
        self.por_base.unlink()
        antes = self.State.search_count([])
        self.State._license_mode(PRODUCTO_BASE)
        self.State._license_mode('cfdi_vault', otra)
        self.State._license_status(PRODUCTO_BASE)
        self.State._license_error(PRODUCTO_BASE, "registrar")
        self.assertEqual(self.State.search_count([]), antes)

    def test_estado_y_mensaje(self):
        self.assertEqual(self.State._license_status(PRODUCTO_BASE)[0], 'none')
        self._token(self.por_base, dias=-3, license_end=_dia(-3))
        modo, mensaje = self.State._license_status(PRODUCTO_BASE)
        self.assertEqual(modo, 'read')
        self.assertIn("solo se puede consultar", mensaje)
        self._token(self.por_base, features=['otra'])
        modo, mensaje = self.State._license_status(PRODUCTO_BASE)
        self.assertEqual(modo, 'none')
        self.assertIn("no incluye", mensaje)

    # ----- Bloqueo -----

    def test_con_licencia_completa_no_se_bloquea(self):
        self._token(self.por_base)
        self.assertIsNone(self.State._license_error(PRODUCTO_BASE, "registrar"))
        self.State._check_license_write(PRODUCTO_BASE, "registrar")

    def test_en_solo_consulta_se_bloquea_con_aviso(self):
        self._token(self.por_base, dias=-3, license_end=_dia(-3))
        with self.assertRaises(RedirectWarning) as error:
            self.State._check_license_write(PRODUCTO_BASE, "registrar la producción")
        mensaje, accion, boton, contexto = error.exception.args
        self.assertIn("No se puede registrar la producción", mensaje)
        self.assertIn("solo se puede consultar", mensaje)
        self.assertEqual(accion, 'dsi_license_client.action_license_notice_dialog')
        self.assertEqual(boton, "Comprar o activar licencia")
        self.assertEqual(contexto['dsi_license_product'], PRODUCTO_BASE)
        self.assertEqual(contexto['dsi_license_company'], self.por_base.company_id.id)

    def test_sin_licencia_se_bloquea_y_ofrece_la_prueba(self):
        error = self.State._license_error(PRODUCTO_BASE, "registrar")
        self.assertIsInstance(error, RedirectWarning)
        self.assertIn("Inicia la prueba de 7 días", error.args[0])
        self.assertEqual(error.args[3]['dsi_license_product'], PRODUCTO_BASE)
        self.assertEqual(error.loglevel, 20)  # INFO: un bloqueo esperado, no una falla
        self._token(self.por_base, dias=-20, license_end=_dia(-20))
        error = self.State._license_error(PRODUCTO_BASE, "registrar")
        self.assertNotIn("prueba de 7 días", error.args[0])
        self.assertIn("Compra o activa tu licencia", error.args[0])

    def test_el_bloqueo_de_la_boveda_lleva_su_producto(self):
        with self.assertRaises(RedirectWarning) as error:
            self.State._check_feature('vault', "importar CFDI")
        self.assertEqual(error.exception.args[3], {
            'dsi_license_product': 'cfdi_vault',
            'dsi_license_company': self.boveda.company_id.id,
        })

    def test_ajustes_muestran_el_modo(self):
        self._token(self.por_base, dias=-3, license_end=_dia(-3))
        self.assertEqual(self.por_base.license_mode, 'read')
        self.assertIn("solo se puede consultar", self.por_base.restriction_text)
        self._token(self.por_base, dias=-3)
        self.assertEqual(self.por_base.license_mode, 'full')
        self.assertFalse(self.por_base.restriction_text)
