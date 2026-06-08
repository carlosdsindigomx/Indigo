import base64
from odoo import models, fields, api, exceptions
from satcfdi.cfdi import CFDI

class SatXmlRaw(models.Model):
    _name = 'sat.xml.raw'
    _description = 'Registro de XML del SAT'
    _rec_name = 'uuid'
    _inherit = ['mail.thread', 'mail.activity.mixin']

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
        ('missing', 'Faltante'),
        ('wrong_company', 'RFC invalido')
    ], string='Estatus', default='pending')

    move_id = fields.Many2one('account.move', string='Factura', readonly=True)
    
    revised = fields.Boolean(string='Revisado', default=False)

    @api.depends('rfc_emisor', 'rfc_receptor')
    def _compute_tipo_operacion(self):
        for record in self:
            mi_rfc = record.env.company.vat
            if mi_rfc:
                if record.rfc_emisor == mi_rfc:
                    record.tipo_operacion = 'emitido'
                elif record.rfc_receptor == mi_rfc:
                    record.tipo_operacion = 'recibido'
                else:
                    record.tipo_operacion = False
            else:
                record.tipo_operacion = False
                
    def _buscar_pago_manual_odoo(self, cfdi):
        """
        Busca un pago manual en Odoo basándose en el UUID de la factura
        relacionada dentro del complemento de pago XML.
        """
        self.ensure_one()
        try:
            complemento = cfdi.get('Complemento', {})
            pagos = complemento.get('Pagos', {})
            
            if not pagos:
                return False
                
            lista_pagos = pagos.get('Pago', [])
            if not isinstance(lista_pagos, list):
                lista_pagos = [lista_pagos]
                
            for pago in lista_pagos:
                monto_total_pago = float(pago.get('Monto', 0.0))
                
                # Extraer los documentos relacionados
                doctos = pago.get('DoctoRelacionado', [])
                if not isinstance(doctos, list):
                    doctos = [doctos]
                    
                for doc in doctos:
                    uuid_factura = doc.get('IdDocumento')
                    if not uuid_factura:
                        continue
                        
                    # Buscamos la Factura Original usando su UUID
                    factura_odoo = self.env['account.move'].search([
                        ('l10n_mx_edi_cfdi_uuid', '=', uuid_factura.upper())
                    ], limit=1)
                    
                    if factura_odoo:
                        # Obtenemos los pagos
                        pagos_aplicados = factura_odoo._get_reconciled_payments()
                        
                        for pago_manual in pagos_aplicados:
                            # Comparamos el monto
                            if abs(pago_manual.amount - monto_total_pago) < 0.1:
                                return pago_manual.move_id # Retornamos el asiento contable del pago
            return False
        except Exception as e:
            _logger.warning("Fallo en la búsqueda inteligente de pago: %s", str(e))
            return False
                
    def action_procesar_xml(self):
        """
        Lee el archivo XML adjunto usando satcfdi, 
        rellena los campos y busca la factura en Odoo.
        """
        for record in self:
            if not record.xml_file:
                raise exceptions.UserError("Por favor, sube un archivo XML antes de procesar.")

            try:
                # Decodificar el archivo
                xml_content = base64.b64decode(record.xml_file)
                
                # Cargar el XML con satcfdi
                invoice = CFDI.from_string(xml_content)
                
                complemento = invoice.get('Complemento', {})
                uuid_extraido = False
                if 'TimbreFiscalDigital' in complemento:
                    uuid_extraido = complemento['TimbreFiscalDigital'].get('UUID')

                # Validación de duplicados basada en UUID
                if uuid_extraido:
                    duplicado = self.env['sat.xml.raw'].search([
                        ('uuid', '=', uuid_extraido),
                        ('id', '!=', record.id) 
                    ], limit=1)
                    
                    if duplicado:
                        record.unlink()
                        raise exceptions.UserError(f"Este XML ya fue cargado previamente en el sistema: {uuid_extraido}")
                
                # Si pasa la validación, le asignamos el UUID 
                record.uuid = uuid_extraido

                # Extraer atributos principales
                fecha_factura = invoice.get('Fecha')
                if fecha_factura:
                    record.date_emission = fecha_factura.date() if hasattr(fecha_factura, 'date') else fecha_factura
                else:
                    record.date_emission = False
                record.amount_total = float(invoice.get('Total', 0.0))
                
                # Mapear el tipo de comprobante
                tipo_letra = invoice.get('TipoDeComprobante')
                mapa_tipos = {'I': 'ingreso', 'E': 'egreso', 'T': 'traslado', 'P': 'pago', 'N': 'nomina'}
                record.cfdi_type = mapa_tipos.get(tipo_letra, False)

                # Extraer datos del emisor y receptor
                record.rfc_emisor = invoice['Emisor'].get('Rfc')
                record.nombre_emisor = invoice['Emisor'].get('Nombre')
                
                record.rfc_receptor = invoice['Receptor'].get('Rfc')
                record.nombre_receptor = invoice['Receptor'].get('Nombre')

                record._compute_tipo_operacion() 
                mi_rfc = record.env.company.vat
                
                mi_rfc_limpio = mi_rfc.replace('MX', '') if mi_rfc else False
                
                if mi_rfc_limpio and mi_rfc_limpio not in [record.rfc_emisor, record.rfc_receptor]:
                    record.match_state = 'wrong_company'

                elif record.uuid: 
                    # 1. Búsqueda directa (por si el pago manual ya tenía el UUID capturado)
                    factura_existente = self.env['account.move'].search([
                        ('l10n_mx_edi_cfdi_uuid', '=', record.uuid)
                    ], limit=1)
                    
                    if factura_existente:
                        record.move_id = factura_existente.id
                        if factura_existente.state == 'cancel':
                            record.match_state = 'discrepancy'
                        else:
                            record.match_state = 'match'
                            
                    # 2. Búsqueda Inteligente (Solo para Pagos huérfanos)
                    elif record.cfdi_type == 'pago':
                        pago_encontrado_move = record._buscar_pago_manual_odoo(invoice)
                        
                        if pago_encontrado_move:
                            record.move_id = pago_encontrado_move.id
                            record.match_state = 'match'
                            
                            # Al adjuntar el XML al registro nativo del pago, 
                            # Odoo absorberá el UUID automáticamente en el campo nativo.
                            self.env['ir.attachment'].create({
                                'name': record.xml_filename,
                                'type': 'binary',
                                'datas': record.xml_file,
                                'res_model': 'account.move',
                                'res_id': pago_encontrado_move.id,
                                'mimetype': 'application/xml'
                            })
                            
                            if hasattr(record, 'message_post'):
                                record.message_post(body=f"Vinculado automáticamente con el pago: {pago_encontrado_move.name}")
                        else:
                            record.match_state = 'missing'
                    else:
                        record.match_state = 'missing'

            except exceptions.UserError as ue:
                raise ue 
            except Exception as e:
                raise exceptions.UserError(f"Error al procesar el archivo XML: {str(e)}")
            
    def action_generar_facturas(self):
        """
        Toma los registros 'Faltantes', crea los contactos si no existen,
        lee el XML original para extraer los conceptos exactos y 
        genera las facturas en estado Borrador.
        """
        partner_obj = self.env['res.partner']
        move_obj = self.env['account.move']
        facturas_creadas = 0
        
        # Filtramos para procesar únicamente los Faltantes
        faltantes = self.filtered(lambda r: r.match_state == 'missing')
        
        if not faltantes:
            raise exceptions.UserError("Solo se puede crear facturas para registros en estatus 'Faltante'.")
           
        for record in faltantes:
            
            if record.cfdi_type not in ['ingreso', 'egreso']:
                record.message_post(body=f"No se genera factura para documentos de tipo {record.cfdi_type}.")
                continue
            
            # Determinar tipo de comprobante y RFC
            if record.tipo_operacion == 'recibido':
                move_type = 'in_invoice'  # Factura de Proveedor
                rfc_target = record.rfc_emisor
                name_target = record.nombre_emisor
            elif record.tipo_operacion == 'emitido':
                move_type = 'out_invoice' # Factura de Cliente
                rfc_target = record.rfc_receptor
                name_target = record.nombre_receptor
            else:
                continue
                
            # Buscar o crear al Contacto
            partner = partner_obj.search([('vat', '=', rfc_target)], limit=1)
            if not partner:
                mx_country = self.env.ref('base.mx', raise_if_not_found=False)
                partner = partner_obj.create({
                    'name': name_target,
                    'vat': rfc_target,
                    'is_company': True,
                    'country_id': mx_country.id if mx_country else False
                })
                
            # Extraer conceptos
            invoice_lines = []
            try:
                xml_content = base64.b64decode(record.xml_file)
                cfdi = CFDI.from_string(xml_content)
                
                # Iteramos sobre cada partida de la factura
                conceptos = cfdi.get('Conceptos', [])
                for concepto in conceptos:
                    descripcion = concepto.get('Descripcion', 'Sin descripción')
                    cantidad = float(concepto.get('Cantidad', 1.0))
                    precio_unitario = float(concepto.get('ValorUnitario', 0.0))
                    
                    # Agregamos la línea al arreglo de creación de Odoo
                    invoice_lines.append((0, 0, {
                        'name': descripcion,
                        'quantity': cantidad,
                        'price_unit': precio_unitario,
                    }))
            except Exception as e:
                # Si falla la lectura, metemos el total global
                invoice_lines = [(0, 0, {
                    'name': 'Concepto global',
                    'quantity': 1,
                    'price_unit': record.amount_total,
                })]
                
            # Crear la Factura en borrador
            move_vals = {
                'move_type': move_type,
                'partner_id': partner.id,
                'invoice_date': record.date_emission,
                'l10n_mx_edi_cfdi_uuid': record.uuid,
                'ref': f"XML Automático",
                'invoice_line_ids': invoice_lines  # Inyectamos todas las partidas aquí
            }
            
            nuevo_move = move_obj.create(move_vals)
            facturas_creadas += 1
            
            # Adjuntar el XML a la factura
            self.env['ir.attachment'].create({
                'name': record.xml_filename,
                'type': 'binary',
                'datas': record.xml_file,
                'res_model': 'account.move',
                'res_id': nuevo_move.id,
                'mimetype': 'application/xml'
            })
            
            #Marcar como procesado
            record.write({
                'move_id': nuevo_move.id,
                'match_state': 'match'
            })
            
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }