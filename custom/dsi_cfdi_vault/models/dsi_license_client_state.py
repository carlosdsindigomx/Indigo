from odoo import models


class DsiLicenseClientState(models.Model):
    _inherit = 'dsi.license.client.state'

    def _reminder_partners(self):
        """El aviso de fin de prueba llega también a quienes usan la Bóveda:
        son los primeros a los que se les va a bloquear algo."""
        partners = super()._reminder_partners()
        usuarios = self.env['res.users'].sudo().search([('share', '=', False)]).filtered(
            lambda u: self.company_id in u.company_ids
            and u.has_group('dsi_cfdi_vault.group_cfdi_vault_user'))
        return partners | usuarios.partner_id
