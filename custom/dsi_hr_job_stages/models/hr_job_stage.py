from odoo import models, fields

class HrJobStage(models.Model):
    _name = 'hr.job.stage'
    _description = 'Etapas de Puesto de Trabajo'
    _order = 'sequence, id'

    name = fields.Char('Nombre', required=True, translate=True)
    sequence = fields.Integer('Secuencia', readonly="1")
    fold = fields.Boolean('Plegada en Kanban', default=False)