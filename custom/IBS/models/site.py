from odoo import models, fields

class Site(models.Model):
    _name = "ibs.site"
    _description = "Site"
    _rec_name = "name"
    
    name = fields.Char(string="Nombre", required=True)
    