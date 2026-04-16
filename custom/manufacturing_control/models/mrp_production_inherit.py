from odoo import api, fields, models, _

"""
Hay que hacer que la declaracion de los turnos se ponga en el princial de cada producto, esto ayudara a dar mas visibilidad
En el principal las cantidad que se declaren por el turno, en los subproductos igual sus respectivos declaraciones.

"""
class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    shift_declaration_ids = fields.One2many(
        'mrp.shift.declaration',
        'production_id',
        string="Declaraciones de Turno",
    )

    shift_total_declared = fields.Float(
        string="Total Declarado en Turnos",
        compute='_compute_shift_total_declared',
        store=True,
    )

    @api.depends('shift_declaration_ids.qty_declared', 'shift_declaration_ids.state')
    def _compute_shift_total_declared(self):
        for production in self:
            production.shift_total_declared = sum(
                declaration.qty_declared
                for declaration in production.shift_declaration_ids
                if declaration.state == 'done'
            )

    def action_view_shift_declarations(self):
        """Open the shift declarations linked to this production order."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Declaraciones de Turno"),
            'res_model': 'mrp.shift.declaration',
            'view_mode': 'list,form',
            'domain': [('production_id', '=', self.id)],
            'context': {'default_production_id': self.id},
        }
