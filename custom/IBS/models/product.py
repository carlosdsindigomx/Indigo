from odoo import models, fields, api
import math
from odoo.exceptions import UserError 

class ProductType(models.Model):
    _name = 'ibs.product_type'
    _description = 'Tipo de Producto'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Tipo', required=True)
    unit_of_measure_ids = fields.Many2many('ibs.unit_of_measure','ibs_type_measure_rel','type_id','measure_id', string='Unidades de medida')
    printer_ids = fields.Many2many('ibs.printer', 'ibs_type_printer_rel', 'type_id', 'printer_id', string='Impresoras')
    technica_data = fields.Html(string="Datos técnicos")
    
class By_producto(models.Model):
    _name = 'ibs.by_product'
    _description = 'Modelo de subproducto'
    _rec_name = 'name'
    _inherit = ['mail.thread', 'image.mixin']
    
    name = fields.Char(string='Subproducto', required=True)
    # image_1920 = fields.Image(string="Imagen", max_width=1920, max_height=1920)
    type_product_id = fields.Many2one('ibs.product_type', string='Tipo de producto', required=True)
    gusset = fields.Boolean(string='Fuelle')
    side_gusset = fields.Boolean(string='Fuelle lateral')
    fin = fields.Boolean(string='Aleta')
    fondo = fields.Boolean(string='Fondo')
    perimeter_gap = fields.Boolean(string='Gap perimetral')
    items_byproduct_ids = fields.Many2many('ibs.byproduct_item', string="Items")
    category_id = fields.Many2one('product.category', string='Categoría', required=True)
    process_line_ids = fields.One2many(
        'ibs.byproduct_processes_line', 
        'by_product_id', 
        string='Procesos'
    )
    
    #Merma
    frames_of_setting = fields.Float(string='Frames para seteo', default=0.0)
    decrease_range_ids = fields.One2many('ibs.byproduct_decrease_range', 'by_product_id', string='Rangos de merma')
    
    #Campos de materiales para configuración
    printing_type = fields.Boolean(string='Tipo de impresión', default=False)
    printing_ink = fields.Boolean(string='Tinta de impresión', default=False)
    printing_substrate = fields.Boolean(string='Sustrato de impresión', default=False)
    first_lamination = fields.Boolean(string='Primera laminación', default=False)
    second_lamination = fields.Boolean(string='Segunda laminación', default=False)
    
    #Campo para agregar items por defecto a cotización
    default_items_ids = fields.Many2many(
        'ibs.item_ibs', 
        string='Items por defecto',
        domain="[('item_type_id.name', 'not in', ['Tinta Impresión', 'Sustrato de Impresión', 'Envío', 'Material de Laminación', 'Empaque', 'Diseño'])]",
        help='Selecciona los tipos de items que se agregarán directamente a la cotización para este subproducto.'
    )
    
    #Operaciones
    lines_operations_ids = fields.One2many('ibs.lines.operations', 'by_product_id', string='Operaciones')
        
    # Campos para configuración de medidas extendidas
    geometry_axis = fields.Selection([
        ('width', 'Desarrollo contra ancho'),
        ('height', 'Desarrollo contra largo')
    ], string="Orientación", required=True,
    help="Define contra qué dimensión de la bobina se coloca el desarrollo total del producto.")

    height_multiplier = fields.Integer(
        string="Alto",
        default=1,
    )

    width_multiplier = fields.Integer(
        string="Ancho",
        default=1,

    )

    gusset_multiplier = fields.Integer(
        string="Fuelle",
        default=0,

    )

    side_gusset_multiplier = fields.Integer(
        string="Fuelle lateral",
        default=0,
  
    )

    fin_multiplier = fields.Integer(
        string="Aleta",
        default=0,
       
    )

    fondo_multiplier = fields.Integer(
        string="Fondo",
        default=0,
       
    )

    apply_dynamic_margin = fields.Boolean(
        string="Margen dinámico",
        
    )

    margin_factor = fields.Float(
        string="Margen",
        default=0.5,
       
    )
    
class TypesOfPrinting(models.Model):
    _name = 'ibs.types_of_printing'
    _description = 'Tipos de impresión'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Tipo de impresión', required=True)
        
class ProductFinish(models.Model):
    _name = 'ibs.product_finish'
    _description = 'Modelo de Acabado de Producto'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Nombre', required=True)
    
class UnitOfMeasure(models.Model):
    _name = 'ibs.unit_of_measure'
    _description = 'Unidad de Medida'
    _rec_name = 'name'
    _inherit = ['mail.thread']
    
    name = fields.Char(string='Unidad de Medida', required=True)
    product_type_ids = fields.Many2many('ibs.product_type', 'ibs_type_measure_rel', 'measure_id', 'type_id', string='Tipos de productos')

