"""Pruebas del mapeo de impuestos del CFDI a impuestos de Odoo.

Existen por dos errores reales encontrados en auditoría, ambos del tipo más
peligroso posible: la factura cuadraba al centavo y no se emitía ningún aviso,
así que el contador no tenía forma de enterarse.

  * Retención de ISR 10% se contabilizaba como retención de IVA 10%, lo que
    mete ISR en el renglón de retenciones de la DIOT, donde no va.
  * IVA 8% de región fronteriza se contabilizaba como IEPS 8%, lo que
    subdeclara el IVA acreditable y pierde las etiquetas "DIOT: 8% N.".

La causa era desempatar buscando subcadenas en `account.tax.name`. En la
localización mexicana los nombres son "10% WH L", "8%" o "8% IEPS": no
contienen "IVA" ni "ISR", así que el desempate puntuaba 0 para todos y ganaba
el primero por secuencia. El discriminador correcto es `tax_group_id`.

Estas pruebas SOLO son significativas sobre la localización mexicana: una base
con `generic_coa` tiene media docena de impuestos genéricos sin colisiones de
tasa y pasaría sin ejercitar nada. Por eso se omiten explícitamente en vez de
dar un falso verde.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMapeoImpuestos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        if cls.company.chart_template != 'mx':
            raise cls.skipTest(
                cls,
                "Requiere la localización mexicana (chart_template='mx'); "
                f"esta base usa '{cls.company.chart_template}'. Sin el catálogo "
                "mexicano no existen las colisiones de tasa que esta prueba cubre.")
        cls.Doc = cls.env['cfdi.document']

    def _grupo(self, clave_sat, tasa, es_retencion):
        impuesto = self.Doc._buscar_impuesto_odoo(clave_sat, tasa, es_retencion, self.company)
        return impuesto.tax_group_id.name if impuesto else None

    # -- Los dos errores que motivaron estas pruebas ---------------------

    def test_retencion_isr_no_se_contabiliza_como_iva(self):
        """Regresión: la retención de ISR 10% iba a la cuenta de IVA retenido.

        Al -10% hay tres candidatos en l10n_mx (una retención de IVA y dos de
        ISR) y el nombre no distingue ninguno.
        """
        grupo = self._grupo('001', 10.0, True)
        self.assertIsNotNone(grupo, "debe encontrar una retención de ISR al 10%")
        self.assertIn('ISR', grupo, f"la retención de ISR terminó en el grupo '{grupo}'")
        self.assertNotIn('VAT', grupo, "una retención de ISR nunca va a un grupo de IVA")

    def test_iva_8_fronterizo_no_se_contabiliza_como_ieps(self):
        """Regresión: el IVA 8% de región fronteriza iba a IEPS acreditable.

        Al 8% hay tres candidatos y el IEPS gana por secuencia si no se
        desempata bien.
        """
        grupo = self._grupo('002', 8.0, False)
        self.assertIsNotNone(grupo, "debe encontrar un IVA al 8%")
        self.assertIn('VAT', grupo, f"el IVA 8% terminó en el grupo '{grupo}'")
        self.assertNotIn('IEPS', grupo, "el IVA fronterizo nunca va a IEPS")

    # -- Que lo que ya estaba bien siga estándolo -----------------------

    def test_casos_que_ya_funcionaban_no_se_rompen(self):
        casos = [
            ('002', 16.0,  False, 'VAT',  "IVA 16% trasladado"),
            ('003',  8.0,  False, 'IEPS', "IEPS 8%"),
            ('003', 53.0,  False, 'IEPS', "IEPS 53%"),
            ('002',  0.0,  False, 'VAT',  "IVA tasa 0"),
            ('002',  4.0,  True,  'VAT',  "retención de IVA 4%"),
            ('002', 10.67, True,  'VAT',  "retención de IVA 10.67%"),
        ]
        for clave, tasa, retencion, esperado, desc in casos:
            with self.subTest(caso=desc):
                grupo = self._grupo(clave, tasa, retencion)
                self.assertIsNotNone(grupo, f"{desc}: no encontró impuesto")
                self.assertIn(esperado, grupo, f"{desc}: cayó en el grupo '{grupo}'")

    def test_traslado_y_retencion_no_se_confunden(self):
        """El signo debe respetarse: un traslado nunca es una retención."""
        traslado = self.Doc._buscar_impuesto_odoo('002', 16.0, False, self.company)
        retencion = self.Doc._buscar_impuesto_odoo('002', 4.0, True, self.company)
        self.assertGreater(traslado.amount, 0, "un traslado tiene tasa positiva")
        self.assertLess(retencion.amount, 0, "una retención tiene tasa negativa")

    # -- Comportamiento ante lo desconocido ------------------------------

    def test_tasa_inexistente_no_inventa_impuesto(self):
        """Sin equivalente configurado se devuelve vacío, no un aproximado.

        Inventar un impuesto descuadraría la factura en silencio; dejarla sin
        él produce una diferencia visible y un aviso en el chatter.
        """
        self.assertFalse(self.Doc._buscar_impuesto_odoo('002', 37.3, False, self.company))

    def test_avisa_cuando_no_hay_equivalente(self):
        concepto = {
            'traslados': [{'impuesto': '002', 'tipo_factor': 'Tasa',
                           'tasa': 37.3, 'importe': 37.3, 'base': 100.0}],
            'retenciones': [],
        }
        impuestos, avisos = self.Doc._impuestos_odoo_de_concepto(concepto, self.company)
        self.assertFalse(impuestos, "no debe asignar ningún impuesto")
        self.assertTrue(avisos, "debe avisar que no hay equivalente")
        self.assertIn('IVA', avisos[0])
