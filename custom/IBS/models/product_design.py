from odoo import models, fields, api

class ProductDesignStage(models.Model):
    _name = 'ibs.product_design_stage'
    _description = 'Etapas de Diseño'
    _order = 'sequence'

    name = fields.Char(string='Nombre de la Etapa', required=True)
    sequence = fields.Integer(string='Secuencia', default=10)
    fold = fields.Boolean(string='Plegado en Kanban', help="Si se marca, la etapa aparecerá recogida.")

class ProductDesign(models.Model):
    _name = 'ibs.product_design'
    _description = 'Diseños por Producto'
    _rec_name = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    
    name = fields.Char(string='Nombre', required=True)
    design_number = fields.Integer(string="Número")
    product_id = fields.Many2one('ibs.product', string='Producto', ondelete='cascade')
    odoo_variant_id = fields.Many2one('product.product', string='Variante', readonly=True)
    description = fields.Text(string='Descripción')
    image = fields.Image(string='Imagen')
    stage_id = fields.Many2one(
        'ibs.product_design_stage', 
        string='Estado', 
        tracking=True,
        group_expand='_read_group_stage_ids' 
    )
    quantity = fields.Integer(string='Cantidad')
    produce = fields.Boolean(string='Producir', default=False)
    
    @api.model
    def _get_default_stage(self):
        return self.env['ibs.product_design_stage'].search([], limit=1, order='sequence')