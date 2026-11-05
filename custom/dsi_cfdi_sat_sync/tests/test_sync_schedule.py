"""Pruebas del cálculo de periodos de la descarga desatendida.

Existen por una razón concreta: la primera versión de `_next_period` aplicaba
los días de recuperación a la frontera de avance, no solo a la ventana de
consulta. El efecto era que con la configuración por omisión (diaria +
3 días de recuperación) la frontera retrocedía 2 días por corrida y la
programación se quedaba clavada para siempre en el primer periodo, remandando
el mismo rango al SAT todos los días. Una sola corrida pasaba la prueba; el
fallo solo aparecía a partir de la segunda, que es justo lo que nadie probó.
"""
import calendar
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSyncSchedulePeriodos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Compañía propia: sat.credential es única por compañía, y la base de
        # desarrollo ya tiene cargada la e.firma real. Sin esto la prueba choca
        # contra sat_credential_company_uniq.
        cls.company = cls.env['res.company'].create({'name': "Prueba Descarga SAT"})
        cls.credential = cls.env['sat.credential'].create({
            'company_id': cls.company.id,
            'rfc': 'AAA010101AAA',
            'state': 'validated',
        })

    def _programacion(self, **kwargs):
        valores = {
            'company_id': self.company.id,
            'credential_id': self.credential.id,
            'date_start': date(2026, 9, 10),
        }
        valores.update(kwargs)
        return self.env['sat.sync.schedule'].create(valores)

    def _simular(self, programacion, hoy, max_corridas=400):
        """Corre _next_period repetidamente, como haría el cron día tras día.

        Devuelve (periodos, se_atoro). Se considera atorada cuando un periodo
        no deja la cobertura más adelante que la corrida anterior.
        """
        periodos = []
        with patch.object(fields.Date, 'context_today', return_value=hoy):
            for _ in range(max_corridas):
                resultado = programacion._next_period()
                if not resultado:
                    return periodos, False
                desde, hasta = resultado
                if programacion.date_covered_to and hasta <= programacion.date_covered_to:
                    return periodos, True
                periodos.append((desde, hasta))
                programacion.date_covered_to = hasta
        return periodos, True

    def test_avanza_en_todas_las_combinaciones(self):
        """Ninguna combinación de periodicidad y recuperación se atora.

        Es la prueba de regresión del bug: con lookback > 0 la frontera debe
        seguir avanzando.
        """
        hoy = date(2026, 12, 31)
        for periodicidad in ('daily', 'weekly', 'biweekly'):
            for lookback in (0, 1, 3, 7):
                with self.subTest(periodicidad=periodicidad, lookback=lookback):
                    prog = self._programacion(periodicity=periodicidad, lookback_days=lookback)
                    periodos, atorada = self._simular(prog, hoy)
                    self.assertFalse(
                        atorada,
                        f"{periodicidad}/lookback={lookback} se atoró tras {len(periodos)} corridas "
                        f"(cubierto hasta {prog.date_covered_to})")
                    self.assertEqual(
                        prog.date_covered_to, hoy,
                        f"{periodicidad}/lookback={lookback} no se puso al día")

    def test_no_deja_huecos_de_cobertura(self):
        """Cada periodo arranca, a más tardar, justo después del anterior.

        Un hueco significa comprobantes que nunca se piden al SAT — el tipo de
        fallo que nadie nota hasta la auditoría.
        """
        hoy = date(2026, 12, 31)
        for periodicidad in ('daily', 'weekly', 'biweekly'):
            with self.subTest(periodicidad=periodicidad):
                prog = self._programacion(periodicity=periodicidad, lookback_days=3)
                periodos, _ = self._simular(prog, hoy)
                anterior_hasta = None
                for desde, hasta in periodos:
                    if anterior_hasta is not None:
                        self.assertLessEqual(
                            desde, anterior_hasta + timedelta(days=1),
                            f"hueco: {anterior_hasta} -> {desde}")
                    anterior_hasta = hasta

    def test_lookback_ensancha_la_ventana_sin_mover_la_frontera(self):
        """El lookback debe verse en date_from, no en date_covered_to."""
        hoy = date(2026, 12, 31)
        prog = self._programacion(periodicity='daily', lookback_days=3)
        with patch.object(fields.Date, 'context_today', return_value=hoy):
            desde1, hasta1 = prog._next_period()
            prog.date_covered_to = hasta1
            desde2, hasta2 = prog._next_period()

        self.assertEqual(hasta1, date(2026, 9, 10), "el primer periodo arranca en date_start")
        self.assertEqual(hasta2, date(2026, 9, 11), "la frontera avanza exactamente un día")
        self.assertEqual(desde2, date(2026, 9, 10),
                         "la ventana retrocede hasta date_start, no más atrás")
        self.assertLess(desde2, hasta2, "la ventana cubre más de un día por el lookback")

    def test_quincenas_naturales(self):
        """Las quincenas son 1-15 y 16-fin de mes, como cierra un contador."""
        hoy = date(2026, 12, 31)
        prog = self._programacion(periodicity='biweekly', lookback_days=0,
                                  date_start=date(2026, 9, 1))
        periodos, _ = self._simular(prog, hoy)
        esperados = [
            (date(2026, 9, 1), date(2026, 9, 15)),
            (date(2026, 9, 16), date(2026, 9, 30)),
            (date(2026, 10, 1), date(2026, 10, 15)),
            (date(2026, 10, 16), date(2026, 10, 31)),
        ]
        self.assertEqual(periodos[:4], esperados)

    def test_febrero_de_año_bisiesto(self):
        """El fin de la segunda quincena sale de calendar, no de un 30 fijo."""
        hoy = date(2028, 3, 31)
        prog = self._programacion(periodicity='biweekly', lookback_days=0,
                                  date_start=date(2028, 2, 16))
        with patch.object(fields.Date, 'context_today', return_value=hoy):
            _, hasta = prog._next_period()
        self.assertEqual(calendar.monthrange(2028, 2)[1], 29, "2028 es bisiesto")
        self.assertEqual(hasta, date(2028, 2, 29))

    def test_no_pide_periodos_futuros(self):
        """Cuando ya se cubrió hasta hoy, no se genera nada."""
        hoy = date(2026, 9, 20)
        prog = self._programacion(periodicity='daily', lookback_days=3)
        prog.date_covered_to = hoy
        with patch.object(fields.Date, 'context_today', return_value=hoy):
            self.assertFalse(prog._next_period())

    def test_periodo_se_recorta_a_hoy(self):
        """Un periodo semanal que rebasa hoy se recorta: no se piden días futuros."""
        hoy = date(2026, 9, 12)
        prog = self._programacion(periodicity='weekly', lookback_days=0)
        with patch.object(fields.Date, 'context_today', return_value=hoy):
            desde, hasta = prog._next_period()
        self.assertEqual(desde, date(2026, 9, 10))
        self.assertEqual(hasta, hoy, "se recorta a hoy, no al día 16")
