from odoo import models, fields, api

class ItemByProduct(models.Model):
    _name = 'ibs.item_by_product'
    _description = 'Ítem por Producto'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    _order = 'sequence, id'
    
    name = fields.Char(related='item_id.name', string='Nombre', store=True)
    product_id = fields.Many2one('ibs.product', string='Producto', ondelete='cascade')
    item_id = fields.Many2one('ibs.item_template', string='Ítem', required=True)
    sequence = fields.Integer(string='Secuencia', default=10)
    
    # Regla de Costo
    cost_type = fields.Many2one(related='item_id.cost_type', string='Tipo de costo', store=True)
    cost_type_name = fields.Char(related='item_id.cost_type.name', string='Regla de costo', store=True)
    
    cost_type_code = fields.Selection(related='item_id.cost_type.code', string='Código de Regla', store=True)
    
    # Costo y precio base
    unit_cost_base = fields.Float(string='Costo', digits=(16, 3))
    unit_price_base = fields.Float(string='Precio', digits=(16, 3))
    
    item_type_id = fields.Many2one(related='item_id.item_type_id', string='Tipo', store=True)
    item_type_name = fields.Char(related='item_id.item_type_id.name', string='Nombre tipo', store=True)

    # Totales calculados
    total_line_cost = fields.Float(string='Costo total', compute='_compute_line_totals', store=True, digits=(16, 3))
    total_line_price = fields.Float(string='Precio total', compute='_compute_line_totals', store=True, digits=(16, 3))
    
    #Para calcular en kilos
    width = fields.Float(related='product_id.total_ext_width', string='Eje')
    length = fields.Float(related='product_id.total_ext_height', string='Desarrollo')
    factor = fields.Float(related='item_id.factor',string='Factor')
    waste = fields.Float(related='item_id.waste',string='Desperdicio')
    frames = fields.Float(string='Frames', digits=(16, 3), default=0.001)
    kg = fields.Float(
        string='Kilos',
        digits=(16, 4),
        compute='_compute_calculate_kg', 
        store=True
    )
    
    product_printer_id = fields.Many2one(
        related='product_id.printer_id', 
        string='Impresora', 
        store=True
    )
    
    @api.onchange('item_id')
    def _onchange_item_id_fill_prices(self):
        if self.item_id:
            self.unit_cost_base = self.item_id.cost_frame_final
            self.unit_price_base = self.item_id.price_per_frame
    
    @api.depends('width', 'length', 'factor', 'waste', 'frames')
    def _compute_calculate_kg(self):
        for line in self:
            current_frames = line.frames or 0.0
            base_weight = line.width * line.length * line.factor * current_frames * line.waste            
            line.kg = base_weight 
            
    @api.depends('item_id', 'product_id.total_amount', 'product_id.piece_by_frame', 'cost_type_code', 'unit_cost_base', 'unit_price_base')
    def _compute_line_totals(self):
        for line in self:
            
            # obtener los frames calculados en el producto
            frames = line.product_id.total_amount or 0.0
            
            # calcular las piezas reales = frames * piezas por frame
            piezas_reales = frames * (line.product_id.piece_by_frame or 1.0)
            
            # Datos del Ítem
            cost = line.unit_cost_base or 0.0
            price = line.unit_price_base or 0.0
            rule = line.cost_type_code or 'per_frame' 
            
            if rule == 'per_frame':
                # Se multiplica por Frames
                line.total_line_cost = cost * frames
                line.total_line_price = price * frames
                
            elif rule == 'per_piece':
                # Se multiplica por las piezas reales
                line.total_line_cost = cost * piezas_reales
                line.total_line_price = price * piezas_reales
                
            elif rule in ['fixed_prorated', 'fixed_external']:
                line.total_line_cost = cost
                line.total_line_price = price
                
            elif rule == 'none':
                # Items sin costo
                line.total_line_cost = 0.0
                line.total_line_price = 0.0
                
            else:
                line.total_line_cost = 0.0
                line.total_line_price = 0.0