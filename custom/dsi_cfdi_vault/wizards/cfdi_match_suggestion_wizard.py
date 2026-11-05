from odoo import fields, models


class CfdiMatchSuggestionWizard(models.TransientModel):
    _name = 'cfdi.match.suggestion.wizard'
    _description = "Sugerencias de conciliación (nivel 3: coincidencia parcial con score)"

    # Se crea ya persistido (con sus líneas) desde
    # cfdi.document.action_open_match_suggestions() — así los botones de las
    # líneas pueden llamar acciones de servidor sin depender de un guardado
    # previo del formulario (los registros "nuevos" sin persistir no dejan
    # ejecutar type="object" sobre sus líneas hijas).
    document_id = fields.Many2one('cfdi.document', string="CFDI", required=True)
    line_ids = fields.One2many(
        'cfdi.match.suggestion.wizard.line', 'wizard_id', string="Sugerencias")


class CfdiMatchSuggestionWizardLine(models.TransientModel):
    _name = 'cfdi.match.suggestion.wizard.line'
    _description = "Sugerencia de conciliación"
    _order = 'score desc'

    wizard_id = fields.Many2one(
        'cfdi.match.suggestion.wizard', required=True, ondelete='cascade')
    move_id = fields.Many2one('account.move', string="Factura candidata", required=True)
    score = fields.Float(string="Score (0-100)")
    rfc_match = fields.Boolean(string="RFC coincide")
    currency_id = fields.Many2one(related='wizard_id.document_id.currency_id')
    amount_diff = fields.Monetary(string="Diferencia en total", currency_field='currency_id')
    date_diff_days = fields.Integer(string="Diferencia en días")

    def action_confirm(self):
        self.ensure_one()
        self.wizard_id.document_id.action_confirm_match(self.move_id.id)
        return {'type': 'ir.actions.act_window_close'}
