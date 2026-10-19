from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Bloque 02 del plan: "el mapeo se recuerda por proveedor, la segunda
    # factura del mismo proveedor ya no pide decisiones".
    cfdi_product_id = fields.Many2one(
        'product.product', string="Producto para CFDI",
        help="Producto que se usa al convertir sus comprobantes en facturas de "
             "proveedor. Se recuerda de la última conversión.")
    cfdi_account_id = fields.Many2one(
        'account.account', string="Cuenta contable para CFDI",
                help="Cuenta de gasto o costo que se aplica a las partidas al convertir "
             "sus comprobantes. Se recuerda de la última conversión.")
