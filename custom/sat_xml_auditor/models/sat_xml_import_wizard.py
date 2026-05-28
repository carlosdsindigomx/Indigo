import base64
from odoo import models, fields, api
from satcfdi.cfdi import CFDI

class SatXmlImportWizard(models.TransientModel):
    _name = 'sat.xml.import.wizard'
    _description = 'Importación de XMLs'

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
        
        xml_count = 0
        xml_duplicados = 0

        for attachment in adjuntos:
            if not attachment.name.lower().endswith('.xml'):
                continue

            # Decodificar para extraer el UUID
            xml_content = base64.b64decode(attachment.datas)
            uuid = False
            
            try:
                cfdi = CFDI.from_string(xml_content)
                complemento = cfdi.get('Complemento', {})
                if 'TimbreFiscalDigital' in complemento:
                    uuid = complemento['TimbreFiscalDigital'].get('UUID')
            except Exception:
                pass 

            # Buscar si ya existe en la tabla
            dominio = [('uuid', '=', uuid)] if uuid else [('xml_filename', '=', attachment.name)]
            existe = sat_raw_env.search(dominio, limit=1)
            
            if existe:
                xml_duplicados += 1
                continue

            # Si no existe, se crea el registro
            nuevo_registro = sat_raw_env.create({
                'xml_filename': attachment.name,
                'xml_file': attachment.datas,
                'match_state': 'pending' # Forzamos el estado a pendiente
            })
            registros_creados += nuevo_registro
            xml_count += 1

        # Procesar solo los registros nuevos
        if registros_creados:
            registros_creados.action_procesar_xml()

        mensaje_final = f'Se importaron {xml_count} facturas XML correctamente. '
        if xml_duplicados > 0:
            mensaje_final += f' Se omitieron {xml_duplicados} archivos que ya estaban registrados.'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Importación',
                'message': mensaje_final,
                'type': 'success' if xml_count > 0 else 'warning',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'}
            }
        }