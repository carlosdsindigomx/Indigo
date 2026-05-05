from odoo import api, fields, models, _


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    shift_declaration_ids = fields.One2many(
        'mrp.shift.declaration',
        'workorder_id',
        string="Declaraciones de Turno",
    )

    # ─── Declaration Limit Override (CU-03) ─────────────────────────
    limit_value = fields.Float(
        string="Límite por Declaración",
        help="Máximo de unidades que se pueden declarar en una sola vez. "
             "Sobrescribe la regla global del Centro de Trabajo. "
             "Dejar en 0 para no aplicar excepción.",
    )

