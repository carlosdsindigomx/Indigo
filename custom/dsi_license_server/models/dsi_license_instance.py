from odoo import fields, models


class DsiLicenseInstance(models.Model):
    _name = 'dsi.license.instance'
    _description = "Instancia activada de una licencia"
    _order = 'date_last_heartbeat desc'

    license_id = fields.Many2one('dsi.license', required=True, ondelete='cascade', index=True)
    database_uuid = fields.Char(string="UUID de la base", required=True)
    base_url = fields.Char()
    odoo_version = fields.Char()
    module_version = fields.Char()
    ip_address = fields.Char()
    date_first_activation = fields.Datetime(default=fields.Datetime.now, required=True)
    date_last_heartbeat = fields.Datetime()
    is_production = fields.Boolean(default=True)
    state = fields.Selection([
        ('active', "Activa"),
        ('inactive', "Inactiva"),
    ], default='active', required=True)

    _license_uuid_uniq = models.Constraint(
        'unique(license_id, database_uuid)',
        "Esta base ya está registrada como instancia de esta licencia.")

    def action_release(self):
        self.write({'state': 'inactive'})
