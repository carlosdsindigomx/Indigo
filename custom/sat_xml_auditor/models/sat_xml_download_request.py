import base64
import io
import logging
import zipfile
import requests
from odoo import models, fields, api, exceptions
from satcfdi.models import Signer
from satcfdi.pacs.sat import SAT, TipoDescargaMasivaTerceros, EstadoComprobante, EstadoSolicitud


_logger = logging.getLogger(__name__)

class SatXmlDownloadRequest(models.Model):
    _name = 'sat.xml.download.request'
    _description = 'Solicitud de Descarga Masiva SAT'
    _order = 'create_date desc'
    _rec_name = 'id_solicitud'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    tenant_id = fields.Many2one(
        comodel_name='sat.hub.tenant', 
        string='Empresa', 
        required=True, 
        default=lambda self: self.env['sat.hub.tenant'].search([], limit=1),
        tracking=True
    )
    
    tipo_solicitud = fields.Selection([
        ('cfdi', 'CFDI'),
        ('metadata', 'Metadata')
    ], string='Tipo de Contenido', default='cfdi', required=True, tracking=True)
    
    tipo_operacion = fields.Selection([
        ('emitido', 'Emitidos'),
        ('recibido', 'Recibidos')
    ], string='Tipo de Operación', default='recibido', required=True, tracking=True)

    date_start = fields.Datetime(string='Fecha Inicial', required=True, tracking=True)
    date_end = fields.Datetime(string='Fecha Final', required=True, tracking=True)
    
    # Datos retornados por el Web Service del SAT
    id_solicitud = fields.Char(string='ID de solicitud', readonly=True, copy=False, tracking=True)
    cod_estatus = fields.Char(string='Código estatus', readonly=True, copy=False)
    mensaje = fields.Text(string='Mensaje', readonly=True, copy=False)
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('requested', 'Solicitado'),
        ('verified', 'Verificado'),
        ('downloaded', 'Descargado'),
        ('error', 'Error')
    ], string='Estatus', default='draft', readonly=True, copy=False, tracking=True)
    
    @api.constrains('date_start', 'date_end', 'tipo_operacion', 'tipo_solicitud', 'tenant_id')
    def _check_overlapping_requests(self):
        """
        Evita que se creen o envíen solicitudes que se empalmen en fechas 
        con peticiones que ya están en proceso o descargadas.
        """
        for record in self:
            if not record.date_start or not record.date_end:
                continue

            # La lógica de intersección de fechas es: 
            # (Inicio_A <= Fin_B) y (Fin_A >= Inicio_B)
            domain = [
                ('id', '!=', record.id),
                ('tenant_id', '=', record.tenant_id.id),
                ('tipo_operacion', '=', record.tipo_operacion),
                ('tipo_solicitud', '=', record.tipo_solicitud),
                ('state', 'in', ['requested', 'verified', 'downloaded']), 
                ('date_start', '<=', record.date_end),
                ('date_end', '>=', record.date_start),
            ]
            
            empalme = self.search(domain, limit=1)
            
            if empalme:
                fecha_ini_str = empalme.date_start.strftime('%d/%m/%Y %H:%M')
                fecha_fin_str = empalme.date_end.strftime('%d/%m/%Y %H:%M')
                
                raise exceptions.ValidationError(
                    f"Conflicto de fechas. Ya existe una solicitud "
                    f"({empalme.id_solicitud or 'En proceso'}) para comprobantes "
                    f"'{record.tipo_operacion}' que abarca del {fecha_ini_str} al {fecha_fin_str}.\n\n"
                    f"Por favor, ajusta las fechas."
                )
    
    @api.model
    def _cron_verificar_y_extraer_descargas(self):
        """
        Método llamado por el Cron para verificar el estatus en el SAT.
        Procesa solicitudes en estado 'requested' o 'verified'.
        """
        # Buscamos las que están pendientes de verificar
        solicitudes_pendientes = self.search([('state', 'in', ['requested', 'verified'])])
        
        for solicitud in solicitudes_pendientes:
            try:
                # Reutilizamos tu método manual
                solicitud.action_verificar_descargar_sat()
                
                # Si la verificación fue exitosa y pasó a estado descargado, extraemos
                if solicitud.state == 'downloaded':
                    solicitud.action_extraer_e_importar_xml()
                    
                self.env.cr.commit()
            except Exception as e:
                _logger.error(f"Error en cron de verificación SAT para la solicitud {solicitud.id}: {str(e)}")

    def action_solicitar_sat(self):
        """
        Genera el SOAP Envelope firmado usando los nuevos métodos V1.5 del SAT
        y almacena el IdSolicitud devuelto por Hacienda.
        """
        for record in self:
            if record.state != 'draft':
                raise exceptions.UserError("Esta solicitud ya fue enviada previamente al SAT.")
            
            tenant = record.tenant_id
            if not tenant.fiel_cert or not tenant.fiel_key or not tenant.fiel_password:
                raise exceptions.UserError(f"La empresa {tenant.name} no tiene credenciales de e.firma cargadas.")

            try:
                cert_bytes = base64.b64decode(tenant.fiel_cert)
                key_bytes = base64.b64decode(tenant.fiel_key)
                
                signer = Signer.load(
                    certificate=cert_bytes,
                    key=key_bytes,
                    password=tenant.fiel_password
                )
                
                cliente_sat = SAT(signer=signer)
                
                tipo_descarga_enum = TipoDescargaMasivaTerceros.CFDI if record.tipo_solicitud == 'cfdi' else TipoDescargaMasivaTerceros.METADATA
                
                if record.tipo_operacion == 'emitido':
                    res = cliente_sat.recover_comprobante_emitted_request(
                        fecha_inicial=record.date_start,
                        fecha_final=record.date_end,
                        rfc_emisor=tenant.rfc,
                        tipo_solicitud=tipo_descarga_enum,
                        estado_comprobante=EstadoComprobante.VIGENTE  
                    )
                else: 
                    res = cliente_sat.recover_comprobante_received_request(
                        fecha_inicial=record.date_start,
                        fecha_final=record.date_end,
                        rfc_receptor=tenant.rfc,
                        tipo_solicitud=tipo_descarga_enum,
                        estado_comprobante=EstadoComprobante.VIGENTE
                    )
                
                cod_estatus = res.get('CodEstatus')
                id_solicitud = res.get('IdSolicitud')
                mensaje = res.get('Mensaje')
                
                if cod_estatus == '5000' and id_solicitud:
                    record.write({
                        'id_solicitud': id_solicitud,
                        'cod_estatus': cod_estatus,
                        'mensaje': mensaje or 'Solicitud aceptada exitosamente.',
                        'state': 'requested'
                    })
                    record.message_post(body=f"Solicitud aceptada exitosamente: {id_solicitud}")
                else:
                    msg_error = mensaje or 'El SAT rechazó la petición.'
                    record.write({
                        'cod_estatus': cod_estatus,
                        'mensaje': msg_error,
                        'state': 'error'
                    })
                    record.message_post(body=f"Rechazo del SAT: {msg_error} Código: {cod_estatus}")
                    
            except requests.exceptions.HTTPError as http_err:
                error_body = http_err.response.text if http_err.response is not None else str(http_err)
                _logger.error("Error Detalle XML: %s", error_body)
                record.write({
                    'mensaje': f'SOAP Fault. Revisa el log. Detalle: {error_body[:200]}...',
                    'state': 'error'
                })
                record.message_post(body=f"Error de comunicación HTTP: {str(http_err)}")
            except Exception as e:
                if hasattr(e, 'response') and e.response is not None:
                    error_body = e.response.text
                    _logger.error("Error del SAT. Detalle XML: %s", error_body)
                    record.write({
                        'mensaje': f'SOAP Fault. Revisa el log. Detalle: {error_body[:200]}...',
                        'state': 'error'
                    })
                else:
                    _logger.error("Fallo al conectar con el SAT: %s", str(e))
                    record.write({
                        'mensaje': f'Fallo de comunicación: {str(e)}',
                        'state': 'error'
                    })
                record.message_post(body=f"Error Crítico: {str(e)}")
                    
    def action_verificar_descargar_sat(self):
        """
        Consulta el estatus de la solicitud en el SAT. Si está terminada, 
        descarga los paquetes ZIP y los guarda como archivos adjuntos en Odoo.
        """
        for record in self:
            if record.state not in ['requested', 'verified']:
                raise exceptions.UserError("La solicitud debe estar en estado 'Solicitado' para poder verificarse.")
            
            if not record.id_solicitud:
                raise exceptions.UserError("No hay un ID de Solicitud válido para consultar.")

            tenant = record.tenant_id
            
            try:
                cert_bytes = base64.b64decode(tenant.fiel_cert)
                key_bytes = base64.b64decode(tenant.fiel_key)
                
                signer = Signer.load(
                    certificate=cert_bytes,
                    key=key_bytes,
                    password=tenant.fiel_password
                )
                cliente_sat = SAT(signer=signer)

                res_status = cliente_sat.recover_comprobante_status(record.id_solicitud)
                est_solicitud = res_status.get("EstadoSolicitud")
                mensaje_estado = res_status.get("Mensaje")
                
                codigo_estado = res_status.get("CodigoEstadoSolicitud")

                if est_solicitud == EstadoSolicitud.TERMINADA:
                    paquetes_ids = res_status.get('IdsPaquetes', [])
                    
                    if not paquetes_ids:
                        record.write({
                            'state': 'verified',
                            'mensaje': 'Terminó la consulta pero NO hay XMLs para ese rango de fechas.'
                        })
                        record.message_post(body="Consulta Finalizada: No hay facturas en el rango de fechas seleccionado.")
                        continue

                    for id_paquete in paquetes_ids:
                        res_descarga, paquete_b64 = cliente_sat.recover_comprobante_download(
                            id_paquete=id_paquete
                        )
                        
                        self.env['ir.attachment'].create({
                            'name': f'SAT_Paquete_{id_paquete}.zip',
                            'type': 'binary',
                            'datas': paquete_b64,
                            'res_model': self._name,
                            'res_id': record.id,
                            'mimetype': 'application/zip'
                        })

                    msg_exito = f'Se descargaron {len(paquetes_ids)} paquetes ZIP.'
                    record.write({
                        'state': 'downloaded',
                        'mensaje': msg_exito + ' Revisa los adjuntos de este registro.'
                    })
                    record.message_post(body=f"Descarga Exitosa: {msg_exito}")

                elif est_solicitud == EstadoSolicitud.ACEPTADA or est_solicitud == EstadoSolicitud.EN_PROCESO:
                    msg_proceso = f'Procesando la solicitud. Estado: {est_solicitud}). Intenta de nuevo en unos minutos.'
                    record.write({
                        'mensaje': msg_proceso
                    })
                    record.message_post(body=f"Intento de verificación: {msg_proceso}")
                
                elif est_solicitud == EstadoSolicitud.RECHAZADA:
                    if str(codigo_estado) == '5004':
                        msg_vacio = 'No se encontró facturas para este RFC en el rango de fechas solicitado.'
                        record.write({
                            'state': 'verified', 
                            'mensaje': msg_vacio
                        })
                        record.message_post(body=f"Consulta Finalizada: {msg_vacio}")
                    else:
                        msg_rechazo = f'La solicitud fue rechazada. Código: {codigo_estado}. Detalles: {mensaje_estado}'
                        record.write({
                            'state': 'error',
                            'mensaje': msg_rechazo
                        })
                        record.message_post(body=f"Rechazo de Solicitud: {msg_rechazo}")
                    
            except Exception as e:
                _logger.error("Error al verificar/descargar: %s", str(e))
                record.message_post(body=f"Error general de conexión: {str(e)}")
                raise exceptions.UserError(f"Ocurrió un error en la comunicación: {str(e)}")
            
            
    def action_extraer_e_importar_xml(self):
        """
        Busca los paquetes ZIP adjuntos a esta solicitud, los descomprime en memoria,
        extrae los archivos XML, verifica que no existan previamente y los inyecta.
        """
        self.ensure_one()
        if self.state != 'downloaded':
            raise exceptions.UserError("La solicitud debe estar en estado 'Descargado' para poder extraerlos.")

        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('name', 'like', '%.zip')
        ])

        if not attachments:
            raise exceptions.UserError("No se encontraron archivos ZIP adjuntos en este registro para procesar.")

        raw_xml_obj = self.env['sat.xml.raw']
        registros_creados = self.env['sat.xml.raw']
        xml_count = 0
        xml_duplicados = 0

        for attachment in attachments:
            zip_bytes = base64.b64decode(attachment.datas)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zfile:
                for file_name in zfile.namelist():
                    if file_name.lower().endswith('.xml'):
                        xml_content = zfile.read(file_name)
                        
                        uuid = False
                        try:
                            cfdi = CFDI.from_string(xml_content)
                            complemento = cfdi.get('Complemento', {})
                            if 'TimbreFiscalDigital' in complemento:
                                uuid = complemento['TimbreFiscalDigital'].get('UUID')
                        except Exception:
                            pass
                        
                        dominio = [('uuid', '=', uuid)] if uuid else [('xml_filename', '=', file_name)]
                        existe = raw_xml_obj.search(dominio, limit=1)
                        
                        if existe:
                            xml_duplicados += 1
                            continue

                        xml_base64 = base64.b64encode(xml_content)
                        nuevo_registro = raw_xml_obj.create({
                            'xml_file': xml_base64,
                            'xml_filename': file_name,
                            'match_state': 'pending'
                        })
                        registros_creados |= nuevo_registro
                        xml_count += 1

        if xml_count > 0:
            try:
                registros_creados.action_procesar_xml()
            except Exception as e:
                _logger.warning("Los XML se extrajeron con éxito pero falló el parseo automático: %s", str(e))
                self.message_post(body=f"Advertencia de parseo: Los XML se extrajeron, pero hubo un error al leer la información interna: {str(e)}")

        mensaje_final = f'Se importaron {xml_count} facturas XML. '
        if xml_duplicados > 0:
            mensaje_final += f'Se omitieron {xml_duplicados} archivos que ya estaban cargados previamente.'

        if xml_count > 0 or xml_duplicados > 0:
            self.message_post(body=mensaje_final)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Extracción Finalizada',
                    'message': mensaje_final,
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise exceptions.UserError("El paquete ZIP no contenía ningún archivo XML válido o todos ya habían sido importados.")