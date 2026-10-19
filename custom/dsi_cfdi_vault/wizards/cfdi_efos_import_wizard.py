from odoo import _, fields, models


class CfdiEfosImportWizard(models.TransientModel):
    _name = 'dsi.cfdi.efos.import.wizard'
    _description = "Cargar lista 69-B (EFOS) del SAT"

    csv_file = fields.Binary(string="Archivo CSV", required=True)
    csv_filename = fields.Char(string="Nombre del archivo")

    def action_import(self):
        self.ensure_one()
        count = self.env['dsi.cfdi.efos.entry']._import_csv_bytes(self.csv_file)
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
