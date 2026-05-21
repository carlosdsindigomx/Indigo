/** @odoo-module **/

import { ListController } from "@web/views/list/list_controller";
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { useService } from "@web/core/utils/hooks";

// Extendemos el controlador de lista nativo de Odoo
export class SatXmlListController extends ListController {
    setup() {
        super.setup();
        this.action = useService("action");
    }

    // Función que se ejecuta al darle clic a nuestro botón
    openImportWizard() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Importar XMLs",
            res_model: "sat.xml.import.wizard",
            views: [[false, "form"]],
            target: "new", 
        });
    }
}

export const satXmlListView = {
    ...listView,
    Controller: SatXmlListController,
    buttonTemplate: "sat_xml_auditor.ListView.Buttons", 
};

registry.category("views").add("sat_xml_list", satXmlListView);