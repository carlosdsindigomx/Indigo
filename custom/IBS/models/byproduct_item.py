from odoo import models, fields

class TypeByProductItem(models.Model):
    _name = 'ibs.type_byproduct_item'
    _description = 'Item de subproducto'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string="Tipo", required=True)

class ByProductItem(models.Model):
    _name = 'ibs.byproduct_item'
    _description = 'Item de subproducto'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    def _default_cost_type(self):
        return self.env['ibs.cost_type'].search([('name', '=', 'Ninguno')], limit=1).id
 
    name = fields.Char(string="Nombre", required=True)
    type_by_product_item = fields.Many2one('ibs.type_byproduct_item', string='Tipo', required=True)  
    cost_type_id = fields.Many2one('ibs.cost_type', string='Tipo de costo', default=_default_cost_type)
    modify_quote = fields.Boolean(string='Modificar cotización')
    cost_frame = fields.Float(string='Costo', digits=(16, 3))
    price_frame = fields.Float(string='Precio', digits=(16, 3))
    materials_ids = fields.One2many('ibs.byproduct_material_list', 'byproduct_item_id', string='Lista de Materiales')
    
    