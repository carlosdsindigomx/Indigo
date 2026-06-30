# -*- coding: utf-8 -*-
from odoo import models, api
from odoo.osv import expression  # Importamos esta librería vital para unir búsquedas

class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.depends('x_studio_nombre_comercial_1')
    def _compute_display_name(self):
        super()._compute_display_name()
        
        for partner in self:
            if hasattr(partner, 'x_studio_nombre_comercial_1') and partner.x_studio_nombre_comercial_1:
                partner.display_name = f"{partner.display_name} - {partner.x_studio_nombre_comercial_1}"

    @api.model
    def _search_display_name(self, operator, value):
        domain = super()._search_display_name(operator, value)
        
        if value:
            custom_domain = [('x_studio_nombre_comercial_1', operator, value)]
            domain = expression.OR([domain, custom_domain])
            
        return domain