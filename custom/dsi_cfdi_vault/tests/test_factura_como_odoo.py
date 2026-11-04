"""La factura creada desde la Bóveda queda como si Odoo hubiera importado el XML.

Existe porque la conversión armaba la factura y creaba el documento CFDI a
mano, y se quedaba sin lo que el import de Odoo sí pone: forma de pago, uso
CFDI, método de pago, tipo de cambio, objeto de impuesto y la marca de partida
importada. Sin esa marca, elegir después un producto en la partida le pisaba el
precio y los impuestos del comprobante con los del catálogo. Ahora el documento
CFDI lo registra el mismo método de Odoo que corre al subir el XML en una
factura, y la Bóveda solo conserva su mapeo de impuestos y cuentas.

También cubre lo que se revisa antes de crear: un CFDI emitido a otro RFC no se
convierte, y uno cuyo folio fiscal ya está en una factura de Odoo se vincula a
ella en lugar de duplicarla (pero no a una cancelada, ni a la de otra empresa
con el mismo RFC).

Usa una empresa mexicana en pesos con el catálogo de México, como
test_folio_fiscal.py: Odoo 20 solo calcula los campos CFDI en esas empresas.
"""
import re
from datetime import date
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import RFC_EMISOR, RFC_RECEPTOR

DECODER = '_l10n_mx_edi_import_cfdi_invoice'
MODULO_CONVERSION = 'odoo.addons.dsi_cfdi_vault.models.cfdi_to_bill'
OTRO_RFC = 'OTR010101AB1'
PDF = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n'


def cfdi_xml(uuid, moneda='MXN', tipo_cambio='', rfc_receptor=RFC_RECEPTOR, objeto_imp='02', prefijo=True,
             forma_pago='04', metodo_pago='PUE', cantidad='1', valor_unitario='1000.00', importe='1000.00',
             descuento=''):
    """CFDI 4.0 de un concepto con IVA 16% y lo que Odoo lee de la cabecera.
    Por omisión, 1,000 + IVA = 1,160.

    Forma de pago 04 y uso G01 a propósito: no son lo que Odoo pone por
    omisión (transferencia y G03), así que si llegan a la factura es porque
    salieron del XML. Sin `prefijo` la raíz declara el espacio de nombres por
    omisión, como lo emiten algunos sistemas, y Odoo ya no lo reconoce como CFDI.
    """
    p, ns = ('cfdi:', ':cfdi') if prefijo else ('', '')
    tc = f' TipoCambio="{tipo_cambio}"' if tipo_cambio else ''
    base = float(importe) - float(descuento or 0)
    iva = round(base * 0.16, 2)
    desc = f' Descuento="{descuento}"' if descuento else ''
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<{p}Comprobante xmlns{ns}="http://www.sat.gob.mx/cfd/4"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="4.0" Fecha="2026-06-15T12:00:00" SubTotal="{importe}"{desc}
                  Total="{base + iva:.2f}" Moneda="{moneda}"{tc} TipoDeComprobante="I" Exportacion="01"
                  MetodoPago="{metodo_pago}" FormaPago="{forma_pago}" LugarExpedicion="06600">
  <{p}Emisor Rfc="{RFC_EMISOR}" Nombre="Proveedor de Prueba" RegimenFiscal="601"/>
  <{p}Receptor Rfc="{rfc_receptor}" Nombre="Empresa MX de prueba" UsoCFDI="G01"
               DomicilioFiscalReceptor="06600" RegimenFiscalReceptor="601"/>
  <{p}Conceptos>
    <{p}Concepto ClaveProdServ="81111500" Cantidad="{cantidad}" ClaveUnidad="E48"
                 Descripcion="Servicio profesional" ValorUnitario="{valor_unitario}"
                 Importe="{importe}"{desc} ObjetoImp="{objeto_imp}">
      <{p}Impuestos>
        <{p}Traslados>
          <{p}Traslado Base="{base:.2f}" Impuesto="002" TipoFactor="Tasa"
                       TasaOCuota="0.160000" Importe="{iva:.2f}"/>
        </{p}Traslados>
      </{p}Impuestos>
    </{p}Concepto>
  </{p}Conceptos>
  <{p}Complemento>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/>
  </{p}Complemento>
