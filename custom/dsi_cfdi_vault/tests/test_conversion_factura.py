"""Reglas de conversión de un CFDI en factura de proveedor.

Existen por tres defectos reales que la auditoría encontró en el botón:

  * `move_type` estaba cableado a 'in_invoice', así que un CFDI de Egreso
    (nota de crédito del proveedor) se registraba como factura por pagar e
    invertía el signo de la cuenta por pagar.
  * No se filtraba por tipo: un CFDI de Pago o de Nómina se podía convertir
    en una factura espuria de total 0, porque sus conceptos vienen en cero.
  * TipoFactor="Exento" se descartaba sin impuesto Y sin aviso. Como exento
    aporta cero, la verificación de totales tampoco lo detectaba: la factura
    cuadraba al centavo con el tratamiento fiscal equivocado.

El tipo de defecto es siempre el mismo y es el peor: la factura sale cuadrada
y nadie se entera.
"""
import base64
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_license_client.tests.common import activar_licencia

RFC_EMISOR = 'PRO010101AA1'
RFC_RECEPTOR = 'EKU9003173C9'


def cfdi_xml(tipo='I', total='1000.00', impuestos_xml='', fecha=date(2026, 6, 15)):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="4.0" Fecha="{fecha.isoformat()}T12:00:00"
                  Total="{total}" SubTotal="{total}" Moneda="MXN"
                  TipoDeComprobante="{tipo}" MetodoPago="PUE" FormaPago="03">
  <cfdi:Emisor Rfc="{RFC_EMISOR}" Nombre="Proveedor de Prueba"/>
  <cfdi:Receptor Rfc="{RFC_RECEPTOR}" Nombre="Mi Empresa"/>
  <cfdi:Conceptos>
    <cfdi:Concepto Descripcion="Servicio profesional" Cantidad="1"
                   ValorUnitario="{total}" Importe="{total}">
      {impuestos_xml}
    </cfdi:Concepto>
  </cfdi:Conceptos>
  <cfdi:Complemento>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{{uuid}}"/>
  </cfdi:Complemento>
