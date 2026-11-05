from odoo import models


class DsiLicenseClientState(models.Model):
    _inherit = 'dsi.license.client.state'

    def _reminder_partners(self):
        """El aviso de fin de prueba de la Bóveda llega también a quienes la
        usan: son los primeros a los que se les va a bloquear algo. El de otros
        productos no les llega."""
        partners = super()._reminder_partners()
        if self.product_code != 'cfdi_vault':
            return partners
        usuarios = self.env['res.users'].sudo().search([('share', '=', False)]).filtered(
            lambda u: self.company_id in u.company_ids
            and u.has_group('dsi_cfdi_vault.group_cfdi_vault_user'))
        return partners | usuarios.partner_id
