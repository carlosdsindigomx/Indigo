from odoo import models, fields

class RangeTemplate(models.Model):
    _name = 'ibs.range_template'
    _description = 'Plantilla de Rangos'
    _rec_name = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Nombre', required=True)
    minimum_range = fields.Integer(string='Rango mínimo')
    maximum_range = fields.Integer(string='Rango máximo') 
    quantity = fields.Integer(string='Cantidad mínima')
    percentage = fields.Float(string='Porcentaje %')
    discount_points = fields.Float(string='Puntos de descuento %')