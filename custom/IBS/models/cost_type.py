from odoo import fields, models, api

class CostType(models.Model):
    _name = "ibs.cost_type"
    _description = 'Modelo de Tipos de costo'
    _rec_name = 'name'
    
    name = fields.Char(string='Nombre', required=True)
    code = fields.Selection([
        ('per_frame', 'Cálculo por Área de impresión'),
        ('per_piece', 'Cálculo por Pieza'),
        ('fixed_prorated', 'Fijo Prorrateado'),
        ('fixed_external', 'Fijo Externo'),
        ('none', 'Sin Costo')
    ], string='Regla de costo', required=True)
    