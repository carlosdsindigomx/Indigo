import base64

from odoo import _, fields, models


class CfdiEfosImportWizard(models.TransientModel):
    _name = 'dsi.cfdi.efos.import.wizard'
    _description = "Cargar lista 69-B (EFOS) del SAT"

    csv_file = fields.Binary(string="Archivo CSV", required=True)
    csv_filename = fields.Char(string="Nombre del archivo")

    def action_import(self):
        self.ensure_one()
        # Odoo 18 y 19 devuelven el base64 que mandó el navegador; Odoo 20, un
        # BinaryValue con el archivo ya decodificado. Sin esto, en 18 y 19 se
        # leía el base64 como si fuera el CSV y nunca aparecía la columna RFC.
        datos = getattr(self.csv_file, 'content', None)
        if datos is None:
            datos = base64.b64decode(self.csv_file)
        count = self.env['dsi.cfdi.efos.entry']._import_csv_bytes(datos)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Lista 69-B actualizada"),
                'message': _("%s contribuyentes cargados. Los CFDI existentes ya se recalcularon.", count),
                'type': 'success',
                'sticky': False,
            },
        }
