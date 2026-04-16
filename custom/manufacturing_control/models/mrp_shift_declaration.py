import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class MrpShiftDeclaration(models.Model):
    _name = 'mrp.shift.declaration'
    _description = 'Declaración de Producción por Turno'
    _order = 'create_date desc'

    # ─── Core Fields ────────────────────────────────────────────────
    production_id = fields.Many2one(
        'mrp.production',
        string="Orden de Producción",
        required=True,
        ondelete='cascade',
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string="Operario",
        required=True,
        index=True,
    )
    operation_id = fields.Many2one(
        'mrp.routing.workcenter',
        string="Operación",
        help="Operación específica reportada (ej. Extrusión, Inyección).",
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
    backorder_created_id = fields.Many2one(
        'mrp.production',
        string="Backorder Generado",
        readonly=True,
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
        """Check overproduction thresholds. Returns dict with status info."""
        self.ensure_one()
        mo = self.production_id

        # Get ALL related MOs from the production group (original + backorders)
        if mo.production_group_id:
            group_mos = mo.production_group_id.production_ids
        else:
            group_mos = mo

        # Original total = sum of done + active
        total_original_qty = sum(
            p.product_qty for p in group_mos.filtered(
                lambda p: p.state != 'cancel'
            )
        )
        # Already done via kiosk
        total_already_done = sum(
            p.shift_total_declared for p in group_mos.filtered(
                lambda p: p.state == 'done'
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
        1. Validate MO state
        2. Validate overproduction
        3. Set qty_producing on the MO
        4. Auto-generate lot if product uses lot tracking
        5. Mark raw materials as picked (proportional consumption via native unit_factor)
        6. Execute button_mark_done with silent backorder
        7. Record the backorder created
        """
        self.ensure_one()
        mo = self.production_id

        # ── Step 1: Validate MO state ──────────────────────────────
        if mo.state in ('done', 'cancel'):
            self.write({
                'state': 'error',
                'error_message': _(
                    "La OP %s está %s. No se puede registrar producción."
                ) % (mo.name, dict(mo._fields['state'].selection).get(mo.state)),
            })
            raise UserError(self.error_message)

        if mo.state == 'draft':
            mo.action_confirm()

        # ── Step 2: Validate overproduction ────────────────────────
        overproduction = self._validate_overproduction()
        if overproduction['status'] == 'blocked':
            self.write({
                'state': 'error',
                'error_message': overproduction['message'],
            })
            raise UserError(self.error_message)

        # ── Step 3: Set qty_producing ──────────────────────────────
        qty_to_produce = min(self.qty_declared, mo.product_qty)
        mo.qty_producing = qty_to_produce

        try:
            # ── Step 4: Auto-generate lot if needed ────────────────
            if mo.product_tracking in ('lot', 'serial') and not mo.lot_producing_ids:
                mo.action_generate_serial()


            # ── Step 6: Execute button_mark_done silently ──────────
            # Determine if we need a backorder (partial production)
            needs_backorder = qty_to_produce < mo.product_qty

            # Context flags explanation:
            # - skip_backorder: skips _get_quantity_produced_issues() so no wizard prompt
            # - mo_ids_to_backorder: tells button_mark_done to call _split_productions()
            # - skip_consumption: skips _get_consumption_issues() so no consumption wizard
            # - skip_redirection: prevents redirecting to the backorder form view
            # - skip_immediate: prevents immediate production wizard
            ctx = {
                'skip_backorder': True,
                'skip_consumption': True,
                'skip_redirection': True,
                'skip_immediate': True,
            }
            if needs_backorder:
                ctx['mo_ids_to_backorder'] = [mo.id]

            result = mo.with_context(**ctx).button_mark_done()

            _logger.info(
                "button_mark_done result for %s: %s (MO state after: %s)",
                mo.name, result, mo.state,
            )

            # ── Step 7: Find and record backorder ──────────────────
            backorder = self.env['mrp.production']
            if needs_backorder and mo.production_group_id:
                backorder = mo.production_group_id.production_ids.filtered(
                    lambda p: p.state not in ('done', 'cancel') and p.id != mo.id
                )[:1]

            self.write({
                'state': 'done',
                'backorder_created_id': backorder.id if backorder else False,
            })

            _logger.info(
                "Shift declaration processed: MO=%s, Employee=%s, Qty=%s, Backorder=%s",
                mo.name,
                self.employee_id.name,
                qty_to_produce,
                backorder.name if backorder else 'None',
            )

            return {
                'success': True,
                'message': _("¡Registrado! %s unidades de %s") % (
                    qty_to_produce,
                    mo.product_id.display_name,
                ),
                'backorder_name': backorder.name if backorder else False,
            }

        except Exception as e:
            self.write({
                'state': 'error',
                'error_message': str(e),
            })
            _logger.exception(
                "Error processing shift declaration for MO=%s: %s",
                mo.name, str(e),
            )
            raise

    # ─── Kiosk API Methods ──────────────────────────────────────────

    @api.model
    def kiosk_validate_production(self, production_barcode):
        """Validate a production order barcode from the kiosk.

        When the operator scans the parent MO (e.g., "Tubo Completo"), the system:
        1. Finds the parent MO
        2. Gets ALL child MOs (sub-assemblies: Manga, Corona, Tapa)
        3. Returns them so the operator can choose which one to report on
        """
        # Search for exact match first (any state except cancel)
        mo = self.env['mrp.production'].search([
            ('name', '=', production_barcode),
            ('state', '!=', 'cancel'),
        ], limit=1)

        if not mo:
            # Try partial match
            mo = self.env['mrp.production'].search([
                ('name', 'ilike', production_barcode),
                ('state', '!=', 'cancel'),
            ], limit=1)

        if not mo:
            return {'error': _("No se encontró una OP con código: %s") % production_barcode}

        # ── Get child MOs (sub-assemblies) ──────────────────────────
        child_mos = self.env['mrp.production']
        try:
            child_mos = mo._get_children()
        except Exception:
            pass

        # Build list of child MOs for the kiosk (deduplicated by production group)
        child_orders = []
        seen_groups = set()
        if child_mos:
            for child in child_mos.filtered(lambda p: p.state != 'cancel').sorted('id'):
                # Deduplicate: skip if we already processed this production group
                group_key = child.production_group_id.id if child.production_group_id else child.id
                if group_key in seen_groups:
                    continue
                seen_groups.add(group_key)
                # For each child MO, also include its backorders
                group_mos = child
                if child.production_group_id:
                    group_mos = child.production_group_id.production_ids.filtered(
                        lambda p: p.state != 'cancel'
                    )
                # Find the active one (not done) in the backorder chain
                active_child = group_mos.filtered(
                    lambda p: p.state in ('confirmed', 'progress')
                )[:1]
                done_qty = sum(
                    p.qty_produced for p in group_mos.filtered(
                        lambda p: p.state == 'done'
                    )
                )
                total_qty = sum(p.product_qty for p in group_mos)

                # Get operations for this child
                operations = []
                bom = active_child.bom_id if active_child else child.bom_id
                if bom and bom.operation_ids:
                    for op in bom.operation_ids:
                        operations.append({
                            'id': op.id,
                            'name': op.name,
                            'workcenter': op.workcenter_id.name,
                        })

                child_orders.append({
                    'id': active_child.id if active_child else child.id,
                    'name': active_child.name if active_child else child.name,
                    'product_name': child.product_id.display_name,
                    'product_qty': active_child.product_qty if active_child else 0,
                    'total_qty': total_qty,
                    'done_qty': done_qty,
                    'state': active_child.state if active_child else child.state,
                    'is_active': bool(active_child),
                    'operations': operations,
                })

        # Also check if the scanned MO itself can be reported on
        # (it might be a standalone MO without children)
        has_children = bool(child_orders)

        # Always add the parent/scanned MO to the list
        # (for standalone MOs this is the only entry;
        #  for parent MOs with children, this is the final assembly step)
        group_mos = mo
        if mo.production_group_id:
            group_mos = mo.production_group_id.production_ids.filtered(
                lambda p: p.state != 'cancel'
            ).sorted('id')

        active_mo = group_mos.filtered(
            lambda p: p.state in ('confirmed', 'progress')
        )[:1]

        if active_mo:
            operations = []
            if active_mo.bom_id and active_mo.bom_id.operation_ids:
                for op in active_mo.bom_id.operation_ids:
                    operations.append({
                        'id': op.id,
                        'name': op.name,
                        'workcenter': op.workcenter_id.name,
                    })

            total_declared = sum(
                p.shift_total_declared for p in group_mos.filtered(
                    lambda x: x.state == 'done'
                )
            )

            label = active_mo.product_id.display_name

            # Check if all children are done before allowing parent production
            parent_is_active = True
            if has_children:
                label = "⭐ " + label + " (Ensamble Final)"
                # Check if ALL child MOs (across all groups) are done
                all_children_done = all(
                    c.get('state') == 'done' or not c.get('is_active')
                    for c in child_orders
                )
                if not all_children_done:
                    parent_is_active = False

            child_orders.append({
                'id': active_mo.id,
                'name': active_mo.name,
                'product_name': label,
                'product_qty': active_mo.product_qty,
                'total_qty': sum(p.product_qty for p in group_mos),
                'done_qty': total_declared,
                'state': active_mo.state,
                'is_active': parent_is_active,
                'blocked_reason': '' if parent_is_active else 'Componentes pendientes',
                'operations': operations,
            })
        elif not has_children:
            if group_mos.filtered(lambda p: p.state == 'draft'):
                return {'error': _(
                    "La orden %s está en estado Borrador. Confírmala primero en el sistema."
                ) % production_barcode}

            # No active MO and no children — everything is done
            return {'error': _(
                "Todas las órdenes de %s ya están terminadas."
            ) % production_barcode}

        return {
            'parent_mo_name': mo.name,
            'parent_product_name': mo.product_id.display_name,
            'has_children': has_children,
            'child_orders': child_orders,
        }

    @api.model
    def kiosk_validate_employee(self, barcode):
        """Validate an employee badge barcode from the kiosk."""
        employee = self.env['hr.employee'].sudo().search([
            ('barcode', '=', barcode),
        ], limit=1)

        if not employee:
            return {'error': _("No se encontró empleado con gafete: %s") % barcode}

        return {
            'employee_id': employee.id,
            'employee_name': employee.name,
        }

    @api.model
    def kiosk_check_overproduction(self, mo_id, qty):
        """Check overproduction thresholds before submitting."""
        declaration = self.new({
            'production_id': mo_id,
            'qty_declared': qty,
        })
        return declaration._validate_overproduction()

    @api.model
    def kiosk_create_and_process(self, mo_id, employee_id, operation_id, qty):
        """Create a shift declaration and process it. Main kiosk entry point."""
        declaration = self.create({
            'production_id': mo_id,
            'employee_id': employee_id,
            'operation_id': operation_id or False,
            'qty_declared': qty,
        })
        return declaration.action_process_declaration()
