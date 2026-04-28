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
        """Return this MO plus all related MOs in the family.
        Busca a la OP principal y a las OPs que la contengan en su origen.
        """
        self.ensure_one()
        
        # 1. Identificar el nombre de la OP Principal (El Padre)
        parent_name = self.name
        
        # Si nuestra OP tiene algo en 'origin', tratamos de encontrar al Padre.
        if self.origin:
            # Primero intentamos quitando posibles espacios en blanco
            clean_origin = self.origin.strip()
            parent_mo = self.env['mrp.production'].search([('name', '=', clean_origin)], limit=1)
            
            # Si no lo encuentra, intentamos separar por el guión
            if not parent_mo and ' - ' in clean_origin:
                parts = [p.strip() for p in clean_origin.split(' - ')]
                parent_mo = self.env['mrp.production'].search([('name', 'in', parts)], limit=1)
                
            # Si no lo encuentra, usamos Regex genérico para secuencias de Odoo
            if not parent_mo:
                import re
                # Busca letras seguidas de una o más diagonales y números al final
                match = re.search(r'[A-Z0-9/]+/\d+', self.origin)
                if match:
                    parent_mo = self.env['mrp.production'].search([('name', '=', match.group(0))], limit=1)
                    
            if parent_mo:
                parent_name = parent_mo.name
                
        # 2. Buscar a la familia: La OP Principal + Todas las hijas directas
        # Usamos ILIKE porque Odoo claramente le está agregando caracteres invisibles o texto extra al origin
        domain = [
            '|', 
            ('name', '=', parent_name), 
            ('origin', 'ilike', parent_name)
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
