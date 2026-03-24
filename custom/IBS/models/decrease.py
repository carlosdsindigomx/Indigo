from odoo import fields, models, api

class Decrease(models.Model):
    _name = 'ibs.byproduct_decrease_range'
    _description = 'Rango de merma para subproducto'

    by_product_id = fields.Many2one('ibs.by_product', required=True, ondelete='cascade')
    range_name = fields.Char(string='Rango', compute='_compute_range_name', store=True)
    minimum = fields.Float(string='Minimo', required=True)
    maximum = fields.Float(string='Maximo', required=True)
    percentage = fields.Float(string='Porcentaje', required=True)
    
    
    @api.depends('minimum', 'maximum')
    def _compute_range_name(self):
        for record in self:
            record.range_name = f"{int(record.minimum)} - {int(record.maximum)}"