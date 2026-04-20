from odoo import models, fields, api

class MrpMasterOrderLine(models.Model):
    _name = 'mrp.master.order.line'
    _description = 'Progreso Dinámico de Subproductos'

    master_order_id = fields.Many2one('mrp.master.order', string='Orden maestra', ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Componente')
    demand_qty = fields.Float(string='Meta')
    produced_qty = fields.Float(string='Cantidad terminada')
    progress_percentage = fields.Float(string='% de avance')


class MrpMasterOrder(models.Model):
    _name = 'mrp.master.order'
    _description = 'Orden Maestra de Producción'

    name = fields.Char(string='Referencia Maestra', required=True, copy=False, default='Nuevo')
    product_id = fields.Many2one('product.product', string='Producto Principal', required=True)
    
    uom_id = fields.Many2one('uom.uom', related='product_id.uom_id', string='Unidad de Medida')
    
    # KPIs Globales
    global_demand = fields.Float(string='Meta Global', required=True, default=0.0)
    total_produced = fields.Float(string='Producido Acumulado', compute='_compute_production_kpis', store=True)
    missing_quantity = fields.Float(string='Faltante', compute='_compute_production_kpis', store=True)
    progress_percentage = fields.Float(string='% de Avance Global', compute='_compute_production_kpis', store=True)
    
    # Relaciones
    production_ids = fields.One2many('mrp.production', 'master_order_id', string='Órdenes Parciales')
    
    subproduct_ids = fields.One2many(
        'mrp.master.order.line', 
        'master_order_id', 
        string='Indicadores de Componentes', 
        compute='_compute_production_kpis', 
        store=True
    )
    
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('confirmed', 'Confirmada'),
        ('progress', 'En progreso'),
        ('to_close', 'Por cerrar'),
        ('done', 'Hecho'),
        ('cancel', 'Cancelada')
    ], string='Estado', compute='_compute_state', store=True)

    kanban_indicators_html = fields.Html(
        string='Indicadores Kanban', 
        compute='_compute_kanban_indicators_html'
    )

    # Calculo de estado
    @api.depends('production_ids.state')
    def _compute_state(self):
        for record in self:
            if not record.production_ids:
                record.state = 'draft'
                continue
            
            states = set(record.production_ids.mapped('state'))
            
            if states == {'cancel'}:
                record.state = 'cancel'
            elif states.issubset({'done', 'cancel'}):
                record.state = 'done'
            elif 'progress' in states:
                record.state = 'progress'
            elif 'to_close' in states:
                record.state = 'to_close'
            elif 'confirmed' in states:
                record.state = 'confirmed'
            else:
                record.state = 'draft'

    # Mini indicadores kanban
    @api.depends('subproduct_ids', 'subproduct_ids.progress_percentage')
    def _compute_kanban_indicators_html(self):
        for record in self:
            if not record.subproduct_ids:
                record.kanban_indicators_html = '<div class="text-muted"><small>Sin componentes fabricados</small></div>'
                continue

            html = '<div class="d-flex flex-column w-100">'
            for sub in record.subproduct_ids:
                name = sub.product_id.name or 'N/A'
                short_name = name[:18] + '..' if len(name) > 18 else name
                pct = round(sub.progress_percentage, 1)

                html += f'''
                    <div class="d-flex justify-content-between align-items-center mb-1" title="{name}">
                        <small class="text-muted text-truncate">{short_name}</small>
                        <small class="fw-bold">{pct}%</small>
                    </div>
                '''
            html += '</div>'
            record.kanban_indicators_html = html


    # Calculo de sub-ensambles
    @api.depends(
        'production_ids.qty_produced', 
        'production_ids.product_qty', 
        'production_ids.state', 
        'global_demand'
    )
    def _compute_production_kpis(self):
        for record in self:
            total_principal = 0.0
            component_data = {}

            # Solo leemos las Órdenes de Producción
            for mo in record.production_ids.filtered(lambda m: m.state != 'cancel'):
                
                if mo.product_id == record.product_id:
                    total_principal += mo.qty_produced
                else:
                    comp_id = mo.product_id.id
                    if comp_id not in component_data:
                        component_data[comp_id] = {'demand': 0.0, 'produced': 0.0}
                    
                    component_data[comp_id]['demand'] += mo.product_qty
                    component_data[comp_id]['produced'] += mo.qty_produced

            # KPIs Globales
            record.total_produced = total_principal
            record.missing_quantity = max(0.0, record.global_demand - total_principal)
            
            if record.global_demand > 0:
                record.progress_percentage = (total_principal / record.global_demand) * 100
            else:
                record.progress_percentage = 0.0

            # KPIs Subproductos
            lines = []
            for comp_id, data in component_data.items():
                demand = data['demand']
                produced = data['produced']
                pct = (produced / demand * 100) if demand > 0 else 0.0
                
                lines.append((0, 0, {
                    'product_id': comp_id,
                    'demand_qty': demand,
                    'produced_qty': produced,
                    'progress_percentage': pct,
                }))

            record.subproduct_ids = [(5, 0, 0)] + lines

    # Boton de cierre
    def action_close_definitive(self):
        self.ensure_one()
        pending_mos = self.production_ids.filtered(lambda m: m.state not in ['done', 'cancel'])
        
        if pending_mos:
            pending_mos.action_cancel()
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Orden Cerrada',
                'message': 'Se han cancelado los remanentes y la orden maestra ha finalizado.',
                'type': 'success',
            }
        }