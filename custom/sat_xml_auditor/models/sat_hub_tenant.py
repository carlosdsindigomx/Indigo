import base64
from datetime import datetime, timedelta
import logging
from odoo import models, fields, api, exceptions

from satcfdi.models import Signer
from satcfdi.pacs.sat import SAT

_logger = logging.getLogger(__name__)

class SatHubTenant(models.Model):
    _name = 'sat.hub.tenant'
    _description = 'Configuración y credenciales del cliente'
    _rec_name = 'name'

    partner_id = fields.Many2one(
        comodel_name='res.partner', 
        string='Contacto',
    )
    
    name = fields.Char(string='Razón social', required=True)
    rfc = fields.Char(string='RFC', required=True)
    
    fiel_cert = fields.Binary(string='Certificado .cer', required=True)
    fiel_cert_name = fields.Char(string='Nombre del .cer')
    
    fiel_key = fields.Binary(string='Llave privada .key', required=True)
    fiel_key_name = fields.Char(string='Nombre del .key')
    
    fiel_password = fields.Char(string='Contraseña', required=True)
    
    # Datos de control del Token de Autenticación
    token_sat = fields.Text(string='Token', readonly=True)
    token_expiration = fields.Datetime(string='Vigencia del token', readonly=True)
    
    auto_download = fields.Boolean(string='Descarga automática', default=False)
    auto_download_days = fields.Integer(
        string='Días hacia atrás', 
        default=1,
        help="Número de días hacia atrás que el sistema consultará automáticamente."
    )
    
    @api.model
    def _cron_solicitar_descargas_diarias(self):
        """
        Método llamado por el Cron para generar peticiones automáticas.
        Busca todos los tenants que tengan la opción activada.
        """
        tenants_activos = self.search([('auto_download', '=', True)])
        request_obj = self.env['sat.xml.download.request']
        
        for tenant in tenants_activos:
            try:
                # Calcular fechas
                hoy = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                fecha_inicio = hoy - timedelta(days=tenant.auto_download_days)
                fecha_fin = hoy - timedelta(seconds=1)

                # Crear solicitud para Emitidos
                req_emitidos = request_obj.create({
                    'tenant_id': tenant.id,
                    'tipo_solicitud': 'cfdi',
                    'tipo_operacion': 'emitido',
                    'date_start': fecha_inicio,
                    'date_end': fecha_fin,
                })
                req_emitidos.action_solicitar_sat()

                # Crear solicitud para Recibidos
                req_recibidos = request_obj.create({
                    'tenant_id': tenant.id,
                    'tipo_solicitud': 'cfdi',
                    'tipo_operacion': 'recibido',
                    'date_start': fecha_inicio,
                    'date_end': fecha_fin,
                })
                req_recibidos.action_solicitar_sat()
                
                self.env.cr.commit() 
                
            except Exception as e:
                _logger.error(f"Error en cron de solicitud automática para el cliente {tenant.name}: {str(e)}")
    
    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        if self.partner_id:
            self.name = self.partner_id.name
            if self.partner_id.vat:
                self.rfc = self.partner_id.vat

    def action_probar_conexion_sat(self):
        """
        Desencripta la FIEL y prepara la conexión SAT.
        """
        self.ensure_one()

        if not self.fiel_cert or not self.fiel_key or not self.fiel_password:
            raise exceptions.UserError("Por favor, sube los archivos de tu e.firma y su contraseña.")

        try:
            # Decodificar los archivos binarios
            cert_bytes = base64.b64decode(self.fiel_cert)
            key_bytes = base64.b64decode(self.fiel_key)
            password_str = self.fiel_password

            # Inicializar el Signer
            try:
                signer = Signer.load(
                    certificate=cert_bytes,
                    key=key_bytes,
                    password=password_str
                )
            except Exception as e:
                raise exceptions.UserError(
                    f"No se pudo desencriptar la e.firma. Verifica la contraseña y los archivos. Error técnico: {str(e)}"
                )

            # Validar el RFC
            if signer.rfc != self.rfc:
                raise exceptions.UserError(
                    f"El RFC de la e.firma ({signer.rfc}) no coincide con el RFC configurado en este formulario ({self.rfc})."
                )

            ahora = fields.Datetime.now()
            caducidad = ahora + timedelta(minutes=5)

            self.write({
                'token_sat': f'Autenticado',
                'token_expiration': caducidad
            })
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Firma Validada',
                    'message': f'La e.firma de {signer.rfc} es correcta.',
                    'type': 'success',
                    'sticky': False,
                }
            }

        except exceptions.UserError as ue:
            raise ue
        except Exception as e:
            _logger.error("Error al autenticar ante el SAT: %s", str(e))
            raise exceptions.UserError(f"Error interno de validación: {str(e)}")