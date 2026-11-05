"""El monto de los CFDI de Pago (complemento de pagos, REP).

Existe porque todos los pagos quedaban con Total 0: en un CFDI de Pago el SAT
exige Total="0" y Moneda="XXX" en el comprobante, y lo pagado va en el
complemento. La Bóveda leía el Total del comprobante, tanto al descargar del
SAT como al importar a mano. Al corregirlo hay dos cosas que no deben cambiar:
la consulta de estatus en el SAT pide el Total del comprobante (0), y un pago
no debe ligarse a la factura que paga solo porque coincide el importe.
"""
from datetime import date
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_license_client.tests.common import activar_licencia

RFC_EMISOR = 'PRO010101AA1'
RFC_RECEPTOR = 'EKU9003173C9'


def cfdi_pago_xml(uuid, pagos, totales=None, version='4.0'):
    """Un CFDI de Pago como los que emite el SAT: Total 0 y Moneda XXX en el
    comprobante, lo pagado en el complemento. `pagos` es una lista de
    (monto, moneda, tipo de cambio); `totales` es MontoTotalPagos (Pagos 2.0).
    Con version='3.3' arma Pagos 1.0, que no tiene Totales."""
    if version == '3.3':
        ns_cfdi, ns_pago, prefijo = 'http://www.sat.gob.mx/cfd/3', 'http://www.sat.gob.mx/Pagos', 'pago10'
    else:
        ns_cfdi, ns_pago, prefijo = 'http://www.sat.gob.mx/cfd/4', 'http://www.sat.gob.mx/Pagos20', 'pago20'
    nodo_totales = f'<{prefijo}:Totales MontoTotalPagos="{totales}"/>' if totales is not None else ''
    nodos_pago = ''.join(
        f'<{prefijo}:Pago FechaPago="2026-10-10T12:00:00" FormaDePagoP="03" MonedaP="{moneda}" '
        f'TipoCambioP="{tipo_cambio}" Monto="{monto}"/>'
        for monto, moneda, tipo_cambio in pagos)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="{ns_cfdi}" xmlns:{prefijo}="{ns_pago}"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="{version}" Fecha="2026-10-13T15:07:09" SubTotal="0" Total="0"
                  Moneda="XXX" TipoDeComprobante="P">
  <cfdi:Emisor Rfc="{RFC_EMISOR}" Nombre="Proveedor de Prueba"/>
  <cfdi:Receptor Rfc="{RFC_RECEPTOR}" Nombre="Mi Empresa"/>
  <cfdi:Conceptos>
    <cfdi:Concepto Cantidad="1" ClaveProdServ="84111506" Descripcion="Pago" Importe="0" ValorUnitario="0"/>
  </cfdi:Conceptos>
  <cfdi:Complemento>
    <{prefijo}:Pagos Version="{'1.0' if version == '3.3' else '2.0'}">{nodo_totales}{nodos_pago}</{prefijo}:Pagos>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/>
  </cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


def cfdi_ingreso_xml(uuid, total):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="4.0" Fecha="2026-10-13T15:07:09" SubTotal="{total}" Total="{total}"
                  Moneda="MXN" TipoDeComprobante="I">
  <cfdi:Emisor Rfc="{RFC_EMISOR}" Nombre="Proveedor de Prueba"/>
  <cfdi:Receptor Rfc="{RFC_RECEPTOR}" Nombre="Mi Empresa"/>
  <cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/></cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