</{p}Comprobante>'''


@tagged('post_install', '-at_install')
class TestFacturaComoOdoo(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
        ], order='sequence, id', limit=1)
        cls.contador = 0

    def _uuid(self):
        type(self).contador += 1
        return f"EEEEEEEE-2222-4000-8000-{self.contador:012d}"

    def _documento(self, uuid=None, moneda='MXN', tipo_cambio='', rfc_receptor=RFC_RECEPTOR,
                   objeto_imp='02', prefijo=True, sat_status='valid', pdf=False, company=None, **concepto):
        """CFDI recibido en la Bóveda. `concepto` pasa a `cfdi_xml` (forma y
        método de pago, cantidad, valor unitario, importe, descuento)."""
        uuid = uuid or self._uuid()
        company = company or self.company
        xml = cfdi_xml(uuid, moneda=moneda, tipo_cambio=tipo_cambio, rfc_receptor=rfc_receptor,
                       objeto_imp=objeto_imp, prefijo=prefijo, **concepto)
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': xml.encode(), 'mimetype': 'text/xml',
        })
        doc = self.env['cfdi.document'].create({
            'company_id': company.id,
            'direction': 'received',
            'uuid': uuid,
            'move_type': 'I',
            'invoice_date': date(2026, 6, 15),
            'amount_total': float(re.search(r' Total="([\d.]+)"', xml).group(1)),
            'currency_id': (self.usd if moneda == 'USD' else self.company.currency_id).id,
            'partner_rfc_issuer': RFC_EMISOR,
            'partner_name_issuer': "Proveedor de Prueba",
            'partner_rfc_receiver': rfc_receptor,
            'sat_status': sat_status,
            'xml_attachment_id': adjunto.id,
        })
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        if pdf:
            doc.pdf_attachment_id = self.env['ir.attachment'].create({
                'name': f'{uuid}.pdf', 'raw': PDF, 'mimetype': 'application/pdf',
                'res_model': 'cfdi.document', 'res_id': doc.id,
            })
        return doc

    def _convertir(self, doc):
        """Por el botón, como el usuario: diario y proveedor se resuelven solos."""
        doc.action_crear_factura_proveedor()
        return doc.move_id

    def _convertir_espiando(self, doc):
        """Convierte y devuelve cada llamada al decoder de CFDI de Odoo: con qué
        adjunto, con qué valor de `new` y con qué total tenía la factura."""
        Move = self.env.registry['account.move']
        original = getattr(Move, DECODER)
        llamadas = []

        def _l10n_mx_edi_import_cfdi_invoice(this, invoice, file_data, new=False):
            llamadas.append({
                'adjunto': file_data.get('origin_attachment') or file_data.get('attachment'),
                'new': new,
                'total': invoice.amount_total,
            })
            return original(this, invoice, file_data, new)

        with patch.object(Move, DECODER, _l10n_mx_edi_import_cfdi_invoice):
            self._convertir(doc)
        return llamadas

    def _importar_con_odoo(self, xml):
        """La factura que crea Odoo al subir este XML en Facturas de proveedor."""
        adjunto = self.env['ir.attachment'].create({
            'name': 'subido_en_odoo.xml', 'raw': xml.encode(), 'mimetype': 'application/xml',
        })
        return self.journal.with_context(default_move_type='in_invoice')._create_document_from_attachment(adjunto.ids)

    def _facturas(self):
        return self.env['account.move'].search_count([
            ('company_id', '=', self.company.id), ('move_type', '=', 'in_invoice')])

    # -- El CFDI lo registra Odoo -----------------------------------------

    def test_el_cfdi_lo_registra_el_import_de_odoo(self):
        doc = self._documento()
        llamadas = self._convertir_espiando(doc)
        factura = doc.move_id

        self.assertEqual(len(llamadas), 1, "el decoder de Odoo se llama una sola vez")
        copia = llamadas[0]['adjunto']
        self.assertEqual(copia, factura.l10n_mx_edi_cfdi_attachment_id)
        self.assertNotEqual(copia, doc.xml_attachment_id, "con la copia, no con el XML de la Bóveda")
        self.assertEqual(_bytes_de_adjunto(copia), _bytes_de_adjunto(doc.xml_attachment_id))
        self.assertFalse(llamadas[0]['new'], "la factura ya existe cuando se llama")

    def test_el_import_de_odoo_no_cambia_el_total(self):
        """Odoo recalcula las líneas de impuesto al salir del decoder; con las
        partidas ya armadas, el total no debe moverse."""
        doc = self._documento()
        llamadas = self._convertir_espiando(doc)
        self.assertEqual(llamadas[0]['total'], 1160.0)
        self.assertEqual(doc.move_id.amount_total, 1160.0)

    def test_un_solo_documento_con_el_estatus_sat_de_la_boveda(self):
        """Odoo deja el documento «No definido»; la Bóveda ya consultó el SAT."""
        for sat_status in ('valid', 'not_found'):
            with self.subTest(sat_status=sat_status):
                factura = self._convertir(self._documento(sat_status=sat_status))
                documentos = factura.l10n_mx_edi_invoice_document_ids
                self.assertEqual(len(documentos), 1)
                self.assertEqual(documentos.state, 'invoice_received')
                self.assertEqual(documentos.sat_state, sat_status)
                self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, sat_status)

    def test_xml_sin_prefijo_cfdi_usa_el_respaldo(self):
        """Odoo no reconoce como CFDI la raíz sin el prefijo `cfdi`. La factura
        igual queda con su documento y su folio fiscal."""
        doc = self._documento(prefijo=False)
        llamadas = self._convertir_espiando(doc)
        factura = doc.move_id

        self.assertFalse(llamadas, "no hay decoder de Odoo para este XML")
        self.assertEqual(factura.l10n_mx_edi_cfdi_uuid, doc.uuid)
        self.assertEqual(factura.l10n_mx_edi_cfdi_state, 'received')
        self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, 'valid')
        self.assertEqual(len(factura.l10n_mx_edi_invoice_document_ids), 1)
        self.assertEqual(factura.amount_total, 1160.0)

    # -- Lo que el import de Odoo pone en la factura ----------------------

    def test_forma_de_pago_uso_y_metodo_de_pago(self):
        factura = self._convertir(self._documento())
        self.assertEqual(factura.l10n_mx_edi_payment_method_id.code, '04')
        self.assertEqual(factura.l10n_mx_edi_usage, 'G01')
        # El método de pago solo recién creada: en 18 es un cálculo sin
        # guardar, y en 19 Odoo lo borra en compras al primer cambio de
        # proveedor, fechas o plazo (su propio import lo pierde igual). Por eso
        # no está en la comparación con el import nativo, y lo que necesite el
        # método de pago de una compra lo lee de la Bóveda.
        if factura._fields['l10n_mx_edi_payment_policy'].store:
            self.assertEqual(factura.l10n_mx_edi_payment_policy, 'PUE')

    def test_forma_de_pago_99_queda_vacia_como_en_odoo(self):
        """La 99 «Por definir», la de todo CFDI en PPD, está archivada en Odoo.
        La factura quedaba con la forma de pago del contacto del proveedor, que
        el comprobante no dice; el import de Odoo la deja vacía."""
        transferencia = self.env['l10n_mx_edi.payment.method'].search([('code', '=', '03')], limit=1)
        self.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1,
            'country_id': self.env.ref('base.mx').id,
            'l10n_mx_edi_payment_method_id': transferencia.id,
        })
        doc = self._documento(forma_pago='99', metodo_pago='PPD')

        factura = self._convertir(doc)
        nativa = self._importar_con_odoo(_bytes_de_adjunto(doc.xml_attachment_id).decode())

        self.assertEqual(factura.partner_id.l10n_mx_edi_payment_method_id, transferencia)
        self.assertFalse(factura.l10n_mx_edi_payment_method_id)
        self.assertFalse(nativa.l10n_mx_edi_payment_method_id)

    def test_precios_antes_de_impuestos_aunque_la_empresa_los_incluya(self):
        """El ValorUnitario de un CFDI siempre es antes de impuestos. En Odoo 20,
        con la empresa en «impuestos incluidos», 1,000 + IVA quedaba como 1,000
        con IVA incluido."""
        if 'document_tax_mode' not in self.env['account.move']._fields:
            self.skipTest("En Odoo 18 y 19 el modo de impuestos es de la empresa, no de la "
                          "factura, y el import de Odoo lo toma igual.")
        self.company.account_price_include = 'tax_included'
        factura = self._convertir(self._documento())
        self.assertEqual(factura.document_tax_mode, 'tax_excluded')
        self.assertEqual(factura.amount_total, 1160.0)

    def test_cantidad_y_descuento_con_los_decimales_del_comprobante(self):
        """Cantidad 0.125 y un descuento de 10.0072 %. Con los 2 decimales de la
        base quedaban en 0.13 y 10.01 %, y la factura daba 45.24 contra los
        43.50 del comprobante."""
        doc = self._documento(cantidad='0.125', valor_unitario='333.333', importe='41.67', descuento='4.17')
        self.assertEqual(doc.amount_total, 43.5)

        factura = self._convertir(doc)

        linea = factura.invoice_line_ids
        self.assertEqual(linea.quantity, 0.125)
        # Sobre el Importe, como Odoo, no sobre Cantidad × ValorUnitario.
        self.assertAlmostEqual(linea.discount, 4.17 / 41.67 * 100)
        self.assertAlmostEqual(factura.amount_total, 43.5)

    def test_moneda_extranjera_lleva_el_tipo_de_cambio_del_comprobante(self):
        """Sin tipos de cambio cargados en Odoo, la factura en dólares quedaba
        a 1:1 con el peso."""
        factura = self._convertir(self._documento(moneda='USD', tipo_cambio='17.50'))
        self.assertEqual(factura.currency_id, self.usd)
        self.assertAlmostEqual(factura.invoice_currency_rate, 1 / 17.5)
        self.assertAlmostEqual(abs(factura.amount_total_signed), 1160.0 * 17.5, places=2)

    def test_el_diario_con_moneda_no_le_cambia_la_moneda_al_comprobante(self):
        """El decoder de Odoo 18 reescribe el tipo de factura y Odoo vuelve a
        calcular la moneda desde el diario: con el diario de compras en
        dólares, un CFDI en pesos quedaba como factura en dólares."""
        self.journal.currency_id = self.usd
        factura = self._convertir(self._documento())
        self.assertEqual(factura.currency_id, self.company.currency_id)
        self.assertEqual(factura.line_ids.currency_id, self.company.currency_id)

    def test_conserva_lo_que_ya_ponia_la_boveda(self):
        """El folio en la referencia, la fecha de emisión y la Descripcion del
        concepto, que el import de Odoo 19 no escribe."""
        doc = self._documento()
        factura = self._convertir(doc)
        self.assertEqual(factura.ref, doc.uuid)
        self.assertEqual(factura.invoice_date, date(2026, 6, 15))
        self.assertEqual(factura.invoice_line_ids.name, "Servicio profesional")

    def test_elegir_producto_conserva_precio_e_impuestos(self):
        """Regresión: al elegir un producto en la partida, Odoo le ponía el
        costo y los impuestos del producto y la factura dejaba de cuadrar con
        el comprobante."""
        factura = self._convertir(self._documento())
        linea = factura.invoice_line_ids
        self.assertTrue(linea.is_imported)
        precio, impuestos = linea.price_unit, linea.tax_ids

        tasa_cero = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'purchase'),
            ('amount_type', '=', 'percent'), ('amount', '=', 0.0),
        ], limit=1)
        producto = self.env['product.product'].create({
            'name': "Producto del catálogo", 'standard_price': 5.0,
            'supplier_taxes_id': [Command.set(tasa_cero.ids)],
        })
        linea.product_id = producto
        self.assertEqual(linea.price_unit, precio)
        self.assertEqual(linea.tax_ids, impuestos)
        self.assertEqual(factura.amount_total, 1160.0)

        # Control: sin la marca, el mismo cambio sí pisa el precio. Si esto
        # fallara, la prueba de arriba no estaría probando nada.
        linea.is_imported = False
        linea.product_id = self.env['product.product'].create({
            'name': "Otro producto del catálogo", 'standard_price': 7.0,
            'supplier_taxes_id': [Command.set(tasa_cero.ids)],
        })
        self.assertEqual(linea.price_unit, 7.0)

    def test_objeto_de_impuesto_por_partida(self):
        """ObjetoImp del concepto. 03 a propósito: con IVA en la partida, lo que
        Odoo calcularía por su cuenta es 02."""
        linea_campos = self.env['account.move.line']._fields
        if 'l10n_mx_edi_tax_object' not in linea_campos:
            self.skipTest("Odoo 18 no guarda el objeto de impuesto por partida.")
        factura = self._convertir(self._documento(objeto_imp='03'))
        self.assertEqual(factura.invoice_line_ids.l10n_mx_edi_tax_object, '03')

    def test_el_pdf_de_la_boveda_es_la_vista_previa(self):
        doc = self._documento(pdf=True)
        factura = self._convertir(doc)
        principal = factura.message_main_attachment_id
        self.assertEqual(principal.mimetype, 'application/pdf')
        self.assertEqual((principal.res_model, principal.res_id), ('account.move', factura.id))
        self.assertEqual(_bytes_de_adjunto(principal), PDF)
        self.assertNotEqual(principal, doc.pdf_attachment_id,
                            "es una copia: el PDF de la Bóveda se queda en la Bóveda")
        # El XML va en el mismo mensaje, pero nunca es el adjunto principal.
        self.assertIn(factura.l10n_mx_edi_cfdi_attachment_id, factura.message_ids.attachment_ids)

    def test_el_mensaje_de_la_conversion_no_se_vuelve_a_importar(self):
        """Odoo 18 toma los adjuntos de un mensaje de la factura como un XML
        subido en ella: lo decodificaba otra vez y publicaba «la factura ya
        tiene partidas, no se actualizó con el adjunto». 19 y 20 ya omiten los
        mensajes del sistema."""
        factura = self._convertir(self._documento(pdf=True))
        cuerpos = " ".join(str(m.body) for m in factura.message_ids)
        self.assertNotIn("was not updated from the attachment", cuerpos)

    def test_no_queda_como_modificada_a_mano(self):
        """Odoo importa sin marcar la factura como modificada a mano, y su
        sugerencia de contabilizar solas las facturas del proveedor cuenta con
        eso. Poner el PDF principal o marcarla por revisar la marcaban."""
        con_pdf = self._convertir(self._documento(pdf=True))
        self.assertFalse(con_pdf.is_manually_modified)

        doc = self._documento()
        doc.amount_total = 1500.0  # no cuadra con la factura: queda por revisar
        por_revisar = self._convertir(doc)
        self.assertTrue(any("no coincide" in str(m.body) for m in por_revisar.message_ids))
        self.assertFalse(por_revisar.is_manually_modified)

        # Control: un cambio del usuario sí la marca. Si esto fallara, la
        # marca de arriba no estaría probando nada.
        por_revisar.narration = "Revisada"
        self.assertTrue(por_revisar.is_manually_modified)

    # -- Lo que se revisa antes de crear ----------------------------------

    def test_rfc_receptor_de_otra_empresa_no_se_convierte(self):
        doc = self._documento(rfc_receptor=OTRO_RFC)
        antes = self._facturas()
        with self.assertRaises(UserError) as error:
            doc.action_crear_factura_proveedor()
        self.assertIn("otro RFC", str(error.exception))
        with self.assertRaises(UserError):
            doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
        self.assertEqual(self._facturas(), antes, "no debió crearse ninguna factura")
        self.assertFalse(doc.move_id)

    def test_sin_rfc_en_la_empresa_no_se_convierte(self):
        """Sin RFC no hay con qué comprobar que el CFDI sea de la empresa (y
        Odoo 20 rechazaría el XML en su import)."""
        self.company.partner_id.vat = False
        doc = self._documento()
        with self.assertRaises(UserError) as error:
            doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
        self.assertIn("no tiene RFC", str(error.exception))
        self.assertFalse(doc.move_id)

    def test_folio_ya_facturado_se_vincula_sin_duplicar(self):
        """La factura ya la había importado Odoo, con el folio en minúsculas
        como lo timbran algunos PAC; la Bóveda lo guarda en mayúsculas."""
        uuid = self._uuid()
        existente = self._importar_con_odoo(cfdi_xml(uuid.lower()))
        self.assertEqual(existente.l10n_mx_edi_cfdi_uuid, uuid.lower())
        doc = self._documento(uuid=uuid)
        antes = self._facturas()

        accion = doc.action_crear_factura_proveedor()

        self.assertEqual(self._facturas(), antes, "no debió crear una segunda factura")
        self.assertEqual(doc.move_id, existente)
        self.assertEqual(doc.match_method, 'uuid_exact')
        self.assertEqual(accion['res_id'], existente.id, "abre la factura que ya existía")
        self.assertEqual(len(existente.l10n_mx_edi_invoice_document_ids), 1,
                         "no se le registra el CFDI dos veces")

    def test_folio_ya_facturado_no_se_crea_por_la_ruta_directa(self):
        uuid = self._uuid()
        self._importar_con_odoo(cfdi_xml(uuid))
        doc = self._documento(uuid=uuid)
        with self.assertRaises(UserError) as error:
            doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
        self.assertIn(uuid, str(error.exception))

    def test_factura_cancelada_no_impide_registrar_el_cfdi(self):
        """Si el usuario canceló la factura que tenía el folio, el CFDI se puede
        volver a registrar, como en la revisión de duplicados de Odoo. Antes el
        botón lo vinculaba a la cancelada y la ruta directa se negaba."""
        uuid = self._uuid()
        cancelada = self._importar_con_odoo(cfdi_xml(uuid))
        cancelada.button_cancel()
        self.assertEqual(cancelada.state, 'cancel')
        doc = self._documento(uuid=uuid)

        self._convertir(doc)

        self.assertTrue(doc.move_id)
        self.assertNotEqual(doc.move_id, cancelada)
        self.assertEqual(doc.move_id.state, 'draft')

    def test_con_cancelada_y_vigente_se_vincula_a_la_vigente(self):
        uuid = self._uuid()
        cancelada = self._importar_con_odoo(cfdi_xml(uuid))
        cancelada.button_cancel()
        vigente = self._importar_con_odoo(cfdi_xml(uuid))
        doc = self._documento(uuid=uuid)
        antes = self._facturas()

        doc.action_crear_factura_proveedor()

        self.assertEqual(self._facturas(), antes, "no debió crear otra factura")
        self.assertEqual(doc.move_id, vigente)

    def test_folio_ya_facturado_en_la_matriz_no_se_duplica_en_la_sucursal(self):
        """La sucursal sin RFC propio recibe con el de su matriz. Si la factura
        con este folio ya está en la matriz, convertirlo en la sucursal
        registraba el mismo CFDI dos veces para el mismo contribuyente."""
        sucursal = self.env['res.company'].create({
            'name': "Sucursal MX de prueba", 'parent_id': self.company.id,
        })
        self.assertFalse(sucursal.partner_id.vat, "recibe con el RFC de la matriz")
        uuid = self._uuid()
        en_matriz = self._importar_con_odoo(cfdi_xml(uuid))
        doc = self._documento(uuid=uuid, company=sucursal)

        convertibles, descartados = doc._clasificar_para_factura()

        self.assertFalse(convertibles)
        self.assertEqual(
            descartados.get("ya hay una factura con su folio fiscal en otra empresa con el mismo RFC"), doc)
        with self.assertRaises(UserError) as error:
            doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
        self.assertIn(en_matriz.display_name, str(error.exception))
        self.assertFalse(doc.move_id)

    def test_rfc_de_la_empresa_con_prefijo_mx_en_odoo_20(self):
        """Odoo 18 guardaba el RFC con el prefijo de país (MXEKU9003173C9) y una
        base que viene de 18 lo conserva. Odoo 20 compara contra ese texto tal
        cual y rechaza el XML; el clasificador debe decirlo antes de crear nada,
        no a mitad del lote. Se simulan las dos: el import de 18 y 19 no revisa
        el RFC y el de 20 sí."""
        self.env.cr.execute("UPDATE res_partner SET vat = %s WHERE id = %s",
                            ['MX' + RFC_RECEPTOR, self.company.partner_id.id])
        self.env.invalidate_all()
        doc = self._documento()
        with patch(f'{MODULO_CONVERSION}._odoo_revisa_rfc_receptor', return_value=False):
            self.assertFalse(doc._problema_rfc_receptor(), "sin la revisión de Odoo 20, el prefijo no estorba")

        if not hasattr(self.env['res.partner'], '_run_vat_checks'):
            self.skipTest("Odoo 18 no tiene _run_vat_checks, con el que se simula la revisión de 20.")
        with patch(f'{MODULO_CONVERSION}._odoo_revisa_rfc_receptor', return_value=True):
            convertibles, descartados = doc._clasificar_para_factura()
            self.assertFalse(convertibles)
            self.assertEqual(
                descartados.get("Odoo no acepta el RFC de su empresa tal como está capturado"), doc)
            with self.assertRaises(UserError) as error:
                doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
            self.assertIn('MX' + RFC_RECEPTOR, str(error.exception))
            self.assertFalse(doc.move_id)

            # Capturado como lo guarda Odoo 20, pasa.
            self.company.partner_id.vat = RFC_RECEPTOR
            self.assertFalse(doc._problema_rfc_receptor())

    def test_en_lote_avisa_lo_que_no_convirtio(self):
        """Antes lo omitido solo iba al log: el usuario veía la factura creada y
        no se enteraba de que el CFDI de otro RFC se había quedado fuera."""
        nuevo = self._documento()
        otro_rfc = self._documento(rfc_receptor=OTRO_RFC)

        accion = (nuevo | otro_rfc).action_crear_factura_proveedor()

        self.assertEqual(accion['tag'], 'display_notification')
        self.assertIn("1 porque se emitieron a otro RFC", accion['params']['message'])
        self.assertEqual(accion['params']['next']['res_id'], nuevo.move_id.id,
                         "después abre la factura que sí se creó")
        self.assertFalse(otro_rfc.move_id)

    def test_el_clasificador_anuncia_los_mismos_motivos(self):
        """Lo que el clasificador anuncia debe ser lo que la conversión acepta."""
        ya_facturado = self._uuid()
        self._importar_con_odoo(cfdi_xml(ya_facturado))
        bueno = self._documento()
        otro_rfc = self._documento(rfc_receptor=OTRO_RFC)
        duplicado = self._documento(uuid=ya_facturado)

        convertibles, descartados = (bueno | otro_rfc | duplicado)._clasificar_para_factura()

        self.assertEqual(convertibles, bueno)
        self.assertEqual(descartados.get("se emitieron a otro RFC, no al de la empresa"), otro_rfc)
        self.assertEqual(descartados.get(duplicado._motivo_ya_facturado()), duplicado)

    # -- La descarga masiva del SAT de Odoo 20 ------------------------------
    # Odoo 20 trae su propia descarga: deja cada CFDI como un documento «Por
    # importar» y su cron crea después la factura. Si la Bóveda lo convierte
    # antes, debe ocupar ese documento; si no, salen dos facturas del mismo
    # CFDI.

    def _por_importar_de_odoo(self, doc, company=None):
        """El documento «Por importar» que deja la descarga nativa de Odoo 20
        con el XML de este CFDI, en `company` (por omisión, la de prueba)."""
        if 'l10n_mx_edi.cfdi.request' not in self.env:
            self.skipTest("Odoo 18 y 19 no tienen descarga masiva del SAT propia.")
        solicitud = self.env['l10n_mx_edi.cfdi.request'].create({
            'company_id': (company or self.company).id,
            'request_type': 'batch_received',
            'state': 'unpacking',
            'emission_date_from': date(2026, 6, 1),
            'emission_date_to': date(2026, 6, 30),
        })
        documento = self.env['l10n_mx_edi.document']._create_documents_from_cfdi_files(
            [_bytes_de_adjunto(doc.xml_attachment_id)], solicitud)
        self.assertEqual(documento.state, 'to_import')
        return documento

    def test_reutiliza_el_por_importar_de_la_descarga_de_odoo_20(self):
        doc = self._documento()
        por_importar = self._por_importar_de_odoo(doc)
        antes = self._facturas()

        factura = self._convertir(doc)

        self.assertEqual(self._facturas(), antes + 1)
        self.assertEqual(factura.l10n_mx_edi_invoice_document_ids, por_importar, "el mismo documento, no otro")
        self.assertEqual((por_importar.state, por_importar.move_id), ('invoice_received', factura))
        self.assertEqual(por_importar.sat_state, 'valid', "con el estatus SAT de la Bóveda")
        self.assertEqual(factura.l10n_mx_edi_cfdi_attachment_id, por_importar.attachment_id)
        self.assertEqual(factura.l10n_mx_edi_cfdi_uuid, doc.uuid)
        self.assertEqual(self.env['l10n_mx_edi.document'].search_count([('attachment_uuid', '=', doc.uuid)]), 1)
        # El cron de Odoo ya no lo toma: no crea una segunda factura.
        self.assertFalse(por_importar._create_record_from_document())
        self.assertEqual(self._facturas(), antes + 1)

    def test_el_respaldo_tambien_ocupa_el_por_importar(self):
        """El XML sin el prefijo `cfdi` que la descarga de Odoo 20 sí dejó «Por
        importar»: su import no lo reconoce y el documento lo arma el respaldo,
        que ocupa ese mismo documento."""
        doc = self._documento(prefijo=False)
        por_importar = self._por_importar_de_odoo(doc)

        llamadas = self._convertir_espiando(doc)
        factura = doc.move_id

        self.assertFalse(llamadas, "no hay decoder de Odoo para este XML")
        self.assertEqual(factura.l10n_mx_edi_invoice_document_ids, por_importar)
        self.assertEqual((por_importar.state, por_importar.move_id, por_importar.sat_state),
                         ('invoice_received', factura, 'valid'))
        self.assertEqual(factura.l10n_mx_edi_cfdi_uuid, doc.uuid)
        self.assertFalse(por_importar._create_record_from_document())

    def test_lo_que_odoo_20_descargo_en_la_matriz_no_se_convierte_en_la_sucursal(self):
        """La sucursal sin RFC propio recibe con el de su matriz. Si la descarga
        de Odoo ya dejó el CFDI en la matriz, su cron creará la factura ahí:
        convertirlo en la sucursal registraría el mismo CFDI dos veces."""
        sucursal = self.env['res.company'].create({
            'name': "Sucursal MX de prueba", 'parent_id': self.company.id,
        })
        doc = self._documento(company=sucursal)
        self._por_importar_de_odoo(doc)

        convertibles, descartados = doc._clasificar_para_factura()

        self.assertFalse(convertibles)
        self.assertEqual(
            descartados.get("Odoo ya los descargó del SAT en otra empresa con el mismo RFC"), doc)
        with self.assertRaises(UserError) as error:
            doc._crear_factura_proveedor(journal=self.journal, crear_proveedor=True)
        self.assertIn(self.company.name, str(error.exception))
        self.assertFalse(doc.move_id)

    # -- Comparación contra el import nativo -------------------------------
    # Los métodos de Odoo que se llaman son privados y pueden cambiar en una
    # actualización. Si dejan de comportarse igual, esta prueba lo dice.

    def test_queda_igual_que_si_odoo_hubiera_importado_el_xml(self):
        """Con un concepto sencillo y con uno de cantidad fraccionaria y
        descuento, donde se notan los decimales."""
        casos = {
            'sencillo': {},
            'fraccionario': {'cantidad': '0.125', 'valor_unitario': '333.333',
                             'importe': '41.67', 'descuento': '4.17'},
        }
        for caso, concepto in casos.items():
            doc = self._documento(**concepto)
            nuestra = self._convertir(doc)
            nativa = self._importar_con_odoo(_bytes_de_adjunto(doc.xml_attachment_id).decode())
            self.assertTrue(nativa.l10n_mx_edi_invoice_document_ids,
                            "el import nativo debió registrar el CFDI; revisa su chatter")

            for campo in ('l10n_mx_edi_cfdi_uuid', 'l10n_mx_edi_cfdi_state', 'l10n_mx_edi_payment_method_id',
                          'l10n_mx_edi_usage', 'currency_id', 'amount_untaxed', 'amount_total',
                          'is_manually_modified'):
                with self.subTest(caso=caso, campo=campo):
                    self.assertEqual(nuestra[campo], nativa[campo])
            for campo in ('quantity', 'price_unit', 'discount'):
                with self.subTest(caso=caso, campo=campo):
                    self.assertEqual(nuestra.invoice_line_ids[campo], nativa.invoice_line_ids[campo])
