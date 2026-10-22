import json
import logging
from datetime import timedelta
from urllib.parse import urlencode

from markupsafe import Markup

from odoo import SUPERUSER_ID, fields, http
from odoo.exceptions import UserError
from odoo.http import request

from ..crypto import sign_token, DsiLicenseKeyError

_logger = logging.getLogger(__name__)

GENERIC_ERROR = "No se pudo procesar la solicitud. Verifica la clave de licencia y el RFC."
# /trial es público y hace una petición saliente por cada solicitud: se
# limita por IP para que no se use para martillar a nadie.
TRIAL_MAX_PER_HOUR = 10


class DsiLicenseController(http.Controller):
    # Todas las rutas escriben (instancias, bitácora) y llevan readonly=False:
    # desde Odoo 19 una ruta auth='none' abre por omisión una transacción de
    # solo lectura cuando hay réplica, y como aquí se atrapan las excepciones
    # para contestar JSON, Odoo nunca se entera de que debía reintentar con
    # una de lectura-escritura.

    def _env(self):
        """Entorno como OdooBot. Las rutas auth='none' no tienen usuario, y el
        chatter (seguimiento de cambios, message_post) necesita un autor: sin
        él fallaba al vencer una licencia y al crear una prueba."""
        return request.env(user=SUPERUSER_ID)

    def _log_call(self, endpoint, license=None, instance=None, success=False,
                   request_data=None, response_data=None, error_message=None):
        try:
            request.env['dsi.license.log'].sudo().create({
                'endpoint': endpoint,
                'license_id': license.id if license else False,
                'instance_id': instance.id if instance else False,
                'success': success,
                'ip_address': request.httprequest.remote_addr,
                'request_data': json.dumps(request_data, default=str) if request_data else False,
                'response_data': json.dumps(response_data, default=str) if response_data else False,
                'error_message': error_message,
            })
            if request.env.cr:
                request.env.cr.commit()
        except Exception:
            _logger.exception("No se pudo escribir la bitácora de licenciamiento")

    @http.route('/dsi/license/v1/activate', type='http', methods=['POST'], auth='none', csrf=False,
                readonly=False)
    def activate(self, **kw):
        data = request.get_json_data()
        License = self._env()['dsi.license']
        license = License.search([('key', '=', (data.get('license_key') or '').strip())], limit=1)
        try:
            if not license:
                raise UserError(GENERIC_ERROR)
            rfc_in = (data.get('rfc') or '').strip().upper()
            if rfc_in != (license.rfc_authorized or '').strip().upper():
                raise UserError(GENERIC_ERROR)
            db_uuid = data.get('db_uuid')
            if not db_uuid:
                raise UserError("Falta db_uuid.")
            odoo_version = data.get('odoo_version') or ''
            license._check_usable(odoo_version)
            license._check_trial_activation(db_uuid, data.get('base_url'))
            instance = license._find_or_create_instance(
                db_uuid=db_uuid,
                base_url=data.get('base_url'),
                odoo_version=odoo_version,
                module_version=data.get('module_version'),
                is_production=bool(data.get('is_production', True)),
            )
            payload = license._build_token_payload(instance)
            token = sign_token(payload, env=request.env)
            response = {'token': token}
            self._log_call('activate', license, instance, success=True,
                            request_data=data, response_data=payload)
            return request.make_json_response(response)
        except DsiLicenseKeyError as e:
            _logger.error("Configuración de licenciamiento incompleta: %s", e)
            self._log_call('activate', license, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)
        except UserError as e:
            self._log_call('activate', license, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': str(e)}, status=400)
        except Exception as e:  # pylint: disable=broad-except
            _logger.exception("Error inesperado en /dsi/license/v1/activate")
            self._log_call('activate', license, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)

    @http.route('/dsi/license/v1/heartbeat', type='http', methods=['POST'], auth='none', csrf=False,
                readonly=False)
    def heartbeat(self, **kw):
        data = request.get_json_data()
        License = self._env()['dsi.license']
        license = License.search([('key', '=', (data.get('license_key') or '').strip())], limit=1)
        instance = license.instance_ids.filtered(
            lambda i: i.database_uuid == data.get('db_uuid')) if license else License.env['dsi.license.instance']
        try:
            if not license or not instance:
                raise UserError(GENERIC_ERROR)
            odoo_version = instance.odoo_version or ''
            license._check_usable(odoo_version)
            instance.date_last_heartbeat = fields.Datetime.now()
            payload = license._build_token_payload(instance)
            token = sign_token(payload, env=request.env)
            response = {'token': token}
            self._log_call('heartbeat', license, instance, success=True,
                            request_data=data, response_data=payload)
            return request.make_json_response(response)
        except DsiLicenseKeyError as e:
            self._log_call('heartbeat', license, instance, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)
        except UserError as e:
            self._log_call('heartbeat', license, instance, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': str(e)}, status=400)
        except Exception as e:  # pylint: disable=broad-except
            _logger.exception("Error inesperado en /dsi/license/v1/heartbeat")
            self._log_call('heartbeat', license, instance, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)

    @http.route('/dsi/license/v1/deactivate', type='http', methods=['POST'], auth='none', csrf=False,
                readonly=False)
    def deactivate(self, **kw):
        data = request.get_json_data()
        License = self._env()['dsi.license']
        license = License.search([('key', '=', (data.get('license_key') or '').strip())], limit=1)
        instance = license.instance_ids.filtered(
            lambda i: i.database_uuid == data.get('db_uuid')) if license else License.env['dsi.license.instance']
        try:
            if not license or not instance:
                raise UserError(GENERIC_ERROR)
            instance.action_release()
            self._log_call('deactivate', license, instance, success=True, request_data=data)
            return request.make_json_response({'ok': True})
        except Exception as e:  # pylint: disable=broad-except
            _logger.exception("Error inesperado en /dsi/license/v1/deactivate")
            self._log_call('deactivate', license, instance, success=False, request_data=data,
                            error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)

    @http.route('/dsi/license/v1/trial', type='http', methods=['POST'], auth='none', csrf=False,
                readonly=False)
    def trial(self, **kw):
        """La base del cliente pide su prueba sin pasar por el portal. Antes de
        darla se comprueba que la solicitud viene de la dirección que declara
        (ver dsi.license._start_trial)."""
        data = request.get_json_data()
        # El secreto del reto no se guarda en la bitácora.
        para_bitacora = {k: v for k, v in data.items() if k != 'challenge_secret'}
        license = instance = None
        try:
            hace_una_hora = fields.Datetime.now() - timedelta(hours=1)
            recientes = request.env['dsi.license.log'].sudo().search_count([
                ('endpoint', '=', 'trial'),
                ('ip_address', '=', request.httprequest.remote_addr),
                ('create_date', '>=', hace_una_hora),
            ])
            if recientes >= TRIAL_MAX_PER_HOUR:
                self._log_call('trial', success=False, request_data=para_bitacora,
                                error_message="Demasiadas solicitudes desde esta IP")
                return request.make_json_response(
                    {'error': "Demasiadas solicitudes. Intenta de nuevo en una hora."}, status=429)
            license, instance = self._env()['dsi.license']._start_trial(data)
            payload = license._build_token_payload(instance)
            token = sign_token(payload, env=request.env)
            self._log_call('trial', license, instance, success=True,
                            request_data=para_bitacora, response_data=payload)
            return request.make_json_response({'token': token, 'license_key': license.key})
        except DsiLicenseKeyError as e:
            _logger.error("Configuración de licenciamiento incompleta: %s", e)
            request.env.cr.rollback()
            self._log_call('trial', success=False, request_data=para_bitacora, error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)
        except UserError as e:
            # Nada a medias: la bitácora hace commit y no debe llevarse un
            # contacto o una licencia que no llegó a entregarse.
            request.env.cr.rollback()
            self._log_call('trial', success=False, request_data=para_bitacora, error_message=str(e))
            return request.make_json_response({'error': str(e)}, status=400)
        except Exception as e:  # pylint: disable=broad-except
            _logger.exception("Error inesperado en /dsi/license/v1/trial")
            request.env.cr.rollback()
            self._log_call('trial', success=False, request_data=para_bitacora, error_message=str(e))
            return request.make_json_response({'error': GENERIC_ERROR}, status=500)

    @http.route('/dsi/license/v1/comprar', type='http', methods=['GET'], auth='none', csrf=False,
                readonly=False)
    def comprar(self, clave=None, producto=None, **kw):
        """Destino del botón «Comprar licencia» de los clientes. Redirige a la
        URL de compra del producto, que se define aquí: los clientes solo
        conocen esta dirección, así que la tienda puede cambiar sin tocarlos.
        Cada clic queda en la bitácora: es un prospecto."""
        env = self._env()
        license = env['dsi.license']
        if clave:
            license = license.search([('key', '=', clave.strip())], limit=1)
        product = license.product_id or env['dsi.license.product'].search(
            [('code', '=', producto or 'cfdi_vault')], limit=1)
        self._log_call('purchase', license or None, success=bool(product.purchase_url),
                        request_data={'clave': clave, 'producto': producto})
        if product.purchase_url:
            destino = product.purchase_url
            if license:
                destino += ('&' if '?' in destino else '?') + urlencode({'clave': license.key})
            return request.redirect(destino, local=False)
        return request.make_response(Markup(
            '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Comprar licencia</title></head>'
            '<body style="font-family: sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">'
            '<h1>La compra en línea todavía no está disponible</h1>'
            '<p>Escríbenos y te ayudamos a contratar tu licencia de %s.</p>'
            '</body></html>') % (product.name or "DS Indigo"),
            headers=[('Content-Type', 'text/html; charset=utf-8')])