</cfdi:Comprobante>'''


IMPUESTO_IVA16 = '''<cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Base="1000.00" Impuesto="002" TipoFactor="Tasa"
                         TasaOCuota="0.160000" Importe="160.00"/>
        </cfdi:Traslados>
      </cfdi:Impuestos>'''

IMPUESTO_EXENTO = '''<cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Base="1000.00" Impuesto="002" TipoFactor="Exento"/>
        </cfdi:Traslados>
      </cfdi:Impuestos>'''

IMPUESTO_CUOTA = '''<cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Base="1000.00" Impuesto="003" TipoFactor="Cuota"
                         TasaOCuota="1.9900" Importe="19.90"/>
        </cfdi:Traslados>
      </cfdi:Impuestos>'''


@tagged('post_install', '-at_install')
class TestConversionFactura(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.vat = RFC_RECEPTOR
        # Convertir a factura requiere licencia vigente.
        activar_licencia(cls, cls.company)
        cls.journal = cls.env['account.journal'].search(
            [('type', '=', 'purchase'), ('company_id', '=', cls.company.id)], limit=1)
        if not cls.journal:
            raise cls.skipTest(cls, "Requiere un diario de compras configurado.")
        cls.proveedor = cls.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1,
        })
        cls.contador = 0

    def _documento(self, tipo='I', total='1000.00', impuestos_xml='', sat_status='valid'):
        type(self).contador += 1
        uuid = f"CCCCCCCC-0000-0000-0000-{self.contador:012d}"
        xml = cfdi_xml(tipo=tipo, total=total, impuestos_xml=impuestos_xml).replace('{uuid}', uuid)
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': xml.encode(), 'mimetype': 'text/xml',
        })
        doc = self.env['cfdi.document'].create({
            'company_id': self.company.id,
            'direction': 'received',
            'uuid': uuid,
            'move_type': tipo,
            'invoice_date': date(2026, 6, 15),
            'amount_total': float(total),
            'currency_id': self.company.currency_id.id,
            'partner_rfc_issuer': RFC_EMISOR,
            'partner_name_issuer': "Proveedor de Prueba",
            'partner_rfc_receiver': RFC_RECEPTOR,
            'sat_status': sat_status,
            'xml_attachment_id': adjunto.id,
        })
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _convertir(self, doc):
        return doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)

    # -- Regresión: el tipo de comprobante decide el tipo de factura ----

    def test_egreso_se_registra_como_nota_de_credito(self):
        """Regresión: un CFDI de Egreso se registraba como factura por pagar.

        Eso invierte el signo del saldo con el proveedor: en vez de reducir lo
        que le debes, lo aumenta.
        """
        factura = self._convertir(self._documento(tipo='E'))
        self.assertEqual(factura.move_type, 'in_refund',
                         "un CFDI de Egreso es una nota de crédito del proveedor")

    def test_ingreso_se_registra_como_factura(self):
        factura = self._convertir(self._documento(tipo='I'))
        self.assertEqual(factura.move_type, 'in_invoice')

    # -- Regresión: lo que NO es cuenta por pagar se rechaza ------------

    def test_rechaza_pago_nomina_y_traslado(self):
        """Regresión: estos creaban una factura espuria de total 0."""
        for tipo in ('P', 'N', 'T'):
            with self.subTest(tipo=tipo):
                doc = self._documento(tipo=tipo)
                with self.assertRaises(UserError):
                    self._convertir(doc)
                self.assertFalse(doc.move_id, "no debe quedar ninguna factura creada")

    def test_rechaza_cancelado_en_el_sat(self):
        """Registrarlo crearía justo la discrepancia que el módulo detecta."""
        doc = self._documento(sat_status='canceled')
        with self.assertRaises(UserError):
            self._convertir(doc)
        self.assertFalse(doc.move_id)

    def test_rechaza_el_ya_vinculado(self):
        doc = self._documento()
        self._convertir(doc)
        with self.assertRaises(UserError):
            self._convertir(doc)

    def test_rechaza_el_emitido_por_nosotros(self):
        doc = self._documento()
        doc.direction = 'issued'
        with self.assertRaises(UserError):
            self._convertir(doc)

    # -- La validación ocurre ANTES de crear nada -----------------------

    def test_nada_se_crea_cuando_se_rechaza(self):
        """Si la validación falla, no debe quedar basura a medias."""
        antes = self.env['account.move'].search_count([('company_id', '=', self.company.id)])
        for tipo in ('P', 'N', 'T'):
            with self.assertRaises(UserError):
                self._convertir(self._documento(tipo=tipo))
        despues = self.env['account.move'].search_count([('company_id', '=', self.company.id)])
        self.assertEqual(antes, despues, "no debió crearse ninguna factura")

    # -- Impuestos con tratamiento especial -----------------------------

    def test_exento_no_se_descarta_en_silencio(self):
        """Regresión: Exento quedaba sin impuesto y SIN aviso.

        Exento y tasa 0% aportan los dos cero pesos, pero van a renglones
        distintos del reporte fiscal. O se resuelve, o se avisa.
        """
        doc = self._documento(impuestos_xml=IMPUESTO_EXENTO)
        impuestos, avisos = doc._impuestos_odoo_de_concepto(
            doc._parse_conceptos()[0], self.company)
        self.assertTrue(
            impuestos or avisos,
            "o se mapea a un impuesto con factor Exento, o se avisa; callar no es opción")
        if impuestos:
            self.assertEqual(impuestos.l10n_mx_factor_type, 'Exento')
            self.assertEqual(impuestos.amount, 0.0)

    def test_cuota_avisa_en_vez_de_multiplicar_por_cien(self):
        """Cuota es importe fijo por unidad, no porcentaje."""
        doc = self._documento(impuestos_xml=IMPUESTO_CUOTA)
        impuestos, avisos = doc._impuestos_odoo_de_concepto(
            doc._parse_conceptos()[0], self.company)
        self.assertFalse(impuestos, "no debe inventar un impuesto porcentual")
        self.assertTrue(avisos, "debe avisar que hay que capturarlo a mano")

    # -- Señal de revisión visible --------------------------------------

    def test_marca_para_revision_cuando_hay_avisos(self):
        """Un aviso solo en el chatter es fácil de no leer."""
        doc = self._documento(impuestos_xml=IMPUESTO_CUOTA)
        factura = self._convertir(doc)
        if 'checked' in factura._fields:
            self.assertFalse(factura.checked,
                             "la factura con avisos debe quedar marcada por revisar")

    # -- Lo esencial que no debe perderse -------------------------------

    def test_la_factura_nace_en_borrador(self):
        factura = self._convertir(self._documento())
        self.assertEqual(factura.state, 'draft', "nunca se contabiliza sola")

    def test_conserva_la_descripcion_del_concepto(self):
        """La Descripcion del CFDI es lo que lee el auditor."""
        factura = self._convertir(self._documento())
        self.assertEqual(factura.invoice_line_ids[0].name, "Servicio profesional")

    def test_vincula_el_comprobante_con_la_factura(self):
        doc = self._documento()
        factura = self._convertir(doc)
        self.assertEqual(doc.move_id, factura)
        self.assertEqual(doc.match_state, 'manual')

    # -- La ruta directa del botón (sin diálogo) -------------------------

    def test_boton_crea_el_borrador_sin_preguntar_nada(self):
        """El botón resuelve diario y proveedor solo, y deja el borrador."""
        doc = self._documento(impuestos_xml=IMPUESTO_IVA16)
        accion = doc.action_crear_factura_proveedor()

        self.assertTrue(doc.move_id, "debe haber creado la factura")
        self.assertEqual(doc.move_id.state, 'draft')
        self.assertEqual(doc.move_id.journal_id.type, 'purchase',
                         "el diario de compras se resuelve solo")
        self.assertEqual(doc.move_id.partner_id.vat, RFC_EMISOR,
                         "el proveedor sale del propio comprobante")
        self.assertEqual(accion['res_model'], 'account.move')
        self.assertEqual(accion['res_id'], doc.move_id.id,
                         "con un solo comprobante abre esa factura")

    def test_boton_da_de_alta_al_proveedor_desconocido(self):
        """Sin diálogo que lo pregunte, el alta ocurre con los datos del XML."""
        rfc_nuevo = 'XNU010101XX1'
        doc = self._documento()
        doc.partner_rfc_issuer = rfc_nuevo
        doc.partner_name_issuer = "Proveedor Recién Llegado"

        doc.action_crear_factura_proveedor()

        proveedor = doc.move_id.partner_id
        self.assertEqual(proveedor.vat, rfc_nuevo)
        self.assertEqual(proveedor.name, "Proveedor Recién Llegado",
                         "se usa el nombre del comprobante, no el RFC")

    def test_boton_en_lote_abre_la_lista(self):
        docs = self._documento() | self._documento() | self._documento()
        accion = docs.action_crear_factura_proveedor()

        self.assertEqual(len(docs.mapped('move_id')), 3)
        self.assertEqual(accion['view_mode'], 'list,form',
                         "con varios comprobantes abre la lista")
        self.assertNotIn('res_id', accion)

    def test_boton_en_lote_omite_lo_no_convertible_y_crea_el_resto(self):
        """Un CFDI de Pago en la selección no debe frenar a los demás."""
        bueno_1, bueno_2 = self._documento(), self._documento()
        pago = self._documento(tipo='P')

        (bueno_1 | pago | bueno_2).action_crear_factura_proveedor()

        self.assertTrue(bueno_1.move_id, "el convertible se creó")
        self.assertTrue(bueno_2.move_id, "el convertible se creó")
        self.assertFalse(pago.move_id, "el CFDI de Pago se omitió")

    def test_boton_aplica_el_mapeo_recordado_del_proveedor(self):
        """Sin diálogo que lo precargue, el mapeo recordado se aplica solo."""
        cuenta = self.env['account.account'].search(
            [('account_type', '=', 'expense'), ('company_ids', 'in', self.company.ids)], limit=1)
        self.proveedor.cfdi_account_id = cuenta

        doc = self._documento()
        doc.action_crear_factura_proveedor()

        self.assertEqual(doc.move_id.invoice_line_ids[0].account_id, cuenta)

    # -- El clasificador que decide qué se convierte ---------------------

    def test_clasificador_separa_y_explica(self):
        docs = (self._documento(tipo='I') | self._documento(tipo='P')
                | self._documento(tipo='N') | self._documento(sat_status='canceled'))
        convertibles, descartados = docs._clasificar_para_factura()
        self.assertEqual(len(convertibles), 1, "solo el de Ingreso es convertible")
        self.assertEqual(sum(len(d) for d in descartados.values()), 3)
        self.assertTrue(all(motivo for motivo in descartados), "cada descarte lleva motivo")
