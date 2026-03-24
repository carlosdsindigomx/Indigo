from odoo import models, fields

class Printer(models.Model):
    _name = 'ibs.printer'
    _description = 'Printer'
    _rec_name = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Nombre', required=True)
    site_id = fields.Many2one('ibs.site', string='Site', required=True)
    width = fields.Float(string='Eje (cm)', required=True)
    height = fields.Float(string='Desarrollo (cm)', required=True)
    product_type_ids = fields.Many2many('ibs.product_type', 'ibs_type_printer_rel', 'printer_id', 'type_id', string='Tipos de producto que se imprimen')
    