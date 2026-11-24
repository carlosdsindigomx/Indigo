"""Pruebas de cuándo y qué pide la descarga automática.

Dos fallas reales las motivaron. La primera versión aplicaba los días de
recuperación a la frontera de avance: la programación se quedaba clavada en el
primer periodo, remandando el mismo rango al SAT todos los días. Después, cada
corrida pedía un solo periodo: con «Diaria» y «Traer desde» en el pasado nunca
se ponía al día (iba siempre igual de atrás), y las pruebas no lo vieron porque
simulaban las corridas sin que avanzara el día. Aquí cada corrida es un día
distinto (_dia_a_dia).

Ahora «Traer desde» siempre se pone al día (hasta hoy) en una sola corrida, y
la periodicidad dice cada cuánto vuelve al SAT después.
"""
import calendar
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_sat_sync.models import sat_sync_schedule
from odoo.addons.dsi_cfdi_sat_sync.models.sat_download_request import ErrorDeAutenticacion
from odoo.addons.dsi_license_client.tests.common import activar_licencia

LOG_SOLICITUD = 'odoo.addons.dsi_cfdi_sat_sync.models.sat_download_request'
HOY = date(2026, 11, 23)


def _periodos(solicitudes):
    return [(solicitud.date_from, solicitud.date_to) for solicitud in solicitudes]


