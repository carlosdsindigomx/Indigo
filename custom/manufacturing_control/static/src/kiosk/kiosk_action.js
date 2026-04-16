/** @odoo-module */

import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class KioskAction extends Component {
    static template = "manufacturing_control.KioskAction";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.moInputRef = useRef("moInput");
        this.empInputRef = useRef("empInput");
        this.qtyInputRef = useRef("qtyInput");

        this.state = useState({
            // Step 1: MO scan
            moBarcode: "",
            moError: "",
            parentData: null,       // { parent_mo_name, parent_product_name, has_children, child_orders }

            // Step 2: Select child MO
            selectedChild: null,    // the selected child order object

            // Step 3: Employee
            empBarcode: "",
            empError: "",
            empData: null,

            // Step 4: Operation
            selectedOperationId: false,

            // Step 5: Quantity
            qtyDeclared: "",
            overproductionWarning: "",
            overproductionBlock: "",

            // General
            isProcessing: false,
            successMessage: "",
        });

        onMounted(() => this.focusNext());
    }

    focusNext() {
        if (!this.state.parentData) {
            this.moInputRef.el?.focus();
        } else if (!this.state.empData) {
            this.empInputRef.el?.focus();
        } else {
            this.qtyInputRef.el?.focus();
        }
    }

    // ─── MO Scan ──────────────────────────────────────────────────

    onMoBarcodeInput(ev) {
        this.state.moBarcode = ev.target.value;
        this.state.moError = "";
    }

    async onMoBarcodeKeydown(ev) {
        if (ev.key !== "Enter") return;
        if (!this.state.moBarcode.trim()) return;

        this.state.isProcessing = true;
        this.state.moError = "";

        try {
            const result = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_validate_production",
                [this.state.moBarcode.trim()]
            );

            if (result.error) {
                this.state.moError = result.error;
            } else {
                this.state.parentData = result;
                this.state.selectedChild = null;
                this.state.selectedOperationId = false;

                // Auto-select if only one child order
                if (result.child_orders && result.child_orders.length === 1) {
                    this.selectChild(result.child_orders[0]);
                }

                setTimeout(() => this.focusNext(), 50);
            }
        } catch (e) {
            this.state.moError = "Error de conexión. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    clearMo() {
        this.state.parentData = null;
        this.state.selectedChild = null;
        this.state.moBarcode = "";
        this.state.selectedOperationId = false;
        this.state.empData = null;
        this.state.empBarcode = "";
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.successMessage = "";
        setTimeout(() => this.focusNext(), 50);
    }

    // ─── Child MO Selection ───────────────────────────────────────

    selectChild(child) {
        if (!child.is_active) return;
        this.state.selectedChild = child;
        this.state.selectedOperationId = false;
        // Auto-select operation if only one
        if (child.operations && child.operations.length === 1) {
            this.state.selectedOperationId = child.operations[0].id;
        }
    }

    clearChild() {
        this.state.selectedChild = null;
        this.state.selectedOperationId = false;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
    }

    // ─── Employee Handling ─────────────────────────────────────────

    onEmpBarcodeInput(ev) {
        this.state.empBarcode = ev.target.value;
        this.state.empError = "";
    }

    async onEmpBarcodeKeydown(ev) {
        if (ev.key !== "Enter") return;
        if (!this.state.empBarcode.trim()) return;

        this.state.isProcessing = true;
        this.state.empError = "";

        try {
            const result = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_validate_employee",
                [this.state.empBarcode.trim()]
            );

            if (result.error) {
                this.state.empError = result.error;
            } else {
                this.state.empData = result;
                setTimeout(() => this.focusNext(), 50);
            }
        } catch (e) {
            this.state.empError = "Error de conexión. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    clearEmp() {
        this.state.empData = null;
        this.state.empBarcode = "";
        setTimeout(() => this.focusNext(), 50);
    }

    // ─── Operation Handling ────────────────────────────────────────

    onOperationChange(ev) {
        this.state.selectedOperationId = parseInt(ev.target.value) || false;
    }

    // ─── Quantity Handling ─────────────────────────────────────────

    onQtyInput(ev) {
        this.state.qtyDeclared = ev.target.value;
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.successMessage = "";
    }

    async onQtyBlur() {
        if (!this.state.qtyDeclared || parseFloat(this.state.qtyDeclared) <= 0) return;
        if (!this.state.selectedChild) return;

        try {
            const result = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_check_overproduction",
                [this.state.selectedChild.id, parseFloat(this.state.qtyDeclared)]
            );

            if (result.status === "blocked") {
                this.state.overproductionBlock = result.message;
                this.state.overproductionWarning = "";
            } else if (result.status === "warning") {
                this.state.overproductionWarning = result.message;
                this.state.overproductionBlock = "";
            }
        } catch (e) {
            // Ignore validation errors silently
        }
    }

    // ─── Computed Properties ──────────────────────────────────────

    get progressPct() {
        if (!this.state.selectedChild) return 0;
        const total = this.state.selectedChild.total_qty;
        if (total <= 0) return 0;
        return Math.min(100, Math.round((this.state.selectedChild.done_qty / total) * 100));
    }

    get hasOperations() {
        return this.state.selectedChild &&
               this.state.selectedChild.operations &&
               this.state.selectedChild.operations.length > 0;
    }

    get canSubmit() {
        if (this.state.isProcessing) return false;
        if (!this.state.selectedChild) return false;
        if (!this.state.empData) return false;
        if (!this.state.qtyDeclared || parseFloat(this.state.qtyDeclared) <= 0) return false;
        if (this.state.overproductionBlock) return false;
        if (this.hasOperations && !this.state.selectedOperationId) return false;
        return true;
    }

    // ─── Submit ───────────────────────────────────────────────────

    async submitDeclaration() {
        if (!this.canSubmit) return;

        this.state.isProcessing = true;
        this.state.successMessage = "";

        try {
            const result = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_create_and_process",
                [
                    this.state.selectedChild.id,
                    this.state.empData.employee_id,
                    this.state.selectedOperationId || false,
                    parseFloat(this.state.qtyDeclared),
                ]
            );

            if (result.success) {
                let msg = result.message;
                if (result.backorder_name) {
                    msg += ` — Backorder: ${result.backorder_name}`;
                }
                this.state.successMessage = msg;

                // Auto-reset after 3 seconds
                setTimeout(() => this.resetKiosk(), 3000);
            }
        } catch (e) {
            this.state.moError = e.message || "Error al registrar. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    resetKiosk() {
        this.state.moBarcode = "";
        this.state.moError = "";
        this.state.parentData = null;
        this.state.selectedChild = null;
        this.state.empBarcode = "";
        this.state.empError = "";
        this.state.empData = null;
        this.state.selectedOperationId = false;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.isProcessing = false;
        this.state.successMessage = "";
        setTimeout(() => this.focusNext(), 50);
    }
}

registry.category("actions").add("manufacturing_control.kiosk_action", KioskAction);
