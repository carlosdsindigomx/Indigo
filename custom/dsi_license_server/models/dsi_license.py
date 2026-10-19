import secrets
import string
import uuid
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

TOKEN_VALIDITY_DAYS = 30


class DsiLicense(models.Model):
    _name = 'dsi.license'
    _description = "Licencia DS Indigo"
    _inherit = ['mail.thread']
    _rec_name = 'key'

    key = fields.Char(
        required=True, copy=False, readonly=True, tracking=True,
        default=lambda self: self._generate_key())
    partner_id = fields.Many2one('res.partner', string="Cliente", required=True, tracking=True)
    product_id = fields.Many2one('dsi.license.product', string="Producto", required=True, tracking=True)
    edition = fields.Selection([
        ('basic', "Basic"),
        ('pro', "Pro"),
    ], default='pro', required=True, tracking=True)
    license_type = fields.Selection([
        ('subscription', "Suscripción"),
        ('perpetual', "Perpetua"),
    ], default='subscription', required=True, tracking=True)
    rfc_authorized = fields.Char(string="RFC autorizado", required=True, tracking=True)
    date_start = fields.Date(default=fields.Date.context_today, required=True)
    date_end = fields.Date(string="Vigente hasta", tracking=True,
                            help="Vacío = licencia perpetua sin fecha de expiración.")
    max_instances = fields.Integer(default=2, required=True,
                                    help="Cupo de activaciones simultáneas (ej. producción + pruebas).")
    odoo_versions = fields.Char(default="19.0", required=True,
                                 help="Versiones de Odoo permitidas para esta licencia, separadas por coma.")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('active', "Activa"),
        ('suspended', "Suspendida"),
        ('expired', "Expirada"),
        ('revoked', "Revocada"),
    ], default='draft', required=True, tracking=True)
    instance_ids = fields.One2many('dsi.license.instance', 'license_id', string="Instancias")
    instance_count = fields.Integer(compute='_compute_instance_count')
    active_instance_count = fields.Integer(compute='_compute_instance_count')
    log_ids = fields.One2many('dsi.license.log', 'license_id', string="Bitácora")
    sale_reference = fields.Char(string="Referencia comercial",
                                  help="Folio de la orden de venta o suscripción de origen (texto libre).")

    _key_uniq = models.Constraint('unique(key)', "Ya existe una licencia con esta clave.")
    _product_rfc_uniq = models.Constraint(
        'unique(product_id, rfc_authorized)',
        "Ya existe una licencia de este producto para este RFC.")

    @api.model
    def _generate_key(self):
        while True:
            groups = ['DSI'] + [
                ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
                for _ in range(3)
            ]
            key = '-'.join(groups)
            if not self.search_count([('key', '=', key)]):
                return key

    @api.depends('instance_ids.state')
    def _compute_instance_count(self):
        for license in self:
            license.instance_count = len(license.instance_ids)
            license.active_instance_count = len(license.instance_ids.filtered(lambda i: i.state == 'active'))

    def action_activate(self):
        self.write({'state': 'active'})

    def action_suspend(self):
        self.write({'state': 'suspended'})

    def action_revoke(self):
        self.write({'state': 'revoked'})
        self.instance_ids.filtered(lambda i: i.state == 'active').write({'state': 'inactive'})

    def action_reset_to_draft(self):
        self.write({'state': 'draft'})

    # -------------------------------------------------------------------
    # Lógica usada por los endpoints (controllers/main.py)
    # -------------------------------------------------------------------

    def _check_usable(self, odoo_version):
        """Lanza UserError con un mensaje claro si la licencia no se puede usar ahora mismo."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_("La licencia no está activa (estado: %s).", dict(self._fields['state'].selection).get(self.state)))
        # Comparación en UTC, no en la tz del usuario que dispara la llamada
        # (los endpoints corren con auth='none', sin usuario real detrás).
        if self.date_end and datetime.utcnow().date() > self.date_end:
            self.state = 'expired'
            raise UserError(_("La licencia expiró el %s.", self.date_end))
        allowed_versions = [v.strip() for v in (self.odoo_versions or '').split(',') if v.strip()]
        # Normaliza sufijos de build como "19.0+e" (enterprise) o "19.0-20260101" antes de comparar.
        normalized_version = (odoo_version or '').split('+')[0].split('-')[0]
        if allowed_versions and normalized_version not in allowed_versions:
            raise UserError(_("Esta licencia no cubre la versión de Odoo %s.", odoo_version))

    def _find_or_create_instance(self, db_uuid, base_url, odoo_version, module_version, is_production):
        self.ensure_one()
        instance = self.instance_ids.filtered(lambda i: i.database_uuid == db_uuid)
        if instance:
            instance.write({
                'base_url': base_url or instance.base_url,
                'odoo_version': odoo_version,
                'module_version': module_version,
                'is_production': is_production,
                'state': 'active',
            })
            return instance
        active_count = len(self.instance_ids.filtered(lambda i: i.state == 'active'))
        if active_count >= self.max_instances:
            raise UserError(_(
                "Esta licencia ya tiene %(count)s instancias activas (cupo: %(max)s). "
                "Libera una instancia desde el portal antes de activar una nueva.",
                count=active_count, max=self.max_instances,
            ))
        return self.env['dsi.license.instance'].create({
            'license_id': self.id,
            'database_uuid': db_uuid,
            'base_url': base_url,
            'odoo_version': odoo_version,
            'module_version': module_version,
            'is_production': is_production,
            'state': 'active',
        })

    def _build_token_payload(self):
        self.ensure_one()
        token_expires = datetime.utcnow().date() + timedelta(days=TOKEN_VALIDITY_DAYS)
        return {
            'license_key': self.key,
            'rfc': self.rfc_authorized,
            'edition': self.edition,
            'features': ['vault', 'reconcile'] if self.edition == 'pro' else ['vault'],
            'license_end': self.date_end.isoformat() if self.date_end else None,
            'token_expires': token_expires.isoformat(),
            'nonce': uuid.uuid4().hex,
        }
