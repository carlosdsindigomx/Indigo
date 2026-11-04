"""Cruce de los CFDI contra la lista 69-B del SAT.

Existen por dos defectos reales:

  * El cruce solo miraba si el RFC aparecía en la lista, sin ver su situación.
    El listado completo del SAT conserva a los que desvirtuaron la presunción o
    ganaron en tribunales (casi el 14% de sus filas), y sus CFDI salían en rojo
    como «Riesgo 69-B» aunque el SAT ya reconoció sus operaciones como reales.
  * La descarga automática apuntaba a la dirección https del SAT, que no
    responde; la misma dirección por http sí. El cron fallaba cada semana y
    solo lo dejaba en el log.
"""
import base64
from datetime import date
from unittest.mock import MagicMock, patch

import requests

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.dsi_cfdi_vault.models.cfdi_efos_entry import DEFAULT_69B_URLS

DESCARGA = 'odoo.addons.dsi_cfdi_vault.models.cfdi_efos_entry.requests.get'


def csv_sat(filas):
    """Un CSV con la forma del listado completo del SAT: dos renglones antes
    del encabezado y en CP1252."""
    lineas = [
        '"Información actualizada al 31 de diciembre de 2025; los listados son de carácter público"',
        'Listado completo de contribuyentes (Artículo 69-B del CFF),,,',
        'No,RFC,Nombre del Contribuyente,Situación del contribuyente',
    ] + [f'{i},{rfc},"{rfc} S.A. DE C.V.",{situacion}' for i, (rfc, situacion) in enumerate(filas, 1)]
    return "\n".join(lineas).encode('cp1252')


@tagged('post_install', '-at_install')
class TestLista69B(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contador = 0
        cls.efos = cls.env['dsi.cfdi.efos.entry']

    def _cfdi_de(self, rfc_emisor, company=None):
        type(self).contador += 1
        uuid = f"BBBBBBBB-0000-0000-0000-{self.contador:012d}"
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': b'<cfdi/>', 'mimetype': 'text/xml',
        })
        return self.env['cfdi.document'].create({
            'company_id': (company or self.env.company).id,
            'uuid': uuid,
            'direction': 'received',
            'move_type': 'I',
            'invoice_date': date(2026, 6, 15),
            'amount_total': 1000.0,
            'partner_rfc_issuer': rfc_emisor,
            'partner_rfc_receiver': 'EKU9003173C9',
            'xml_attachment_id': adjunto.id,
        })

    def test_solo_presuntos_y_definitivos_son_riesgo(self):
        presunto = self._cfdi_de('PRE010101AA1')
        definitivo = self._cfdi_de('DEF010101AA1')
        desvirtuado = self._cfdi_de('DES010101AA1')
        sentencia = self._cfdi_de('SEN010101AA1')
        limpio = self._cfdi_de('LIM010101AA1')

        self.efos._import_csv_bytes(csv_sat([
            ('PRE010101AA1', 'Presunto'),
            ('DEF010101AA1', 'Definitivo'),
            ('DES010101AA1', 'Desvirtuado'),
            ('SEN010101AA1', 'Sentencia Favorable'),
        ]))

        self.assertTrue(presunto.is_efos_risk)
        self.assertTrue(definitivo.is_efos_risk)
        self.assertFalse(desvirtuado.is_efos_risk)
        self.assertFalse(sentencia.is_efos_risk)
        self.assertFalse(limpio.is_efos_risk)

    def test_el_que_se_desvirtua_deja_de_ser_riesgo_al_recargar(self):
        doc = self._cfdi_de('PRE010101AA1')
        self.efos._import_csv_bytes(csv_sat([('PRE010101AA1', 'Presunto')]))
        self.assertTrue(doc.is_efos_risk)

        self.efos._import_csv_bytes(csv_sat([('PRE010101AA1', 'Desvirtuado')]))
        self.assertFalse(doc.is_efos_risk)

    def test_situacion_desconocida_cuenta_como_riesgo(self):
        doc = self._cfdi_de('OTR010101AA1')
        self.efos._import_csv_bytes(csv_sat([('OTR010101AA1', 'Algo que el SAT agregue después')]))
        self.assertEqual(self.efos.search([('rfc', '=', 'OTR010101AA1')]).situation, 'other')
        self.assertTrue(doc.is_efos_risk)

    def test_el_csv_subido_en_el_asistente_se_lee(self):
        """«Cargar lista 69-B» recibe el archivo en base64, como lo manda el
        navegador. En 18 y 19 se leía el base64 como si fuera el CSV («No se
        encontró una columna de RFC»); en 20 el campo ya lo da decodificado.
        Lo carga un gestor contable con una sola empresa abierta, y aun así se
        recalculan los CFDI de todas: la lista es una sola."""
        empresa_b = self.env['res.company'].create({'name': "Empresa B de prueba"})
        de_b = self._cfdi_de('PRE010101AA1', company=empresa_b)
        gestor = new_test_user(
            self.env, login='gestor-69b', groups='base.group_user,account.group_account_manager',
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])
        asistente = self.env['dsi.cfdi.efos.import.wizard'].with_user(gestor).with_context(
            allowed_company_ids=self.env.company.ids).create({
                'csv_file': base64.b64encode(csv_sat([('PRE010101AA1', 'Presunto')])).decode(),
                'csv_filename': 'Listado_Completo_69-B.csv',
            })

        asistente.action_import()

        self.assertEqual(self.efos.search([]).mapped('rfc'), ['PRE010101AA1'])
        self.assertTrue(de_b.is_efos_risk, "también en la empresa que el gestor no tiene abierta")

    def test_si_https_no_responde_se_descarga_por_http(self):
        respuesta = MagicMock(content=csv_sat([('PRE010101AA1', 'Presunto')]))
        with patch(DESCARGA, side_effect=[requests.ConnectTimeout("sin respuesta"), respuesta]) as descarga:
            resultado = self.efos._refresh_from_url()

        self.assertTrue(resultado['ok'])
        self.assertEqual(resultado['url'], DEFAULT_69B_URLS[1])
        self.assertTrue(resultado['url'].startswith('http://'))
        self.assertEqual(
            [llamada.args[0] for llamada in descarga.call_args_list], list(DEFAULT_69B_URLS))
        self.assertEqual(self.efos.search_count([]), 1)

    def test_una_pagina_que_no_es_el_csv_no_borra_la_lista(self):
        self.efos._import_csv_bytes(csv_sat([('PRE010101AA1', 'Presunto')]))
        mantenimiento = MagicMock(content=b'<html>Sitio en mantenimiento</html>')
        with patch(DESCARGA, return_value=mantenimiento):
            resultado = self.efos._refresh_from_url()

        self.assertFalse(resultado['ok'])
        self.assertEqual(self.efos.search([]).mapped('rfc'), ['PRE010101AA1'])

    def test_url_configurada_se_respeta(self):
        cp = self.env['ir.config_parameter'].sudo()
        url = 'https://espejo.example.com/69b.csv'
        if hasattr(cp, 'set_str'):
            cp.set_str('dsi_cfdi_vault.efos_list_url', url)
        else:
            cp.set_param('dsi_cfdi_vault.efos_list_url', url)
        with patch(DESCARGA, side_effect=requests.ConnectTimeout("sin respuesta")) as descarga:
            resultado = self.efos._refresh_from_url()

        self.assertFalse(resultado['ok'])
        self.assertEqual([llamada.args[0] for llamada in descarga.call_args_list], [url])
