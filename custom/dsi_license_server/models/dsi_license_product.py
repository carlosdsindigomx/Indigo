from odoo import fields, models


class DsiLicenseProduct(models.Model):
    _name = 'dsi.license.product'
    _description = "Producto licenciable DS Indigo"

    name = fields.Char(required=True)
    code = fields.Char(required=True, help="Identificador técnico estable, ej. 'cfdi_vault'.")
    supported_versions = fields.Char(
        default="19.0", required=True,
        help="Versiones de Odoo soportadas, separadas por coma. Ej: 17.0,18.0,19.0")
    active = fields.Boolean(default=True)
    allow_trial = fields.Boolean(
        string="Permitir prueba",
        help="La base del cliente puede pedir sola una prueba desde Ajustes > Licenciamiento "
             "DS Indigo, sin pasar por aquí. Desmárcalo para dejar de darlas.")
    trial_days = fields.Integer(string="Días de prueba", default=7)

    _code_uniq = models.Constraint('unique(code)', "Ya existe un producto con este código.")
