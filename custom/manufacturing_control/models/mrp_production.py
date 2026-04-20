from odoo import models, fields, api

class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    master_order_id = fields.Many2one('mrp.master.order', string='Consola Maestra', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        productions = super().create(vals_list)
        for prod in productions:
            linked_master_id = False

            # Backorder
            if '-' in prod.name:
                base_name = prod.name.split('-')[0]
                sibling_mo = self.env['mrp.production'].search([
                    '|',
                    ('name', '=', base_name),
                    ('name', '=like', f"{base_name}-%"),
                    ('master_order_id', '!=', False),
                    ('id', '!=', prod.id)
                ], limit=1)
                
                if sibling_mo:
                    linked_master_id = sibling_mo.master_order_id.id

            # Sub-ensamble
            elif prod.origin and 'MO' in prod.origin:
                parent_mo = self.env['mrp.production'].search([
                    '|',
                    ('name', '=', prod.origin),
                    ('name', '=like', f"{prod.origin}-%"),
                    ('master_order_id', '!=', False)
                ], limit=1)
                
                if parent_mo:
                    linked_master_id = parent_mo.master_order_id.id

            # Asignar o crear Consola Maestra
            if linked_master_id:
                prod.master_order_id = linked_master_id
            else:
                # Si no está ligada a nada, es el inicio de un nuevo proyecto
                master_name = prod.origin or 'Manual'
                base_mo_name = prod.name.split('-')[0] if prod.name else 'Nueva'
                
                master = self.env['mrp.master.order'].create({
                    'product_id': prod.product_id.id,
                    'global_demand': prod.product_qty,
                    'name': f"Consola: {master_name} - {base_mo_name}",
                })
                prod.master_order_id = master.id
                
        return productions