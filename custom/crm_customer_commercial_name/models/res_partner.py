# -*- coding: utf-8 -*-
from odoo import models, api
from odoo.osv import expression

class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.depends('x_studio_nombre_comercial_1')
    def _compute_display_name(self):
        # 1. Dejamos que Odoo calcule el nombre base original
        super()._compute_display_name()
        
        # 2. Si tiene nombre comercial, dejamos ÚNICAMENTE el nombre comercial en pantalla
        for partner in self:
            if hasattr(partner, 'x_studio_nombre_comercial_1') and partner.x_studio_nombre_comercial_1:
                partner.display_name = partner.x_studio_nombre_comercial_1

    @api.model
    def _search_display_name(self, operator, value):
        # Mantiene la propiedad de poder buscar por cualquiera de los dos nombres
        domain = super()._search_display_name(operator, value)
        if value:
            custom_domain = [('x_studio_nombre_comercial_1', operator, value)]
            domain = expression.OR([domain, custom_domain])
        return domain

    @api.model
    def name_create(self, name):
        # Cuando se crea una empresa rápida desde el CRM, 
        partner = self.create({
            'name': name,
            'x_studio_nombre_comercial_1': name
        })
        return partner.id, partner.display_name