@tagged('post_install', '-at_install')
class TestCfdiPago(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.vat = RFC_RECEPTOR
        activar_licencia(cls, cls.company)
        cls.Doc = cls.env['cfdi.document']
        cls.contador = 0

    def _uuid(self):
        type(self).contador += 1
        return f"EEEEEEEE-2222-4000-8000-{self.contador:012d}"

    def _guardar(self, xml, **valores):
        vals = self.Doc._parse_cfdi_xml(xml)
        adjunto = self.env['ir.attachment'].create({
            'name': f"{vals['uuid']}.xml", 'raw': xml, 'mimetype': 'text/xml',
        })
        datos = {k: v for k, v in vals.items() if k != 'parse_state'}
        datos.update({
            'company_id': self.company.id,
            'direction': 'received',
            'xml_attachment_id': adjunto.id,
            'currency_id': vals['currency_id'] or self.company.currency_id.id,
        })
        datos.update(valores)
        return self.Doc.create(datos)

    # -- El monto sale del complemento -----------------------------------

    def test_pagos_2_0_toma_el_monto_total_de_pagos(self):
        vals = self.Doc._parse_cfdi_xml(cfdi_pago_xml(self._uuid(), [(125.22, 'MXN', 1)], totales=125.22))
        self.assertEqual(vals['amount_total'], 125.22)
        self.assertEqual(self.env['res.currency'].browse(vals['currency_id']).name, 'MXN')

    def test_pagos_1_0_suma_los_pagos_en_su_moneda(self):
        xml = cfdi_pago_xml(self._uuid(), [(100.0, 'USD', 18.5), (50.0, 'USD', 18.7)], version='3.3')
        vals = self.Doc._parse_cfdi_xml(xml)
        self.assertEqual(vals['amount_total'], 150.0)
        self.assertEqual(self.env['res.currency'].browse(vals['currency_id']).name, 'USD')

    def test_pagos_1_0_en_varias_monedas_se_suma_en_pesos(self):
        xml = cfdi_pago_xml(self._uuid(), [(100.0, 'USD', 18), (200.0, 'MXN', 1)], version='3.3')
        vals = self.Doc._parse_cfdi_xml(xml)
        self.assertEqual(vals['amount_total'], 2000.0)
        self.assertEqual(self.env['res.currency'].browse(vals['currency_id']).name, 'MXN')

    def test_una_factura_no_cambia(self):
        vals = self.Doc._parse_cfdi_xml(cfdi_ingreso_xml(self._uuid(), '1160.00'))
        self.assertEqual(vals['amount_total'], 1160.0)

    # -- Lo que no debe cambiar --------------------------------------------

    def test_la_consulta_al_sat_manda_el_total_del_comprobante(self):
        pago = self._guardar(cfdi_pago_xml(self._uuid(), [(125.22, 'MXN', 1)], totales=125.22))
        totales_enviados = []

        def consulta(_modelo, _rfc_emisor, _rfc_receptor, total, _uuid):
            totales_enviados.append(total)
            return {'value': 'valid'}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            pago.action_verify_sat_status()
        self.assertEqual(totales_enviados, [0.0])
        self.assertEqual(pago.sat_status, 'valid')

    def test_un_pago_no_se_liga_a_la_factura_por_importe(self):
        if not self.env['account.journal'].search(
                [('type', '=', 'purchase'), ('company_id', '=', self.company.id)], limit=1):
            self.skipTest("Requiere un diario de compras configurado.")
        proveedor = self.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'country_id': self.env.ref('base.mx').id,
        })
        factura = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': proveedor.id,
            'invoice_date': date(2026, 10, 13),
            'invoice_line_ids': [(0, 0, {'name': "Servicio", 'quantity': 1, 'price_unit': 125.22, 'tax_ids': []})],
        })
        factura.action_post()

        pago = self._guardar(cfdi_pago_xml(self._uuid(), [(125.22, 'MXN', 1)], totales=125.22))
        pago.action_match_automatically()
        self.assertFalse(pago.move_id)

        # Un CFDI de Ingreso con los mismos datos sí se liga: lo que se excluye
        # es el pago, no la conciliación por importe.
        ingreso = self._guardar(cfdi_ingreso_xml(self._uuid(), '125.22'))
        ingreso.action_match_automatically()
        self.assertEqual(ingreso.move_id, factura)

    def test_los_pagos_guardados_en_cero_se_corrigen(self):
        pago = self._guardar(cfdi_pago_xml(self._uuid(), [(125.22, 'MXN', 1)], totales=125.22),
                             amount_total=0.0)
        self.assertEqual(pago.amount_total, 0.0)
        self.Doc._recompute_payment_amounts()
        self.assertEqual(pago.amount_total, 125.22)
