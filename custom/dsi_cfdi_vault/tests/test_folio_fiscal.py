"""El Folio fiscal de la factura creada desde la Bóveda.

Existe porque la factura quedaba sin Folio fiscal: Odoo lo calcula
(l10n_mx_edi_cfdi_uuid) de un documento CFDI «Recibido» con el XML, y ese
documento solo se creaba al subir el XML en Facturas, no al convertir desde la
Bóveda. Sin folio, además, la conciliación por UUID exacto ya no encontraba la
factura.

Usa una empresa mexicana en pesos con el catálogo de México: Odoo 20 solo
calcula los campos CFDI en esas empresas (en 18 y 19, en cualquiera).
"""
from datetime import date

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import IMPUESTO_IVA16, RFC_EMISOR, RFC_RECEPTOR, cfdi_xml

UUID = 'DDDDDDDD-1111-4000-8000-000000000001'


@tagged('post_install', '-at_install')
class TestFolioFiscal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)

    def _documento(self):
        xml = cfdi_xml(impuestos_xml=IMPUESTO_IVA16).replace('{uuid}', UUID)
        adjunto = self.env['ir.attachment'].create({
            'name': f'{UUID}.xml', 'raw': xml.encode(), 'mimetype': 'text/xml',
        })
        doc = self.env['cfdi.document'].create({
            'company_id': self.company.id,
            'direction': 'received',
            'uuid': UUID,
            'move_type': 'I',
            'invoice_date': date(2026, 6, 15),
            'amount_total': 1160.0,
            'currency_id': self.company.currency_id.id,
            'partner_rfc_issuer': RFC_EMISOR,
            'partner_name_issuer': "Proveedor de Prueba",
            'partner_rfc_receiver': RFC_RECEPTOR,
            'sat_status': 'valid',
            'xml_attachment_id': adjunto.id,
        })
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def test_la_factura_lleva_el_folio_fiscal_y_el_xml(self):
        doc = self._documento()
        doc.action_crear_factura_proveedor()
        factura = doc.move_id

        self.assertEqual(factura.l10n_mx_edi_cfdi_uuid, UUID)
        self.assertEqual(factura.l10n_mx_edi_cfdi_state, 'received')
        self.assertEqual(factura.l10n_mx_edi_cfdi_sat_state, 'valid')
        adjunto = factura.l10n_mx_edi_cfdi_attachment_id
        self.assertEqual((adjunto.res_model, adjunto.res_id), ('account.move', factura.id))
        self.assertEqual(_bytes_de_adjunto(adjunto), _bytes_de_adjunto(doc.xml_attachment_id))

    def test_la_conciliacion_por_uuid_encuentra_la_factura(self):
        doc = self._documento()
        doc.action_crear_factura_proveedor()
        factura = doc.move_id
        doc.action_unlink_move()

        doc.action_match_automatically()
        self.assertEqual(doc.move_id, factura)
        self.assertEqual(doc.match_method, 'uuid_exact')
