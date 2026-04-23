from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    manufacturing_overproduction_warning = fields.Float(
        string="Alerta Sobreproducción (%)",
        config_parameter='manufacturing_control.overproduction_warning',
        default=100.0,
        help="Porcentaje de la meta a partir del cual se muestra una alerta amarilla al operario.",
    )

    manufacturing_overproduction_block = fields.Float(
        string="Bloqueo Sobreproducción (%)",
        config_parameter='manufacturing_control.overproduction_block',
        default=105.0,
        help="Porcentaje de la meta a partir del cual se bloquea el registro de producción.",
    )
