import base64
from datetime import datetime, timedelta
import logging
from odoo import models, fields, api, exceptions

# NUEVAS RUTAS EXACTAS PARA satcfdi 4.4.16
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
                    'title': '¡Firma Validada!',
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