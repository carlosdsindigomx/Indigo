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
        default=lambda self: self.env['sat.hub.tenant'].search([], limit=1)
    )
    
    tipo_solicitud = fields.Selection([
        ('cfdi', 'CFDI'),
        ('metadata', 'Metadata')
    ], string='Tipo de Contenido', default='cfdi', required=True)
    
    tipo_operacion = fields.Selection([
        ('emitido', 'Emitidos'),
        ('recibido', 'Recibidos')
    ], string='Tipo de Operación', default='recibido', required=True)

    date_start = fields.Datetime(string='Fecha Inicial', required=True)
    date_end = fields.Datetime(string='Fecha Final', required=True)
    
    # Datos retornados por el Web Service del SAT
    id_solicitud = fields.Char(string='ID de solicitud', readonly=True, copy=False)
    cod_estatus = fields.Char(string='Código estatus', readonly=True, copy=False)
    mensaje = fields.Text(string='Mensaje', readonly=True, copy=False)
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('requested', 'Solicitado'),
        ('verified', 'Verificado'),
        ('downloaded', 'Descargado'),
        ('error', 'Error')
    ], string='Estatus', default='draft', readonly=True, copy=False)

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
                # 1. Desencriptar y cargar la FIEL en memoria
                cert_bytes = base64.b64decode(tenant.fiel_cert)
                key_bytes = base64.b64decode(tenant.fiel_key)
                
                signer = Signer.load(
                    certificate=cert_bytes,
                    key=key_bytes,
                    password=tenant.fiel_password
                )
                
                # 2. Inicializar el cliente oficial del SAT
                cliente_sat = SAT(signer=signer)
                
                # Convertimos el string del selection al Enum que espera satcfdi
                tipo_descarga_enum = TipoDescargaMasivaTerceros.CFDI if record.tipo_solicitud == 'cfdi' else TipoDescargaMasivaTerceros.METADATA
                
                # 3. Disparar petición síncrona usando los métodos V1.5
                if record.tipo_operacion == 'emitido':
                    res = cliente_sat.recover_comprobante_emitted_request(
                        fecha_inicial=record.date_start,
                        fecha_final=record.date_end,
                        rfc_emisor=tenant.rfc,
                        tipo_solicitud=tipo_descarga_enum,
                        estado_comprobante=EstadoComprobante.VIGENTE  # Parámetro obligatorio para XMLs
                    )
                else:  # 'recibido'
                    res = cliente_sat.recover_comprobante_received_request(
                        fecha_inicial=record.date_start,
                        fecha_final=record.date_end,
                        rfc_receptor=tenant.rfc,
                        tipo_solicitud=tipo_descarga_enum,
                        estado_comprobante=EstadoComprobante.VIGENTE
                    )
                
                # 4. Analizar la respuesta del árbol SOAP devuelto por Hacienda
                cod_estatus = res.get('CodEstatus')
                id_solicitud = res.get('IdSolicitud')
                mensaje = res.get('Mensaje')
                
                # Código 5000 significa que el SAT aceptó abrirnos el turno de descarga con éxito
                if cod_estatus == '5000' and id_solicitud:
                    record.write({
                        'id_solicitud': id_solicitud,
                        'cod_estatus': cod_estatus,
                        'mensaje': mensaje or 'Solicitud aceptada exitosamente.',
                        'state': 'requested'
                    })
                else:
                    # Captura de errores de negocio (Ej. 5002 Solicitudes agotadas o 5005 ya existente)
                    record.write({
                        'cod_estatus': cod_estatus,
                        'mensaje': mensaje or 'El SAT rechazó la petición por reglas de negocio.',
                        'state': 'error'
                    })
                    
            except requests.exceptions.HTTPError as http_err:
                error_body = http_err.response.text if http_err.response is not None else str(http_err)
                _logger.error("Error HTTP del SAT. Detalle XML: %s", error_body)
                record.write({
                    'mensaje': f'SOAP Fault. Revisa el log. Detalle: {error_body[:200]}...',
                    'state': 'error'
                })
            except Exception as e:
                if hasattr(e, 'response') and e.response is not None:
                    error_body = e.response.text
                    _logger.error("Error del SAT. Detalle XML: %s", error_body)
                    record.write({
                        'mensaje': f'SOAP Fault. Revisa el log. Detalle: {error_body[:200]}...',
                        'state': 'error'
                    })
                else:
                    _logger.error("Fallo crítico al conectar con el SAT: %s", str(e))
                    record.write({
                        'mensaje': f'Fallo de comunicación: {str(e)}',
                        'state': 'error'
                    })
                    
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
                # 1. Preparar la firma y el cliente SAT (igual que en la petición)
                cert_bytes = base64.b64decode(tenant.fiel_cert)
                key_bytes = base64.b64decode(tenant.fiel_key)
                
                signer = Signer.load(
                    certificate=cert_bytes,
                    key=key_bytes,
                    password=tenant.fiel_password
                )
                cliente_sat = SAT(signer=signer)

                # 2. Consultar el estado de la descarga en el SAT
                res_status = cliente_sat.recover_comprobante_status(record.id_solicitud)
                est_solicitud = res_status.get("EstadoSolicitud")
                mensaje_estado = res_status.get("Mensaje")

                if est_solicitud == EstadoSolicitud.TERMINADA:
                    # 3. El SAT ya procesó los XMLs. Procedemos a descargar los paquetes ZIP.
                    paquetes_ids = res_status.get('IdsPaquetes', [])
                    
                    if not paquetes_ids:
                        record.write({
                            'state': 'verified',
                            'mensaje': 'El SAT terminó la consulta pero indicó que NO hay XMLs para ese rango de fechas.'
                        })
                        continue

                    # Descargar cada paquete disponible
                    for id_paquete in paquetes_ids:
                        res_descarga, paquete_b64 = cliente_sat.recover_comprobante_download(
                            id_paquete=id_paquete
                        )
                        
                        # TRUCO DE ODOO: El SAT nos devuelve el ZIP en Base64. 
                        # Odoo guarda los archivos adjuntos exactamente en Base64, así que 
                        # podemos pasarlo directo sin necesidad de decodificarlo.
                        self.env['ir.attachment'].create({
                            'name': f'SAT_Paquete_{id_paquete}.zip',
                            'type': 'binary',
                            'datas': paquete_b64,
                            'res_model': self._name,
                            'res_id': record.id,
                            'mimetype': 'application/zip'
                        })

                    # Actualizamos el registro marcándolo como exitoso
                    record.write({
                        'state': 'downloaded',
                        'mensaje': f'¡Éxito! Se descargaron {len(paquetes_ids)} paquetes ZIP. Revisa los adjuntos de este registro.'
                    })

                elif est_solicitud == EstadoSolicitud.ACEPTADA or est_solicitud == EstadoSolicitud.ENPROCESO:
                    # El SAT sigue trabajando en juntar los archivos
                    record.write({
                        'mensaje': f'El SAT sigue procesando la solicitud (Estado: {est_solicitud}). Intenta de nuevo en unos minutos.'
                    })
                
                elif est_solicitud == EstadoSolicitud.RECHAZADA or est_solicitud == EstadoSolicitud.ERROR:
                    # El SAT rechazó la petición internamente
                    record.write({
                        'state': 'error',
                        'mensaje': f'El SAT rechazó la solicitud o marcó error. Detalle: {mensaje_estado}'
                    })
                    
            except Exception as e:
                _logger.error("Error al verificar/descargar en el SAT: %s", str(e))
                raise exceptions.UserError(f"Ocurrió un error al comunicarse con el SAT: {str(e)}")
            
            
    def action_extraer_e_importar_xml(self):
        """
        Busca los paquetes ZIP adjuntos a esta solicitud, los descomprime en memoria,
        extrae los archivos XML y los inyecta de forma masiva en la mesa de Auditoría.
        """
        self.ensure_one()
        if self.state != 'downloaded':
            raise exceptions.UserError("La solicitud debe estar en estado 'Paquetes Descargados' para poder extraerlos.")

        # 1. Buscar los archivos adjuntos tipo ZIP vinculados a este registro de solicitud
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

        for attachment in attachments:
            zip_bytes = base64.b64decode(attachment.datas)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zfile:
                for file_name in zfile.namelist():
                    if file_name.lower().endswith('.xml'):
                        xml_content = zfile.read(file_name)
                        xml_base64 = base64.b64encode(xml_content)
                        nuevo_registro = raw_xml_obj.create({
                            'xml_file': xml_base64,
                            'xml_filename': file_name,
                            'match_state': 'pending'

                        })
                        # Los acumulamos en un recordset de Odoo para procesarlos juntos al final
                        registros_creados |= nuevo_registro
                        xml_count += 1

        if xml_count > 0:
            try:
                registros_creados.action_procesar_xml()
            except Exception as e:
                _logger.warning("Los XML se extrajeron con éxito pero falló el parseo automático: %s", str(e))

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Extracción Exitosa',
                    'message': f'Se extrajeron e importaron {xml_count} facturas XML.',
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise exceptions.UserError("El paquete ZIP se abrió correctamente, pero no contenía ningún archivo con extensión .xml adentro.")