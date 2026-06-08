import base64
from odoo import models, api, exceptions
from satcfdi.cfdi import CFDI

class SatXmlReport(models.AbstractModel):
    _name = 'report.sat_xml_auditor.report_sat_xml_template'
    _description = 'Parser para PDF de XML SAT'

    @api.model
    def _get_report_values(self, docids, data=None):
        docs_originales = self.env['sat.xml.raw'].browse(docids)
        docs = docs_originales.filtered(lambda r: r.cfdi_type != 'pago')
        
        if not docs:
            raise exceptions.UserError("Los CFDI de tipo 'Pago' no son compatibles con este formato de reporte.")
        
        xml_data_dict = {}
        
        for doc in docs:
            # Diccionario base por si el XML falla o está vacío
            datos = {
                'conceptos': [],
                'version': '',
                'serie_folio': 'S/N',
                'forma_pago': '',
                'metodo_pago': '',
                'moneda': 'MXN',
                'uso_cfdi': '',
                'regimen_emisor': '',
                'regimen_receptor': '',
                'cp_receptor': '',
                'lugar_expedicion': '',
                'subtotal': '0.00',
                'descuento': '0.00',
                'total_trasladados': '0.00',
                'total_retenidos': '0.00',
            }
            
            if doc.xml_file:
                try:
                    xml_content = base64.b64decode(doc.xml_file)
                    cfdi = CFDI.from_string(xml_content)
                    
                    # Atributos principales del Comprobante
                    datos['version'] = cfdi.get('Version', '')
                    serie = cfdi.get('Serie', '')
                    folio = cfdi.get('Folio', '')
                    if serie or folio:
                        datos['serie_folio'] = f"{serie}{folio}"
                        
                    datos['forma_pago'] = cfdi.get('FormaPago', 'N/A')
                    datos['metodo_pago'] = cfdi.get('MetodoPago', 'N/A')
                    datos['moneda'] = cfdi.get('Moneda', 'MXN')
                    datos['lugar_expedicion'] = cfdi.get('LugarExpedicion', '')
                    datos['subtotal'] = cfdi.get('SubTotal', '0.00')
                    datos['descuento'] = cfdi.get('Descuento', '0.00')
                    
                    # Nodos de Emisor y Receptor
                    emisor = cfdi.get('Emisor', {})
                    receptor = cfdi.get('Receptor', {})
                    datos['regimen_emisor'] = emisor.get('RegimenFiscal', '')
                    datos['uso_cfdi'] = receptor.get('UsoCFDI', '')
                    datos['regimen_receptor'] = receptor.get('RegimenFiscalReceptor', '')
                    datos['cp_receptor'] = receptor.get('DomicilioFiscalReceptor', '')
                    
                    # Nodo de Conceptos
                    lista_conceptos = cfdi.get('Conceptos', [])
                    if not isinstance(lista_conceptos, list):
                        lista_conceptos = [lista_conceptos]
                    datos['conceptos'] = lista_conceptos
                    
                    # Nodo de Impuestos
                    impuestos = cfdi.get('Impuestos', {})
                    if impuestos:
                        datos['total_trasladados'] = impuestos.get('TotalImpuestosTrasladados', '0.00')
                        datos['total_retenidos'] = impuestos.get('TotalImpuestosRetenidos', '0.00')
                        
                except Exception:
                    pass
                    
            xml_data_dict[doc.id] = datos

        return {
            'doc_ids': docids,
            'doc_model': 'sat.xml.raw',
            'docs': docs,
            'xml_data': xml_data_dict,
        }