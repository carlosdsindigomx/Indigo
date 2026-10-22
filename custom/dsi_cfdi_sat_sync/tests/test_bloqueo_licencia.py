"""La descarga del SAT requiere licencia vigente.

Antes no la revisaba nadie: sin activar nada, la descarga automática corría
igual. Sin licencia, ni el botón ni las tareas programadas hablan con el SAT, y
la programación no da el periodo por cubierto (si lo hiciera, al activar la
licencia ese periodo ya no se pediría nunca).
"""
from datetime import date
from unittest.mock import patch

from odoo.exceptions import RedirectWarning
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_license_client.tests.common import token_de_prueba, usar_llave_de_prueba


@tagged('post_install', '-at_install')
class TestBloqueoLicenciaSat(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        usar_llave_de_prueba(cls)
        # Compañía propia: sat.credential es única por compañía.
        cls.company = cls.env['res.company'].create({'name': "Prueba Licencia SAT"})
        cls.credential = cls.env['sat.credential'].create({
            'company_id': cls.company.id, 'rfc': 'AAA010101AAA', 'state': 'validated',
        })
        cls.programacion = cls.env['sat.sync.schedule'].create({
            'company_id': cls.company.id,
            'credential_id': cls.credential.id,
            'date_start': date(2026, 9, 1),
        })
        cls.solicitud = cls.env['sat.download.request'].create({
            'company_id': cls.company.id,
            'credential_id': cls.credential.id,
            'date_from': date(2026, 9, 1),
            'date_to': date(2026, 9, 30),
        })

    def _avances(self):
        return patch.object(self.env.registry['sat.download.request'], '_avanzar_sin_excepciones',
                            autospec=True)

    def _correr_crons(self):
        # Los crons hacen commit entre solicitudes; dentro de una prueba no se puede.
        with patch.object(self.env.cr, 'commit', lambda *args: None):
            self.env['sat.sync.schedule']._cron_generar_solicitudes()
            self.env['sat.sync.schedule']._cron_avanzar_solicitudes()

    def test_sin_licencia_los_botones_no_hablan_con_el_sat(self):
        with self.assertRaises(RedirectWarning) as error:
            self.solicitud.action_autenticar()
        self.assertIn("No se puede descargar CFDI del SAT", str(error.exception))
        self.assertEqual(self.solicitud.state, 'draft')
        with self.assertRaises(RedirectWarning):
            self.programacion.action_generar_ahora()

    def test_sin_licencia_la_descarga_automatica_no_avanza(self):
        with self._avances() as avance:
            self._correr_crons()
        avance.assert_not_called()
        self.assertFalse(self.programacion.request_ids)
        self.assertFalse(self.programacion.date_covered_to)
        self.assertIn("no tiene una licencia", self.programacion.last_error)
        # No es una falla técnica: no dispara el aviso de fallas seguidas.
        self.assertEqual(self.programacion.consecutive_failures, 0)

    def test_con_licencia_la_descarga_automatica_avanza(self):
        self.env['dsi.license.client.state']._get_or_create(self.company).token = \
            token_de_prueba(self.env)
        with self._avances() as avance:
            self._correr_crons()
        self.assertTrue(self.programacion.request_ids)
        self.assertTrue(avance.called)
        self.assertIn(self.solicitud, [llamada.args[0] for llamada in avance.call_args_list])
