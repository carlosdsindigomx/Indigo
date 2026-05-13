from odoo import models, fields

class TdmxShift(models.Model):
    _name = 'tdmx.shift'
    _description = 'Turno TDMX'
    _order = 'start_time'
    _rec_name = 'name'

    number = fields.Char(string='Número', required=True)
    name = fields.Char(string='Nombre', required=True)
    start_time = fields.Float(string='Hora inicio', required=True)
    end_time = fields.Float(string='Hora final', required=True)
