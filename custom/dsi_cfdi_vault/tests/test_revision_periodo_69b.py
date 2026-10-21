"""Revisión de las compras de un periodo contra la lista 69-B.

Existe porque el cruce automático solo cubre los CFDI de la bóveda: una factura
de proveedor capturada a mano en Odoo, sin CFDI en la bóveda, nunca se
comparaba contra la lista. Estas pruebas cuidan qué entra en la revisión (solo
compras, solo el periodo, solo presuntos y definitivos) y que una lista que no
se pudo actualizar no la detenga.
"""
from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_vault.wizards.cfdi_efos_check_wizard import _rfc_normalizado

from .test_lista_69b import csv_sat

DEFINITIVO, PRESUNTO, DESVIRTUADO = 'DEF010101AA1', 'PRE010101AA1', 'DES010101AA1'


@tagged('post_install', '-at_install')
class TestRevisionPeriodo69B(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        if not cls.env['account.journal'].search(
                [('type', '=', 'purchase'), ('company_id', '=', cls.company.id)], limit=1):
            raise cls.skipTest(cls, "Requiere un diario de compras configurado.")
        cls.env['dsi.cfdi.efos.entry']._import_csv_bytes(csv_sat([
            (DEFINITIVO, 'Definitivo'),
            (PRESUNTO, 'Presunto'),
            (DESVIRTUADO, 'Desvirtuado'),
        ]))
        mx = cls.env.ref('base.mx')
        cls.proveedores = {
            rfc: cls.env['res.partner'].create({
                'name': f"Proveedor {rfc}", 'vat': rfc, 'country_id': mx.id, 'is_company': True,
            })
            for rfc in (DEFINITIVO, PRESUNTO, DESVIRTUADO)
        }
        cls.contador = 0

    def _cfdi(self, rfc_emisor, fecha, direction='received'):
        type(self).contador += 1
        uuid = f"AAAAAAAA-0000-0000-0000-{self.contador:012d}"
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': b'<cfdi/>', 'mimetype': 'text/xml',
        })
        return self.env['cfdi.document'].create({
            'company_id': self.company.id,
            'uuid': uuid,
            'direction': direction,
            'move_type': 'I',
            'invoice_date': fecha,
            'amount_total': 1000.0,
            'partner_rfc_issuer': rfc_emisor,
            'partner_rfc_receiver': 'EKU9003173C9',
            'xml_attachment_id': adjunto.id,
        })

    def _factura(self, rfc, fecha, move_type='in_invoice'):
        return self.env['account.move'].create({
            'move_type': move_type,
            'company_id': self.company.id,
            'partner_id': self.proveedores[rfc].id,
            'invoice_date': fecha,
            'invoice_line_ids': [(0, 0, {'name': "Servicio", 'quantity': 1, 'price_unit': 100.0})],
        })

    def _revisar(self, **valores):
        wizard = self.env['dsi.cfdi.efos.check.wizard'].create({
            'company_id': self.company.id,
            'date_from': date(2026, 9, 1),
            'date_to': date(2026, 9, 30),
            'refresh_list': False,
            **valores,
        })
        wizard.action_check()
        return wizard

    def test_encuentra_compras_del_periodo_de_proveedores_en_riesgo(self):
        cfdi_definitivo = self._cfdi(DEFINITIVO, date(2026, 9, 10))
        self._cfdi(DESVIRTUADO, date(2026, 9, 10))                  # ya no es riesgo
        self._cfdi(PRESUNTO, date(2026, 8, 31))                     # fuera del periodo
        self._cfdi(PRESUNTO, date(2026, 9, 10), direction='issued')  # venta, no compra
        factura_definitivo = self._factura(DEFINITIVO, date(2026, 9, 15))
        nota_presunto = self._factura(PRESUNTO, date(2026, 9, 20), move_type='in_refund')
        self._factura(DESVIRTUADO, date(2026, 9, 15))
        self._factura(PRESUNTO, date(2026, 9, 15), move_type='out_invoice')  # venta

        wizard = self._revisar()

        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.cfdi_ids, cfdi_definitivo)
        self.assertEqual(wizard.move_ids, factura_definitivo | nota_presunto)
        self.assertEqual(wizard.cfdi_checked_count, 2)
        self.assertEqual(wizard.move_checked_count, 3)
        # Primero el definitivo, que es el que obliga a corregir.
        self.assertEqual(wizard.line_ids.mapped('rfc'), [DEFINITIVO, PRESUNTO])
        definitivo = wizard.line_ids[0]
        self.assertEqual(definitivo.situation, 'definitivo')
        self.assertEqual((definitivo.cfdi_count, definitivo.move_count), (1, 1))
        self.assertEqual(definitivo.partner_id, self.proveedores[DEFINITIVO])

    def test_los_botones_abren_solo_lo_hallado(self):
        cfdi = self._cfdi(DEFINITIVO, date(2026, 9, 10))
        factura = self._factura(DEFINITIVO, date(2026, 9, 15))
        self._factura(DESVIRTUADO, date(2026, 9, 15))
        wizard = self._revisar()

        self.assertEqual(wizard.action_view_cfdi()['domain'], [('id', 'in', cfdi.ids)])
        accion = wizard.action_view_moves()
        self.assertEqual(accion['domain'], [('id', 'in', factura.ids)])
        # Sin id: si no, al recargar la página se abriría la acción estándar con
        # todas las facturas de proveedor.
        self.assertNotIn('id', accion)
        self.assertEqual(accion['display_name'], accion['name'])

    def test_factura_sin_cfdi_en_la_boveda_tambien_se_revisa(self):
        # Es el hueco que motivó el asistente: capturada a mano, sin XML.
        factura = self._factura(PRESUNTO, date(2026, 9, 5))
        wizard = self._revisar()
        self.assertEqual(wizard.move_ids, factura)
        self.assertFalse(wizard.cfdi_ids)

    def test_si_no_se_puede_actualizar_la_lista_revisa_con_la_cargada(self):
        factura = self._factura(DEFINITIVO, date(2026, 9, 5))
        with patch.object(self.env.registry['dsi.cfdi.efos.entry'], '_refresh_from_url',
                          lambda _modelo: {'ok': False, 'error': "sin respuesta"}):
            wizard = self._revisar(refresh_list=True)
        self.assertIn("sin respuesta", wizard.refresh_error)
        self.assertEqual(wizard.move_ids, factura)

    def test_sin_lista_cargada_avisa(self):
        self.env['dsi.cfdi.efos.entry'].search([]).unlink()
        with self.assertRaises(UserError):
            self._revisar()

    def test_fechas_invertidas(self):
        with self.assertRaises(UserError):
            self._revisar(date_from=date(2026, 9, 30), date_to=date(2026, 9, 1))

    def test_rfc_normalizado(self):
        self.assertEqual(_rfc_normalizado(' mx-def010101aa1 '), DEFINITIVO)
        self.assertEqual(_rfc_normalizado('DEF010101AA1'), DEFINITIVO)
        self.assertEqual(_rfc_normalizado(False), '')
