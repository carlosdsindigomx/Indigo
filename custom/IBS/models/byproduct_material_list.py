from odoo import fields, models, api

class ByproductMaterialList(models.Model):
    _name = 'ibs.byproduct_material_list' 
    _description = 'Lista de Materiales para Subproductos'
    _rec_name = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Materia prima', related='product_id.name', required=True)
    byproduct_item_id = fields.Many2one('ibs.byproduct_item', string='Subproducto', ondelete='cascade')
    item_type_id = fields.Many2one(related='byproduct_item_id.type_by_product_item', string='Tipo de Ítem', store=True)
    product_id = fields.Many2one('product.product', string='Producto')
    bom = fields.Selection(string='BOM', selection=[('master', 'Maestro'), ('variant', 'Variante')], default='master')
    quantity_frame = fields.Float(string='Cantidad', digits=(16, 3))
    full = fields.Boolean(string='Completo', default=False)
    less_than_one = fields.Boolean(string='Menor a 1', default=False)

