from odoo import models, fields, api

class ByproductItemLine(models.Model):
    _name = 'ibs.byproduct_item_line'
    _description = 'Línea de Item del Subproducto'
    _inherit = ['mail.thread']

    product_id = fields.Many2one('ibs.product', string='Producto', required=True, ondelete='cascade')
    item_byproduct = fields.Many2one('ibs.byproduct_item', string='Item', required=True)
    item_type_id = fields.Many2one('ibs.type_byproduct_item', string='Tipo')
    
    # Regla de Costo
    cost_type = fields.Many2one(related='item_byproduct.cost_type_id', string='Tipo de costo', store=True)
    cost_type_name = fields.Char(related='cost_type.name', string='Nombre Regla', store=True)
    
    modify_quote = fields.Boolean(string='Modificar cotización')
    
    cost_type_code = fields.Selection(related='cost_type.code', string='Regla de costo', store=True)
    
    # Costo y precio base
    cost_frame = fields.Float(string='Costo', digits=(16, 3))
    price_frame = fields.Float(string='Precio', digits=(16, 3))
    
    # Totales calculados
    total_line_cost = fields.Float(string='Costo Total', compute='_compute_line_totals', store=True, digits=(16, 3))
    total_line_price = fields.Float(string='Precio Total', compute='_compute_line_totals', store=True, digits=(16, 3))
    
    available_item_ids = fields.Many2many(
        related='product_id.by_product.items_byproduct_ids', 
        readonly=True
    )

    @api.onchange('item_byproduct')
    def _onchange_item_byproduct(self):
        if self.item_byproduct:
            self.cost_frame = self.item_byproduct.cost_frame
            self.price_frame = self.item_byproduct.price_frame
            self.item_type_id = self.item_byproduct.type_by_product_item

    @api.depends('cost_frame', 'price_frame', 'product_id.total_amount', 'product_id.piece_by_frame', 'cost_type_code')
    def _compute_line_totals(self):
        for line in self:
            
            # Obtener los frames calculados en el producto
            frames = line.product_id.total_amount or 0.0
            
            # Calcular las piezas reales = frames * piezas por frame
            piezas_reales = frames * (line.product_id.piece_by_frame or 1.0)
            
            cost = line.cost_frame or 0.0
            price = line.price_frame or 0.0
            
            rule_code = line.cost_type_code or 'per_frame'
            
            if rule_code == 'per_frame':
                # Se multiplica por frames totales
                line.total_line_cost = cost * frames
                line.total_line_price = price * frames
                
            elif rule_code == 'per_piece':
                # Se multiplica por las piezas reales
                line.total_line_cost = cost * piezas_reales
                line.total_line_price = price * piezas_reales
                
            elif rule_code in ['fixed_prorated', 'fixed_external']:
                # Cobro único plano
                line.total_line_cost = cost
                line.total_line_price = price
                
            elif rule_code == 'none':
                line.total_line_cost = 0.0
                line.total_line_price = 0.0
                
            else:
                line.total_line_cost = 0.0
                line.total_line_price = 0.0