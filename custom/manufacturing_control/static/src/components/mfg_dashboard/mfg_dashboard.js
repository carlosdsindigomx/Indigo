/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class ManufacturingDashboard extends Component {
    static template = "manufacturing_control.ManufacturingDashboard";

    setup() {
        this.orm    = useService("orm");
        this.action = useService("action");

        const today = new Date();
        const dow   = today.getDay();
        
        // Calcular el Lunes
        const monday = new Date(today);
        monday.setDate(today.getDate() - (dow === 0 ? 6 : dow - 1));
        
        // Calcular el Domingo (Lunes + 6 días)
        const sunday = new Date(monday);
        sunday.setDate(monday.getDate() + 6);

        this.state = useState({
            loading:      true,
            data:         null,
            maxLoad:      1,
            dateFrom:     this._toDateStr(monday),
            dateTo:       this._toDateStr(sunday),
            activePreset: "week",
        });

        onWillStart(() => this._loadData());
    }

    // ── Helpers ──────────────────────────────────────────────────────

    _toDateStr(d) {
        return d.toISOString().slice(0, 10);
    }

    get _dateDomain() {
        const domain = [];
        if (this.state.dateFrom) {
            domain.push(["create_date", ">=", this.state.dateFrom + " 00:00:00"]);
        }
        if (this.state.dateTo) {
            domain.push(["create_date", "<=", this.state.dateTo + " 23:59:59"]);
        }
        return domain;
    }

    // ── Data ────────────────────────────────────────────────────────

    async _loadData() {
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "mrp.master.order",
                "get_dashboard_data",
                [],
                { date_from: this.state.dateFrom, date_to: this.state.dateTo }
            );
            this.state.data    = data;
            this.state.maxLoad = data.workcenters && data.workcenters.length
                ? Math.max(...data.workcenters.map(w => w.pending), 1)
                : 1;
        } finally {
            this.state.loading = false;
        }
    }

    // ── Filter ───────────────────────────────────────────────────────

    _setPreset(preset) {
        const today = new Date();
        let from, to = new Date(today);

        switch (preset) {
            case "week": {
                const daysBack = today.getDay() === 0 ? 6 : today.getDay() - 1;
                
                // Desde el Lunes
                from = new Date(today);
                from.setDate(today.getDate() - daysBack);
                
                // Hasta el Domingo
                to = new Date(from);
                to.setDate(from.getDate() + 6);
                break;
            }
            case "month":
                from = new Date(today.getFullYear(), today.getMonth(), 1);
                break;
            case "last_month":
                from = new Date(today.getFullYear(), today.getMonth() - 1, 1);
                to   = new Date(today.getFullYear(), today.getMonth(), 0);
                break;
            case "year":
                from = new Date(today.getFullYear(), 0, 1);
                break;
        }

        this.state.dateFrom     = this._toDateStr(from);
        this.state.dateTo       = this._toDateStr(to);
        this.state.activePreset = preset;
        this._loadData();
    }

    // ── Navigation a Gantt por Centro de Trabajo ─────────────────────

    async _openWorkcenterGantt(wcId, wcName) {
        // Redirige al modelo de órdenes de trabajo (mrp.workorder)
        return this.action.doAction({
            type:      "ir.actions.act_window",
            name:      `Planeación - ${wcName}`,
            res_model: "mrp.workorder",
            view_mode: "gantt,list,form",
            views:     [[false, "gantt"], [false, "list"], [false, "form"]],
            // Filtramos por el centro de trabajo y quitamos las canceladas/hechas
            domain: [
                ["workcenter_id", "=", wcId],
                ["state", "not in", ["done", "cancel"]]
            ],
            context: {
                search_default_workcenter_id: wcId,
            }
        });
    }

    onDateFromChange(ev) {
        this.state.dateFrom     = ev.target.value;
        this.state.activePreset = "custom";
        if (this.state.dateFrom && this.state.dateTo) {
            this._loadData();
        }
    }

    onDateToChange(ev) {
        this.state.dateTo       = ev.target.value;
        this.state.activePreset = "custom";
        if (this.state.dateFrom && this.state.dateTo) {
            this._loadData();
        }
    }

    // ── Computed ─────────────────────────────────────────────────────

    get totalOrders() {
        const d = this.state.data;
        if (!d) return 0;
        return (d.abiertas || 0) + (d.terminadas || 0) + (d.canceladas || 0);
    }

    get pctEnProceso()   { return this._pct(this.state.data?.en_proceso  || 0); }
    get pctConfirmadas() { return this._pct(this.state.data?.confirmadas || 0); }
    get pctPorCerrar()   { return this._pct(this.state.data?.por_cerrar  || 0); }
    get pctBorrador()    { return this._pct(this.state.data?.borrador    || 0); }
    get pctTerminadas()  { return this._pct(this.state.data?.terminadas  || 0); }

    _pct(val) {
        const total = this.totalOrders;
        if (!total) return "0";
        return ((val / total) * 100).toFixed(1);
    }

    // ── Workcenter helpers ────────────────────────────────────────────

    wcBarWidth(pending) {
        return ((pending / this.state.maxLoad) * 100).toFixed(0);
    }

    wcBadgeClass(pending) {
        if (pending >= 20) return "badge-danger"; // Más de 20 horas
        if (pending >= 8)  return "badge-warning"; // Más de un turno (8h)
        return "badge-ok";
    }

    wcBarClass(pending) {
        if (pending >= 10) return "bar-danger";
        if (pending >= 5)  return "bar-warning";
        return "bar-ok";
    }

    // ── Navigation ───────────────────────────────────────────────────

    async _openViewByProduct(productId, stateFilter) {
        // El filtro de fecha solo aplica a terminadas; estados abiertos se muestran siempre
        const isOpenState = !stateFilter || stateFilter === "open" || stateFilter !== "done";
        const datePart = isOpenState ? [] : [...this._dateDomain];
        const domain = [...datePart, ["product_id", "=", productId]];
        const stateLabels = {
            progress:  "En Proceso",
            confirmed: "Confirmadas",
            to_close:  "Por Cerrar",
            draft:     "Borradores",
            done:      "Terminadas",
        };
        if (stateFilter && stateFilter !== "open") {
            domain.push(["state", "=", stateFilter]);
        } else if (stateFilter === "open") {
            domain.push(["state", "not in", ["done", "cancel"]]);
        }
        const name = stateFilter ? stateLabels[stateFilter] || "Órdenes por Producto" : "Órdenes por Producto";
        return this.action.doAction({
            type:      "ir.actions.act_window",
            name,
            res_model: "mrp.master.order",
            view_mode: "kanban,list,pivot,form",
            views:     [[false, "kanban"], [false, "list"], [false, "pivot"], [false, "form"]],
            domain,
            context: { search_default_group_by_state: 1 },
        });
    }

    async _openView(filter) {
        // El filtro de fecha solo aplica a órdenes terminadas; las abiertas se muestran siempre
        const isOpenState = ["open", "progress", "confirmed", "to_close", "draft", "atrasadas"].includes(filter);
        let domain   = isOpenState ? [] : [...this._dateDomain];
        let resModel = "mrp.master.order";
        let name     = "Consola Maestra";
        let views    = [[false, "kanban"], [false, "list"], [false, "pivot"], [false, "form"]];

        switch (filter) {
            case "open":
                domain.push(["state", "not in", ["done", "cancel"]]);
                name = "Órdenes Abiertas";
                break;
            case "progress":
                domain.push(["state", "=", "progress"]);
                name = "En Proceso";
                break;
            case "confirmed":
                domain.push(["state", "=", "confirmed"]);
                name = "Confirmadas";
                break;
            case "to_close":
                domain.push(["state", "=", "to_close"]);
                name = "Por Cerrar";
                break;
            case "done":
                domain.push(["state", "=", "done"]);
                name = "Terminadas";
                break;
            case "draft":
                domain.push(["state", "=", "draft"]);
                name = "Borradores";
                break;
            case "atrasadas":
                const nowUtc = new Date().toISOString().replace('T', ' ').substring(0, 19);
                domain.push(
                    ["state", "not in", ["done", "cancel", "draft"]],
                    ["date_deadline", "<", nowUtc],
                    ["date_deadline", "!=", false]
                );
                name = "Órdenes Atrasadas";
                break;
            case "bloqueadas":
                resModel = "mrp.production";
                name     = "Órdenes Bloqueadas — Sin Material";
                views    = [[false, "list"], [false, "form"]];
                domain   = [
                    ["master_order_id", "!=", false],
                    ["state", "not in", ["done", "cancel"]],
                    ["components_availability_state", "in", ["late", "unavailable"]],
                ];
                break;
            default:
                break;
        }

        return this.action.doAction({
            type:      "ir.actions.act_window",
            name,
            res_model: resModel,
            view_mode: views.map(v => v[1]).join(","),
            views,
            domain,
        });
    }
}

registry.category("actions").add("manufacturing_control.dashboard", ManufacturingDashboard);
