from odoo import fields, models


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    source = fields.Selection(
        selection_add=[('sat_download', "Descarga Masiva SAT")],
        ondelete={'sat_download': 'set default'},
    )
