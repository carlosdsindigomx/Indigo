import logging
import random
from datetime import datetime, timedelta

import requests

from odoo import _, api, fields, models, release
from odoo.exceptions import UserError

from ..crypto import verify_and_decode_token

_logger = logging.getLogger(__name__)

GRACE_DAYS = 15
REQUEST_TIMEOUT = 5

# Dirección del servidor de licencias de DS Indigo. Viene fija de fábrica con
# el módulo — el cliente NUNCA debe verla ni poder editarla desde la interfaz
# (por eso el campo es readonly y no aparece en la vista). Si algún día hay
# que apuntar a otra dirección, se cambia esta constante y se libera una
# actualización del módulo — no es una opción de configuración del cliente.
DEFAULT_ACTIVATION_URL = "https://obi-dsindigo.odoo.com"


def _param(env, clave, por_omision=''):
    """Lee un parámetro de sistema en Odoo 19 y en Odoo 20.

    Odoo 20 sustituyó el `get_param` genérico por accesores tipados
    (`get_str`, `get_int`, `get_bool`, `get_float`). Todos los parámetros que
    usamos son cadenas, así que basta con `get_str` cuando existe.
    """
    cp = env['ir.config_parameter'].sudo()
    if hasattr(cp, 'get_str'):
        return cp.get_str(clave, por_omision)
    return cp.get_param(clave, por_omision)


