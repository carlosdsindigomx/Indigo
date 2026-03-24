from odoo import models, fields

class MrpProduction(models.Model):
    _inherit = 'mrp.production'
    
    dummy_id = fields.Many2one('ibs.dummies', string='Dummie origen', readonly=True)
    is_preventa_dummy = fields.Boolean(string='Es Dummy de Preventa', default=False, readonly=True)