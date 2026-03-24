from odoo import models, fields, api    

class Cost(models.Model):
    _name = 'ibs.costs'
    _description = 'Tabla de Costos'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Nombre')
    product_id = fields.Many2one('ibs.product', string='Producto', required=True, ondelete='cascade')
    range_template_id = fields.Many2one('ibs.range_template', string='Plantilla de Rango')
    minimum_range = fields.Integer(string='Rango Mínimo')
    maximum_range = fields.Integer(string='Rango Máximo')
    range_pieces = fields.Char(string='Rango Piezas', )
    quantity_per_piece = fields.Float(string='Cantidad Piezas', digits=(16, 3))
    aux_price_per_piece = fields.Float(string='Precio por Pieza aux', digits=(16, 3))
    price_per_piece = fields.Float(string='Precio por Pieza', digits=(16, 3))
    total_sale = fields.Float(string='Precio Total',)
    percentage = fields.Float(string='Porcentaje %', related='range_template_id.percentage')
    min_pieces_calculated = fields.Float(string='Mínimo de Piezas Calc.', digits=(16, 3))
    discount_points = fields.Float(string='Puntos de descuento')
    total_discount_percentage = fields.Float(string='% Desc. total', compute='_compute_row_discount', store=True)

    # Campos con descuento
    price_per_piece_discount = fields.Float(string='Pieza con Descuento', compute='_compute_row_discount', store=True, digits=(16, 3))
    total_sale_discount = fields.Float(string='Total con descuento', compute='_compute_row_discount', store=True, digits=(16, 3))
    
    is_kilo_mode = fields.Boolean(related='product_id.is_kilo_mode', store=True, readonly=True)
    is_meter_mode = fields.Boolean(related='product_id.is_meter_mode', store=True, readonly=True)
    
    @api.depends('price_per_piece', 'quantity_per_piece', 'discount_points', 'product_id.cliente_discount')
    def _compute_row_discount(self):
        for record in self:
            total_discount_pct = record.product_id.cliente_discount + record.discount_points
            
            record.total_discount_percentage = total_discount_pct
            
            discount_factor = 1 + (total_discount_pct / 100.0)

            record.price_per_piece_discount = record.price_per_piece * discount_factor
            record.total_sale_discount = record.price_per_piece_discount * record.quantity_per_piece