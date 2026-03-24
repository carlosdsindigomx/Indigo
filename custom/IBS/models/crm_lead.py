from odoo import models, fields, api
from odoo.exceptions import UserError

class CrmLead(models.Model):
    _inherit = 'crm.lead'

    ibs_product_count = fields.Integer(
        string='Cotizaciones IBS', 
        compute='_compute_ibs_product_count'
    )

    def _compute_ibs_product_count(self):
        for lead in self:
            lead.ibs_product_count = self.env['ibs.product'].search_count([('lead_id', '=', lead.id)])

    def action_view_ibs_products(self):
        self.ensure_one()
        
        if not self.partner_id:
            raise UserError("Para abrir el cotizador, primero debes asignar un Cliente a esta oportunidad.")
            
        ibs_client = self.env['ibs.client'].search([('partner_id', '=', self.partner_id.id)], limit=1)
        
        if not ibs_client:
            ibs_client = self.env['ibs.client'].create({
                'partner_id': self.partner_id.id
            })
            
        return {
            'type': 'ir.actions.act_window',
            'name': 'Cotizador IBS',
            'view_mode': 'list,form', 
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'ibs.product',
            'domain': [('lead_id', '=', self.id)],
            'context': {
                'default_lead_id': self.id,
                'default_client_id': ibs_client.id, 
            },
        }