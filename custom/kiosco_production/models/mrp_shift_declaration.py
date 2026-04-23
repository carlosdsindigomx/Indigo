import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class MrpShiftDeclaration(models.Model):
    _name = 'mrp.shift.declaration'
    _description = 'Declaración de Producción por Turno'
    _order = 'create_date desc'

    # ─── Core Fields ────────────────────────────────────────────────
    workorder_id = fields.Many2one(
        'mrp.workorder',
        string="Orden de Trabajo",
        required=True,
        ondelete='cascade',
        index=True,
    )
    production_id = fields.Many2one(
        'mrp.production',
        string="Orden de Producción",
        related='workorder_id.production_id',
        store=True,
        index=True,
    )
    workcenter_id = fields.Many2one(
        'mrp.workcenter',
        string="Centro de Trabajo",
        related='workorder_id.workcenter_id',
        store=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string="Operario",
        required=True,
        index=True,
    )
    qty_declared = fields.Float(
        string="Cantidad Declarada",
        required=True,
        digits='Product Unit of Measure',
    )
    date_declared = fields.Datetime(
        string="Fecha de Declaración",
        default=fields.Datetime.now,
        readonly=True,
    )

    # ─── State Machine ──────────────────────────────────────────────
    state = fields.Selection(
        [
            ('draft', 'Borrador'),
            ('done', 'Procesado'),
            ('error', 'Error'),
        ],
        string="Estado",
        default='draft',
        readonly=True,
        index=True,
    )

    # ─── Traceability ───────────────────────────────────────────────
    production_name = fields.Char(
        string="Referencia OP",
        related='production_id.name',
        store=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string="Producto",
        related='production_id.product_id',
        store=True,
    )
    workcenter_name = fields.Char(
        string="Centro de Trabajo",
        related='workcenter_id.name',
        store=True,
    )
    workorder_name = fields.Char(
        string="Operación",
        related='workorder_id.name',
        store=True,
    )
    error_message = fields.Text(
        string="Detalle de Error",
        readonly=True,
    )

    # ─── Computed Info ───────────────────────────────────────────────
    production_state = fields.Selection(
        related='production_id.state',
        string="Estado OP",
    )
    production_qty = fields.Float(
        related='production_id.product_qty',
        string="Cantidad Meta OP",
    )

    # ─── Constraints ────────────────────────────────────────────────
    @api.constrains('qty_declared')
    def _check_qty_declared(self):
        for record in self:
            if record.qty_declared <= 0:
                raise ValidationError(
                    _("La cantidad declarada debe ser mayor a cero.")
                )

    # ─── Overproduction Validation ──────────────────────────────────

    def _validate_overproduction(self):
        """Check overproduction thresholds against the work order."""
        self.ensure_one()
        wo = self.workorder_id

        if not wo:
            return {'status': 'ok', 'pct': 0, 'message': ''}

        # Total quantity for the WO
        total_original_qty = getattr(wo, 'qty_production', wo.production_id.product_qty)

        # Already declared via kiosk for this WO
        total_already_done = sum(
            d.qty_declared for d in wo.shift_declaration_ids.filtered(
                lambda d: d.state == 'done'
            )
        )
        projected_total = total_already_done + self.qty_declared
        pct = (projected_total / total_original_qty * 100) if total_original_qty else 0

        # Read thresholds from Settings (ir.config_parameter)
        ICP = self.env['ir.config_parameter'].sudo()
        warning_pct = float(ICP.get_param(
            'manufacturing_control.overproduction_warning', '100.0'
        ))
        block_pct = float(ICP.get_param(
            'manufacturing_control.overproduction_block', '105.0'
        ))

        if pct > block_pct:
            return {
                'status': 'blocked',
                'pct': pct,
                'message': _(
                    "BLOQUEADO: La producción total alcanzaría %.1f%%, "
                    "superando el límite de %.1f%%."
                ) % (pct, block_pct),
            }
        elif pct > warning_pct:
            return {
                'status': 'warning',
                'pct': pct,
                'message': _(
                    "ADVERTENCIA: La producción total alcanzaría %.1f%%, "
                    "superando la meta del %.1f%%."
                ) % (pct, warning_pct),
            }
        return {
            'status': 'ok',
            'pct': pct,
            'message': '',
        }

    # ─── Main Processing Logic ──────────────────────────────────────

    def action_process_declaration(self):
        """
        Process a shift declaration:
        1. Validate WO and MO state
        2. Validate overproduction
        3. Record the declaration as done
        """
        self.ensure_one()
        wo = self.workorder_id
        mo = self.production_id

        # ── Step 1: Validate states ─────────────────────────────────
        if mo.state in ('done', 'cancel'):
            self.write({
                'state': 'error',
                'error_message': _(
                    "La OP %s está %s. No se puede registrar producción."
                ) % (mo.name, dict(mo._fields['state'].selection).get(mo.state)),
            })
            raise UserError(self.error_message)

        if wo.state in ('done', 'cancel'):
            self.write({
                'state': 'error',
                'error_message': _(
                    "La orden de trabajo %s está %s. No se puede registrar."
                ) % (wo.name, dict(wo._fields['state'].selection).get(wo.state)),
            })
            raise UserError(self.error_message)

        # ── Step 2: Validate overproduction ────────────────────────
        overproduction = self._validate_overproduction()
        if overproduction['status'] == 'blocked':
            self.write({
                'state': 'error',
                'error_message': overproduction['message'],
            })
            raise UserError(self.error_message)

        try:
            # ── Step 3: Mark declaration as done ───────────────────
            self.write({'state': 'done'})
            
            # Update the work order qty_produced
            if hasattr(wo, 'qty_produced'):
                wo.qty_produced += self.qty_declared

            _logger.info(
                "Shift declaration processed: WO=%s, MO=%s, Employee=%s, Qty=%s, Workcenter=%s",
                wo.name,
                mo.name,
                self.employee_id.name,
                self.qty_declared,
                wo.workcenter_id.name,
            )

            return {
                'success': True,
                'message': _("¡Registrado! %s unidades en %s") % (
                    self.qty_declared,
                    wo.workcenter_id.name,
                ),
            }

        except Exception as e:
            self.write({
                'state': 'error',
                'error_message': str(e),
            })
            _logger.exception(
                "Error processing shift declaration for WO=%s: %s",
                wo.name, str(e),
            )
            raise

    # ─── Kiosk API Methods ──────────────────────────────────────────

    @api.model
    def kiosk_validate_employee(self, barcode):
        """Validate an employee badge barcode and return their kiosk config."""
        employee = self.env['hr.employee'].sudo().search([
            ('barcode', '=', barcode),
        ], limit=1)

        if not employee:
            return {'error': _("No se encontró empleado con gafete: %s") % barcode}

        # Look up kiosk config
        config = self.env['kiosk.employee.config'].sudo().search([
            ('employee_id', '=', employee.id),
        ], limit=1)

        workcenter_ids = []
        if config:
            workcenter_ids = [{
                'id': wc.id,
                'name': wc.name,
            } for wc in config.workcenter_ids]

        return {
            'employee_id': employee.id,
            'employee_name': employee.name,
            'has_config': bool(config and config.workcenter_ids),
            'workcenter_ids': workcenter_ids,
        }

    @api.model
    def kiosk_get_available_workcenters(self):
        """Return all available work centers for kiosk configuration."""
        workcenters = self.env['mrp.workcenter'].sudo().search([])
        return [{
            'id': wc.id,
            'name': wc.name,
        } for wc in workcenters]

    @api.model
    def kiosk_save_employee_config(self, employee_id, workcenter_ids):
        """Save or update the employee's kiosk work center configuration."""
        config = self.env['kiosk.employee.config'].sudo().search([
            ('employee_id', '=', employee_id),
        ], limit=1)

        if config:
            config.write({'workcenter_ids': [(6, 0, workcenter_ids)]})
        else:
            self.env['kiosk.employee.config'].sudo().create({
                'employee_id': employee_id,
                'workcenter_ids': [(6, 0, workcenter_ids)],
            })

        return {'success': True}

    @api.model
    def kiosk_get_workorders(self, production_barcode, employee_id):
        """Get work orders for a production order and its sub-orders,
        filtered by employee's work centers.

        1. Find the production order by barcode/name
        2. Find all sub-orders that reference it in their origin field
        3. Collect work orders from the entire family
        4. Filter by the employee's configured work centers
        5. Return grouped by MO
        """
        # Search for the production order
        mo = self.env['mrp.production'].search([
            ('name', '=', production_barcode),
            ('state', '!=', 'cancel'),
        ], limit=1)

        if not mo:
            mo = self.env['mrp.production'].search([
                ('name', 'ilike', production_barcode),
                ('state', '!=', 'cancel'),
            ], limit=1)

        if not mo:
            return {'error': _("No se encontró una OP con código: %s") % production_barcode}

        if mo.state in ('done', 'cancel', 'draft'):
            state_label = dict(mo.fields_get(['state'])['state']['selection']).get(mo.state, mo.state)
            return {'error': _(
                "La orden %s está en estado %s."
            ) % (mo.name, state_label)}

        # Get employee's configured work centers
        config = self.env['kiosk.employee.config'].sudo().search([
            ('employee_id', '=', employee_id),
        ], limit=1)

        if not config or not config.workcenter_ids:
            return {'error': _(
                "No tiene centros de trabajo configurados. Reconfigure el kiosco."
            )}

        employee_wc_ids = config.workcenter_ids.ids

        # ── Build family: parent MO + child MOs via native mechanism ─
        family_mos = mo._get_family_orders().filtered(
            lambda m: m.state not in ('done', 'cancel')
        )

        # ── Collect work orders from entire family ──────────────────
        family_orders = []
        has_any_wo = False

        for family_mo in family_mos.sorted('id'):
            workorders = family_mo.workorder_ids.filtered(
                lambda wo: wo.workcenter_id.id in employee_wc_ids
                    and wo.state not in ('done', 'cancel')
            )

            if not workorders:
                continue

            has_any_wo = True
            wo_list = []
            for wo in workorders.sorted('id'):
                # Get total declared for this work order from kiosk
                total_declared = sum(
                    d.qty_declared for d in self.search([
                        ('workorder_id', '=', wo.id),
                        ('state', '=', 'done'),
                    ])
                )

                wo_list.append({
                    'id': wo.id,
                    'name': wo.name,
                    'workcenter_id': wo.workcenter_id.id,
                    'workcenter_name': wo.workcenter_id.name,
                    'product_name': family_mo.product_id.display_name,
                    'product_qty': family_mo.product_qty,
                    'qty_produced': wo.qty_produced,
                    'total_declared': total_declared,
                    'state': wo.state,
                })

            family_orders.append({
                'mo_id': family_mo.id,
                'mo_name': family_mo.name,
                'product_name': family_mo.product_id.display_name,
                'product_qty': family_mo.product_qty,
                'is_parent': family_mo.id == mo.id,
                'workorders': wo_list,
            })

        if not has_any_wo:
            # Check if there are WOs in the family but none match
            all_family_wos = family_mos.mapped('workorder_ids').filtered(
                lambda wo: wo.state not in ('done', 'cancel')
            )
            if all_family_wos:
                return {'error': _(
                    "La OP %s y sus subórdenes tienen órdenes de trabajo, pero ninguna "
                    "coincide con sus centros de trabajo configurados."
                ) % mo.name}
            else:
                done_wos = family_mos.mapped('workorder_ids').filtered(
                    lambda wo: wo.state == 'done'
                )
                if done_wos:
                    return {'error': _(
                        "Todas las órdenes de trabajo de %s y sus subórdenes ya están terminadas."
                    ) % mo.name}
                return {'error': _(
                    "La OP %s no tiene órdenes de trabajo. "
                    "Verifique que la lista de materiales tenga operaciones definidas."
                ) % mo.name}

        return {
            'mo_name': mo.name,
            'mo_product_name': mo.product_id.display_name,
            'mo_qty': mo.product_qty,
            'family_orders': family_orders,
        }

    @api.model
    def kiosk_check_overproduction(self, workorder_id, qty):
        """Check overproduction thresholds before submitting."""
        wo = self.env['mrp.workorder'].browse(workorder_id)
        if not wo.exists():
            return {'status': 'error', 'message': _("Orden de trabajo no encontrada.")}

        declaration = self.new({
            'workorder_id': workorder_id,
            'qty_declared': qty,
        })
        return declaration._validate_overproduction()

    @api.model
    def kiosk_create_and_process(self, workorder_id, employee_id, qty):
        """Create a shift declaration and process it. Main kiosk entry point."""
        declaration = self.create({
            'workorder_id': workorder_id,
            'employee_id': employee_id,
            'qty_declared': qty,
        })
        return declaration.action_process_declaration()
