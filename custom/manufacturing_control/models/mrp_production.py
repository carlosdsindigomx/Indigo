from odoo import models, fields, api

class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    master_order_id = fields.Many2one('mrp.master.order', string='Orden maestra', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        productions = super().create(vals_list)
        
        for prod in productions:
            linked_master_id = False

            # Backorders
            if prod.name and '-' in prod.name:
                base_name = prod.name.split('-')[0]
                
                parent = self.env['mrp.production'].search([
                    '|',
                    ('name', '=', base_name),
                    ('name', '=like', f'{base_name}-%'),
                    ('master_order_id', '!=', False)
                ], limit=1)
                
                if parent:
                    linked_master_id = parent.master_order_id.id

            # Órdenes Manuales 
            if not linked_master_id and prod.origin:
                parent = self.env['mrp.production'].search([
                    ('name', '=', prod.origin),
                    ('master_order_id', '!=', False)
                ], limit=1)
                if parent:
                    linked_master_id = parent.master_order_id.id

            # Órdenes desde Ventas
            if not linked_master_id and prod.origin:
                base_origin = prod.origin.split(' - ')[-1].strip() if ' - ' in prod.origin else prod.origin.strip()

                parent = self.env['mrp.production'].search([
                    '|',
                    ('origin', '=', base_origin),
                    ('origin', '=like', f'% - {base_origin}'),
                    ('master_order_id', '!=', False),
                    ('id', '!=', prod.id)
                ], limit=1, order='create_date asc')
                
                if parent:
                    linked_master_id = parent.master_order_id.id

            # Asignar o crear orden maestra
            if linked_master_id:
                prod.master_order_id = linked_master_id
            else:
                # Si pasa las 4 validaciones y no tiene padre, es una orden nueva.
                base_mo_name = prod.name.split('-')[0] if prod.name else 'Nueva'
                
                master = self.env['mrp.master.order'].create({
                    'product_id': prod.product_id.id,
                    'global_demand': prod.product_qty,
                    'name': base_mo_name
                })
                prod.master_order_id = master.id
                
        return productions