import base64
from lxml import etree
from odoo import models, fields, api, exceptions

class SatXmlRaw(models.Model):
    _name = 'sat.xml.raw'
    _description = 'Registro Crudo de XML del SAT'
    _rec_name = 'uuid'

    xml_file = fields.Binary(string='Archivo XML', required=True, attachment=True)
    uuid = fields.Char(string='UUID', index=True, copy=False)
    xml_filename = fields.Char(string='Nombre del Archivo')
    date_emission = fields.Date(string='Fecha')
    
    rfc_emisor = fields.Char(string='RFC Emisor')
    nombre_emisor = fields.Char(string='Emisor')
    rfc_receptor = fields.Char(string='RFC Receptor')
    nombre_receptor = fields.Char(string='Receptor')
    
    amount_total = fields.Float(string='Total')
    cfdi_type = fields.Selection([
        ('ingreso', 'I - Ingreso '),
        ('egreso', 'E - Egreso'),
        ('traslado', 'T - Traslado'),
        ('pago', 'P - Pago'),
        ('nomina', 'N - Nómina')
    ], string='Tipo de Comprobante')

    tipo_operacion = fields.Selection([
        ('emitido', 'Emitida'),
        ('recibido', 'Recibida')
    ], string='Tipo de Operación', compute='_compute_tipo_operacion', store=True)

    match_state = fields.Selection([
        ('pending', 'Pendiente de Validar'),
        ('match', 'Sincronizado'),
        ('discrepancy', 'Discrepancia de Estatus'),
        ('missing', 'Faltante')
    ], string='Estatus', default='pending')

    move_id = fields.Many2one('account.move', string='Factura en Odoo', readonly=True)

    @api.depends('rfc_emisor')
    def _compute_tipo_operacion(self):
        for record in self:
            # Comparamos el RFC del emisor con el RFC de la compañía (res.company)
            if record.rfc_emisor and record.env.company.vat:
                if record.rfc_emisor == record.env.company.vat:
                    record.tipo_operacion = 'emitido'
                else:
                    record.tipo_operacion = 'recibido'
            else:
                record.tipo_operacion = False
                
    def action_procesar_xml(self):
        """
        Lee el archivo XML adjunto, extrae los nodos del CFDI 4.0,
        rellena los campos y busca la factura en Odoo para auditarla.
        """
        for record in self:
            if not record.xml_file:
                raise exceptions.UserError("Por favor, sube un archivo XML antes de procesar.")

            try:
                # Decodificar el archivo binario guardado en Odoo
                xml_content = base64.b64decode(record.xml_file)
                
                # Parsear el XML con lxml
                root = etree.fromstring(xml_content)
                
                # Definir los Namespaces del SAT obligatorios para poder buscar los nodos
                namespaces = {
                    'cfdi': 'http://www.sat.gob.mx/cfd/4',
                    'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'
                }

                # Extraer atributos principales del nodo raíz (<cfdi:Comprobante>)
                record.date_emission = root.get('Fecha')[:10] if root.get('Fecha') else False
                record.amount_total = float(root.get('Total', 0.0))
                
                # Mapear el Tipo de Comprobante (I, E, T, P, N)
                tipo_letra = root.get('TipoDeComprobante')
                mapa_tipos = {'I': 'ingreso', 'E': 'egreso', 'T': 'traslado', 'P': 'pago', 'N': 'nomina'}
                record.cfdi_type = mapa_tipos.get(tipo_letra, False)

                # Extraer datos del Emisor (<cfdi:Emisor>)
                nodo_emisor = root.find('.//cfdi:Emisor', namespaces)
                if nodo_emisor is not None:
                    record.rfc_emisor = nodo_emisor.get('Rfc')
                    record.nombre_emisor = nodo_emisor.get('Nombre')

                # Extraer datos del Receptor (<cfdi:Receptor>)
                nodo_receptor = root.find('.//cfdi:Receptor', namespaces)
                if nodo_receptor is not None:
                    record.rfc_receptor = nodo_receptor.get('Rfc')
                    record.nombre_receptor = nodo_receptor.get('Nombre')

                # Extraer el UUID del Timbre Fiscal (<tfd:TimbreFiscalDigital>)
                nodo_timbre = root.find('.//tfd:TimbreFiscalDigital', namespaces)
                if nodo_timbre is not None:
                    record.uuid = nodo_timbre.get('UUID')

                record._compute_tipo_operacion()

                #  Buscar la factura en Odoo
                if record.uuid:
                    factura_existente = self.env['account.move'].search([
                        ('l10n_mx_edi_cfdi_uuid', '=', record.uuid)
                    ], limit=1)
                    
                    if factura_existente:
                        # Si existe, la vinculamos al registro
                        record.move_id = factura_existente.id
                        
                        # Si en Odoo la factura está cancelada, marcamos discrepancia
                        if factura_existente.state == 'cancel':
                            record.match_state = 'discrepancy'
                        else:
                            record.match_state = 'match'
                    else:
                        # Si no existe en Odoo, la marcamos como faltante
                        record.match_state = 'missing'

            except Exception as e:
                raise exceptions.UserError(f"Error al procesar el archivo XML: {str(e)}")