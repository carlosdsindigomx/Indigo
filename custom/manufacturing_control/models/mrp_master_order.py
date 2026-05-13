from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import timedelta
import json

class MrpMasterOrderLine(models.Model):
    _name = 'mrp.master.order.line'
    _description = 'Progreso Dinámico de Subproductos'

    master_order_id = fields.Many2one('mrp.master.order', string='Orden maestra', ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Componente')
    demand_qty = fields.Float(string='Meta')
    produced_qty = fields.Float(string='Cantidad terminada')
    progress_percentage = fields.Float(string='% de avance')
    
    def action_view_productions(self):
        self.ensure_one()
        return {
            'name': f'Órdenes: {self.product_id.name}',
            'type': 'ir.actions.act_window',
            'res_model': 'mrp.production',
            'view_mode': 'list,form',
            'domain': [
                ('master_order_id', '=', self.master_order_id.id),
                ('product_id', '=', self.product_id.id)
            ],
        }


class MrpMasterOrder(models.Model):
    _name = 'mrp.master.order'
    _description = 'Orden Maestra de Producción'

    name = fields.Char(string='Referencia Maestra', required=True, copy=False, default='Nuevo')
    product_id = fields.Many2one('product.product', string='Producto Principal', required=True)
    uom_id = fields.Many2one('uom.uom', related='product_id.uom_id', string='Unidad de Medida')
    date_deadline = fields.Datetime(
        string='Fecha Límite',
        compute='_compute_date_deadline',
        store=True
    )
    
    date_type = fields.Selection([
        ('deadline', 'Fecha Límite'),
        ('start', 'Fecha Programada'),
        ('none', 'Sin Fecha')
    ], string='Tipo de Fecha Mostrada', compute='_compute_date_deadline', store=True)
    
    
    semaforo = fields.Char(string='Semáforo',       compute='_compute_semaforo')
    semaforo_label = fields.Char(string='Estado del plazo', compute='_compute_semaforo')
    deadline_delta = fields.Char(string='Tiempo límite', compute='_compute_semaforo')
    # KPIs Globales
    global_demand = fields.Float(string='Meta Global', required=True, default=0.0)
    total_produced = fields.Float(string='Producido Acumulado', compute='_compute_production_kpis', store=True)
    missing_quantity = fields.Float(string='Faltante', compute='_compute_production_kpis', store=True)
    progress_percentage = fields.Float(string='% de Avance Global', compute='_compute_production_kpis', store=True)

    # Relaciones
    production_ids = fields.One2many('mrp.production', 'master_order_id', string='Órdenes Parciales')
    subproduct_ids = fields.One2many(
        'mrp.master.order.line', 'master_order_id',
        string='Indicadores de Componentes',
        compute='_compute_production_kpis', store=True
    )
    
    main_date_start = fields.Datetime(
        string='Inicio Programado (Principal)', 
        related='main_production_id.date_start', 
        store=True
    )

    state = fields.Selection([
        ('draft',     'Borrador'),
        ('confirmed', 'Confirmada'),
        ('progress',  'En progreso'),
        ('to_close',  'Por cerrar'),
        ('done',      'Hecho'),
        ('cancel',    'Cancelada'),
    ], string='Estado', compute='_compute_state', store=True)

    kanban_indicators_html = fields.Html(
        string='Indicadores Kanban', compute='_compute_kanban_indicators_html'
    )
    
    main_production_id = fields.Many2one(
        'mrp.production', string='Orden de fabricación',
        compute='_compute_main_production_id', store=True
    )
    components_json   = fields.Char(string='Componentes JSON',          compute='_compute_components_json')
    workcenters_json  = fields.Char(string='Centros de trabajo JSON',   compute='_compute_workcenters_json')
    
    # Progreso estimado
    estimated_progress = fields.Float(
        string='% Estimado', 
        compute='_compute_workcenters_json', 
        store=True
    )
    
    #Cantidad estimada
    estimated_qty = fields.Float(
        string='Cantidad Estimada', 
        compute='_compute_workcenters_json', 
        store=True
    )

    @api.depends('subproduct_ids', 'subproduct_ids.progress_percentage')
    def _compute_components_json(self):
        for record in self:
            data = []
            for sub in record.subproduct_ids:
                data.append({
                    'product_id': sub.product_id.id,
                    'name': sub.product_id.name or 'N/A',
                    'pct': round(sub.progress_percentage, 1),
                })
            # Convertimos la lista de diccionarios a un string JSON
            record.components_json = json.dumps(data)
            
    @api.depends(
        'production_ids.workorder_ids.state',
        'production_ids.workorder_ids.workcenter_id',
        'production_ids.workorder_ids.qty_production',
        'production_ids.workorder_ids.qty_produced',
        'progress_percentage'
    )
    def _compute_workcenters_json(self):
        for record in self:
            wc_data = {}
            for mo in record.production_ids.filtered(lambda m: m.state != 'cancel'):
                for wo in mo.workorder_ids.filtered(lambda w: w.state != 'cancel'):
                    wc_id = wo.workcenter_id.id
                    if not wc_id:
                        continue
                    if wc_id not in wc_data:
                        wc_data[wc_id] = {
                            'id':         wc_id,
                            'name':       wo.workcenter_id.name,
                            'total_qty':  0.0,
                            'prod_qty':   0.0,
                            'pendiente':  0,
                            'en_proceso': 0,
                            'terminada':  0,
                            'bloqueada':  0,
                        }
                    d = wc_data[wc_id]
                    d['total_qty'] += wo.qty_production
                    d['prod_qty']  += wo.qty_produced
                    
                    if wo.state == 'done':
                        d['terminada'] += 1
                    elif wo.state == 'progress':
                        d['en_proceso'] += 1
                    elif wo.state == 'blocked':
                        d['bloqueada'] += 1
                    else:
                        d['pendiente'] += 1

            result = []
            percentages = [] 
            
            for data in wc_data.values():
                total = data['total_qty']
                produced = data['prod_qty']
                pct = round((produced / total * 100) if total > 0 else 0.0, 1)
                
                percentages.append(pct) 
                
                result.append({
                    'id':         data['id'],
                    'name':       data['name'],
                    'total_qty':  round(total, 2),
                    'prod_qty':   round(produced, 2),
                    'pct':        pct,
                    'pendiente':  data['pendiente'],
                    'en_proceso': data['en_proceso'],
                    'terminada':  data['terminada'],
                    'bloqueada':  data['bloqueada'],
                })
            result.sort(key=lambda x: x['name'])
            record.workcenters_json = json.dumps(result)
            
            if percentages:
                bottleneck_pct = min(percentages)
                record.estimated_progress = bottleneck_pct
                record.estimated_qty = record.global_demand * (bottleneck_pct / 100.0)
            else:
                record.estimated_progress = record.progress_percentage
                record.estimated_qty = record.total_produced

    def action_open_specific_component(self):
        self.ensure_one()
        # Recuperamos el ID del producto que inyectamos desde el XML
        product_id = self.env.context.get('ctx_product_id')
        
        if not product_id:
            return
            
        return {
            'name': 'Órdenes del Componente',
            'type': 'ir.actions.act_window',
            'res_model': 'mrp.production',
            'view_mode': 'list,form',
            # Aplicamos el dominio exacto: esta orden maestra + este producto
            'domain': [('master_order_id', '=', self.id), ('product_id', '=', product_id)],
        }
    
    def _format_delta(self, total_seconds):
        s = int(abs(total_seconds))
        if s < 60:
            return f"{s}s"
        m = s // 60
        if m < 60:
            return f"{m}m"
        h, rem_m = s // 3600, (s % 3600) // 60
        if h < 24:
            return f"{h}h {rem_m}m" if rem_m else f"{h}h"
        d, rem_h = s // 86400, (s % 86400) // 3600
        if d < 7:
            return f"{d}d {rem_h}h" if rem_h else f"{d}d"
        w, rem_d = d // 7, d % 7
        return f"{w} sem {rem_d}d" if rem_d else f"{w} sem"

    @api.depends('date_deadline', 'state', 'date_type')
    def _compute_semaforo(self):
        now  = fields.Datetime.now()
        warn = now + timedelta(hours=24)
        
        for record in self:
            if not record.date_deadline or record.state in ('done', 'cancel'):
                record.semaforo       = ''
                record.semaforo_label = ''
                record.deadline_delta = ''
                continue
                
            diff  = (record.date_deadline - now).total_seconds()
            label = self._format_delta(diff)
            is_start = record.date_type == 'start'
            
            if diff < 0:
                record.semaforo       = 'red'
                record.semaforo_label = 'Atrasada' if is_start else 'Vencida'
                record.deadline_delta = f"Atrasada hace {label}" if is_start else f"Vencida hace {label}"
                
            elif record.date_deadline < warn:
                if is_start:
                    record.semaforo       = 'blue'
                    record.semaforo_label = 'Inicia pronto'
                    record.deadline_delta = f"Inicia en {label}"
                else:
                    record.semaforo       = 'orange'
                    record.semaforo_label = 'Por vencer'
                    record.deadline_delta = f"Vence en {label}"
                
            else:
                record.semaforo       = 'green'
                record.semaforo_label = 'Programada' if is_start else 'En tiempo'
                record.deadline_delta = f"Inicia en {label}" if is_start else f"Vence en {label}"
                

    @api.depends('main_production_id.date_deadline', 'main_production_id.date_start')
    def _compute_date_deadline(self):
        for record in self:
            main_mo = record.main_production_id
            
            # Si por alguna razón no hay orden principal activa, lo dejamos vacío
            if not main_mo:
                record.date_deadline = False
                record.date_type = 'none'
                continue

            # Evaluamos solo la orden principal
            if main_mo.date_deadline:
                record.date_deadline = main_mo.date_deadline
                record.date_type = 'deadline'
                
            # Evaluamos la fecha programada de la principal
            elif main_mo.date_start:
                record.date_deadline = main_mo.date_start
                record.date_type = 'start'
                
            else:
                record.date_deadline = False
                record.date_type = 'none'

    # Estado
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
            elif 'progress' in states or ('done' in states and not states.issubset({'done', 'cancel'})):
                record.state = 'progress'
            elif 'to_close' in states:
                record.state = 'to_close'
            elif 'confirmed' in states:
                record.state = 'confirmed'
            else:
                record.state = 'draft'

    # ── Mini indicadores kanban ──────────────────────────────────────
    @api.depends('subproduct_ids', 'subproduct_ids.progress_percentage')
    def _compute_kanban_indicators_html(self):
        for record in self:
            if not record.subproduct_ids:
                record.kanban_indicators_html = (
                    '<div class="text-muted"><small>Sin componentes fabricados</small></div>'
                )
                continue
            html = '<div class="d-flex flex-column w-100">'
            for sub in record.subproduct_ids:
                name = sub.product_id.name or 'N/A'
                short_name = name[:18] + '..' if len(name) > 18 else name
                pct = round(sub.progress_percentage, 1)
                html += (
                    f'<div class="d-flex justify-content-between align-items-center mb-1" title="{name}">'
                    f'<small class="text-muted text-truncate">{short_name}</small>'
                    f'<small class="fw-bold">{pct}%</small>'
                    f'</div>'
                )
            html += '</div>'
            record.kanban_indicators_html = html

    # ── KPIs de producción ───────────────────────────────────────────
    @api.depends(
        'production_ids.qty_produced', 'production_ids.product_qty',
        'production_ids.workorder_ids.qty_produced',
        'production_ids.state', 'global_demand'
    )
    def _compute_production_kpis(self):
        for record in self:
            total_principal = 0.0
            component_data = {}

            for mo in record.production_ids.filtered(lambda m: m.state != 'cancel'):
                if mo.product_id == record.product_id:
                    total_principal += mo.qty_produced
                else:
                    comp_id = mo.product_id.id
                    if comp_id not in component_data:
                        component_data[comp_id] = {
                            'demand': 0.0, 
                            'produced': 0.0, 
                            'w_pcts': [] 
                        }
                        
                    component_data[comp_id]['demand'] += mo.product_qty
                    component_data[comp_id]['produced'] += mo.qty_produced
                    
                    for wo in mo.workorder_ids.filtered(lambda w: w.state != 'cancel'):
                        pct = (wo.qty_produced / wo.qty_production * 100) if wo.qty_production > 0 else 0.0
                        component_data[comp_id]['w_pcts'].append(pct)

            record.total_produced = total_principal
            record.missing_quantity = max(0.0, record.global_demand - total_principal)
            record.progress_percentage = (
                (total_principal / record.global_demand) * 100
                if record.global_demand > 0 else 0.0
            )

            lines = []
            for comp_id, data in component_data.items():
                demand, produced = data['demand'], data['produced']
                
                if data['w_pcts']:
                    prog_pct = min(data['w_pcts'])
                else:
                    prog_pct = (produced / demand * 100) if demand > 0 else 0.0
                    
                lines.append((0, 0, {
                    'product_id':          comp_id,
                    'demand_qty':          demand,
                    'produced_qty':        produced,
                    'progress_percentage': prog_pct,
                }))
                
            record.subproduct_ids = [(5, 0, 0)] + lines

    @api.depends('production_ids', 'production_ids.product_id')
    def _compute_main_production_id(self):
        for record in self:
            main = record.production_ids.filtered(
                lambda m: m.product_id == record.product_id and m.state != 'cancel'
            )
            record.main_production_id = main[0] if main else False

    def action_view_productions(self):
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'name':      f'Órdenes de fabricación — {self.name}',
            'res_model': 'mrp.production',
            'view_mode': 'list,form',
            'views':     [[False, 'list'], [False, 'form']],
            'domain':    [('master_order_id', '=', self.id)],
        }

    # ── Botón de cierre ──────────────────────────────────────────────
    def action_close_definitive(self):
        self.ensure_one()
        
        # Buscamos si hay operaciones pendientes de terminar
        active_mos = self.production_ids.filtered(lambda m: m.state != 'cancel')
        pending_wos = active_mos.mapped('workorder_ids').filtered(lambda w: w.state not in ['done', 'cancel'])
        pending_mos = active_mos.filtered(lambda m: m.state not in ['to_close', 'done'])
        
        # Bloqueamos si los operadores no han terminado
        if pending_mos or pending_wos:
            raise UserError('No puedes cerrar la Orden. Aún hay operaciones o producciones pendientes de terminar.')
            
        # Tomamos las órdenes que ya están listas y las cerramos
        mos_to_close = active_mos.filtered(lambda m: m.state == 'to_close')
        if mos_to_close:
            action = mos_to_close.button_mark_done()
            
            if isinstance(action, dict):
                return action
            
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Orden Cerrada',
                'message': 'La orden ha finalizado.',
                'type': 'success',
            }
        }

    # Panel ejecutivo
    @api.model
    def get_dashboard_data(self, date_from=None, date_to=None):
        today      = fields.Date.today()
        week_start = today - timedelta(days=today.weekday())

        # Rango de fechas: por defecto la semana actual
        if not date_from:
            date_from = str(week_start)
        if not date_to:
            date_to = str(today)

        date_domain = [
            ('create_date', '>=', '{} 00:00:00'.format(date_from)),
            ('create_date', '<=', '{} 23:59:59'.format(date_to)),
        ]

        # Órdenes abiertas: sin filtro de fecha (siempre visibles)
        open_states = ['draft', 'confirmed', 'progress', 'to_close']
        counts = {s: self.search_count([('state', '=', s)]) for s in open_states}
        # Terminadas y canceladas: sí aplica el filtro de fecha
        counts['done']   = self.search_count(date_domain + [('state', '=', 'done')])
        counts['cancel'] = self.search_count(date_domain + [('state', '=', 'cancel')])
        abiertas = sum(counts[s] for s in open_states)

        # Terminadas esta semana
        terminadas_semana = self.search_count([
            ('state', '=', 'done'),
            ('write_date', '>=', '{} 00:00:00'.format(week_start)),
        ])

        # Atrasadas: activas (excluyendo borradores) con deadline vencida
        now = fields.Datetime.now()
        atrasadas = self.search_count([
            ('state', 'not in', ['done', 'cancel', 'draft']),
            ('date_deadline', '<', now),
            ('date_deadline', '!=', False),
        ])

        # Bloqueadas: todas las activas sin material (sin filtro de fecha)
        Production = self.env['mrp.production']
        waiting = Production.search([
            ('master_order_id', '!=', False),
            ('state', 'not in', ['done', 'cancel']),
            ('components_availability_state', 'in', ['late', 'unavailable']),
        ])
        bloqueadas = len(set(waiting.mapped('master_order_id').ids))

        # Carga de centros de trabajo
        workcenters_data = []
        now = fields.Datetime.now() # Para calcular atrasos

        try:
            Workorder = self.env['mrp.workorder']
            Workcenter = self.env['mrp.workcenter']
            
            for wc in Workcenter.search([('active', '=', True)]):
                active_orders = Workorder.search([
                    ('workcenter_id', '=', wc.id),
                    ('state', 'not in', ['done', 'cancel']),
                ])
                
                if active_orders:
                    total_hours = sum(active_orders.mapped(lambda o: o.duration_expected or 0.0)) / 60.0
                    pending_units = sum(active_orders.mapped(lambda o: (o.qty_production or 0.0) - (o.qty_produced or 0.0)))
                    in_prog = len(active_orders.filtered(lambda o: o.state == 'progress'))
                    
                    late_orders = active_orders.filtered(
                        lambda o: o.date_start and o.date_start < now and o.state == 'pending'
                    )
                    
                    workcenters_data.append({
                        'id':          wc.id,
                        'name':        wc.name,
                        'pending':     round(total_hours, 1),
                        'units':       int(pending_units),
                        'count':       len(active_orders),
                        'late_count':  len(late_orders),
                        'in_progress': in_prog,
                    })
            
            workcenters_data.sort(key=lambda x: x['pending'], reverse=True)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Error cargando workcenters: %s", e)
            pass

        # Datos por producto principal
        # Órdenes abiertas sin filtro de fecha + órdenes terminadas dentro del período
        products_data = []
        open_orders = self.search([('state', 'not in', ['cancel', 'done'])])
        done_orders  = self.search(date_domain + [('state', '=', 'done')])
        orders = open_orders | done_orders
        product_map = {}
        for order in orders:
            pid = order.product_id.id
            if not pid:
                continue
            if pid not in product_map:
                product_map[pid] = {
                    'id':             pid,
                    'name':           order.product_id.display_name,
                    'en_proceso':     0,
                    'confirmadas':    0,
                    'por_cerrar':     0,
                    'borrador':       0,
                    'terminadas':     0,
                    'total_demand':   0.0,
                    'total_produced': 0.0,
                }
            pm = product_map[pid]
            pm['total_demand']   += order.global_demand
            pm['total_produced'] += order.total_produced
            state_map = {
                'progress':  'en_proceso',
                'confirmed': 'confirmadas',
                'to_close':  'por_cerrar',
                'draft':     'borrador',
                'done':      'terminadas',
            }
            if order.state in state_map:
                pm[state_map[order.state]] += 1

        for pid, pm in product_map.items():
            pm['abiertas'] = pm['en_proceso'] + pm['confirmadas'] + pm['por_cerrar'] + pm['borrador']
            demand, produced = pm['total_demand'], pm['total_produced']
            pm['progress']       = round((produced / demand * 100) if demand > 0 else 0.0, 1)
            pm['total_demand']   = round(demand, 2)
            pm['total_produced'] = round(produced, 2)
            products_data.append(pm)

        products_data.sort(key=lambda x: (-x['abiertas'], x['name']))

        return {
            'date_from':         date_from,
            'date_to':           date_to,
            'abiertas':          abiertas,
            'en_proceso':        counts['progress'],
            'por_cerrar':        counts['to_close'],
            'confirmadas':       counts['confirmed'],
            'borrador':          counts['draft'],
            'terminadas':        counts['done'],
            'terminadas_semana': terminadas_semana,
            'canceladas':        counts['cancel'],
            'atrasadas':         atrasadas,
            'bloqueadas':        bloqueadas,
            'workcenters':       workcenters_data[:8],
            'products':          products_data,
        }