class DsiLicenseClientState(models.Model):
    _name = 'dsi.license.client.state'
    _description = "Estado de licenciamiento DS Indigo de esta base"
    _inherit = ['mail.thread']

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _("Licenciamiento — %s", rec.company_id.name)

    activation_url = fields.Char(
        string="Servidor de licencias", required=True, readonly=True,
        default=lambda self: DEFAULT_ACTIVATION_URL,
        help="Dirección fija del servidor de licencias de DS Indigo. No es configurable por el "
             "cliente: viene de fábrica con el módulo y solo cambia mediante una actualización.")
    license_key = fields.Char(string="Clave de licencia", tracking=True)
    token = fields.Text(string="Token firmado")
    token_expires = fields.Date(string="Token válido hasta")
    license_end = fields.Date(string="Licencia vigente hasta")
    edition = fields.Char()
    features = fields.Char(help="Lista separada por comas de las funciones habilitadas por el token.")
    state = fields.Selection([
        ('not_activated', "Sin activar"),
        ('active', "Vigente"),
        ('grace', "En gracia"),
        ('restricted', "Restringido"),
    ], compute='_compute_state', store=True)
    last_check = fields.Datetime(string="Última verificación")
    last_error = fields.Char(string="Último error", tracking=True)

    _company_uniq = models.Constraint(
        'unique(company_id)', "Ya existe un registro de licenciamiento para esta empresa.")

    @api.depends('token_expires')
    def _compute_state(self):
        # Comparación en UTC, nunca en la zona horaria del usuario/compañía:
        # cerca de medianoche, context_today() puede adelantar o atrasar un
        # día el estado vigente/gracia/restringido según la tz configurada.
        today = datetime.utcnow().date()
        for rec in self:
            if not rec.token_expires:
                rec.state = 'not_activated'
            elif today <= rec.token_expires:
                rec.state = 'active'
            elif today <= rec.token_expires + timedelta(days=GRACE_DAYS):
                rec.state = 'grace'
            else:
                rec.state = 'restricted'

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @api.model
    def _get_or_create(self, company=None):
        company = company or self.env.company
        rec = self.sudo().search([('company_id', '=', company.id)], limit=1)
        if not rec:
            rec = self.sudo().create({'company_id': company.id})
        return rec

    def _get_db_uuid(self):
        return _param(self.env, 'database.uuid')

    def _get_base_url(self):
        return _param(self.env, 'web.base.url')

    def _get_module_version(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'dsi_license_client')], limit=1)
        return module.installed_version or False

    def _get_proxies(self):
        """Soporte de proxy corporativo: parámetro de sistema opcional
        'dsi_license_client.proxy_url', usado tanto para http como https."""
        proxy_url = _param(self.env, 'dsi_license_client.proxy_url')
        if not proxy_url:
            return None
        return {'http': proxy_url, 'https': proxy_url}

    def _apply_token(self, token):
        """Verifica localmente el token y actualiza los campos derivados."""
        self.ensure_one()
        payload = verify_and_decode_token(token)
        self.write({
            'token': token,
            'token_expires': payload.get('token_expires'),
            'license_end': payload.get('license_end'),
            'edition': payload.get('edition'),
            'features': ','.join(payload.get('features') or []),
            'last_check': fields.Datetime.now(),
            'last_error': False,
        })

    def is_feature_enabled(self, feature):
        """Punto de entrada para que otros módulos (ej. dsi_cfdi_vault) pregunten
        si pueden operar. Nunca bloquea el arranque del módulo: solo se llama
        cuando el usuario ejecuta una acción real."""
        self.ensure_one()
        if self.state not in ('active', 'grace'):
            return False
        return feature in (self.features or '').split(',')

    # -------------------------------------------------------------------
    # Activación manual
    # -------------------------------------------------------------------

    def action_activate(self):
        self.ensure_one()
        if not self.license_key:
            raise UserError(_("Captura una clave de licencia antes de activar."))
        company = self.company_id
        rfc = (company.partner_id.vat or '').strip()
        if not rfc:
            raise UserError(_("La empresa no tiene RFC capturado (Contactos > %s > RFC).", company.name))
        payload = {
            'license_key': self.license_key.strip(),
            'rfc': rfc,
            'db_uuid': self._get_db_uuid(),
            'odoo_version': release.version,
            'module_version': self._get_module_version(),
            'base_url': self._get_base_url(),
            'is_production': True,
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/activate",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
        except requests.RequestException as e:
            self.last_error = str(e)
            raise UserError(_("No se pudo contactar al servidor de licencias: %s", e)) from e

        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get('token'):
            error = data.get('error') or _("Respuesta inesperada del servidor de licencias.")
            self.last_error = error
            raise UserError(error)

        self._apply_token(data['token'])

    def action_deactivate(self):
        """Libera voluntariamente el cupo de esta instancia en el servidor de
        licencias (ej. antes de dar de baja un ambiente de prueba). No borra
        el registro local: solo limpia el token, quedando en 'not_activated'."""
        self.ensure_one()
        if not self.license_key:
            raise UserError(_("Esta base no tiene una licencia activada."))
        payload = {
            'license_key': self.license_key.strip(),
            'db_uuid': self._get_db_uuid(),
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/deactivate",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
        except requests.RequestException as e:
            raise UserError(_("No se pudo contactar al servidor de licencias: %s", e)) from e

        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get('ok'):
            error = data.get('error') or _("Respuesta inesperada del servidor de licencias.")
            raise UserError(error)

        self.write({
            'token': False,
            'token_expires': False,
            'license_end': False,
            'edition': False,
            'features': False,
            'last_check': fields.Datetime.now(),
            'last_error': False,
        })

    # -------------------------------------------------------------------
    # Heartbeat (cron) — NUNCA bloqueante
    # -------------------------------------------------------------------

    @api.model
    def _cron_heartbeat(self):
        # Jitter: cada corrida reprograma la siguiente ±90 min para no concentrar
        # la carga de todas las instancias en el servidor a la misma hora exacta.
        cron = self.env.ref('dsi_license_client.ir_cron_heartbeat', raise_if_not_found=False)
        if cron:
            jitter = timedelta(minutes=random.randint(-90, 90))
            cron.sudo().nextcall = fields.Datetime.now() + timedelta(days=1) + jitter
        for rec in self.sudo().search([('token', '!=', False)]):
            try:
                rec._do_heartbeat()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Heartbeat de licenciamiento falló para company_id=%s", rec.company_id.id)

    def _do_heartbeat(self):
        self.ensure_one()
        payload = {
            'license_key': self.license_key,
            'db_uuid': self._get_db_uuid(),
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/heartbeat",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
            data = response.json() if response.content else {}
        except requests.RequestException as e:
            self.last_error = str(e)
            return
        if response.status_code == 200 and data.get('token'):
            self._apply_token(data['token'])
        else:
            self.last_error = data.get('error') or _("El servidor de licencias no renovó el token.")
