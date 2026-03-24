from odoo import models, fields, api

class Client(models.Model):
    _name = 'ibs.client'
    _description = 'Cliente'
    _rec_name = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    
    partner_id = fields.Many2one('res.partner', string='Contacto')

    name = fields.Char(string='Nombre', related='partner_id.name', store=True)
    email = fields.Char(string='Correo', related='partner_id.email')
    phone = fields.Char(string='Teléfono', related='partner_id.phone')
    
    address = fields.Char(
        string='Dirección', 
        related='partner_id.contact_address', 
        readonly=True
    )