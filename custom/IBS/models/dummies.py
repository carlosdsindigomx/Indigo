from odoo import models, fields, _
from odoo.exceptions import UserError

class Dummies(models.Model):
    _name = 'ibs.dummies'
    _description = 'Gestión de Muestras y Dummies (SAC-03)'
    _rec_name = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Nombre', required=True, copy=False, default=lambda self: _('Nuevo'))
    version = fields.Integer(string='Versión', default=1, tracking=True)

    project_id = fields.Many2one('project.project', string='Proyecto', required=True, tracking=True)
    analytic_account_id = fields.Many2one(related='project_id.account_id', string='Cuenta analítica', store=True)
    
    ibs_product_id = fields.Many2one('ibs.product', string='Producto Cotizado', required=True, tracking=True)
    odoo_product_id = fields.Many2one('product.product', related='ibs_product_id.odoo_product', string='Producto para producir', store=True)
    
    product_qty = fields.Float(string='Cantidad', default=1.0, required=True)

    state = fields.Selection([
        ('draft', 'Borrador'),
        ('in_production', 'En producción'),
        ('done', 'Muestra entregada'),
        ('cancel', 'Cancelada')
    ], string='Estado', default='draft', tracking=True)

    mrp_production_ids = fields.One2many('mrp.production', 'dummy_id', string='Órdenes de producción')

    def action_generate_mrp(self):
        for record in self:
            if not record.odoo_product_id:
                raise UserError("El producto seleccionado aún no ha sido creado")

            bom = self.env['mrp.bom'].search([('product_tmpl_id', '=', record.odoo_product_id.product_tmpl_id.id)], limit=1)
            if not bom:
                raise UserError("El producto no tiene una Lista de Materiales configurada para fabricarse.")

            mrp_vals = {
                'product_id': record.odoo_product_id.id,
                'product_qty': record.product_qty,
                'bom_id': bom.id,
                'dummy_id': record.id,
                'origin': f"Muestra/Dummy: {record.name}",
                'analytic_distribution': {str(record.analytic_account_id.id): 100} if record.analytic_account_id else False,
                'is_preventa_dummy': True, 
            }
            
            produccion = self.env['mrp.production'].create(mrp_vals)
            record.state = 'in_production'
            record.message_post(body=f"Se generó la Orden de Producción: {produccion.name} para esta muestra.", message_type='notification')