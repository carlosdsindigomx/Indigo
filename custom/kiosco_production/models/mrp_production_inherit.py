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

        # ── Strategy 1: production_group_id (native, most reliable) ───
        if self.production_group_id:
            group = self.production_group_id
            # Collect all MOs in our group (handles backorders in same group)
            family = group.production_ids

            # Add child MOs (sub-assemblies triggered by this MO)
            child_mos = group.child_ids.production_ids
            family |= child_mos

            # Add parent MOs (the MO that triggered us)
            parent_mos = group.parent_ids.production_ids
            family |= parent_mos

            # If we found a parent, also get its other children (our siblings)
            for parent_group in group.parent_ids:
                family |= parent_group.child_ids.production_ids

            if len(family) > 1:
                return family

        # ── Strategy 2: origin field (fallback for manual MOs) ────────
        parent_name = self.name

        if self.origin:
            clean_origin = self.origin.strip()
            parent_mo = self.env['mrp.production'].search(
                [('name', '=', clean_origin)], limit=1,
            )

            if not parent_mo and ' - ' in clean_origin:
                parts = [p.strip() for p in clean_origin.split(' - ')]
                parent_mo = self.env['mrp.production'].search(
                    [('name', 'in', parts)], limit=1,
                )

            if not parent_mo:
                import re
                match = re.search(r'[A-Z0-9/]+/\d+', self.origin)
                if match:
                    parent_mo = self.env['mrp.production'].search(
                        [('name', '=', match.group(0))], limit=1,
                    )

            if parent_mo:
                parent_name = parent_mo.name
                # If the parent has a production group, use native hierarchy
                if parent_mo.production_group_id:
                    group = parent_mo.production_group_id
                    family = group.production_ids | group.child_ids.production_ids
                    if len(family) > 1:
                        return family

        # ── Final fallback: origin ILIKE ──────────────────────────────
        domain = [
            '|',
            ('name', '=', parent_name),
            ('origin', 'ilike', parent_name),
        ]
        family_mos = self.env['mrp.production'].search(domain)

        return self | family_mos

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
