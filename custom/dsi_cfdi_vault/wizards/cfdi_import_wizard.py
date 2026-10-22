import io
import zipfile

from ..models.cfdi_conceptos import _bytes_de_adjunto
from odoo import _, api, fields, models


class CfdiImportWizard(models.TransientModel):
    _name = 'cfdi.import.wizard'
    _description = "Importar CFDI"

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True,
        default=lambda self: self.env.company)
    attachment_ids = fields.Many2many(
        'ir.attachment', string="Archivos (XML sueltos o un ZIP)")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('done', "Terminado"),
    ], string="Estado", default='draft')
    result_ids = fields.One2many(
        'cfdi.import.wizard.line', 'wizard_id', string="Resultado")
    imported_count = fields.Integer(string="Importados", compute='_compute_counts')
    duplicate_count = fields.Integer(string="Duplicados", compute='_compute_counts')
    error_count = fields.Integer(string="Con error", compute='_compute_counts')

    @api.depends('result_ids.state')
    def _compute_counts(self):
        for wizard in self:
            wizard.imported_count = len(wizard.result_ids.filtered(lambda r: r.state == 'imported'))
            wizard.duplicate_count = len(wizard.result_ids.filtered(lambda r: r.state == 'duplicate'))
            wizard.error_count = len(wizard.result_ids.filtered(lambda r: r.state == 'error'))

    def _collect_xml_files(self):
        """Return a list of (filename, xml_bytes) from the uploaded attachments,
        expanding any .zip into its .xml members."""
        files = []
        for attachment in self.attachment_ids:
            name = attachment.name or ''
            if name.lower().endswith('.zip'):
                try:
                    with zipfile.ZipFile(io.BytesIO(_bytes_de_adjunto(attachment))) as zf:
                        for member in zf.namelist():
                            if member.lower().endswith('.xml'):
                                files.append((member.rsplit('/', 1)[-1], zf.read(member)))
                except zipfile.BadZipFile:
                    files.append((name, None))
            elif name.lower().endswith('.xml'):
                files.append((name, _bytes_de_adjunto(attachment)))
        return files

    def action_import(self):
        self.ensure_one()
        self.env['dsi.license.client.state']._check_feature(
            'vault', _("importar CFDI"), self.company_id)
        self.result_ids.unlink()
        CfdiDocument = self.env['cfdi.document']
        company = self.company_id
        company_rfc = (company.partner_id.vat or '').upper()

        files = self._collect_xml_files()
        new_docs = CfdiDocument
        for filename, xml_bytes in files:
            if xml_bytes is None:
                self._add_result(filename, 'error', _("El archivo ZIP está dañado o no se pudo leer."))
                continue

            vals = CfdiDocument._parse_cfdi_xml(xml_bytes)
            if vals.get('parse_state') == 'error':
                self._add_result(filename, 'error', vals.get('parse_error'))
                continue

            uuid = vals['uuid']
            existing = CfdiDocument.search([
                ('company_id', '=', company.id), ('uuid', '=', uuid),
            ], limit=1)
            if existing:
                self._add_result(filename, 'duplicate', _("El UUID %s ya existe en la bóveda.", uuid),
                                  cfdi_document_id=existing.id)
                continue

            xml_attachment = self.env['ir.attachment'].create({
                'name': filename,
                'raw': xml_bytes,
                'mimetype': 'text/xml',
                'res_model': 'cfdi.document',
                'res_id': 0,
            })

            direction = 'issued' if vals.get('partner_rfc_issuer', '').upper() == company_rfc else 'received'

            doc_vals = {
                'company_id': company.id,
                'direction': direction,
                'xml_attachment_id': xml_attachment.id,
                **{k: v for k, v in vals.items() if k != 'parse_state'},
            }
            if not doc_vals.get('currency_id'):
                doc_vals['currency_id'] = company.currency_id.id
            doc = CfdiDocument.create(doc_vals)
            xml_attachment.write({'res_id': doc.id})
            new_docs |= doc
            self._add_result(filename, 'imported', _("Importado correctamente."), cfdi_document_id=doc.id)

        if new_docs:
            new_docs.action_match_automatically()

        self.state = 'done'
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _add_result(self, name, state, message, cfdi_document_id=False):
        self.env['cfdi.import.wizard.line'].create({
            'wizard_id': self.id,
            'name': name,
            'state': state,
            'message': message,
            'cfdi_document_id': cfdi_document_id,
        })

    def action_view_imported(self):
        self.ensure_one()
        docs = self.result_ids.filtered(lambda r: r.cfdi_document_id).mapped('cfdi_document_id')
        return {
            'type': 'ir.actions.act_window',
            'name': "CFDI importados",
            'res_model': 'cfdi.document',
            'view_mode': 'list,form',
            'domain': [('id', 'in', docs.ids)],
        }


class CfdiImportWizardLine(models.TransientModel):
    _name = 'cfdi.import.wizard.line'
    _description = "Resultado de importación de CFDI"

    wizard_id = fields.Many2one('cfdi.import.wizard', required=True, ondelete='cascade')
    name = fields.Char(string="Archivo")
    state = fields.Selection([
        ('imported', "Importado"),
        ('duplicate', "Duplicado"),
        ('error', "Error"),
    ], required=True)
    message = fields.Char(string="Detalle")
    cfdi_document_id = fields.Many2one('cfdi.document', string="CFDI")
