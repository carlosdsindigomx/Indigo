import base64

from odoo import _, fields, models


class SatCredentialImportWizard(models.TransientModel):
    _name = 'sat.credential.import.wizard'
    _description = "Cargar e.firma (FIEL)"

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, default=lambda self: self.env.company)
    cer_file = fields.Binary(string="Certificado (.cer)", required=True)
    cer_filename = fields.Char()
    key_file = fields.Binary(string="Llave privada (.key)", required=True)
    key_filename = fields.Char()
    password = fields.Char(string="Contraseña de la llave privada", required=True)

    def action_import(self):
        self.ensure_one()
        Credential = self.env['sat.credential'].sudo()
        # Los campos Binary no se decodifican solos al leerlos desde Python —
        # devuelven exactamente el base64 que se les escribió (confirmado
        # empíricamente: attachment=True no cambia este comportamiento).
        values = Credential._parse_and_validate(
            cer_bytes=base64.b64decode(self.cer_file), key_bytes=base64.b64decode(self.key_file),
            password=self.password, company=self.company_id,
        )
        values['company_id'] = self.company_id.id
        values['cer_filename'] = self.cer_filename

        existing = Credential.search([('company_id', '=', self.company_id.id)], limit=1)
        if existing:
            existing.write(values)
            credential = existing
        else:
            credential = Credential.create(values)

        return {
            'type': 'ir.actions.act_window',
            'name': _("e.firma"),
            'res_model': 'sat.credential',
            'view_mode': 'form',
            'res_id': credential.id,
        }
