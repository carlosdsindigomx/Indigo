from odoo import fields, models


class KioskEmployeeConfig(models.Model):
    _name = 'kiosk.employee.config'
    _description = 'Configuración de Kiosco por Empleado'
    _rec_name = 'employee_id'

    employee_id = fields.Many2one(
        'hr.employee',
        string="Empleado",
        required=True,
        ondelete='cascade',
        index=True,
    )
    workcenter_ids = fields.Many2many(
        'mrp.workcenter',
        string="Centros de Trabajo",
        help="Centros de trabajo en los que este operario puede registrar producción desde el kiosco.",
    )

    _sql_constraints = [
        ('employee_unique', 'unique(employee_id)',
         'Ya existe una configuración de kiosco para este empleado.'),
    ]
