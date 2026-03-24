from odoo import models, fields
import logging

_logger = logging.getLogger(__name__)

class ProjectTask(models.Model):
    _inherit = 'project.task'
    
    ibs_product_id = fields.Many2one('ibs.product', string='Producto IBS Relacionado')
    
    def write(self, vals):
        res = super(ProjectTask, self).write(vals)

        if 'stage_id' in vals:
            for task in self:
                if task._is_last_stage(vals['stage_id']):
                    task._trigger_next_process_task()
        
        return res

    def _is_last_stage(self, stage_id):
        self.ensure_one()
        last_stage = self.env['project.task.type'].search([
            ('project_ids', 'in', self.project_id.id)
        ], order='sequence desc', limit=1)
        
        return last_stage.id == stage_id

    def _trigger_next_process_task(self):
        """
        Lógica para buscar el siguiente proceso y crear la tarea.
        """
        self.ensure_one()
        product = self.ibs_product_id
        
        if not product or not product.by_product:
            return

        _logger.info(f"--- Buscando siguiente proceso para producto: {product.name} ---")

        current_process_line = self.env['ibs.byproduct_processes_line'].search([
            ('by_product_id', '=', product.by_product.id),
            ('process_id.project', '=', self.project_id.id)
        ], limit=1)

        if not current_process_line:
            _logger.warning("No se encontró la configuración de este proceso en el subproducto.")
            return

        # 2. Buscar SIGUIENTE línea
        next_process_line = self.env['ibs.byproduct_processes_line'].search([
            ('by_product_id', '=', product.by_product.id),
            ('sequence', '>', current_process_line.sequence)
        ], order='sequence asc', limit=1)

        if not next_process_line:
            _logger.info("El producto ha terminado todos sus procesos.")
            return

        next_project = next_process_line.process_id.project
        if not next_project:
            _logger.warning(f"El proceso '{next_process_line.process_id.name}' no tiene proyecto asignado.")
            return

        try:
            new_task_vals = {
                'name': f'{product.name}', 
                'project_id': next_project.id,
                'ibs_product_id': product.id, 
                'partner_id': product.client_id.id,
                'description': f'Tarea generada automáticamente desde el proceso anterior: {self.project_id.name}',
                'user_ids': [],
            }

            ctx = dict(self.env.context)
            
            ctx.pop('default_state', None) 
            
            ctx.pop('default_stage_id', None)

            new_task = self.env['project.task'].with_context(ctx).create(new_task_vals)

            product.active_task_id = new_task.id
            
            _logger.info(f"Siguiente tarea creada: {new_task.name} en proyecto {next_project.name}")
            product.message_post(body=f"Producto avanzó de {self.project_id.name} a {next_project.name}")
            
            if product.general_task_id:
                general_task = product.general_task_id
                next_process_name = next_process_line.process_id.name
                
                # Buscamos la etapa en el Proyecto General que coincida con el nombre del NUEVO proceso
                next_general_stage = self.env['project.task.type'].search([
                    ('project_ids', 'in', general_task.project_id.id),
                    ('name', '=', next_process_name)
                ], limit=1)
                
                if next_general_stage:
                    general_task.write({'stage_id': next_general_stage.id})
                    _logger.info(f"Tarea general movida a la etapa: {next_process_name}")
                else:
                    _logger.warning(f"No se encontró etapa '{next_process_name}' en el Proyecto General")

        except Exception as e:
            self.env.cr.rollback()
            _logger.error(f"Error al crear siguiente tarea: {str(e)}")
