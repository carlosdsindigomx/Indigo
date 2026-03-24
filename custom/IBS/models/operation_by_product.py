from odoo import fields, models

class OperationByProduct(models.Model):
    _name = 'ibs.operation_by_product'
    _description = 'Operaciones por Producto'
    _order = 'sequence, id'

    name = fields.Char(string='Nombre', required=True)
    product_id = fields.Many2one('ibs.product', string='Producto', ondelete='cascade')
    work_center_id = fields.Many2one('mrp.workcenter', string='Centro de trabajo', required=True)
    bom = fields.Selection(
        string='BOM', 
        selection=[('master', 'Maestro'), ('sheet', 'Sabana'), ('variant', 'Variante')], 
        default='master'
    )
    frames = fields.Integer(string='Frames')
    minutes = fields.Float(string='Minutos')
    sequence = fields.Integer(string='Secuencia', default=10)