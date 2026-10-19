from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    cfdi_document_ids = fields.One2many(
        'cfdi.document', 'move_id', string="CFDI vinculados")
    cfdi_document_count = fields.Integer(
        string="Número de CFDI vinculados", compute='_compute_cfdi_document_count')

    @api.depends('cfdi_document_ids')
    def _compute_cfdi_document_count(self):
        for move in self:
            move.cfdi_document_count = len(move.cfdi_document_ids)

    def action_view_cfdi_documents(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "CFDI vinculados",
            'res_model': 'cfdi.document',
            'view_mode': 'list,form',
            'domain': [('move_id', '=', self.id)],
        }
