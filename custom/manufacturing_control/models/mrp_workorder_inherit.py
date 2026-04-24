from odoo import api, fields, models, _

class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    shift_declaration_ids = fields.One2many(
        'mrp.shift.declaration',
        'workorder_id',
        string="Declaraciones de Turno",
    )
