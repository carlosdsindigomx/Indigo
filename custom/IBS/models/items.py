from odoo import models, fields, api

class ItemType(models.Model):
    _name = 'ibs.item_type'
    _description = 'Tipo de Ítem'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Tipo de ítem', required=True)
    cost_type_id = fields.Many2one('ibs.cost_type',string='Tipo de costo')
    
class ItemIbs(models.Model):
    _name = 'ibs.item_ibs'
    _description = 'Ítem'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string="Nombre", required=True)
    item_type_id = fields.Many2one('ibs.item_type', string='Tipo de ítem', required=True)
    cost_type = fields.Many2one(related='item_type_id.cost_type_id', string='Tipo de costo',readonly=True)

class ItemTemplate(models.Model):
    _name = 'ibs.item_template'
    _description = 'Ítem'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Nombre', required=True)
    item_id = fields.Many2one('ibs.item_ibs', string='ítem', required=True)
    item_type_id = fields.Many2one(related='item_id.item_type_id', string='Tipo de ítem', required=True)
    printer = fields.Many2one('ibs.printer', string='Impresora', required=True)
    cost_type = fields.Many2one(related='item_id.cost_type', string='Tipo de costo')
    cost_frame_final = fields.Float(string='Costo', required=True, digits=(16, 3))
    price_per_frame = fields.Float(string='Precio', required=True, digits=(16, 3))
    list_of_materials_ids = fields.One2many('ibs.list_of_materials', 'item_id', string='Lista de Materiales')
    item_type_name = fields.Char(related='item_type_id.name', string="Nombre del Tipo")
    
    factor = fields.Float(string='Factor', digits=(16, 5))
    waste = fields.Float(string='Desperdicio', digits=(16, 5), default=1.00)
    
    #Dependencias de acabados
    required_finish_id = fields.Many2one(
        'ibs.byproduct_item', 
        string='Dependencia de acabado',
        help='Si seleccionas un acabado aquí, este item solo se aplicará a la cotización si el cliente eligió ese acabado (Ej. Zipper, Válvula, etc.).'
    )