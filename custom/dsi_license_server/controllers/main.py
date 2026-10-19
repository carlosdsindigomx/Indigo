import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from ..crypto import sign_token, DsiLicenseKeyError

_logger = logging.getLogger(__name__)

GENERIC_ERROR = "No se pudo procesar la solicitud. Verifica la clave de licencia y el RFC."


class DsiLicenseController(http.Controller):

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

    @http.route('/dsi/license/v1/activate', type='http', methods=['POST'], auth='none', csrf=False)
    def activate(self, **kw):
        data = request.get_json_data()
        License = request.env['dsi.license'].sudo()
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
            instance = license._find_or_create_instance(
                db_uuid=db_uuid,
                base_url=data.get('base_url'),
                odoo_version=odoo_version,
                module_version=data.get('module_version'),
                is_production=bool(data.get('is_production', True)),
            )
            payload = license._build_token_payload()
            payload['db_uuid'] = db_uuid
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

    @http.route('/dsi/license/v1/heartbeat', type='http', methods=['POST'], auth='none', csrf=False)
    def heartbeat(self, **kw):
        data = request.get_json_data()
        License = request.env['dsi.license'].sudo()
        license = License.search([('key', '=', (data.get('license_key') or '').strip())], limit=1)
        instance = license.instance_ids.filtered(
            lambda i: i.database_uuid == data.get('db_uuid')) if license else License.env['dsi.license.instance']
        try:
            if not license or not instance:
                raise UserError(GENERIC_ERROR)
            odoo_version = instance.odoo_version or ''
            license._check_usable(odoo_version)
            instance.date_last_heartbeat = fields.Datetime.now()
            payload = license._build_token_payload()
            payload['db_uuid'] = instance.database_uuid
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

    @http.route('/dsi/license/v1/deactivate', type='http', methods=['POST'], auth='none', csrf=False)
    def deactivate(self, **kw):
        data = request.get_json_data()
        License = request.env['dsi.license'].sudo()
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
