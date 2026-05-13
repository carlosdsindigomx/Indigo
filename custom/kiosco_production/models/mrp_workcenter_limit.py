from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MrpWorkcenterLimit(models.Model):
    _name = 'mrp.workcenter.limit'
    _description = 'Límite de Declaración por Centro de Trabajo'
    _rec_name = 'workcenter_id'

    workcenter_id = fields.Many2one(
        'mrp.workcenter',
        string="Centro de Trabajo",
        required=True,
        ondelete='cascade',
        index=True,
    )
    limit_value = fields.Float(
        string="Límite por Declaración",
        required=True,
        help="Máximo de unidades que se pueden declarar en una sola vez.",
    )

    _sql_constraints = [
        ('workcenter_unique', 'unique(workcenter_id)',
         'Ya existe un límite configurado para este Centro de Trabajo.'),
    ]

    @api.constrains('limit_value')
    def _check_limit_value(self):
        for record in self:
            if record.limit_value <= 0:
                raise ValidationError(
                    _("El valor del límite debe ser mayor a cero.")
                )

