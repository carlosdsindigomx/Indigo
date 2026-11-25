"""La pantalla principal de la Bóveda (Bóveda CFDI › CFDI) abre en los CFDI
pendientes de conciliar, agrupados por mes del comprobante, del más reciente al
más antiguo. Los dos son filtros de la barra de búsqueda: se pueden quitar.

El orden de los meses sale del default_order de la lista, no del _order del
modelo: sin él, Odoo agrupaba del mes más antiguo al más reciente.
"""
from lxml import etree

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval


@tagged('post_install', '-at_install')
class TestPantallaCfdi(TransactionCase):

    def _arch(self, vista, tipo):
        return etree.fromstring(self.env['cfdi.document'].get_view(vista.id, tipo)['arch'])

    def test_abre_en_pendientes_agrupados_por_mes(self):
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document')
        contexto = safe_eval(accion.context)
        self.assertEqual({clave for clave in contexto if clave.startswith('search_default_')},
                         {'search_default_filter_pending', 'search_default_group_invoice_month'})

        busqueda = self._arch(accion.search_view_id, 'search')
        pendientes, = busqueda.xpath("//filter[@name='filter_pending']")
        self.assertEqual(safe_eval(pendientes.get('domain')), [('match_state', '=', 'pending')])
        por_mes, = busqueda.xpath("//filter[@name='group_invoice_month']")
        self.assertEqual(safe_eval(por_mes.get('context')), {'group_by': 'invoice_date:month'})

    def test_el_mes_mas_reciente_va_arriba(self):
        """En la lista y en el kanban: la pantalla abre en lista, pero Odoo
        recuerda la última vista usada."""
        lista = self._arch(self.env.ref('dsi_cfdi_vault.view_cfdi_document_list'), 'list')
        self.assertEqual(lista.get('default_order'), 'invoice_date desc, id desc')
        kanban = self._arch(self.env.ref('dsi_cfdi_vault.view_cfdi_document_kanban'), 'kanban')
        self.assertEqual(kanban.get('default_order'), 'invoice_date desc, id desc')

    def test_un_filtro_por_estado_de_conciliacion_y_por_tipo(self):
        """Cada estado de conciliación y cada tipo de comprobante tiene su filtro,
        además de Emitidos y Recibidos."""
        Cfdi = self.env['cfdi.document']
        busqueda = self._arch(self.env.ref('dsi_cfdi_vault.view_cfdi_document_search'), 'search')
        dominios = [safe_eval(filtro.get('domain')) for filtro in busqueda.xpath('//filter[@domain]')]
        esperados = (
            [[('direction', '=', direccion)] for direccion in ('issued', 'received')]
            + [[('match_state', '=', estado)] for estado, _ in Cfdi._fields['match_state'].selection]
            + [[('move_type', '=', tipo)] for tipo, _ in Cfdi._fields['move_type'].selection]
        )
        for dominio in esperados:
            self.assertIn(dominio, dominios)

    def test_emitidos_y_recibidos_en_todas_las_pantallas_de_cfdi(self):
        acciones = self.env['ir.actions.act_window'].search([('res_model', '=', 'cfdi.document')])
        self.assertGreater(len(acciones), 5)
        for accion in acciones:
            with self.subTest(accion=accion.xml_id or accion.name):
                vista = accion.search_view_id or self.env['ir.ui.view']
                arch = etree.fromstring(
                    self.env['cfdi.document'].get_view(vista.id or None, 'search')['arch'])
                for nombre in ('filter_issued', 'filter_received', 'filter_pending', 'filter_type_ingreso'):
                    self.assertTrue(arch.xpath(f"//filter[@name='{nombre}']"), nombre)