def _dias(desde, cuantos):
    return [desde + timedelta(days=n) for n in range(cuantos)]


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
        activar_licencia(cls, company=cls.company)

    def _programacion(self, **kwargs):
        valores = {
            'company_id': self.company.id,
            'credential_id': self.credential.id,
            'date_start': date(2026, 9, 1),
            # Una solicitud por periodo (recibidos, XML): los periodos se leen
            # directo de las solicitudes.
            'sync_issued': False,
            'download_metadata': False,
        }
        valores.update(kwargs)
        return self.env['sat.sync.schedule'].create(valores)

    def _hoy(self, dia):
        return patch.object(fields.Date, 'context_today', return_value=dia)

    def _generar(self, programacion, dia, forzar=False):
        """Lo que crea la tarea diaria ese día (o el botón, con `forzar`)."""
        with self._hoy(dia):
            return programacion._generar_solicitudes(forzar=forzar)

    def _boton(self, programacion, dia):
        with self._hoy(dia):
            accion = programacion.action_generar_ahora()
        return self.env['sat.download.request'].search(accion['domain'], order='id')

    def _dia_a_dia(self, programacion, desde, hasta):
        """Corre la tarea diaria cada día, de `desde` a `hasta`. Devuelve
        {día: periodos pedidos} de los días en que fue al SAT."""
        visitas = {}
        dia = desde
        while dia <= hasta:
            creadas = self._generar(programacion, dia)
            if creadas:
                visitas[dia] = _periodos(creadas)
            dia += timedelta(days=1)
        return visitas

    def _disparos(self, tarea):
        cron = self.env.ref(tarea)
        return self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])

    def test_se_pone_al_dia_de_una_vez_sea_cual_sea_la_periodicidad(self):
        """Del 1 de septiembre a hoy en una sola corrida, sin huecos ni días
        repetidos. La primera vez no hay recuperación: nada se pidió antes."""
        esperados = {'daily': 84, 'weekly': 12, 'biweekly': 6}
        for periodicidad, cuantos in esperados.items():
            with self.subTest(periodicidad=periodicidad):
                programacion = self._programacion(periodicity=periodicidad)
                creadas = self._generar(programacion, HOY)
                periodos = _periodos(creadas)
                self.assertEqual(len(periodos), cuantos)
                self.assertEqual(periodos[0][0], date(2026, 9, 1))
                self.assertEqual(periodos[-1][1], HOY, "hasta hoy, como vaya el día")
                for (_desde, hasta), (desde, _hasta) in zip(periodos, periodos[1:]):
                    self.assertEqual(desde, hasta + timedelta(days=1), "ni huecos ni días repetidos")
                self.assertEqual((programacion.date_covered_from, programacion.date_covered_to),
                                 (date(2026, 9, 1), HOY))
                self.assertEqual(set(creadas.mapped('state')), {'draft'})

    def test_las_quincenas_son_naturales_y_febrero_sale_del_calendario(self):
        programacion = self._programacion(periodicity='biweekly', date_start=date(2028, 2, 1))
        self.assertEqual(calendar.monthrange(2028, 2)[1], 29, "2028 es bisiesto")
        self.assertEqual(_periodos(self._generar(programacion, date(2028, 3, 20))), [
            (date(2028, 2, 1), date(2028, 2, 15)),
            (date(2028, 2, 16), date(2028, 2, 29)),
            (date(2028, 3, 1), date(2028, 3, 15)),
            (date(2028, 3, 16), date(2028, 3, 20)),
        ])

    def test_ya_al_dia_vuelve_al_sat_segun_la_periodicidad(self):
        """Diaria, cada día; semanal, a los siete días; quincenal, el 1 y el 16.
        Cada vez trae lo que falta hasta ese día y empieza con los días de
        recuperación (3 por omisión). La quincenal pide la quincena que terminó
        y, aparte, lo que va de la nueva."""
        ultimo_dia = date(2027, 1, 20)
        casos = {
            'daily': (_dias(HOY, (ultimo_dia - HOY).days + 1), 1,
                      {date(2026, 11, 24): [(date(2026, 11, 21), date(2026, 11, 24))]}),
            'weekly': ([HOY + timedelta(days=7 * n) for n in range(9)], 1,
                       {date(2026, 11, 30): [(date(2026, 11, 21), date(2026, 11, 30))]}),
            'biweekly': ([HOY, date(2026, 12, 1), date(2026, 12, 16), date(2027, 1, 1), date(2027, 1, 16)], 2,
                         {date(2026, 12, 1): [(date(2026, 11, 21), date(2026, 11, 30)),
                                              (date(2026, 12, 1), date(2026, 12, 1))],
                          date(2026, 12, 16): [(date(2026, 11, 29), date(2026, 12, 15)),
                                               (date(2026, 12, 16), date(2026, 12, 16))]}),
        }
        for periodicidad, (dias, por_ida, ejemplos) in casos.items():
            with self.subTest(periodicidad=periodicidad):
                programacion = self._programacion(periodicity=periodicidad)
                visitas = self._dia_a_dia(programacion, HOY, ultimo_dia)
                self.assertEqual(list(visitas), dias)
                for dia, periodos in list(visitas.items())[1:]:
                    self.assertEqual(len(periodos), por_ida, f"{dia}: periodos de la ida")
                    self.assertEqual(periodos[-1][1], dia, f"{dia}: hasta ese día")
                for dia, periodos in ejemplos.items():
                    self.assertEqual(visitas[dia], periodos)
                # Sin huecos: con la recuperación los periodos se enciman, así
                # que cada uno se mide contra todo lo pedido antes que él.
                pedidos = sorted(periodo for periodos in visitas.values() for periodo in periodos)
                cubierto = pedidos[0][1]
                for desde, hasta in pedidos[1:]:
                    self.assertLessEqual(desde, cubierto + timedelta(days=1), "sin huecos")
                    cubierto = max(cubierto, hasta)
                self.assertEqual(cubierto, dias[-1])
                self.assertEqual(programacion.date_covered_to, dias[-1])

    def test_la_recuperacion_solo_ensancha_la_primera_solicitud(self):
        """Tras días sin ir al SAT (licencia vencida, servidor apagado), la
        primera vuelve tres días sobre lo cubierto y las demás no se enciman.
        Nunca antes de «Traer desde», y con 0 repite igual el último día, que
        se pidió antes de que terminara."""
        programacion = self._programacion(date_start=date(2026, 11, 1))
        programacion.date_covered_to = date(2026, 11, 15)
        self.assertEqual(_periodos(self._generar(programacion, HOY)), [
            (date(2026, 11, 13), date(2026, 11, 16)),
            *((dia, dia) for dia in _dias(date(2026, 11, 17), 7)),
        ])

        sin_recuperacion = self._programacion(date_start=date(2026, 11, 1), lookback_days=0)
        sin_recuperacion.date_covered_to = date(2026, 11, 20)
        self.assertEqual(_periodos(self._generar(sin_recuperacion, HOY))[0],
                         (date(2026, 11, 20), date(2026, 11, 21)))

        recien_empezada = self._programacion(date_start=date(2026, 11, 20))
        recien_empezada.date_covered_to = date(2026, 11, 20)
        self.assertEqual(_periodos(self._generar(recien_empezada, HOY))[0],
                         (date(2026, 11, 20), date(2026, 11, 21)))

    def test_pide_hoy_pero_nunca_dias_futuros(self):
        hoy_mismo = self._programacion(date_start=HOY)
        self.assertEqual(_periodos(self._generar(hoy_mismo, HOY)), [(HOY, HOY)])
        futura = self._programacion(date_start=HOY + timedelta(days=10))
        self.assertFalse(self._generar(futura, HOY))
        self.assertFalse(self._generar(futura, HOY, forzar=True))
        self.assertFalse(futura.date_covered_to)
        with self.assertRaisesRegex(UserError, "Todavía no hay nada que pedir"):
            self._boton(futura, HOY)

    def test_el_boton_no_espera_y_vuelve_a_pedir_lo_de_hoy_sin_repetir(self):
        """El botón pide aunque no le toque ir al SAT. Si ya estaba pedido hasta
        hoy, vuelve a pedir los últimos días para traer lo que se timbró
        después, sin repetir nunca las fechas de una solicitud anterior."""
        programacion = self._programacion(periodicity='weekly', date_start=date(2026, 11, 1))
        programacion.date_covered_to = date(2026, 11, 19)
        self.assertFalse(self._generar(programacion, HOY), "a la semana le faltan días: la tarea no va")
        self.assertEqual(_periodos(self._boton(programacion, HOY)), [(date(2026, 11, 17), HOY)])
        self.assertEqual(programacion.date_covered_to, HOY)
        self.assertEqual(_periodos(self._boton(programacion, HOY)), [(date(2026, 11, 21), HOY)])
        self.assertEqual(_periodos(self._boton(programacion, HOY)), [(date(2026, 11, 20), HOY)])

        veces = 0
        with self.assertRaisesRegex(UserError, "ya se volvió a pedir varias veces"):
            while veces < 20:
                self._boton(programacion, HOY)
                veces += 1
        self.assertEqual(veces, 5, "los 8 inicios posibles menos los 3 ya pedidos")
        fechas = _periodos(programacion.request_ids)
        self.assertEqual(len(fechas), len(set(fechas)), "ninguna solicitud repite fechas")
        self.assertEqual(programacion.date_covered_to, HOY)

    def test_mover_traer_desde_antes_pide_solo_lo_que_falta(self):
        programacion = self._programacion(date_start=date(2026, 11, 1))
        self._generar(programacion, HOY)
        self.assertEqual((programacion.date_covered_from, programacion.date_covered_to),
                         (date(2026, 11, 1), HOY))

        programacion.date_start = date(2026, 10, 1)
        with self._hoy(HOY):
            self.assertEqual(programacion.date_next_visit, HOY, "lo que falta se pide ya")
        self.assertEqual(_periodos(self._generar(programacion, HOY)),
                         [(dia, dia) for dia in _dias(date(2026, 10, 1), 31)])
        self.assertEqual((programacion.date_covered_from, programacion.date_covered_to),
                         (date(2026, 10, 1), HOY))

        programacion.date_start = date(2026, 10, 15)
        self.assertFalse(self._generar(programacion, HOY), "moverla después no repite nada")
        self.assertEqual(programacion.date_covered_from, date(2026, 10, 1))

    def test_las_que_ya_iban_al_dia_tambien_piden_lo_que_falta(self):
        """Las programaciones de antes no tienen «Cubierto desde»: empezaron en
        su «Traer desde», y eso se anota al moverlo."""
        programacion = self._programacion(date_start=date(2026, 11, 1))
        programacion.date_covered_to = HOY
        programacion.date_start = date(2026, 10, 25)
        self.assertEqual(programacion.date_covered_from, date(2026, 11, 1))
        self.assertEqual(_periodos(self._generar(programacion, HOY)),
                         [(dia, dia) for dia in _dias(date(2026, 10, 25), 7)])

    def test_sin_nada_que_pedir_no_mueve_lo_cubierto(self):
        programacion = self._programacion(sync_received=False)
        self.assertFalse(self._generar(programacion, HOY))
        self.assertFalse(programacion.date_covered_to)
        with self.assertRaisesRegex(UserError, "No hay nada que pedir"):
            self._boton(programacion, HOY)
        programacion.sync_received = True
        self.assertEqual(self._generar(programacion, HOY)[0].date_from, date(2026, 9, 1))

    def test_guardar_lanza_la_descarga_y_crear_lanza_el_envio(self):
        """Crear la programación o cambiar lo que le toca pedir lanza la tarea
        que genera las solicitudes, sin esperar a su corrida diaria. Generarlas
        no habla con el SAT: lanza la tarea que las manda."""
        generar, avanzar = sat_sync_schedule.TAREA_DE_GENERAR, sat_sync_schedule.TAREA_DE_AVANZAR
        antes = self._disparos(generar)
        programacion = self._programacion()
        self.assertEqual(self._disparos(generar), antes + 1, "al crearla")
        programacion.periodicity = 'weekly'
        self.assertEqual(self._disparos(generar), antes + 2, "al cambiar la periodicidad")
        programacion.lookback_days = 5
        self.assertEqual(self._disparos(generar), antes + 2, "lo demás no la lanza")

        envios = self._disparos(avanzar)
        with patch.object(self.env.registry['sat.download.request'], '_avanzar_sin_excepciones') as avance:
            self.assertTrue(self._generar(programacion, HOY))
        avance.assert_not_called()
        self.assertEqual(self._disparos(avanzar), envios + 1)

    def test_siguiente_ida_al_sat(self):
        casos = {'daily': date(2026, 11, 24), 'weekly': date(2026, 11, 30), 'biweekly': date(2026, 12, 1)}
        for periodicidad, esperada in casos.items():
            with self.subTest(periodicidad=periodicidad):
                programacion = self._programacion(periodicity=periodicidad)
                with self._hoy(HOY):
                    self.assertEqual(programacion.date_next_visit, HOY, "sin ponerse al día: hoy")
                self._generar(programacion, HOY)
                programacion.invalidate_recordset(['date_next_visit'])
                with self._hoy(HOY):
                    self.assertEqual(programacion.date_next_visit, esperada)


