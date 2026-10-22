from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


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
    purchase_url = fields.Char(
        string="URL de compra",
        help="A dónde lleva el botón «Comprar licencia» de los clientes. Pasa por "
             "/dsi/license/v1/comprar, así que se puede definir o cambiar aquí sin actualizar "
             "el módulo en cada cliente. Se le agrega ?clave=<clave de la licencia>. Vacía, "
             "el cliente ve una página que dice que la compra en línea aún no está disponible.")

    @api.constrains('purchase_url')
    def _check_purchase_url(self):
        for product in self:
            if product.purchase_url and not product.purchase_url.startswith(('https://', 'http://')):
                raise ValidationError(_("La URL de compra debe empezar con https://"))

    _code_uniq = models.Constraint('unique(code)', "Ya existe un producto con este código.")