class Producto(models.Model):
    _name = 'ibs.product'
    _description = 'Modelo de Producto'
    _rec_name = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    
    #Producto
    name = fields.Char(string='Nombre', compute='_compute_name', store=True, readonly=True)
    client_id = fields.Many2one('ibs.client', string='Cliente', required=True, tracking=True)
    cliente_discount = fields.Float(string='% Descuento', tracking=True)
    product_type = fields.Many2one('ibs.product_type', string='Tipo de Producto', required=True, tracking=True)
    by_product = fields.Many2one('ibs.by_product', string='Subproducto', required=True, tracking=True)
    number_of_changes = fields.Integer(string='Cantidad de cambios', default=1, tracking=True)
    number_of_pieces = fields.Integer(string='Cantidad', tracking=True)
    measure = fields.Many2one('ibs.unit_of_measure', string='Unidad', required=True, tracking=True)
    
    by_product_image = fields.Image(related='by_product.image_256', string='Imagen subproducto', readonly=True)
    state = fields.Selection([
        ('quote', 'Cotizar'),
        ('quote_approval', 'Vobo Cotización'),
        ('negotiation', 'Negociación'),
        ('revision', 'Revisión SAC'),
        ('authorized_sac', 'Autorizado SAC'),
        ('enter_op', 'Ingresar OP'),
        ('finalized', 'Finalizado')
    ], string='Estado', default='quote', tracking=True, group_expand='_expand_states')
    
    technica_data = fields.Html(related='product_type.technica_data')
    customer_response = fields.Html(string='Respuesta cliente')
    
    #Impresión
    printer_id = fields.Many2one('ibs.printer', string='Impresora', required=True, tracking=True)
    types_of_printing = fields.Many2one('ibs.types_of_printing', string='Tipo de impresión', tracking=True)
    printing_inks = fields.Many2one('ibs.item_template', string='Tinta de impresión', tracking=True)
    site_id = fields.Many2one('ibs.site', related='printer_id.site_id', readonly=True)
    
    #Estructura
    substrate_printing = fields.Many2one('ibs.item_template', string='Sustrato de impresión', tracking=True)
    first_lamination = fields.Many2one('ibs.item_template', string='Primera laminación', tracking=True)
    second_lamination = fields.Many2one('ibs.item_template', string='Segunda laminación', tracking=True)
    printing_summary = fields.Char(string='Estructura', compute='_compute_printing_summary', store=True, readonly=True)

    #Medidas de Pieza
    height = fields.Float(string='Alto (cm)', required=True, tracking=True)
    width = fields.Float(string='Ancho (cm)', required=True, tracking=True)
    gusset = fields.Float(string='Fuelle (cm)', tracking=True)
    side_gusset = fields.Float(string='Fuelle lateral (cm)', tracking=True)
    fin_seal = fields.Float(string='Aleta (cm)', tracking=True)
    fondo = fields.Float(string="Fondo", required=True, tracking=True)
    perimeter_gap = fields.Float(string='Gap perimetral', required=True, tracking=True)
    final_measure = fields.Char(string='Medida final (cm)', compute='_compute_final_measures', store=True,readonly=True)
    extended_measure = fields.Char(string='Medida extendida (cm)', compute='_compute_calculate_extended_measures', store=True,readonly=True)
    
    #Matriz de calculo de medidas extendidas
    bobbin_width = fields.Float(related='printer_id.width', string='Eje (cm)', default=74.0) 
    bobbin_height = fields.Float(related='printer_id.height', string='Desarrollo (cm)', default=112.0) 
    
    height_ext_val = fields.Float(string='Alto Extendido', compute='_compute_calculate_extended_measures', store=True) 
    width_ext_val = fields.Float(string='Ancho Extendido', compute='_compute_calculate_extended_measures', store=True) 
    
    fits_height = fields.Integer(string='Repeticiones Alto', compute='_compute_calculate_extended_measures', store=True)
    fits_width = fields.Integer(string='Repeticiones Ancho', compute='_compute_calculate_extended_measures', store=True)
    
    total_ext_height = fields.Float(string='Total Alto Calc.', compute='_compute_calculate_extended_measures', store=True)
    total_ext_width = fields.Float(string='Total Ancho Calc.', compute='_compute_calculate_extended_measures', store=True) 
    
    piece_by_frame = fields.Integer(string='Piezas por área de impresión', compute='_compute_calculate_extended_measures', store=True, readonly=True)
    
    total_amount = fields.Float(string='Total Frames', compute='_compute_calculate_extended_measures', store=True, readonly=True)
    
    #Acabados    
    finishing_summary = fields.Char(string='Acabados', compute='_compute_finishing_summary', store=True, readonly=True)
    
    #items
    item_by_product_ids = fields.One2many('ibs.item_by_product', 'product_id', domain=[('item_type_name', 'not in', ['Envío', 'Empaque', 'Logística'])],string='Ítems')
    
    #Campos para obtener configuracion de medida del subproducto
    has_gusset = fields.Boolean(related='by_product.gusset', readonly=True)
    has_side_gusset = fields.Boolean(related='by_product.side_gusset', readonly=True)
    has_fin = fields.Boolean(related='by_product.fin', readonly=True)
    has_fondo = fields.Boolean(related='by_product.fondo', readonly=True)
    has_gap = fields.Boolean(related='by_product.perimeter_gap', readonly=True)
    
    #Campos para obtener configuración de estructura y materiales de subproducto
    has_printing_type = fields.Boolean(related='by_product.printing_type', readonly=True)
    has_printing_ink = fields.Boolean(related='by_product.printing_ink', readonly=True)
    has_printing_substrate = fields.Boolean(related='by_product.printing_substrate', readonly=True)
    has_first_lamination = fields.Boolean(related='by_product.first_lamination', readonly=True)
    has_second_lamination = fields.Boolean(related='by_product.second_lamination', readonly=True)
        
    #Rangos de venta y costos variables
    costs_ids = fields.One2many('ibs.costs', 'product_id', string='Costos')
    
    total_cost_per_frame = fields.Float(string='Total Costo x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_cost_per_piece = fields.Float(string='Total Costo x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_profit_per_frame = fields.Float(string='Total Utilidad x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_profit_per_piece = fields.Float(string='Total Utilidad x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_price_per_frame = fields.Float(string='Total Precio x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_price_per_piece = fields.Float(string='Total Precio x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    
    #Costo fijos
    total_cost_per_frame_fixed = fields.Float(string='Total Costo x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_cost_per_piece_fixed = fields.Float(string='Total Costo x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_profit_per_frame_fixed = fields.Float(string='Total Utilidad x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_profit_per_piece_fixed = fields.Float(string='Total Utilidad x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_price_per_frame_fixed = fields.Float(string='Total Precio x Frame', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))
    total_price_per_piece_fixed = fields.Float(string='Total Precio x Pieza', compute='_compute_total_price_per_piece', store=True, digits=(16, 3))

    profit_margin = fields.Float(string='Margen de Utilidad', compute='_compute_total_price_per_piece', store=True, digits=(16, 4))  
    
    #Lista de materiales
    material_by_product_ids = fields.One2many('ibs.material_by_product', 'product_id', string='Materiales por Producto')
    
    #lista de diseños
    design_ids = fields.One2many('ibs.product_design', 'product_id', string='Lista de Diseños')
        
    original_quantity = fields.Float(string='Cantidad original', compute='_compute_total_final', store=True, digits=(16, 3))
    minimum_quantity = fields.Float(string='Cantidad minima', compute='_compute_total_final', store=True, digits=(16, 3))
    unit_price = fields.Float(string='Precio unitario', compute='_compute_total_final', store=True, digits=(16, 3))
    unit_price_discount = fields.Float(string='Precio unitario con descuento', compute='_compute_total_final', store=True, digits=(16, 3))
    total = fields.Float(string='Total', compute='_compute_total_final', store=True, digits=(16, 3))
    total_discount = fields.Float(string='Total con descuento', compute='_compute_total_final', store=True, digits=(16, 3))
    
    # Envío
    logistics_item_ids = fields.One2many(
        'ibs.item_by_product', 
        'product_id', 
        domain=[('item_type_name', 'in', ['Envío', 'Empaque', 'Logística'])],
        string='Envío y Empaque'
    )
        
    shipping_zone = fields.Selection([
        ('metropolitana', 'Zona Metropolitana'),
        ('interior', 'Interior'),
        ('extranjero', 'Extranjero')
    ], string='Zona de Envío', default='metropolitana')    

    box_quantity = fields.Integer(string='Cantidad de Cajas')
    weight_kg = fields.Float(string='Peso en Kg')

    # Medidas de la caja
    box_width = fields.Float(string='Medida de la caja (Ancho) cm')
    box_length = fields.Float(string='Medida de la caja (Largo) cm')
    box_height = fields.Float(string='Medida de la caja (Alto) cm')

    dest_name = fields.Char(string='Nombre Destinatario')
    dest_street = fields.Char(string='Destino: Calle y número')
    dest_colonia = fields.Char(string='Destino: Colonia')

    dest_zip = fields.Char(string='Destino: CP') 
    dest_city = fields.Char(string='Destino: Municipio')
    dest_state = fields.Char(string='Destino: Estado')
    dest_country = fields.Char(string='Destino: País')
    
    is_palletized = fields.Boolean(string='Entarimado')
    
    #Campos de documentación
    quality_certificate = fields.Boolean(string='Certificado de Calidad')
    color_chart = fields.Boolean(string='Carta de Color')
    technical_sheet = fields.Boolean(string='Ficha Técnica')
    house_sample = fields.Boolean(string='Pie de Casa')
    invoice = fields.Boolean(string='Factura')
    purchase_order = fields.Boolean(string='Orden de Compra')
    fumigation_certificate = fields.Boolean(string='Certificado de Fumigación')
    
    # Campo de texto para "Otra documentación"
    other_documentation = fields.Char(string='Otra Documentación')
        
    #Items de subproducto
    items_byproduct_ids = fields.One2many('ibs.byproduct_item_line', 'product_id', string='Items del Subproducto')
    
    items_acabados_ids = fields.One2many(
    'ibs.byproduct_item_line',
    'product_id',
    domain=[('item_type_id.name', '=', 'Acabados')]
    )
    
    #Campos de pestaña "Otros"
    accepted_joints = fields.Integer(string='Uniones aceptadas')
    customer_process = fields.Selection(string='Proceso del cliente', selection=[('manual', 'Manual'), ('automatico', 'Automático'), ('semiautomatico', 'Semiautomático')])
    core_measurement = fields.Selection(string='Medida del core', selection=[('3', '3'), ('6', '6'), ('8', '8')])
    output_type = fields.Selection(string='Tipo de salida', selection=[('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5'), ('6', '6'), ('7', '7'), ('8', '8')])
    aplicacion = fields.Selection(string='Aplicación', selection=[('pet', 'PET'), ('vidrio', 'Vidrio'), ('acrilico', 'Acrilico'), ('carton', 'Cartón')])
    exposure_type = fields.Selection(string='Tipo de exposición', selection=[('anaquel', 'Anaquel'), ('refrigeracion', 'Refrigeración'), ('humedad', 'Humedad')])
    
    # Tabla HTML
    finishing_cost_table = fields.Html(
        string='Tabla Resumen de Costos (Acabados)',
        compute='_compute_finishing_cost_table',
        store=False
    )
    
    odoo_product = fields.Many2one('product.template', string='Producto Odoo', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Orden de Venta', readonly=True, copy=False)
    
    kilos_per_frame_structure = fields.Float(
        string='Kg por Frame', 
        compute='_compute_total_project_kg', 
        store=True,
        digits=(16, 4),
        help="Suma de los kilos de Sustrato y Laminaciones para 1 Frame"
    )
    
    total_project_kg = fields.Float(
        string='Total Kilos Producto', 
        compute='_compute_total_project_kg', 
        store=True,
        digits=(16, 4)
    )

    yield_pieces_kg = fields.Float(
        string='Rendimiento (Pzas/Kg)', 
        compute='_compute_total_project_kg', 
        store=True,
        digits=(16, 2)
    )

    total_project_meters = fields.Float(
        string='Total Metros Lineales', 
        compute='_compute_total_meters', 
        store=True,
        digits=(16, 2)
    )
    
    is_kilo_mode = fields.Boolean(compute='_compute_modes', store=True)
    is_meter_mode = fields.Boolean(compute='_compute_modes', store=True)
    
    
    shipping_address_id = fields.Many2one(
        'res.partner', 
        string='Dirección de Envío',
        domain="[('parent_id', '=', client_id)]"  
    )

    # ID de la tarea
    active_task_id = fields.Many2one(
        'project.task', 
        string='Tarea de Proceso Actual', 
        readonly=True
    )

    current_process_name = fields.Char(
        related='active_task_id.project_id.name', 
        string='Proceso', 
        store=True, 
        readonly=True
    )

    current_stage_name = fields.Char(
        related='active_task_id.stage_id.name', 
        string='Etapa', 
        store=True, 
        readonly=True
    )
    
    #Alerta de medida
    is_print_area_exceeded = fields.Boolean(
        string='Supera Área de Impresión',
        compute='_compute_is_print_area_exceeded',
        store=True,
        tracking=True
    )
    
    applied_range_discount = fields.Float(
        string='% Puntos descuento', 
        compute='_compute_total_final', 
        store=True, 
        readonly=True,
    )
    
    
    # Operaciones editables en la cotización
    operation_by_product_ids = fields.One2many('ibs.operation_by_product', 'product_id', string='Operaciones del Producto')
    
    # Oportunidad
    lead_id = fields.Many2one('crm.lead', string='Oportunidad', readonly=True)
    
    def action_authorize_sac(self):
        for record in self:
            record.state = 'authorized_sac' 
            mensaje = f"Autorizado por: {self.env.user.name}."
            record.message_post(body=mensaje, message_type='notification')
    
    kanban_state = fields.Selection([
        ('normal', 'En Progreso'),
        ('done', 'Listo'),
        ('blocked', 'Bloqueada')
    ], string='Estado Kanban', default='normal')
    
    general_task_id = fields.Many2one('project.task', string='Tarea en Proyecto General', readonly=True)
    
    code = fields.Char(string="Referencia", readonly=True, copy=False, default="New")
    
    @api.onchange('by_product')
    def _onchange_by_product_sync_operations(self):
        lines = [(5, 0, 0)]
        
        if self.by_product and self.by_product.lines_operations_ids:
            for op in self.by_product.lines_operations_ids:
                lines.append((0, 0, {
                    'name': op.name,
                    'work_center_id': op.work_center_id.id,
                    'frames': op.frames,
                    'minutes': op.minutes,
                    'bom': op.bom,
                    'sequence': op.sequence,
                }))
                
        self.operation_by_product_ids = lines
    
    @api.depends('width_ext_val', 'height_ext_val', 'bobbin_width', 'bobbin_height')
    def _compute_is_print_area_exceeded(self):
        for record in self:
            if record.bobbin_width and record.bobbin_height:
                record.is_print_area_exceeded = (
                    record.width_ext_val > record.bobbin_width or 
                    record.height_ext_val > record.bobbin_height
                )
            else:
                record.is_print_area_exceeded = False
    
    #Función para actualizar cálculos
    def action_update_all_calculations(self):
        for record in self:
            record._compute_total_price_per_piece()
            record._action_calculate_ranges()
            record._compute_total_final()
            record.action_generate_materials_list()
        return True    
    
    @api.depends('items_acabados_ids', 'total_amount', 'number_of_pieces')
    def _compute_finishing_cost_table(self):
        for record in self:
            frames = record.total_amount or 0.0
            qty = record.number_of_pieces or 1.0

            data = {
                'frame': {'price': 0.0},
                'prorated': {'price': 0.0},
                'external': {'price': 0.0},
                'piece': {'price': 0.0},
                'totals': {'price': 0.0},
                'currency': record.env.company.currency_id,
                'qty_frames': frames,
                'qty_pieces': qty
            }
            
            for line in record.items_acabados_ids:
                rule_code = line.cost_type_code or 'per_frame'
                p_val = line.price_frame

                if rule_code == 'per_frame':
                    data['frame']['price'] += (frames * p_val)
                elif rule_code == 'fixed_prorated':
                    data['prorated']['price'] += p_val
                elif rule_code == 'fixed_external':
                    data['external']['price'] += p_val
                elif rule_code == 'per_piece':
                    data['piece']['price'] += (qty * p_val)

            for key in ['frame', 'prorated', 'external', 'piece']:
                total_p = data[key]['price']
                
                data[key]['px_frame'] = total_p / frames if frames > 0 else 0.0
                
                data[key]['px_piece'] = total_p / qty if qty > 0 else 0.0

            data['totals']['price'] = sum(data[k]['price'] for k in ['frame', 'prorated', 'external', 'piece'])
            data['totals']['px_frame'] = data['totals']['price'] / frames if frames > 0 else 0.0
            data['totals']['px_piece'] = data['totals']['price'] / qty if qty > 0 else 0.0

            record.finishing_cost_table = record.env['ir.qweb']._render('IBS.template_tabla_costos_acabados', data)
    
    #Función para calculos de Precios de Rangos Venta
    @api.depends('number_of_pieces', 'piece_by_frame', 'costs_ids', 'cliente_discount', 
                 'items_byproduct_ids.total_line_price', 'item_by_product_ids.total_line_price')
    def _compute_total_final(self):
        for record in self:
            total_externos = 0.0
            
            # Sumar todos los items con tipo de costo Fijo externo
            for line in record.item_by_product_ids:
                if line.cost_type_code == 'fixed_external':
                    total_externos += line.total_line_price
            for line in record.items_byproduct_ids:
                if line.cost_type_code == 'fixed_external':
                    total_externos += line.total_line_price
            for line in record.logistics_item_ids:
                if line.cost_type_code == 'fixed_external':
                    total_externos += line.total_line_price
            
            record.original_quantity = record.number_of_pieces
            pxf = record.piece_by_frame or 1.0
            
            # Ordena los rangos de costo
            costs = record.costs_ids.sorted(key=lambda r: r.minimum_range)
            
            if not costs:
                record.minimum_quantity = 0.0
                record.unit_price = 0.0
                record.unit_price_discount = 0.0
                record.total = 0.0 
                record.total_discount = 0.0
                continue
            
            # Tenemos la cantidad minima del primer rango
            record.minimum_quantity = costs[0].minimum_range
            
            # Identificar la unidad para regla de metros
            uom_name = record.measure.name.lower() if record.measure else ''
            is_meter = 'metro' in uom_name or 'meter' in uom_name
            
            # Buscar en qué escalón estamos para saber el porcentaje de ganancia
            margin_to_apply = costs[0].range_template_id.percentage / 100.0 if costs[0].range_template_id else 0.0
            range_discount_points = costs[0].discount_points
            found = False
            
            for line in costs:
                min_pcs = line.minimum_range
                max_pcs = line.maximum_range
                
                if round(min_pcs, 2) <= round(record.number_of_pieces, 2) <= round(max_pcs, 2):
                    margin_to_apply = line.range_template_id.percentage / 100.0 if line.range_template_id else 0.0
                    range_discount_points = line.discount_points
                    found = True
                    break
            
            # Calculo dinamico
            frames_reales = record.total_amount or 1.0
            qty_reales = record.number_of_pieces if record.number_of_pieces > 0 else 1.0

            # 1. Costo variable unitario
            var_cost_total = record.total_cost_per_frame * frames_reales
            unit_var_cost = var_cost_total / qty_reales
            
            # 2. Costo fijo unitario exacto prorrateado a la cantidad de la OP
            fixed_cost_total = record.total_cost_per_frame_fixed
            unit_fixed_cost = fixed_cost_total / qty_reales
            
            # 3. Costo técnico + Margen del escalafón
            technical_cost = unit_var_cost + unit_fixed_cost
            selected_price = technical_cost * (1 + margin_to_apply)
            
            if is_meter:
                selected_price = selected_price * 0.88
                
            selected_price = round(selected_price, 3)
            
            # Asignación de totales
            record.unit_price = selected_price
            
            record.applied_range_discount = range_discount_points
            
            total_discount_applied = record.cliente_discount + range_discount_points
            discount_factor = (1 + (total_discount_applied / 100))
            
            record.unit_price_discount = selected_price * discount_factor
            
            subtotal_producto = record.unit_price * record.number_of_pieces
            subtotal_producto_desc = record.unit_price_discount * record.number_of_pieces
            
            record.total = round(subtotal_producto + total_externos, 3)
            record.total_discount = round(subtotal_producto_desc + total_externos, 3)
    
    
    #Calculo para costos y precios de venta de cada item
    @api.depends('item_by_product_ids.total_line_cost', 'item_by_product_ids.total_line_price',
                 'items_byproduct_ids.total_line_cost', 'items_byproduct_ids.total_line_price',
                 'logistics_item_ids.total_line_cost', 'logistics_item_ids.total_line_price',
                 'number_of_pieces', 'total_amount', 'piece_by_frame', 'measure')
    def _compute_total_price_per_piece(self):
        for record in self:
            var_cost_total = 0.0
            var_price_total = 0.0
            fixed_prorated_cost = 0.0
            fixed_prorated_price = 0.0
            
            frames = record.total_amount or 0.0
            piezas_por_frame = record.piece_by_frame or 1.0
            
            piezas_reales = frames * piezas_por_frame
            
            if piezas_reales <= 0:
                piezas_reales = 1.0 
            if frames <= 0:
                frames = 1.0

            # 1. Items Producto
            for line in record.item_by_product_ids:
                rule = line.cost_type_code
                if rule in ['per_frame', 'per_piece']:
                    var_cost_total += line.total_line_cost
                    var_price_total += line.total_line_price
                elif rule == 'fixed_prorated':
                    fixed_prorated_cost += line.total_line_cost
                    fixed_prorated_price += line.total_line_price

            # 2. Items Subproducto
            for line in record.items_byproduct_ids:
                rule = line.cost_type_code
                if rule in ['per_frame', 'per_piece']:
                    var_cost_total += line.total_line_cost
                    var_price_total += line.total_line_price
                elif rule == 'fixed_prorated':
                    fixed_prorated_cost += line.total_line_cost
                    fixed_prorated_price += line.total_line_price
            
            # 3. Items Logística
            for line in record.logistics_item_ids:
                rule = line.cost_type_code
                if rule in ['per_frame', 'per_piece']:
                    var_cost_total += line.total_line_cost
                    var_price_total += line.total_line_price
                elif rule == 'fixed_prorated':
                    fixed_prorated_cost += line.total_line_cost
                    fixed_prorated_price += line.total_line_price
            
            # Variables por frame
            record.total_cost_per_frame = var_cost_total / frames
            record.total_price_per_frame = var_price_total / frames
            record.total_profit_per_frame = record.total_price_per_frame - record.total_cost_per_frame
            
            # Variables por pieza
            var_cost_piece = var_cost_total / piezas_reales
            var_price_piece = var_price_total / piezas_reales
            record.total_profit_per_piece = var_price_piece - var_cost_piece

            # Fijos
            record.total_cost_per_frame_fixed = fixed_prorated_cost 
            record.total_price_per_frame_fixed = fixed_prorated_price
            record.total_profit_per_frame_fixed = fixed_prorated_price - fixed_prorated_cost

            # Unitarios Fijos (Prorrateados = Divididos entre el total de piezas reales)
            fixed_cost_piece = fixed_prorated_cost / piezas_reales
            fixed_price_piece = fixed_prorated_price / piezas_reales
            
            record.total_cost_per_piece_fixed = fixed_cost_piece
            record.total_price_per_piece_fixed = fixed_price_piece
            record.total_profit_per_piece_fixed = fixed_price_piece - fixed_cost_piece
            
            total_project_cost = var_cost_total + fixed_prorated_cost
            total_project_price = var_price_total + fixed_prorated_price
            
            # Costo Final
            record.total_cost_per_piece = total_project_cost / piezas_reales
            record.total_price_per_piece = total_project_price / piezas_reales
            
            # Utilidad Global por pieza
            profit_global = record.total_price_per_piece - record.total_cost_per_piece
            record.total_profit_per_piece = profit_global
            
            if record.total_price_per_piece > 0:
                record.profit_margin = profit_global / record.total_price_per_piece
            else:
                record.profit_margin = 0.0
            
                
    # Función para calcular rangos venta
    def _action_calculate_ranges(self):
        self.ensure_one()
        
        # Obtener unidad
        uom_name = self.measure.name.lower() if self.measure else ''
        is_kilo = 'kilo' in uom_name or 'kg' in uom_name
        is_meter = 'metro' in uom_name or 'meter' in uom_name
        
        # Equivalencias de 1 frame a la unidad comercial
        pxf = self.piece_by_frame or 1.0
        weight_per_frame = self.kilos_per_frame_structure or 0.0
        frame_length_m = (self.bobbin_height or 0.0) / 100.0

        # Costos bases 
        var_cost_per_frame = self.total_cost_per_frame
        total_fixed_money = self.total_cost_per_frame_fixed

        # limpiar tabla anterior
        lines = [(5, 0, 0)] 
        templates = self.env['ibs.range_template'].search([], order='minimum_range asc')

        # Iterar sobre la plantilla de rangos
        for tmpl in templates:
            
            target_qty_for_proration = 0.0 
            min_val_display = 0.0
            max_val_display = 0.0
            range_label = ""
            
            # plantilla de frames a la unidad comercial
            if is_meter:
                target_qty_for_proration = tmpl.quantity * frame_length_m
                min_val_display = tmpl.minimum_range * frame_length_m
                max_val_display = tmpl.maximum_range * frame_length_m
                range_label = f"{min_val_display:,.0f} m - {max_val_display:,.0f} m"
                
            elif is_kilo:
                if weight_per_frame > 0:
                    target_qty_for_proration = tmpl.quantity * weight_per_frame
                    min_val_display = tmpl.minimum_range * weight_per_frame
                    max_val_display = tmpl.maximum_range * weight_per_frame
                    range_label = f"{min_val_display:,.2f} kg - {max_val_display:,.2f} kg"
                else:
                    continue 
                    
            else:
                target_qty_for_proration = tmpl.quantity * pxf
                min_val_display = tmpl.minimum_range * pxf
                max_val_display = tmpl.maximum_range * pxf
                range_label = f"{min_val_display:,.0f} - {max_val_display:,.0f} pzas"
            
            if target_qty_for_proration <= 0: 
                continue
           
            # Calcular Costos para este escalón específico
            tier_frames = tmpl.quantity # Cuántos frames requiere este escalón
            
            # 1. Costo Variable Unitario
            tier_var_cost_total = tier_frames * var_cost_per_frame
            variable_unit_cost_base = tier_var_cost_total / target_qty_for_proration
            
            # 2. Prorrateo del Costo Fijo Unitario
            fixed_price_component = total_fixed_money / target_qty_for_proration
            
            # 3. Costo Técnico Real (Suma de Variable + Fijo sin ganancia)
            technical_cost = variable_unit_cost_base + fixed_price_component
            
            # 4. Margen de ganancia de la plantilla
            range_margin = tmpl.percentage / 100.0
            
            # 5. PRECIO FINAL: Aplicar Margen al costo total
            final_unit_price = technical_cost * (1 + range_margin)
            
            final_unit_price = technical_cost * (1 + range_margin)
            if is_meter:
                final_unit_price = final_unit_price * 0.88
            
            final_unit_price = round(final_unit_price, 3)
            technical_cost = round(technical_cost, 3)
            total_sale = final_unit_price * target_qty_for_proration
            
            lines.append((0, 0, {
                'name': f'{self.client_id.name or "Cte"} - {range_label}',
                'range_template_id': tmpl.id,
                'minimum_range': min_val_display, 
                'maximum_range': max_val_display,
                'range_pieces': range_label,
                'quantity_per_piece': target_qty_for_proration,
                'aux_price_per_piece': technical_cost,
                'price_per_piece': final_unit_price,
                'total_sale': total_sale,
                'discount_points': tmpl.discount_points,
            }))
            
        self.costs_ids = lines
                
    # Generar resumen de Estructura
    @api.depends('substrate_printing', 'first_lamination', 'second_lamination')
    def _compute_printing_summary(self):
        for record in self:
            materiales = []
            
            if record.substrate_printing:
                materiales.append(record.substrate_printing.name)
            
            if record.first_lamination:
                materiales.append(record.first_lamination.name)
            
            if record.second_lamination:
                materiales.append(record.second_lamination.name)
                
            record.printing_summary = " / ".join(materiales)
        
    #Agregar automáticamente los items relacionados al subproducto
    @api.onchange('printing_inks', 'substrate_printing', 'first_lamination', 'second_lamination', 'by_product', 'printer_id', 'items_acabados_ids', 'number_of_changes')
    def _onchange_sync_items(self):
            # Limpiar la tabla antes de volver a llenarla
            lines = [(5, 0, 0)] 
            
            # Contador para mantener el orden de secuencia
            seq = 1
            
            # Función auxiliar para armar el diccionario de la línea
            def get_line_vals(template_record, current_seq):
                return {
                    'name': template_record.name,
                    'item_id': template_record.id,
                    'sequence': current_seq,
                    'unit_cost_base': template_record.cost_frame_final,
                    'unit_price_base': template_record.price_per_frame,
                }
                
            # Se inyectan directamente desde los campos Many2one que ya seleccionó el usuario
            if self.printing_inks:
                lines.append((0, 0, get_line_vals(self.printing_inks, seq)))
                seq += 1
            if self.substrate_printing:
                lines.append((0, 0, get_line_vals(self.substrate_printing, seq)))
                seq += 1
            if self.first_lamination:
                lines.append((0, 0, get_line_vals(self.first_lamination, seq)))
                seq += 1
            if self.second_lamination:
                lines.append((0, 0, get_line_vals(self.second_lamination, seq)))
                seq += 1

            # Verificamos que tengamos subproducto, items por defecto y una impresora seleccionada
            if self.by_product and self.by_product.default_items_ids and self.printer_id:
                
                # Extraemos los acabados que el usuario seleccionó
                selected_finish_ids = self.items_acabados_ids.mapped('item_byproduct.id')
                
                # Contamos cuántos materiales de laminación hay seleccionados
                lamination_count = 0
                if self.first_lamination:
                    lamination_count += 1
                if self.second_lamination:
                    lamination_count += 1
                    

                for generic_item in self.by_product.default_items_ids:
                    times_to_add = 1 
                    type_name = generic_item.item_type_id.name.lower() if generic_item.item_type_id else ''
                    
                    if 'laminación' in type_name or 'laminacion' in type_name:
                        times_to_add = lamination_count
                    
                    if times_to_add == 0:
                        continue
                    
                    #Traemos todos los items configurados por impresora
                    possible_templates = self.env['ibs.item_template'].search([
                        ('item_id', '=', generic_item.id),
                        ('printer', '=', self.printer_id.id)
                    ])

                    best_template = False      
                    fallback_template = False 

                    # Evaluamos los items
                    for pt in possible_templates:
                        # Si el item exige un acabado específico
                        if pt.required_finish_id:
                            # Verifica si ese acabado está entre los seleccionados
                            if pt.required_finish_id.id in selected_finish_ids:
                                best_template = pt
                                break
                        else:
                            fallback_template = pt

                    # Si hay uno específico lo usamos, si no, usamos el general
                    final_template = best_template if best_template else fallback_template

                    # Validación de seguridad
                    if not final_template:
                        raise UserError(
                            f"¡Falta configuración!\n\n"
                            f"El subproducto requiere el ítem '{generic_item.name}'.\n"
                            f"No se encontró una tarifa genérica, ni una que coincida con los acabados "
                            f"seleccionados para la impresora '{self.printer_id.name}'."
                        )
                    
                    # Inyectamos el ítem ganador a la tabla de la cotización
                    for _ in range(times_to_add):
                        lines.append((0, 0, get_line_vals(final_template, seq)))
                        seq += 1
                        
            # Item de diseño
            if self.number_of_changes and self.number_of_changes > 0 and self.printer_id:
                design_templates = self.env['ibs.item_template'].search([
                    ('item_type_id.name', 'ilike', 'Diseño'),
                    ('printer', '=', self.printer_id.id)
                ], limit=1)
                if design_templates:
                    design_vals = get_line_vals(design_templates, 0)
                    design_vals['unit_cost_base'] *= self.number_of_changes
                    design_vals['unit_price_base'] *= self.number_of_changes
                    lines.append((0, 0, design_vals))

            # Asignamos la lista procesada a nuestro campo One2many
            self.item_by_product_ids = lines
        
        
    @api.onchange('item_by_product_ids')
    def _onchange_sync_table_to_fields(self):
        current_item_ids = []
        
        found_ink = False
        found_substrate = False
        
        found_laminations = []

        for line in self.item_by_product_ids:
            if not line.item_id or not line.item_id.item_type_id:
                continue
            
            current_item_ids.append(line.item_id.id)
            
            type_name = line.item_id.item_type_id.name.strip()
            
            if type_name == 'Tinta Impresión':
                found_ink = True
                if self.printing_inks != line.item_id:
                    self.printing_inks = line.item_id

            elif type_name == 'Sustrato de Impresión':
                found_substrate = True
                if self.substrate_printing != line.item_id:
                    self.substrate_printing = line.item_id

            elif type_name == 'Material de Laminación':
                found_laminations.append(line.item_id)

        if self.printing_inks and not found_ink:
            self.printing_inks = False
            
        if self.substrate_printing and not found_substrate:
            self.substrate_printing = False
        
        if not found_laminations:
            self.first_lamination = False
            self.second_lamination = False
            
        else:
            if self.first_lamination and self.first_lamination in found_laminations:
                found_laminations.remove(self.first_lamination)
            else:
                self.first_lamination = found_laminations.pop(0)

            if found_laminations:
                if self.second_lamination and self.second_lamination in found_laminations:
                    pass 
                else:
                    self.second_lamination = found_laminations.pop(0)
            else:
                self.second_lamination = False
        
    # Acción para crear producto
    def action_create_product(self):
        self.ensure_one()
        
        if self.odoo_product:
             return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Atención',
                    'message': 'Este producto ya fue creado anteriormente.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

        # 1. VALIDACIÓN Y OBTENCIÓN DE CÓDIGO UNSPSC
        if not self.by_product or not self.by_product.category_id:
            raise UserError("El subproducto no tiene asignado una categoría, por favor asigne una categoría antes de crear el producto.")

        if not self.by_product.category_id.unspsc_category_id:
            raise UserError("La categoría del subproducto no tiene un código UNSPSC asignado. Por favor asigne uno en la categoría antes de crear el producto.")
        
        unspsc_id = self.by_product.category_id.unspsc_category_id.id

        # 2. OBTENER RUTA DE MANUFACTURA
        manufacture_route = self.env.ref('mrp.route_warehouse0_manufacture', raise_if_not_found=False)
        route_vals = [(4, manufacture_route.id)] if manufacture_route else []

        # 3. Búsqueda de Categoría
        categ_id = self.by_product.category_id.id if self.by_product else False
        if not categ_id:
            category_search = self.env['product.category'].search([('name', 'ilike', 'All')], limit=1)
            if not category_search:
                category_search = self.env['product.category'].search([], limit=1)
            categ_id = category_search.id if category_search else False

        # 4. Unidades de Medida
        # UoM de la cotización
        odoo_uom = self.env['uom.uom'].search([('name', '=', self.measure.name)], limit=1)
        if not odoo_uom:
            if self.measure.name and 'Pieza' in self.measure.name:
                odoo_uom = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
            elif self.measure.name and 'Kilo' in self.measure.name:
                odoo_uom = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
            elif self.measure.name and 'Metro' in self.measure.name:
                odoo_uom = self.env.ref('uom.product_uom_meter', raise_if_not_found=False)
            if not odoo_uom:
                 odoo_uom = self.env['uom.uom'].search([], limit=1)

        # UoM para Sábana (Unidades)
        uom_units = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        if not uom_units:
            uom_units = self.env['uom.uom'].search([('name', 'ilike', 'Unidad')], limit=1)

        # UoM para Maestro (Kilos)
        uom_kg = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
        if not uom_kg:
            uom_kg = self.env['uom.uom'].search([('name', 'in', ['kg', 'Kg', 'Kilos', 'Kilogramos'])], limit=1)

        # 5. CONSTRUCCIÓN DE NOMBRES DE PRODUCTOS (0015S y 0015M)
        base_code = self.code if self.code else 'Nuevo'
        sub_name = self.by_product.name if self.by_product else 'SinSubproducto'
        
        name_sheet = f"{base_code}S - {sub_name}"
        name_master = f"{base_code}M - {sub_name}"

        # --- 6. CREACIÓN DE LOS 3 PRODUCTOS ---

        # Creación del producto Sábana (Nivel 3)
        vals_sheet = {
            'name': name_sheet,
            'is_storable': True,
            'type': 'consu', 
            'categ_id': categ_id,
            'uom_id': uom_units.id if uom_units else odoo_uom.id, # Forzado a Unidades
            'unspsc_code_id': unspsc_id, 
            'route_ids': route_vals,     
        }
        sheet_product = self.env['product.product'].create(vals_sheet)

        # Creación del producto Maestro (Nivel 2)
        vals_master = {
            'name': name_master,
            'is_storable': True,
            'type': 'consu', 
            'categ_id': categ_id,
            'uom_id': uom_kg.id if uom_kg else odoo_uom.id, 
            'unspsc_code_id': unspsc_id, 
            'route_ids': route_vals,     
        }
        master_product = self.env['product.product'].create(vals_master)

        # Creación de la plantilla de la Variante (Nivel 1)
        vals_variant_tmpl = {
            'name': self.name,
            'is_storable': True,
            'type': 'consu', 
            'categ_id': categ_id,
            'list_price': self.unit_price,
            'uom_id': odoo_uom.id,
            'description_sale': self.finishing_summary,
            'standard_price': self.total_cost_per_piece,
            'unspsc_code_id': unspsc_id, 
            'route_ids': route_vals,     
        }
        variant_tmpl = self.env['product.template'].create(vals_variant_tmpl)

        # --- LÓGICA DE ATRIBUTOS Y DISEÑOS ---
        if self.design_ids:
            attribute = self.env['product.attribute'].search([('name', '=', 'Diseño')], limit=1)
            if not attribute:
                attribute = self.env['product.attribute'].create({
                    'name': 'Diseño',
                    'create_variant': 'always'
                })

            attr_value_ids = []
            for design in self.design_ids:
                val = self.env['product.attribute.value'].search([
                    ('attribute_id', '=', attribute.id), 
                    ('name', '=', design.name)
                ], limit=1)
                if not val:
                    val = self.env['product.attribute.value'].create({
                        'attribute_id': attribute.id, 
                        'name': design.name
                    })
                attr_value_ids.append(val.id)

            if attr_value_ids:
                self.env['product.template.attribute.line'].create({
                    'product_tmpl_id': variant_tmpl.id,
                    'attribute_id': attribute.id,
                    'value_ids': [(6, 0, attr_value_ids)]
                })

        if variant_tmpl.product_variant_ids:
            self.odoo_product = variant_tmpl.id
            
            if self.design_ids:
                for variant in variant_tmpl.product_variant_ids:
                    design_attr_val = variant.product_template_attribute_value_ids.filtered(
                        lambda v: v.attribute_id.name == 'Diseño'
                    )
                    if design_attr_val:
                        matching_design = self.design_ids.filtered(
                            lambda d: d.name == design_attr_val[0].name
                        )
                        if matching_design:
                            matching_design.odoo_variant_id = variant.id
                            if matching_design.image:
                                    variant.image_1920 = matching_design.image
        else:
            self.odoo_product = False


        # --- CÁLCULOS MATEMÁTICOS PARA CANTIDADES DE BOM ---
        total_piezas = self.number_of_pieces if self.number_of_pieces > 0 else 1.0
        total_frames = self.total_amount or 0.0

        # 1. Calcular frames de merma para los tiempos de máquina
        frames_seteo = self.by_product.frames_of_setting if self.by_product else 0.0
        porcentaje_merma = 0.0
        
        if self.by_product and self.by_product.decrease_range_ids:
            # Buscamos en qué rango cae el tiraje total de frames
            for rango in self.by_product.decrease_range_ids:
                if rango.minimum <= total_frames <= rango.maximum:
                    porcentaje_merma = rango.percentage / 100.0
                    break

        # Total de frames que se irán a la basura por ajuste de máquina y rango
        total_frames_merma = frames_seteo + (total_frames * porcentaje_merma)
        
        # EL DATO CLAVE: Lo que realmente va a girar la máquina
        frames_totales_produccion = total_frames + total_frames_merma

        # Lógica de distribución: Proporción para todo el proyecto
        qty_master_bom = total_piezas / 100.0  # Ej. 1,000 Kg

        # --- 7. CREACIÓN DE LISTAS DE MATERIALES ---

        # BOM del producto Sábana
        sheet_bom = self.env['mrp.bom'].create({
            'product_tmpl_id': sheet_product.product_tmpl_id.id,
            'product_qty': 1.0, 
            'product_uom_id': uom_units.id if uom_units else odoo_uom.id,
            'type': 'normal', 
            'code': f"BOM S - {name_sheet}", 
        })

        if self.operation_by_product_ids:
            sheet_ops = self.operation_by_product_ids.filtered(lambda op: op.bom == 'sheet')
            if sheet_ops:
                op_vals = []
                for op in sheet_ops:
                    # Sábana usa tiempo fijo (1 unidad), no le afecta la merma de impresión
                    tiempo_total_op = (1.0 / op.frames) * op.minutes if op.frames > 0 else op.minutes
                    
                    op_vals.append({
                        'name': op.name,
                        'workcenter_id': op.work_center_id.id,
                        'bom_id': sheet_bom.id,
                        'sequence': op.sequence,
                        'time_cycle_manual': tiempo_total_op,
                    })
                self.env['mrp.routing.workcenter'].create(op_vals)


        # BOM del producto Maestro
        master_bom = self.env['mrp.bom'].create({
            'product_tmpl_id': master_product.product_tmpl_id.id,
            'product_qty': qty_master_bom, # Produce el total de Kilos (Ej. 1000)
            'product_uom_id': uom_kg.id if uom_kg else odoo_uom.id, # <--- Forzamos los Kilos aquí
            'type': 'normal', 
            'code': f"BOM M - {name_master}", 
        })

        master_bom_line_vals = []
        # Agregamos la sábana (1 unidad es suficiente para el lote completo)
        master_bom_line_vals.append({
            'bom_id': master_bom.id,
            'product_id': sheet_product.id,
            'product_qty': 1.0, 
            'product_uom_id': sheet_product.uom_id.id, 
        })

        if self.material_by_product_ids:
            for mat in self.material_by_product_ids.filtered(lambda m: m.bom == 'master'):
                if mat.raw_material_id and mat.quantity_frame > 0:
                    master_bom_line_vals.append({
                        'bom_id': master_bom.id,
                        'product_id': mat.raw_material_id.id,
                        'product_qty': mat.quantity_frame, 
                        'product_uom_id': mat.raw_material_id.uom_id.id, 
                    })
        if master_bom_line_vals:
            self.env['mrp.bom.line'].create(master_bom_line_vals)

        if self.operation_by_product_ids:
            master_ops = self.operation_by_product_ids.filtered(lambda op: op.bom == 'master')
            if master_ops:
                op_vals = []
                for op in master_ops:
                    # USAMOS frames_totales_produccion (Incluye merma)
                    tiempo_total_op = (frames_totales_produccion / op.frames) * op.minutes if op.frames > 0 else 0.0
                    
                    op_vals.append({
                        'name': op.name,
                        'workcenter_id': op.work_center_id.id,
                        'bom_id': master_bom.id,
                        'sequence': op.sequence,
                        'time_cycle_manual': tiempo_total_op,
                    })
                self.env['mrp.routing.workcenter'].create(op_vals)


        # BOM del producto Variante 
        variant_bom = self.env['mrp.bom'].create({
            'product_tmpl_id': variant_tmpl.id,
            'product_qty': total_piezas, 
            'product_uom_id': odoo_uom.id,
            'type': 'normal', 
            'code': f"BOM final - {self.name}", 
        })

        variant_bom_line_vals = []
        # Agregamos el producto Maestro 
        variant_bom_line_vals.append({
            'bom_id': variant_bom.id,
            'product_id': master_product.id,
            'product_qty': qty_master_bom, 
            'product_uom_id': master_product.uom_id.id, 
        })

        if self.material_by_product_ids:
            for mat in self.material_by_product_ids.filtered(lambda m: m.bom == 'variant'):
                if mat.raw_material_id and mat.quantity_frame > 0:
                    variant_bom_line_vals.append({
                        'bom_id': variant_bom.id,
                        'product_id': mat.raw_material_id.id,
                        'product_qty': mat.quantity_frame, 
                        'product_uom_id': mat.raw_material_id.uom_id.id, 
                    })
        if variant_bom_line_vals:
            self.env['mrp.bom.line'].create(variant_bom_line_vals)

        if self.operation_by_product_ids:
            variant_ops = self.operation_by_product_ids.filtered(lambda op: op.bom == 'variant')
            if variant_ops:
                op_variant_vals = []
                for op in variant_ops:
                    # USAMOS frames_totales_produccion (Incluye merma)
                    tiempo_total_op = (frames_totales_produccion / op.frames) * op.minutes if op.frames > 0 else 0.0
                    
                    op_variant_vals.append({
                        'name': op.name,
                        'workcenter_id': op.work_center_id.id,
                        'bom_id': variant_bom.id,
                        'sequence': op.sequence,
                        'time_cycle_manual': tiempo_total_op,
                    })
                self.env['mrp.routing.workcenter'].create(op_variant_vals)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Éxito',
                'message': f'Productos Sábana, Maestro y Variante creados',
                'type': 'success',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.client',
                    'tag': 'reload',
                }
            }
        }
    
    #Crear orden de venta
    def action_create_sales_order(self):
        self.ensure_one()

        if self.sale_order_id:
            raise UserError(f"Ya existe una Orden de Venta generada para esta cotización: {self.sale_order_id.name}")

        if not self.odoo_product:
            raise UserError("No se puede crear la Orden de Venta porque el 'Producto Odoo' no ha sido creado.")
        
        if not self.client_id or not self.client_id.partner_id:
            raise UserError("El cliente seleccionado no tiene un contacto válido.")

        if self.design_ids:
            designs_to_produce = self.design_ids.filtered(lambda d: d.produce)
            total_designs_qty = sum(designs_to_produce.mapped('quantity'))
            if total_designs_qty != self.number_of_pieces:
                raise UserError(
                    f"¡Cantidades descuadradas!\n\n"
                    f"La suma de las cantidades en los diseños ({total_designs_qty:,.0f}) "
                    f"no coincide con la Cantidad Total a producir ({self.number_of_pieces:,.0f}).\n"
                    f"Por favor, ajusta el reparto antes de crear la Orden de Venta."
                )
        vals_so = {
            'partner_id': self.client_id.partner_id.id,
            'state': 'draft',
            'date_order': fields.Datetime.now(),
            'origin': self.name,
            'partner_shipping_id': self.shipping_address_id.id if self.shipping_address_id else self.client_id.partner_id.id
        }
        
        sale_order = self.env['sale.order'].create(vals_so)

        self.sale_order_id = sale_order.id

        price_unit = self.unit_price_discount if self.unit_price_discount > 0 else self.unit_price
        
        lines_vals = []
        
        # 2. Recorrer los diseños para crear las líneas
        if self.design_ids:
            for design in self.design_ids.filtered(lambda d: d.produce and d.quantity > 0):
                if design.odoo_variant_id:
                    if design.description:
                        line_name = f"{design.odoo_variant_id.display_name}\n{design.description}"
                    else:
                        line_name = design.odoo_variant_id.display_name

                    lines_vals.append({
                        'order_id': sale_order.id,
                        'product_id': design.odoo_variant_id.id,
                        'product_uom_qty': design.quantity,
                        'price_unit': price_unit,
                        'name': line_name,  # Aquí pasamos el texto que acabamos de armar
                        'product_uom_id': self.odoo_product.uom_id.id,
                    })
        else:
            # Fallback si no hay diseños específicos
            variant = self.odoo_product.product_variant_ids[0] if self.odoo_product.product_variant_ids else False
            if variant:
                lines_vals.append({
                    'order_id': sale_order.id,
                    'product_id': variant.id,
                    'product_uom_qty': self.number_of_pieces,
                    'price_unit': price_unit,
                    'name': f"{variant.display_name} - {self.finishing_summary or ''}",
                    'product_uom_id': self.odoo_product.uom_id.id,
                })

        if not lines_vals:
            raise UserError("No se pudieron generar las líneas para la Orden de Venta. Verifica las variantes del producto.")

        self.env['sale.order.line'].create(lines_vals)
    
        self.state = 'enter_op'
        
        return {
            'name': 'Orden de Venta',
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': sale_order.id,
            'view_mode': 'form',
            'target': 'current',
        }
        
    def action_view_sales_order(self):
        self.ensure_one()
        if self.sale_order_id:
            return {
                'name': 'Orden de Venta',
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': self.sale_order_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
                
    # Al cambiar el subproducto poner de nuevo en 0
    @api.onchange('by_product')
    def _onchange_by_product_reset_dimensions(self):
        if self.by_product:

            if not self.has_gusset:
                self.gusset = 0.0
            
            if not self.has_side_gusset:
                self.side_gusset = 0.0
            
            if not self.has_fin:
                self.fin_seal = 0.0
                
            if not self.has_fondo:
                self.fondo = 0.0
                
            if not self.has_gap:
                self.perimeter_gap = 0.0
    
    # Colocar vacío el subproducto e impresora al cambiar el producto
    @api.onchange('product_type')
    def _onchange_product_type(self):
        self.by_product = False
        self.printer_id = False
    
    # Función principal que calcula medidas extendidas, distribución en el frame y cantidad total de frames necesarios
    @api.depends('height','width','gusset','side_gusset','fin_seal','fondo','perimeter_gap','bobbin_width','bobbin_height','number_of_pieces','by_product',
    'by_product.geometry_axis','by_product.height_multiplier','by_product.width_multiplier','by_product.gusset_multiplier','by_product.side_gusset_multiplier',
    'by_product.fin_multiplier','by_product.fondo_multiplier','by_product.apply_dynamic_margin','by_product.margin_factor','measure', 'item_by_product_ids.kg', 'item_by_product_ids.item_type_name')
    def _compute_calculate_extended_measures(self):
        for record in self:
            # Geometría y distribución
            desarrollo, transversal = self._calculate_geometry(record)
            rep_width, rep_height, piezas_frame = self._calculate_frame_distribution(record, desarrollo, transversal)

            # CORRECCIÓN: Invertir asignaciones para la tabla visual si la orientación es contra el Ancho
            if record.by_product and record.by_product.geometry_axis == 'width':
                record.width_ext_val = desarrollo
                record.height_ext_val = transversal
                record.fits_width = rep_width
                record.fits_height = rep_height
                record.total_ext_width = rep_width * desarrollo
                record.total_ext_height = rep_height * transversal
            else:
                record.width_ext_val = transversal
                record.height_ext_val = desarrollo
                record.fits_width = rep_width
                record.fits_height = rep_height
                record.total_ext_width = rep_width * transversal
                record.total_ext_height = rep_height * desarrollo

            record.piece_by_frame = piezas_frame
            record.extended_measure = f"{record.width_ext_val:.2f} cm X {record.height_ext_val:.2f} cm" if record.width_ext_val and record.height_ext_val else ""
                        
            # Calculo de frames segun la unidad
            cantidad_ingresada = record.number_of_pieces or 0.0
            
            # Detectar unidad seleccionada
            uom_name = record.measure.name.lower() if record.measure else ''
            is_kilo = 'kilo' in uom_name or 'kg' in uom_name
            is_meter = 'metro' in uom_name or 'meter' in uom_name
            
            if is_meter:
                # Metros -> 1 Frame = Alto de bobina en metros
                frame_length_m = (record.bobbin_height or 0.0) / 100.0
                if frame_length_m > 0:
                    record.total_amount = math.ceil(cantidad_ingresada / frame_length_m)
                else:
                    record.total_amount = 0

            elif is_kilo:
                # Kilos -> 1 Frame = Peso de Sustrato + Laminaciones
                relevant_lines = record.item_by_product_ids.filtered(lambda l: 
                    l.item_type_name and (
                        'sustrato' in l.item_type_name.lower() or 
                        'laminación' in l.item_type_name.lower() or
                        'laminacion' in l.item_type_name.lower()
                    )
                )
                
                peso_por_frame = sum(relevant_lines.mapped('kg'))
                
                if peso_por_frame > 0:
                    record.total_amount = math.ceil(cantidad_ingresada / peso_por_frame)
                else:
                    record.total_amount = 0
            
            else:
                # Piezas -> 1 Frame = Cantidad de piezas que caben en el frame según la geometría calculada
                if piezas_frame:
                    record.total_amount = math.ceil(
                        record.number_of_pieces / piezas_frame
                    )
                else:
                    record.total_amount = 0
                
    # calcula cuánto mide realmente una sola pieza ya extendida sumando los fuelles, aletas, fondos y márgenes.
    def _calculate_geometry(self, record):
        sub = record.by_product
        if not sub:
            return 0.0, 0.0

        # Desarrollo grande
        desarrollo = 0.0

        desarrollo += (record.height or 0.0) * sub.height_multiplier
        desarrollo += (record.width or 0.0) * sub.width_multiplier
        desarrollo += (record.gusset or 0.0) * sub.gusset_multiplier
        desarrollo += (record.side_gusset or 0.0) * sub.side_gusset_multiplier
        desarrollo += (record.fin_seal or 0.0) * sub.fin_multiplier
        desarrollo += (record.fondo or 0.0) * sub.fondo_multiplier

        # Margen dinámico
        if sub.apply_dynamic_margin and desarrollo > 0:
            # CORRECCIÓN: Depender de la orientación para el cálculo del margen
            if sub.geometry_axis == 'width':
                bobbin_dim = record.bobbin_width or 74.0
            else:
                bobbin_dim = record.bobbin_height or 112.0
                
            margen = sub.margin_factor * math.floor(bobbin_dim / desarrollo)
            desarrollo += margen

        # Gap
        transversal_base = record.width if sub.height_multiplier else record.height

        if record.perimeter_gap:
            desarrollo += record.perimeter_gap
            transversal_base += record.perimeter_gap

        return desarrollo, transversal_base
    
    # Función para calcular cuántas piezas caben en un frame considerando las medidas extendidas
    def _calculate_frame_distribution(self, record, desarrollo, transversal):
        if not desarrollo or not transversal:
            return 0, 0, 0

        frame_width = record.bobbin_width or 74.0
        frame_height = record.bobbin_height or 112.0

        if record.by_product and record.by_product.geometry_axis == 'width':
            # Desarrollo grande contra ancho (74)
            rep_width = math.floor(frame_width / desarrollo)
            rep_height = math.floor(frame_height / transversal)
        else:
            # Desarrollo grande contra avance (112)
            rep_width = math.floor(frame_width / transversal)
            rep_height = math.floor(frame_height / desarrollo)

        piezas_por_frame = rep_width * rep_height

        return rep_width, rep_height, piezas_por_frame
                
    # Calculo de medidas Final
    @api.depends('height', 'width')
    def _compute_final_measures(self):
        for record in self:
            if record.height > 0 and record.width > 0:
                record.final_measure = f"{record.height} cm X {record.width} cm"
            else:
                record.final_measure = ""
                
    # Para el nombre del producto    
    @api.depends('client_id', 'by_product')
    def _compute_name(self):
        for record in self:
            subproducto = record.by_product.name if record.by_product else 'SinSubproducto'
            code = record.code if record.code else 'Nuevo'
            
            if record.client_id and record.by_product:
                record.name = f"{code} - {subproducto}"
            else:
                record.name = "Nuevo producto"
                
    # Sobrescribir método create para generar código secuencial y diseños iniciales           
    @api.model
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code', 'New') == 'New':
                vals['code'] = self.env['ir.sequence'].next_by_code('ibs.product.sequence') or 'New'

        records = super().create(vals_list)

        for record in records:
            if record.number_of_changes and record.number_of_changes > 0:
                record._generate_designs()

            record._add_default_logistics_items()

        return records
    
    def _add_default_logistics_items(self):
        self.ensure_one()

        lines = []

        packing = self.env['ibs.item_template'].search(
            [('item_type_id.name', 'ilike', 'Empaque')],
            limit=1
        )

        shipping = self.env['ibs.item_template'].search(
            [('item_type_id.name', 'ilike', 'Envío')],
            limit=1
        )

        if packing:
            lines.append((0, 0, {
                'item_id': packing.id,
                'sequence': 100,
                'unit_cost_base': packing.cost_frame_final,
                'unit_price_base': packing.price_per_frame,
            }))

        if shipping:
            lines.append((0, 0, {
                'item_id': shipping.id,
                'sequence': 110,
                'unit_cost_base': shipping.cost_frame_final,
                'unit_price_base': shipping.price_per_frame,
            }))

        if lines:
            self.write({'logistics_item_ids': lines})

    # Sobrescribir método write para detectar cambios de estado y cantidad de cambios
    def write(self, vals):
        res = super().write(vals)

        if 'state' in vals and vals['state'] == 'enter_op':
            general_project_id_param = self.env['ir.config_parameter'].sudo().get_param('obi:id_proyecto_general')
            if not general_project_id_param:
                raise UserError((
                    "No se ha configurado el 'Proyecto General' en los Parámetros del Sistema.\n"
                    "Por favor, configure la clave 'obi:id_proyecto_general' con el ID del proyecto."
                ))
                
            general_project = self.env['project.project'].browse(int(general_project_id_param))
            if not general_project.exists():
                raise UserError(_(
                    f"El Proyecto General configurado ID: {general_project_id_param} no existe en el sistema."
                ))
                
            for record in self:
                if not record.active_task_id:
                    record._create_initial_process_task()
                    
        if 'number_of_changes' in vals:
            self._generate_designs()

        return res

    # Función para generar tareas de proceso al ingresar a OP
    def _create_initial_process_task(self):
        self.ensure_one()       
        if not self.by_product:
            return
        
        first_process_line = self.env['ibs.byproduct_processes_line'].search([
            ('by_product_id', '=', self.by_product.id)
        ], order='sequence asc, id asc', limit=1)

        if not first_process_line:
            return

        if not first_process_line.process_id.project:
            return

        project = first_process_line.process_id.project

        task_vals = {
            'name': f'{self.name}', 
            'project_id': project.id,
            'ibs_product_id': self.id, 
            'partner_id': self.client_id.id,
            'description': f'Tarea generada automáticamente para {self.name}',
        }
        created_task = self.env['project.task'].create(task_vals)
        
        self.active_task_id = created_task.id
        
        general_project_id_param = self.env['ir.config_parameter'].sudo().get_param('obi:id_proyecto_general')
        general_project = self.env['project.project'].browse(int(general_project_id_param))
        
        if general_project:
            process_name = first_process_line.process_id.name
            general_stage = self.env['project.task.type'].search([
                ('project_ids', 'in', general_project.id),
                ('name', '=', process_name)
            ], limit=1)
            
            general_task_vals = {
                'name': f'{self.name} - Seguimiento General',
                'project_id': general_project.id,
                'ibs_product_id': self.id,
                'partner_id': self.client_id.id,
                'description': 'Seguimiento general del producto.',
            }
            
            if general_stage:
                general_task_vals['stage_id'] = general_stage.id
            
            general_task = self.env['project.task'].create(general_task_vals)
            self.general_task_id = general_task.id
    
    # Para lista de materiales
    def action_generate_materials_list(self):
        self.ensure_one()
        
        total_frames = self.total_amount or 0.0
        total_pieces = self.number_of_pieces or 1.0

        # --- 1. LÓGICA DE CÁLCULO DE FRAMES DE MERMA ---
        frames_seteo = self.by_product.frames_of_setting if self.by_product else 0.0
        porcentaje_merma = 0.0
        
        if self.by_product and self.by_product.decrease_range_ids:
            # Buscamos en qué rango cae el tiraje total de frames
            for rango in self.by_product.decrease_range_ids:
                if rango.minimum <= total_frames <= rango.maximum:
                    porcentaje_merma = rango.percentage / 100.0
                    break

        # Total de frames que se irán a la basura
        total_frames_merma = frames_seteo + (total_frames * porcentaje_merma)

        # --- 2. FUNCIÓN DE CÁLCULO DE CANTIDADES POR REGLA ---
        def calcular_cantidades(rendimiento, completo, menor_a_uno, rule_code):
            # Si no hay rendimiento o la regla es 'none', no calculamos nada
            if not rendimiento or rendimiento <= 0 or rule_code == 'none':
                return 0.0, 0.0, 0.0
            
            # Determinamos cuál es la base del cálculo y a quién le aplica la merma
            if rule_code == 'per_frame':
                base_qty = total_frames
                waste_qty = total_frames_merma # Solo aquí aplicamos la merma de la máquina
            elif rule_code == 'per_piece':
                base_qty = total_pieces
                waste_qty = 0.0 # No se desperdician piezas enteras por calibrar frames
            elif rule_code in ['fixed_prorated', 'fixed_external']:
                base_qty = 1.0 
                waste_qty = 0.0
            else:
                base_qty = total_frames
                waste_qty = 0.0
                
            cantidad_base = base_qty / rendimiento
            cantidad_merma = waste_qty / rendimiento
            
            if completo:
                cantidad_base = math.ceil(cantidad_base)
                cantidad_merma = math.ceil(cantidad_merma)

            if not menor_a_uno and cantidad_base > 0 and cantidad_base < 1.0:
                cantidad_base = 1.0
                
            cantidad_total = cantidad_base + cantidad_merma
                
            return cantidad_base, cantidad_merma, cantidad_total

        # 1. Agrupar los cálculos
        calculated_data = {}

        # Ítems de estructura
        for line in self.item_by_product_ids:
            if line.item_id and line.item_id.list_of_materials_ids:
                rule_code = line.item_id.cost_type.code if line.item_id.cost_type else 'per_frame'
                
                for mat in line.item_id.list_of_materials_ids:
                    if mat.product_id:
                        origin_name = mat.item_id.name if mat.item_id else 'Ítem de estructura'
                        
                        # Recibimos la base, la merma y el total
                        c_base, c_merma, c_total = calcular_cantidades(mat.quantity_frame, mat.full, mat.less_than_one, rule_code)
                        mat_id = mat.product_id.id
                        bom_value = mat.bom
                        
                        if mat_id in calculated_data:
                            calculated_data[mat_id]['quantity_frame'] += c_base
                            calculated_data[mat_id]['quantity_waste'] += c_merma
                            calculated_data[mat_id]['quantity_total'] += c_total
                        else:
                            calculated_data[mat_id] = {
                                'component_name': origin_name, 
                                'quantity_frame': c_base,
                                'quantity_waste': c_merma,
                                'quantity_total': c_total,
                                'bom': bom_value
                            }

        # Ítems de acabados
        for line in self.items_byproduct_ids:
            if line.item_byproduct and line.item_byproduct.materials_ids:
                # Extraemos el código de la regla de costo desde el acabado
                rule_code = line.item_byproduct.cost_type_id.code if line.item_byproduct.cost_type_id else 'per_frame'
                
                for mat in line.item_byproduct.materials_ids:
                    if mat.product_id:
                        origin_name = mat.byproduct_item_id.name if mat.byproduct_item_id else 'Acabado'
                        
                        # Recibimos la base, la merma y el total
                        c_base, c_merma, c_total = calcular_cantidades(mat.quantity_frame, mat.full, mat.less_than_one, rule_code)
                        mat_id = mat.product_id.id
                        bom_value = mat.bom
                        
                        if mat_id in calculated_data:
                            calculated_data[mat_id]['quantity_frame'] += c_base
                            calculated_data[mat_id]['quantity_waste'] += c_merma
                            calculated_data[mat_id]['quantity_total'] += c_total
                        else:
                            calculated_data[mat_id] = {
                                'component_name': origin_name, 
                                'quantity_frame': c_base,
                                'quantity_waste': c_merma,
                                'quantity_total': c_total,
                                'bom': bom_value
                            }

        # 2. SISTEMA DE COMANDOS DE ODOO
        commands = []
        
        for existing_line in self.material_by_product_ids:
            if existing_line.is_auto:
                mat_id = existing_line.raw_material_id.id
                
                if mat_id in calculated_data:
                    commands.append((1, existing_line.id, {
                        'quantity_frame': calculated_data[mat_id]['quantity_frame'],
                        'quantity_waste': calculated_data[mat_id]['quantity_waste'],
                        'quantity_total': calculated_data[mat_id]['quantity_total'],
                        'component_name': calculated_data[mat_id]['component_name'],
                        'bom': calculated_data[mat_id]['bom'],
                    }))
                    del calculated_data[mat_id]
                else:
                    commands.append((2, existing_line.id, 0))

        # 3. ibsOMANDO 0: Crear los que faltan
        for mat_id, data in calculated_data.items():
            commands.append((0, 0, {
                'raw_material_id': mat_id,
                'component_name': data['component_name'],
                'quantity_frame': data['quantity_frame'],
                'quantity_waste': data['quantity_waste'],
                'quantity_total': data['quantity_total'],
                'bom': data['bom'],
                'is_auto': True
            }))

        if commands:
            self.material_by_product_ids = commands
        
        return True
    
    @api.depends('measure')
    def _compute_modes(self):
        for record in self:
            name = record.measure.name.lower() if record.measure else ''
            record.is_kilo_mode = 'kilo' in name or 'kg' in name
            record.is_meter_mode = 'metro' in name or 'meter' in name

    @api.depends('total_amount', 'bobbin_height')
    def _compute_total_meters(self):
        for record in self:
            frame_length_m = (record.bobbin_height or 0.0) / 100.0
            
            record.total_project_meters = (record.total_amount or 0.0) * frame_length_m

    @api.depends('item_by_product_ids.kg', 'item_by_product_ids.item_type_name', 'total_amount', 'piece_by_frame')
    def _compute_total_project_kg(self):
        for record in self:
            relevant_lines = record.item_by_product_ids.filtered(lambda l: 
                l.item_type_name and (
                    'sustrato' in l.item_type_name.lower() or 
                    'laminación' in l.item_type_name.lower() or
                    'laminacion' in l.item_type_name.lower()
                )
            )
            
            unit_weight_sum = sum(relevant_lines.mapped('kg'))
            record.kilos_per_frame_structure = unit_weight_sum
            
            frames_totales = record.total_amount or 0.0
            record.total_project_kg = unit_weight_sum * frames_totales

            if unit_weight_sum > 0:
                frames_per_kg = 1.0 / unit_weight_sum
                pxf = record.piece_by_frame or 1.0
                record.yield_pieces_kg = frames_per_kg * pxf
            else:
                record.yield_pieces_kg = 0.0
    
    def _expand_states(self, states, domain):
        return [key for key, val in type(self).state.selection]
    
    #Resumen de acabados
    @api.depends('items_acabados_ids')
    def _compute_finishing_summary(self):
        for record in self:
            if record.items_acabados_ids:
                nombres = record.items_acabados_ids.mapped('item_byproduct.name')
                texto_final = " - ".join(nombres)
                record.finishing_summary = texto_final
            else:
                record.finishing_summary = ""

    #Colocar información de envio del cliente
    @api.onchange('client_id')
    def _onchange_client_id_shipping_info(self):
        if self.client_id and self.client_id.partner_id:
            partner = self.client_id.partner_id
            
            self.dest_name = partner.name or ''
            self.dest_street = partner.street or ''
            self.dest_colonia = partner.street2 or ''
            self.dest_zip = partner.zip or ''
            self.dest_city = partner.city or ''
            
            self.dest_state = partner.state_id.name if partner.state_id else ''
            self.dest_country = partner.country_id.name if partner.country_id else ''
        else:
            self.dest_name = ''
            self.dest_street = ''
            self.dest_colonia = ''
            self.dest_zip = ''
            self.dest_city = ''
            self.dest_state = ''
            self.dest_country = ''
           
    def _generate_designs(self):
        max_changes = 500
        
        for record in self:
            if record.number_of_changes > max_changes:
                raise UserError(f"Por seguridad, no se pueden crear más de {max_changes} diseños a la vez.")
            
            if record.number_of_changes > 0:
                record.design_ids.unlink() 

                first_stage = self.env['ibs.product_design_stage'].search([], limit=1, order='sequence')
                
                design_vals = []
                for i in range(1, record.number_of_changes + 1):
                    design_vals.append({
                        'product_id': record.id,
                        'design_number': i,
                        'name': f'D{i}',
                        'stage_id': first_stage.id if first_stage else False,
                    })
                
                if design_vals:
                    self.env['ibs.product_design'].create(design_vals)
                    
    
    