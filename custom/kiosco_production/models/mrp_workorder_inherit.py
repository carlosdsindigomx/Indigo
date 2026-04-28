from odoo import api, fields, models, _


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    shift_declaration_ids = fields.One2many(
        'mrp.shift.declaration',
        'workorder_id',
        string="Declaraciones de Turno",
    )

    # ─── Declaration Limit Override (CU-03) ─────────────────────────
    limit_type = fields.Selection(
        [('percentage', 'Porcentaje'), ('fixed', 'Cantidad Fija')],
        string="Tipo de Límite",
        help="Sobrescribe la regla global del Centro de Trabajo para esta operación específica.",
    )
    limit_value = fields.Float(
        string="Valor del Límite",
        help="Valor de la excepción. Porcentaje: ej. 110 = 110%% de la meta. "
             "Cantidad Fija: unidades exactas.",
    )
