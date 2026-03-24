from odoo import fields, models

class Processes(models.Model):
    _name = 'ibs.processes'
    _description = 'Procesos'
    _rec_name = 'name'

    name = fields.Char(string='Nombre', required=True)
    project = fields.Many2one('project.project', string='Proyecto')
    
class ByProductProcessesLine(models.Model):
    _name = 'ibs.byproduct_processes_line'
    _description = 'Línea de Procesos del Subproducto'
    _order = 'sequence, id'
    _rec_name = 'process_id'

    by_product_id = fields.Many2one('ibs.by_product', string='Subproducto', ondelete='cascade')
    process_id = fields.Many2one('ibs.processes', string='Proceso', required=True)
    sequence = fields.Integer(string='Secuencia', default=10)