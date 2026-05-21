from odoo import models, fields, api

class SatXmlImportWizard(models.TransientModel):
    _name = 'sat.xml.import.wizard'
    _description = 'Importación Masiva de XMLs'

    attachment_ids = fields.Many2many(
        comodel_name='ir.attachment',
        string='Archivos XML',
        required=True,
        help='Puedes seleccionar múltiples archivos XML para importar.'
    )

    def action_importar_masivo(self):
        adjuntos = self.attachment_ids
        self.attachment_ids = False
        
        if not adjuntos:
            return {'type': 'ir.actions.client', 'tag': 'reload'}

        sat_raw_env = self.env['sat.xml.raw']
        registros_creados = self.env['sat.xml.raw']

        for attachment in adjuntos:
            if not attachment.name.lower().endswith('.xml'):
                continue

            nuevo_registro = sat_raw_env.create({
                'xml_filename': attachment.name,
                'xml_file': attachment.datas,
            })
            registros_creados += nuevo_registro

        if registros_creados:
            registros_creados.action_procesar_xml()

        return {'type': 'ir.actions.client', 'tag': 'reload'}