from odoo import models, fields, api

class HrJob(models.Model):
    _inherit = 'hr.job'

    job_stage_id = fields.Many2one(
        'hr.job.stage', 
        string='Etapa',
        group_expand='_read_group_stage_ids',
        default=lambda self: self._default_job_stage_id(),
        tracking=True
    )

    def _default_job_stage_id(self):
        return self.env['hr.job.stage'].search([], order='sequence', limit=1)

    def _read_group_stage_ids(self, stages, domain, order=None, **kwargs):
        return stages.search([])