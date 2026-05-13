/** @odoo-module */

import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class KioskAction extends Component {
    static template = "manufacturing_control.KioskAction";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.empInputRef = useRef("empInput");
        this.moInputRef = useRef("moInput");
        this.qtyInputRef = useRef("qtyInput");

        this.state = useState({
            // ─── Screen control ───────────────────────────────
            // "employee" | "config" | "production" | "success"
            screen: "config",

            // ─── Employee session ─────────────────────────────
            empBarcode: "",
            empError: "",
            empData: null,          // { employee_id, employee_name, has_config, workcenter_ids }

            // ─── Kiosk configuration ──────────────────────────
            availableWorkcenters: [],   // [{ id, name }]
            selectedWcIds: new Set(),   // IDs selected in config
            wcSearch: "",               // Search filter for work centers

            // ─── Production order ─────────────────────────────
            moBarcode: "",
            moError: "",
            moData: null,           // { mo_name, mo_product_name, mo_qty }

            // ─── Work orders (grouped by MO family) ─────────
            familyOrders: [],       // [{ mo_id, mo_name, product_name, is_parent, workorders: [...] }]
            selectedWO: null,       // selected work order object

            // ─── Quantity ─────────────────────────────────────
            qtyDeclared: "",
            overproductionWarning: "",
            overproductionBlock: "",

            // ─── General ──────────────────────────────────────
            isProcessing: false,
            successMessage: "",
        });

        onMounted(async () => {
            // Check if session already has work centers configured
            const sessionCheck = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_has_session_config",
                []
            );
            if (sessionCheck.has_config) {
                // Session already configured → go straight to employee login
                this.state.screen = "employee";
            } else {
                // No config → load workcenters and show config screen
                await this._loadWorkcenters();
                this.state.screen = "config";
            }
            this._focusCurrent();
        });
    }

    _focusCurrent() {
        setTimeout(() => {
            if (this.state.screen === "employee") {
                this.empInputRef.el?.focus();
            } else if (this.state.screen === "production") {
                this.moInputRef.el?.focus();
            }
        }, 80);
    }

    // ═══════════════════════════════════════════════════════════════
    //  SCREEN: Employee Login
    // ═══════════════════════════════════════════════════════════════

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
                // Config was already saved before employee login → go to production
                this.state.screen = "production";
                this._focusCurrent();
            }
        } catch (e) {
            this.state.empError = "Error de conexión. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    // ═══════════════════════════════════════════════════════════════
    //  SCREEN: Kiosk Configuration
    // ═══════════════════════════════════════════════════════════════

    async _loadWorkcenters() {
        try {
            const wcs = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_get_available_workcenters",
                []
            );
            this.state.availableWorkcenters = wcs;
            // Pre-select already configured ones
            this.state.selectedWcIds = new Set(
                (this.state.empData?.workcenter_ids || []).map(wc => wc.id)
            );
        } catch (e) {
            this.state.empError = "Error cargando centros de trabajo.";
        }
    }

    toggleWorkcenter(wcId) {
        const s = new Set(this.state.selectedWcIds);
        if (s.has(wcId)) {
            s.delete(wcId);
        } else {
            s.add(wcId);
        }
        this.state.selectedWcIds = s;
    }

    isWcSelected(wcId) {
        return this.state.selectedWcIds.has(wcId);
    }

    onWcSearchInput(ev) {
        this.state.wcSearch = ev.target.value;
    }

    get filteredWorkcenters() {
        const q = this.state.wcSearch.trim().toLowerCase();
        if (!q) return this.state.availableWorkcenters;
        return this.state.availableWorkcenters.filter(
            wc => wc.name.toLowerCase().includes(q)
        );
    }

    get canSaveConfig() {
        return this.state.selectedWcIds.size > 0 && !this.state.isProcessing;
    }

    async saveConfig() {
        if (!this.canSaveConfig) return;

        this.state.isProcessing = true;
        try {
            await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_save_session_config",
                [[...this.state.selectedWcIds]]
            );

            this.state.wcSearch = "";

            // Update local data (only if employee already logged in)
            if (this.state.empData) {
                this.state.empData.has_config = true;
                this.state.empData.workcenter_ids = this.state.availableWorkcenters.filter(
                    wc => this.state.selectedWcIds.has(wc.id)
                );
            }

            // After saving config → go to employee login
            this.state.screen = "employee";
            this._focusCurrent();
        } catch (e) {
            this.state.empError = "Error guardando configuración.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    async showReconfigure() {
        await this._loadWorkcenters();
        this.state.screen = "config";
    }

    // ═══════════════════════════════════════════════════════════════
    //  SCREEN: Production Order + Work Orders
    // ═══════════════════════════════════════════════════════════════

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
                "kiosk_get_workorders",
                [this.state.moBarcode.trim(), this.state.empData.employee_id]
            );

            if (result.error) {
                this.state.moError = result.error;
            } else {
                this.state.moData = {
                    mo_name: result.mo_name,
                    mo_product_name: result.mo_product_name,
                    mo_qty: result.mo_qty,
                };
                this.state.familyOrders = result.family_orders || [];
                this.state.selectedWO = null;
                this.state.qtyDeclared = "";
                this.state.overproductionWarning = "";
                this.state.overproductionBlock = "";

                // Auto-select if only one work order across entire family
                const allWos = this.state.familyOrders.flatMap(fo => fo.workorders);
                if (allWos.length === 1) {
                    this.selectWorkOrder(allWos[0]);
                }
            }
        } catch (e) {
            this.state.moError = "Error de conexión. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    clearMo() {
        this.state.moData = null;
        this.state.moBarcode = "";
        this.state.familyOrders = [];
        this.state.selectedWO = null;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.successMessage = "";
        this._focusCurrent();
    }

    // ─── Work Order Selection ─────────────────────────────────────

    selectWorkOrder(wo) {
        this.state.selectedWO = wo;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        setTimeout(() => this.qtyInputRef.el?.focus(), 80);
    }

    clearWorkOrder() {
        this.state.selectedWO = null;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
    }

    // ─── Quantity ─────────────────────────────────────────────────

    onQtyInput(ev) {
        this.state.qtyDeclared = ev.target.value;
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.successMessage = "";
    }

    async onQtyBlur() {
        if (!this.state.qtyDeclared || parseFloat(this.state.qtyDeclared) <= 0) return;
        if (!this.state.selectedWO) return;

        try {
            const result = await this.orm.call(
                "mrp.shift.declaration",
                "kiosk_check_overproduction",
                [this.state.selectedWO.id, parseFloat(this.state.qtyDeclared)]
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

    get woProgressPct() {
        if (!this.state.selectedWO) return 0;
        const total = this.state.selectedWO.product_qty;
        if (total <= 0) return 0;
        return Math.min(100, Math.round((this.state.selectedWO.total_declared / total) * 100));
    }

    get canSubmit() {
        if (this.state.isProcessing) return false;
        if (!this.state.selectedWO) return false;
        if (!this.state.empData) return false;
        if (!this.state.qtyDeclared || parseFloat(this.state.qtyDeclared) <= 0) return false;
        if (this.state.overproductionBlock) return false;
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
                    this.state.selectedWO.id,
                    this.state.empData.employee_id,
                    parseFloat(this.state.qtyDeclared),
                ]
            );

            if (result.success) {
                this.state.successMessage = result.message;
                this.state.screen = "success";

                // Auto-reset to production screen after 3 seconds
                setTimeout(() => this._resetToProduction(), 3000);
            }
        } catch (e) {
            this.state.moError = e.message || "Error al registrar. Intente de nuevo.";
        } finally {
            this.state.isProcessing = false;
        }
    }

    _resetToProduction() {
        this.state.moBarcode = "";
        this.state.moError = "";
        this.state.moData = null;
        this.state.familyOrders = [];
        this.state.selectedWO = null;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.successMessage = "";
        this.state.screen = "production";
        this._focusCurrent();
    }

    resetKiosk() {
        this.state.empBarcode = "";
        this.state.empError = "";
        this.state.empData = null;
        this.state.availableWorkcenters = [];
        this.state.selectedWcIds = new Set();
        this.state.moBarcode = "";
        this.state.moError = "";
        this.state.moData = null;
        this.state.familyOrders = [];
        this.state.selectedWO = null;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.isProcessing = false;
        this.state.successMessage = "";
        this.state.screen = "config";
        // Reload workcenters for a fresh config screen
        this._loadWorkcenters();
        this._focusCurrent();
    }

    logoutEmployee() {
        this.state.empBarcode = "";
        this.state.empError = "";
        this.state.empData = null;
        this.state.moBarcode = "";
        this.state.moError = "";
        this.state.moData = null;
        this.state.familyOrders = [];
        this.state.selectedWO = null;
        this.state.qtyDeclared = "";
        this.state.overproductionWarning = "";
        this.state.overproductionBlock = "";
        this.state.isProcessing = false;
        this.state.successMessage = "";
        this.state.screen = "employee";
        this._focusCurrent();
    }
}

registry.category("actions").add("manufacturing_control.kiosk_action", KioskAction);
