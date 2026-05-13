import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

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
    shift_id = fields.Many2one(
        'tdmx.shift',
        string="Turno",
        readonly=True,
        index=True,
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

    # ─── Overproduction Validation (CU-08: Cascading Rules) ────────

    def _get_applicable_limit(self):
        """Determine the applicable limit (max units per declaration).

        Cascade:
          1. Workorder exception (CU-03) → highest priority
          2. Workcenter global limit (CU-02)
        Returns: limit_value (float) or 0 if no rule.
        """
        self.ensure_one()
        wo = self.workorder_id

        # ── Level 1: Workorder exception (CU-03) ───────────────────
        if wo.limit_value:
            return wo.limit_value

        # ── Level 2: Workcenter global limit (CU-02) ───────────────
        wc_limit = self.env['mrp.workcenter.limit'].sudo().search([
            ('workcenter_id', '=', wo.workcenter_id.id),
        ], limit=1)
        if wc_limit:
            return wc_limit.limit_value

        # No rule configured → process without limit
        return 0

    def _validate_overproduction(self):
        """Check if this single declaration exceeds the allowed limit.

        The limit applies PER SINGLE DECLARATION (max units at once),
        not against the accumulated total.

        Cascade: WO exception → CT global → no limit.
        """
        self.ensure_one()
        wo = self.workorder_id

        if not wo:
            return {'status': 'ok', 'pct': 0, 'message': ''}

        # ── Get applicable limit (max units per declaration) ───────
        max_allowed = self._get_applicable_limit()

        if not max_allowed:
            # No rule configured → process without limit
            return {'status': 'ok', 'pct': 0, 'message': ''}

        # ── Compare single declaration vs limit ────────────────────
        if self.qty_declared > max_allowed:
            return {
                'status': 'blocked',
                'pct': 0,
                'message': _(
                    "BLOQUEADO: Está declarando %.1f unidades, "
                    "el máximo permitido por declaración es %.1f unidades."
                ) % (self.qty_declared, max_allowed),
            }

        return {
            'status': 'ok',
            'pct': 0,
            'message': '',
        }

    # ─── Raw Material Consumption (Regla de 3) ───────────────────────

    def _consume_raw_materials(self):
        """Register proportional material consumption using the rule of three.

        Only consumes components assigned to this specific workorder
        (wo.move_raw_ids), which correspond to the work center where
        the operator declared. These same stock.move records appear
        in the MO's Components tab automatically.

        Rule of three:
          percentage = qty_declared / mo.product_qty
          consumption = percentage × move.product_uom_qty (total demanded)
        """
        self.ensure_one()
        wo = self.workorder_id
        mo = self.production_id

        # Planned quantity for the MO (denominator of rule of three)
        planned_qty = mo.product_qty
        if not planned_qty:
            _logger.warning(
                "MO=%s has no planned quantity, cannot calculate consumption.",
                mo.name,
            )
            return

        # Only components assigned to this workorder (this work center)
        raw_moves = wo.move_raw_ids.filtered(
            lambda m: m.state not in ('done', 'cancel')
        )

        if not raw_moves:
            _logger.info(
                "No raw materials assigned to WO=%s (workcenter=%s) for MO=%s",
                wo.name, wo.workcenter_id.name, mo.name,
            )
            return

        # Total declared via kiosk for this WO (past done + current)
        total_kiosk_qty = sum(
            d.qty_declared for d in wo.shift_declaration_ids.filtered(
                lambda d: d.state == 'done' and d.id != self.id
            )
        ) + self.qty_declared

        # Percentage of total kiosk production vs planned
        pct_produced = total_kiosk_qty / planned_qty

        consumption_details = []
        for move in raw_moves:
            # Rule of three: total proportional consumption
            # consumption = (total_kiosk_qty / product_qty) × product_uom_qty
            correct_qty = move.product_uom.round(
                pct_produced * move.product_uom_qty
            )
            if correct_qty <= 0:
                continue

            # SET the quantity (not accumulate) — avoids duplication
            # with Odoo's pre-filled values
            move._set_quantity_done(correct_qty)
            move.picked = True

            consumption_details.append(
                f"{move.product_id.name}: {correct_qty} {move.product_uom.name}"
            )

        _logger.info(
            "Raw material consumption SET: MO=%s, WO=%s, "
            "total_kiosk_qty=%s (%.1f%%), components=[%s]",
            mo.name, wo.name, total_kiosk_qty,
            pct_produced * 100,
            ', '.join(consumption_details),
        )

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

            # ── Step 4: Register proportional material consumption ─
            self._consume_raw_materials()

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
        """Validate an employee badge barcode and return their kiosk config.
        Work center config is read from the Odoo session (shared across all operators).
        """
        employee = self.env['hr.employee'].sudo().search([
            ('barcode', '=', barcode),
        ], limit=1)

        if not employee:
            return {'error': _("No se encontró empleado con gafete: %s") % barcode}

        # Read work center config from session (shared for this kiosk)
        session_wc_ids = request.session.get('kiosk_workcenter_ids', [])
        workcenter_ids = []
        if session_wc_ids:
            workcenters = self.env['mrp.workcenter'].sudo().browse(session_wc_ids).exists()
            workcenter_ids = [{
                'id': wc.id,
                'name': wc.name,
            } for wc in workcenters]

        return {
            'employee_id': employee.id,
            'employee_name': employee.name,
            'has_config': bool(session_wc_ids),
            'workcenter_ids': workcenter_ids,
        }

    @api.model
    def kiosk_get_available_workcenters(self):
        """Return all available work centers for kiosk configuration."""
        workcenters = self.env['mrp.workcenter'].sudo().search([], order='name')
        return [{
            'id': wc.id,
            'name': wc.name,
        } for wc in workcenters]

    @api.model
    def kiosk_save_session_config(self, workcenter_ids):
        """Save work center configuration to the Odoo session.
        This config is shared across all operators using this kiosk session.
        """
        request.session['kiosk_workcenter_ids'] = workcenter_ids
        return {'success': True}

    @api.model
    def kiosk_has_session_config(self):
        """Check if the session already has work centers configured."""
        session_wc_ids = request.session.get('kiosk_workcenter_ids', [])
        return {'has_config': bool(session_wc_ids)}

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

        # Get work centers from session (shared for this kiosk)
        session_wc_ids = request.session.get('kiosk_workcenter_ids', [])

        if not session_wc_ids:
            return {'error': _(
                "No tiene centros de trabajo configurados. Reconfigure el kiosco."
            )}

        employee_wc_ids = session_wc_ids

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
        
        # ── Lógica de Asignación Automática de Turno ──
        shift_id = False
        now_utc = fields.Datetime.now()
        now_local = fields.Datetime.context_timestamp(self, now_utc)
        current_hour = now_local.hour + now_local.minute / 60.0

        shifts = self.env['tdmx.shift'].sudo().search([])
        best_shift = None
        min_diff = float('inf')

        for shift in shifts:
            # Calcular la diferencia absoluta considerando el cruce de medianoche (24 horas)
            diff = abs(shift.end_time - current_hour)
            diff = min(diff, 24.0 - diff)
            
            # Buscar el turno cuya hora de cierre se acerque más, con margen de +/- 1 hora
            if diff <= 1.0 and diff < min_diff:
                min_diff = diff
                best_shift = shift

        if best_shift:
            shift_id = best_shift.id

        declaration = self.create({
            'workorder_id': workorder_id,
            'employee_id': employee_id,
            'qty_declared': qty,
            'shift_id': shift_id,
        })
        return declaration.action_process_declaration()
