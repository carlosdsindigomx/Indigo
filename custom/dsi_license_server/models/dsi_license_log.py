from odoo import fields, models


class DsiLicenseLog(models.Model):
    _name = 'dsi.license.log'
    _description = "Bitácora de llamadas al servidor de licencias"
    _order = 'create_date desc'

    license_id = fields.Many2one('dsi.license', ondelete='cascade', index=True)
    instance_id = fields.Many2one('dsi.license.instance', ondelete='set null')
    endpoint = fields.Selection([
        ('activate', "Activar"),
        ('heartbeat', "Heartbeat"),
        ('deactivate', "Desactivar"),
    ], required=True)
    success = fields.Boolean()
    ip_address = fields.Char()
    request_data = fields.Text()
    response_data = fields.Text()
    error_message = fields.Char()