@tagged('post_install', '-at_install')
class TestAvanzarEnTandas(TransactionCase):
    """La tarea de cada hora manda, verifica y descarga en tandas: al ponerse
    al día pueden ser cientos de solicitudes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.credenciales = cls.env['sat.credential']
        for nombre in ("Tandas A", "Tandas B"):
            empresa = cls.env['res.company'].create({'name': nombre})
            cls.credenciales |= cls.env['sat.credential'].create({
                'company_id': empresa.id, 'rfc': 'AAA010101AAA', 'state': 'validated'})
            activar_licencia(cls, company=empresa)

    def _solicitud(self, credencial, estado='draft', consultada_hace=None):
        solicitud = self.env['sat.download.request'].create({
            'company_id': credencial.company_id.id,
            'credential_id': credencial.id,
            'date_from': date(2026, 9, 1),
            'date_to': date(2026, 9, 1),
            'state': estado,
        })
        if consultada_hace is not None:
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE sat_download_request SET write_date = %s WHERE id = %s",
                [fields.Datetime.now() - timedelta(minutes=consultada_hace), solicitud.id])
            solicitud.invalidate_recordset(['write_date'])
        return solicitud

    def _correr(self, sin_respuesta=()):
        """Corre la tarea; devuelve las solicitudes de estas pruebas que intentó,
        en orden. Las de `sin_respuesta` hacen como si el SAT no contestara."""
        intentadas = []

        def avanzar(solicitud):
            intentadas.append(solicitud)
            return solicitud not in sin_respuesta

        with patch.object(self.env.registry['sat.download.request'], '_avanzar_sin_excepciones', avanzar):
            self.env['sat.sync.schedule']._cron_avanzar_solicitudes()
        return [solicitud for solicitud in intentadas if solicitud.credential_id in self.credenciales]

    def test_primero_manda_las_nuevas_y_no_consulta_a_cada_rato(self):
        a = self.credenciales[0]
        vieja = self._solicitud(a, 'verifying', consultada_hace=45)
        reciente = self._solicitud(a, 'verifying', consultada_hace=5)
        nueva_1 = self._solicitud(a)
        nueva_2 = self._solicitud(a, 'authenticated')
        self.assertEqual(self._correr(), [nueva_1, nueva_2, vieja])
        self.assertNotIn(reciente, self._correr(), "la consultada hace poco espera su turno")

    def test_si_el_sat_no_contesta_deja_las_demas_de_esa_efirma(self):
        a, b = self.credenciales
        a_1, a_2, b_1 = self._solicitud(a), self._solicitud(a), self._solicitud(b)
        self.assertEqual(self._correr(sin_respuesta=[a_1]), [a_1, b_1])
        self.assertNotIn(a_2, self._correr(sin_respuesta=[a_1]), "sigue esperando a la siguiente corrida")

    def test_avisa_si_el_sat_no_contesta_o_no_acepta_la_efirma(self):
        """Lo que usa la tarea para dejar para después las demás de esa e.firma.
        Un rechazo de la solicitud misma no: es de ella, no de la e.firma."""
        solicitud = self._solicitud(self.credenciales[0])
        casos = [
            (ErrorDeAutenticacion("El SAT rechazó la autenticación"), False),
            (UserError("El SAT rechazó la solicitud de descarga"), True),
            (ConnectionError("sin red"), False),
        ]
        for error, esperado in casos:
            with self.subTest(error=type(error).__name__), patch.object(
                    self.env.registry['sat.download.request'], 'action_autenticar', side_effect=error):
                if isinstance(error, UserError):
                    self.assertIs(solicitud._avanzar_sin_excepciones(), esperado)
                else:
                    with self.assertLogs(LOG_SOLICITUD, level='ERROR'):
                        self.assertIs(solicitud._avanzar_sin_excepciones(), esperado)

    def test_trabaja_en_tandas_y_lanza_la_siguiente(self):
        for credencial in self.credenciales:
            self._solicitud(credencial)
        cron = self.env.ref(sat_sync_schedule.TAREA_DE_AVANZAR)
        Disparo = self.env['ir.cron.trigger']
        antes = Disparo.search([('cron_id', '=', cron.id)])
        intentadas = []

        def avanzar(solicitud):
            intentadas.append(solicitud)
            return True

        with patch.object(sat_sync_schedule, 'SEGUNDOS_POR_TANDA', -1), \
                patch.object(self.env.registry['sat.download.request'], '_avanzar_sin_excepciones', avanzar):
            self.env['sat.sync.schedule']._cron_avanzar_solicitudes()
        self.assertEqual(len(intentadas), 1, "se acabó el tiempo de la tanda tras la primera")
        siguiente = Disparo.search([('cron_id', '=', cron.id)]) - antes
        self.assertEqual(len(siguiente), 1)
        espera = siguiente.call_at - fields.Datetime.now()
        self.assertTrue(timedelta(minutes=1) < espera <= timedelta(minutes=sat_sync_schedule.MINUTOS_ENTRE_TANDAS))
