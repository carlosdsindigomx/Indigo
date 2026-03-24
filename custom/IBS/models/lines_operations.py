from odoo import fields, models

class LinesOperations(models.Model):
    _name = 'ibs.lines.operations'
    _description = 'Lines Operations'
    _rec_name = 'name'

    name = fields.Char(string='Nombre', required=True)
    by_product_id = fields.Many2one('ibs.by_product', string='Subproducto',ondelete='cascade')
    work_center_id = fields.Many2one('mrp.workcenter', string='Centro de trabajo')
    frames = fields.Integer(string='Frames')
    minutes = fields.Float(string='Minutos')
    bom = fields.Selection(string='BOM', selection=[('master', 'Maestro'), ('sheet', 'Sabana'), ('variant', 'Variante')], default='master')
    sequence = fields.Integer(string='Secuencia', default=10)
    