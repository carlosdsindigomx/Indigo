/** @odoo-module **/
// «Documentos relacionados» de un REP: la fila de un renglón ya ligado abre
// directo su pago de Odoo, y la de uno cuya factura está en Odoo sin pago abre
// una ventana corta para crearlo. Los demás abren su ficha como siempre: ahí
// está «Elegir pago». Qué abrir lo decide el servidor
// (dsi.cfdi.payment.line.action_abrir_desde_la_fila).
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { X2ManyField, x2ManyField } from "@web/views/fields/x2many/x2many_field";

export class RenglonesDePagoField extends X2ManyField {
    setup() {
        super.setup();
        this.dsiOrm = useService("orm");
        this.dsiAcciones = useService("action");
    }

    async openRecord(record) {
        if (record.resId) {
            const accion = await this.dsiOrm.call(record.resModel, "action_abrir_desde_la_fila", [
                [record.resId],
            ]);
            if (accion) {
                return this.dsiAcciones.doAction(accion);
            }
        }
        return super.openRecord(record);
    }
}

export const renglonesDePagoField = {
    ...x2ManyField,
    component: RenglonesDePagoField,
};

registry.category("fields").add("dsi_renglones_de_pago", renglonesDePagoField);
