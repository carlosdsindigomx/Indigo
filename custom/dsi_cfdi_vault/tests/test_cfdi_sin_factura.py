"""«CFDI sin factura» agrupa por la otra parte del CFDI.

Sale agrupado en clientes, proveedores y empleados, y dentro de cada grupo por
nombre y RFC. La otra parte es el receptor en los emitidos y el emisor en los
recibidos: agrupar por emisor juntaba todos los emitidos bajo la propia
empresa.
"""
from datetime import date

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

EMPRESA = ('EKU9003173C9', "ESCUELA KEMPER URGATE")


@tagged('post_install', '-at_install')
class TestCfdiSinFactura(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contador = 0
        cls.empresa = cls.env['res.company'].create({'name': "Prueba CFDI sin factura"})

    def _cfdi(self, direccion, emisor, receptor, tipo='I'):
        type(self).contador += 1
        uuid = f"CCCCCCCC-0000-0000-0000-{self.contador:012d}"
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': b'<cfdi/>', 'mimetype': 'text/xml',
        })
        return self.env['cfdi.document'].create({
            'company_id': self.empresa.id,
            'uuid': uuid,
            'direction': direccion,
            'move_type': tipo,
            'invoice_date': date(2026, 11, 2),
            'amount_total': 1160.0,
            'partner_rfc_issuer': emisor[0],
            'partner_name_issuer': emisor[1],
            'partner_rfc_receiver': receptor[0],
            'partner_name_receiver': receptor[1],
            'xml_attachment_id': adjunto.id,
        })

    def test_la_otra_parte_es_el_receptor_en_los_emitidos_y_el_emisor_en_los_recibidos(self):
        compra = self._cfdi('received', ('PRO010101AA1', "PROVEEDOR UNO"), EMPRESA)
        venta = self._cfdi('issued', EMPRESA, ('CLI010101AA1', "CLIENTE UNO"))
        nomina = self._cfdi('issued', EMPRESA, ('EMPL010101AA1', "JUAN PÉREZ"), tipo='N')
        sin_nombre = self._cfdi('received', ('PRO020202AA2', False), EMPRESA)

        self.assertEqual((compra.counterpart_kind, compra.counterpart_name),
                         ('supplier', "PROVEEDOR UNO (PRO010101AA1)"))
        self.assertEqual((venta.counterpart_kind, venta.counterpart_name),
                         ('customer', "CLIENTE UNO (CLI010101AA1)"))
        self.assertEqual((nomina.counterpart_kind, nomina.counterpart_name),
                         ('employee', "JUAN PÉREZ (EMPL010101AA1)"))
        self.assertEqual(sin_nombre.counterpart_name, "PRO020202AA2", "sin nombre, el RFC")

        compra.partner_name_issuer = "PROVEEDOR UNO SA DE CV"
        self.assertEqual(compra.counterpart_name, "PROVEEDOR UNO SA DE CV (PRO010101AA1)")

    def test_cfdi_sin_factura_sale_agrupado_por_clientes_y_proveedores(self):
        self._cfdi('received', ('PRO010101AA1', "PROVEEDOR UNO"), EMPRESA)
        self._cfdi('received', ('PRO010101AA1', "PROVEEDOR UNO"), EMPRESA)
        self._cfdi('received', ('PRO030303AA3', "PROVEEDOR DOS"), EMPRESA)
        self._cfdi('issued', EMPRESA, ('CLI010101AA1', "CLIENTE UNO"))

        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_no_move')
        contexto = safe_eval(accion.context)
        self.assertEqual(
            {clave for clave in contexto if clave.startswith('search_default_group_')},
            {'search_default_group_counterpart_kind', 'search_default_group_counterpart'})
        busqueda = self.env['cfdi.document'].get_view(accion.search_view_id.id, 'search')['arch']
        for filtro in ('group_counterpart_kind', 'group_counterpart'):
            self.assertIn(f'name="{filtro}"', busqueda, "sin el filtro, el agrupado de entrada no hace nada")

        dominio = safe_eval(accion.domain) + [('company_id', '=', self.empresa.id)]
        grupos = self.env['cfdi.document']._read_group(
            dominio, ['counterpart_kind', 'counterpart_name'], ['__count'])
        self.assertEqual(sorted(grupos), [
            ('customer', "CLIENTE UNO (CLI010101AA1)", 1),
            ('supplier', "PROVEEDOR DOS (PRO030303AA3)", 1),
            ('supplier', "PROVEEDOR UNO (PRO010101AA1)", 2),
        ])
