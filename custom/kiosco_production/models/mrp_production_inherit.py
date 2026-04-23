from odoo import api, fields, models, _


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

    def _get_family_orders(self):
        """Return this MO plus all related MOs in the family."""
        self.ensure_one()
        
        domain = [('id', '!=', self.id)]
        or_conditions = []
        
        # 1. Child MOs that explicitly reference this MO's name
        or_conditions.append(('origin', 'ilike', self.name))
        
        # 2. MOs that share the exact same origin (e.g. the same Sales Order)
        if self.origin:
            or_conditions.append(('origin', '=', self.origin))
            
        # 3. MOs in the same procurement group (standard Odoo MTO behavior)
        if hasattr(self, 'procurement_group_id') and self.procurement_group_id:
            or_conditions.append(('procurement_group_id', '=', self.procurement_group_id.id))
            
        if len(or_conditions) == 1:
            domain.append(or_conditions[0])
        elif len(or_conditions) > 1:
            # Prefix with '|' for OR operations in Odoo domains
            combined_or = ['|'] * (len(or_conditions) - 1) + or_conditions
            domain = combined_or + domain
            
        related_mos = self.env['mrp.production'].search(domain)
        return self | related_mos

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
