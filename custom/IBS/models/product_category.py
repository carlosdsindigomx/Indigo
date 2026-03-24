from odoo import models, fields

class ProductCategory(models.Model):
    _inherit = 'product.category'

    unspsc_category_id = fields.Many2one('product.unspsc.code', string='Categoría de UNSPSC')