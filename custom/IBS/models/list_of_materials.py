from odoo import models, fields, api

class ListOfMaterials(models.Model):
    _name = 'ibs.list_of_materials'
    _description = 'Lista de Materiales'
    _rec_name = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Materia prima', related='product_id.name', required=True)
    item_id = fields.Many2one('ibs.item_template', string='Ítem')
    item_type_id = fields.Many2one(related='item_id.item_type_id', string='Tipo de Ítem', store=True)
    product_id = fields.Many2one('product.product', string='Producto')
    bom = fields.Selection(string='BOM', selection=[('master', 'Maestro'), ('sheet', 'Sabana'), ('variant', 'Variante')], default='master')
    quantity_frame = fields.Float(string='Cantidad', digits=(16, 3))
    full = fields.Boolean(string='Completo', default=False)
    less_than_one = fields.Boolean(string='Menor a 1', default=False)
    
class MaterialByProduct(models.Model):
    _name = 'ibs.material_by_product'
    _description = 'Material por Producto'
    _rec_name = 'name'
    _order = 'sequence, id'
    _inherit = ['mail.thread']

    name = fields.Char(related='raw_material_id.name', string='Nombre', store=True)
    product_id = fields.Many2one('ibs.product', string='Producto', ondelete='cascade')
    raw_material_id = fields.Many2one('product.product', string='Materia Prima', required=True)
    bom = fields.Selection(string='BOM', selection=[('master', 'Maestro'), ('sheet', 'Sabana'), ('variant', 'Variante')], default='master')
    component_name = fields.Char(string='Origen')
    quantity_frame = fields.Float(string='Cantidad base', digits=(16, 3))
    quantity_waste = fields.Float(string='Merma calculada', digits=(16, 3))
    quantity_total = fields.Float(string='Cantidad real', digits=(16, 3))
    is_auto = fields.Boolean(string='Generado automáticamente', default=False)
    sequence = fields.Integer(string='Secuencia', default=10)
    
    

    
