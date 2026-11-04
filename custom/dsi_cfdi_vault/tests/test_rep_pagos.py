"""Los pagos (REP) recibidos ligados a su pago de Odoo.

Existe porque el REP solo se ligaba a la factura que paga: en el pago de Odoo
no se veía, y con varios pagos en una factura nadie decía cuál era el del REP.
Ahora cada documento relacionado de un REP recibido se liga al pago que aplicó
ese importe a su factura (un account.payment o el movimiento del banco
conciliado directo), con una copia del XML en el pago, y lo ambiguo queda «Por
elegir» para el contador.

Lo que más cuidan estas pruebas:

  * la regla: el importe a cinco centavos (en la moneda de la factura o en la
    del pago), la ventana de días, y los desempates por el «Asiento del pago»
    puesto a mano, el saldo anterior y la fecha; si no, «Por elegir»;
  * dos REP nunca se quedan con el mismo pago, y uno cancelado lo suelta para
    su sustituto en el momento;
  * pagos agrupados, pagos que Odoo dividió en uno por factura, parcialidades,
    notas de crédito (no son pagos, pero cuentan en el saldo), facturas en
    dólares pagadas en pesos, el movimiento del banco sin account.payment y el
    pago sin asiento del módulo accountant;
  * cuando Odoo reparte el pago distinto que el REP (una nota de crédito en
    un pago agrupado, un descuento como quebranto, un pago sin asiento de
    varias facturas con un REP por factura), las reglas de respaldo ligan, y
    no ligan nada ambiguo;
  * una liga a mano que se pierde lo dice en el REP, con el nombre del pago
    aunque se haya borrado;
  * la liga se mantiene al día sola al pagar, desconciliar o volver a
    publicar (precommit), al actualizar y en la revisión diaria; un error al
    religar nunca impide registrar un pago;
  * la copia del XML: una por REP y pago, sin mensajes, sin importarla como
    factura, reutilizada y nunca borrada;
  * el contrato de account.move._dsi_pagos_aplicados, que también usa la
    discrepancia de compras PPD;
  * el asistente «Elegir pago», sus permisos y la licencia, y lo que ve quien
    solo factura.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_rep_facturas.py (tests/common_pagos.py).
"""
from datetime import date, datetime
from unittest.mock import patch

import psycopg2
from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, RedirectWarning, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import SQL
from odoo.tools.safe_eval import safe_eval

from odoo.addons.dsi_cfdi_vault.models.rep_pagos import PARAMETRO_VENTANA, _estado_de_pago_conciliado

from .common_pagos import (
    compra, copias_en, empresa_mx, guardar, pagar, pagar_con_extracto, pagar_sin_asiento, rep, sat_dice,
    tipo_de_cambio, venta,
)
from .test_rep_facturas import docto, rep_xml

# Las claves que trae cada pago aplicado (el contrato de _dsi_pagos_aplicados).
CLAVES_DEL_CONTRATO = {
    'clave', 'pago', 'asiento', 'extracto', 'diario', 'fecha', 'fecha_banco', 'importe', 'moneda',
    'importe_pago', 'moneda_pago', 'monto', 'moneda_monto', 'parciales', 'sin_asiento', 'saldo_anterior',
    'lineas_rep', 'tiene_rep',
}
FECHA_VIEJA = datetime(2000, 1, 1)


@tagged('post_install', '-at_install')
class TestRepPagos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        empresa_mx(cls, 'boveda-rep-pagos')
        cls.Linea = cls.env['dsi.cfdi.payment.line']
        cls.Doc = cls.env['cfdi.document']
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return "ABABABAB-1111-4000-8000-%012d" % self.contador

    def _poner_parametro(self, clave, valor):
        cp = self.env['ir.config_parameter'].sudo()
        (cp.set_str if hasattr(cp, 'set_str') else cp.set_param)(clave, valor)

    def _envejecer(self, registros):
        """Pone una fecha de modificación vieja: si algo los vuelve a escribir,
        cambia (dentro de la prueba, todo lo escrito lleva la misma hora)."""
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE %s SET write_date = %s WHERE id IN %s",
            SQL.identifier(registros._table), FECHA_VIEJA, tuple(registros.ids)))
        registros.invalidate_recordset(['write_date'])

    def _sin_escribir(self, registros):
        registros.invalidate_recordset(['write_date'])
        return all(registro.write_date == FECHA_VIEJA for registro in registros)

    def _revisiones_programadas(self):
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        return self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])

    def _nota_de_credito(self, importe, fecha, factura):
        nota = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.proveedor.id,
            'invoice_date': fecha,
            'invoice_line_ids': [Command.create({
                'name': "Descuento", 'quantity': 1, 'price_unit': importe, 'tax_ids': [Command.clear()],
            })],
        })
        nota.action_post()
        (factura.line_ids | nota.line_ids).filtered(lambda l: l.account_type == 'liability_payable').reconcile()
        return nota

    def _nota_suelta(self, importe, fecha):
        """Una nota de crédito publicada y sin conciliar: «Registrar pago» la
        paga junto con las facturas y el pago sale por la diferencia."""
        nota = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.proveedor.id,
            'invoice_date': fecha,
            'invoice_line_ids': [Command.create({
                'name': "Descuento", 'quantity': 1, 'price_unit': importe, 'tax_ids': [Command.clear()],
            })],
        })
        nota.action_post()
        return nota

    def _tiene_rep(self, factura, pago):
        """Si el pago `pago` aplicado a la factura ya tiene su REP: lo que lee
        la discrepancia de compras PPD pagadas sin REP."""
        return next(e['tiene_rep'] for e in factura._dsi_pagos_aplicados()[factura] if e['pago'] == pago)

    def _elegir_a_mano(self, linea, pago):
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].browse(linea.action_elegir_pago()['res_id'])
        asistente.option_ids.filtered(lambda o: o.account_payment_id == pago).action_elegir()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('manual', pago))

    def _mensajes_de_liga_perdida(self, rep_doc):
        return rep_doc.message_ids.filtered(lambda m: "Se quitó la liga a mano" in (m.body or ''))

    # -- La liga ----------------------------------------------------------

    def test_un_rep_se_liga_al_pago_que_paga_su_factura(self):
        folio, factura = compra(self)
        pago = pagar(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids

        self.assertEqual(linea.payment_link_state, 'linked')
        self.assertEqual(linea.account_payment_id, pago)
        self.assertEqual(linea.payment_move_id, pago.move_id)
        self.assertEqual(linea.payment_amount_applied, 1000.0)
        self.assertFalse(linea.payment_link_note)
        copia = linea.payment_attachment_id
        self.assertEqual((copia.res_model, copia.res_id, copia.name),
                         ('account.payment', pago.id, f"REP-{rep_doc.uuid}.xml"))
        self.assertEqual(copias_en(self, pago, rep_doc), copia)
        self.assertEqual(len(copias_en(self, factura, rep_doc)), 1, "la copia en la factura sigue")
        self.assertEqual(pago.dsi_rep_count, 1)
        self.assertEqual(pago.move_id.dsi_rep_count, 1)
        self.assertEqual(self.Linea.search(pago.move_id.action_view_rep_lines()['domain']), linea)

    def test_el_xml_queda_en_el_pago_sin_mensajes_ni_importacion(self):
        """La copia no deja mensaje, no se vuelve el adjunto principal y no
        crea documentos CFDI de Odoo; ligar otra vez no escribe nada ni crea
        otra copia."""
        folio, factura = compra(self)
        pago = pagar(self, factura)
        mensajes = (len(pago.message_ids), len(factura.message_ids))
        documentos = (pago.move_id.l10n_mx_edi_document_ids, factura.l10n_mx_edi_document_ids)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'linked')
        self.env.cr.flush()

        self.assertEqual((len(pago.message_ids), len(factura.message_ids)), mensajes)
        self.assertFalse(pago.message_main_attachment_id)
        self.assertEqual(pago.move_id.l10n_mx_edi_document_ids, documentos[0])
        # La compra gana su «Pago recibido» (es de test_rep_facturas), no otro.
        self.assertEqual(len(factura.l10n_mx_edi_document_ids - documentos[1]), 1)

        Adjunto = self.env['ir.attachment']
        adjuntos = Adjunto.search_count([])
        self._envejecer(linea)
        rep_doc._ligar_reps()
        self.Inconsistencia._ligar_reps_con_pagos(linea, 'rep')
        self.env.cr.flush()
        self.assertTrue(self._sin_escribir(linea))
        self.assertEqual(Adjunto.search_count([]), adjuntos)

    def test_el_pago_registrado_despues_del_rep_se_liga_al_confirmar(self):
        folio, factura = compra(self)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'no_payment')
        self.assertEqual(linea.payment_link_note, "La factura no tiene pagos en Odoo.")

        pago = pagar(self, factura)
        self.assertEqual(linea.payment_link_state, 'linked')
        self.assertEqual(linea.account_payment_id, pago)
        self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)

    def test_un_nodo_de_dos_facturas_y_un_pago_agrupado(self):
        folio1, compra1 = compra(self, 1000.0)
        folio2, compra2 = compra(self, 500.0)
        pago = pagar(self, compra1 | compra2, agrupado=True)
        self.assertEqual(len(pago), 1)
        rep_doc = rep(self, (1500.0, 'MXN', '1', [docto(folio1, 1000.0), docto(folio2, 500.0)]))
        lineas = rep_doc.dsi_payment_line_ids

        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertEqual(sorted(lineas.mapped('payment_amount_applied')), [500.0, 1000.0])
        copia = copias_en(self, pago, rep_doc)
        self.assertEqual(len(copia), 1)
        self.assertEqual(lineas.payment_attachment_id, copia)
        self.assertEqual(len(copias_en(self, compra1, rep_doc)), 1)
        self.assertEqual(len(copias_en(self, compra2, rep_doc)), 1)

    def test_un_pago_agrupado_y_un_rep_por_factura(self):
        folio1, compra1 = compra(self, 1000.0)
        folio2, compra2 = compra(self, 500.0)
        pago = pagar(self, compra1 | compra2, agrupado=True)
        rep1 = rep(self, (1000.0, 'MXN', '1', [docto(folio1, 1000.0)]))
        rep2 = rep(self, (500.0, 'MXN', '1', [docto(folio2, 500.0)]))

        lineas = (rep1 | rep2).dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertEqual(len(copias_en(self, pago, rep1)), 1)
        self.assertEqual(len(copias_en(self, pago, rep2)), 1)
        self.assertEqual(pago.dsi_rep_count, 2)

    def test_pago_dividido_en_odoo_y_un_nodo(self):
        """«Registrar pago» de dos facturas sin agrupar: un pago por factura, y
        el REP con un solo nodo por la suma. Cada documento con el suyo."""
        folio1, compra1 = compra(self, 1000.0)
        folio2, compra2 = compra(self, 500.0)
        pagos = pagar(self, compra1 | compra2, agrupado=False)
        self.assertEqual(len(pagos), 2)
        rep_doc = rep(self, (1500.0, 'MXN', '1', [docto(folio1, 1000.0), docto(folio2, 500.0)]))

        for linea in rep_doc.dsi_payment_line_ids:
            self.assertEqual(linea.payment_link_state, 'linked')
            self.assertEqual(linea.account_payment_id, pagos.filtered(lambda p, f=linea.invoice_id: f in p.invoice_ids))
        self.assertEqual(len(rep_doc.dsi_payment_line_ids.account_payment_id), 2)

    def test_parcialidades_se_desempatan_por_fecha(self):
        """Dos pagos de 500 en octubre y dos REP con el mismo saldo anterior: lo
        que los separa es la fecha."""
        folio, factura = compra(self)
        primero = pagar(self, factura, importe=500.0, fecha='2026-10-01')
        segundo = pagar(self, factura, importe=500.0, fecha='2026-10-20')
        rep1 = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0, parcialidad='1')], {'fecha': '2026-10-01'}))
        rep2 = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0, parcialidad='2')], {'fecha': '2026-10-20'}))

        self.assertEqual(rep1.dsi_payment_line_ids.account_payment_id, primero)
        self.assertEqual(rep2.dsi_payment_line_ids.account_payment_id, segundo)
        self.assertEqual(set((rep1 | rep2).dsi_payment_line_ids.mapped('payment_link_state')), {'linked'})

    def test_parcialidades_del_mismo_dia_por_saldo_anterior(self):
        folio, factura = compra(self)
        primero = pagar(self, factura, importe=500.0)
        segundo = pagar(self, factura, importe=500.0)
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['saldo_anterior'] for e in unidades], [1000.0, 500.0])

        rep2 = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 500.0, parcialidad='2')]))
        rep1 = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0, parcialidad='1')]))
        self.assertEqual(rep1.dsi_payment_line_ids.account_payment_id, primero)
        self.assertEqual(rep2.dsi_payment_line_ids.account_payment_id, segundo)

    def test_pagos_del_mismo_dia_en_el_orden_de_sus_ids(self):
        """Los pagos aplicados del mismo día van en el orden de sus ids como
        números, también en «Elegir pago». Como texto, 'pago:1000' iba antes
        que 'pago:999' y la prueba de arriba dependía de los ids."""
        # Los dos pagos quedan con ids de distinto número de dígitos (99 y
        # 100, 999 y 1000…).
        self.env.cr.execute("SELECT nextval('account_payment_id_seq')")
        siguiente = self.env.cr.fetchone()[0] + 1
        self.env.cr.execute("SELECT setval('account_payment_id_seq', %s)", [10 ** len(str(siguiente)) - 2])
        folio, factura = compra(self)
        primero = pagar(self, factura, importe=500.0)
        segundo = pagar(self, factura, importe=500.0)
        self.assertEqual(len(str(segundo.id)), len(str(primero.id)) + 1)

        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['pago'] for e in unidades], [primero, segundo])
        self.assertEqual([e['saldo_anterior'] for e in unidades], [1000.0, 500.0])
        # Los dos cuadran y están a 0 días: el empate queda en ese orden.
        linea = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 500.0)])).dsi_payment_line_ids
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].browse(linea.action_elegir_pago()['res_id'])
        self.assertEqual([(o.account_payment_id, o.matches, o.days) for o in asistente.option_ids],
                         [(primero, True, 0), (segundo, True, 0)])

    def test_sin_desempate_queda_por_elegir_y_el_contador_elige(self):
        """Dos pagos iguales el mismo día y dos REP que dicen lo mismo: no hay
        cómo decidir y los dos quedan «Por elegir». El contador elige con
        «Elegir pago» (el asistente escribe con sudo: él solo lee las líneas) y
        el otro toma el pago que queda. «Volver a automático» lo deja ligado
        por la regla."""
        folio, factura = compra(self)
        pago1 = pagar(self, factura, importe=500.0)
        pago2 = pagar(self, factura, importe=500.0)
        rep_a = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0, parcialidad='1')]), ligar=False)
        rep_b = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0, parcialidad='2')]), ligar=False)
        (rep_a | rep_b)._ligar_reps()
        linea_a, linea_b = rep_a.dsi_payment_line_ids, rep_b.dsi_payment_line_ids
        self.assertEqual((linea_a | linea_b).mapped('payment_link_state'), ['to_choose', 'to_choose'])
        self.assertIn("también lo pide", linea_b.payment_link_note)
        self.assertIn(pago1.display_name, linea_b.payment_link_note)

        accion = linea_b.with_user(self.usuario).action_elegir_pago()
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].with_user(self.usuario).browse(accion['res_id'])
        self.assertEqual(len(asistente.option_ids), 2)
        self.assertTrue(all(asistente.option_ids.mapped('matches')))
        self.assertEqual(asistente.option_ids.account_payment_id, pago1 | pago2)
        asistente.option_ids.filtered(lambda o: o.account_payment_id == pago2).action_elegir()

        self.assertEqual((linea_b.payment_link_state, linea_b.account_payment_id), ('manual', pago2))
        self.assertEqual((linea_a.payment_link_state, linea_a.account_payment_id), ('linked', pago1))
        self.assertTrue(rep_b.message_ids.filtered(lambda m: "ligado a mano" in (m.body or '')))
        self.assertEqual(len(copias_en(self, pago2, rep_b)), 1)
        with self.assertRaises(AccessError):
            linea_a.with_user(self.usuario).write({'payment_link_state': 'manual'})

        linea_b.with_user(self.usuario).action_liga_automatica()
        self.assertEqual((linea_b.payment_link_state, linea_b.account_payment_id), ('linked', pago2))
        self.assertEqual((linea_a.payment_link_state, linea_a.account_payment_id), ('linked', pago1))
        self.assertTrue(rep_b.message_ids.filtered(lambda m: "liga automática" in (m.body or '')))

    def test_el_asistente_valida_en_el_servidor(self):
        """Un pago que dejó de estar aplicado entre abrir y elegir no se liga, y
        un REP emitido no abre el asistente."""
        folio, factura = compra(self)
        pago = pagar(self, factura, fecha='2026-08-01')
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'to_choose')
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].with_user(self.usuario).browse(
            linea.with_user(self.usuario).action_elegir_pago()['res_id'])
        opcion = asistente.option_ids
        self.assertEqual((opcion.account_payment_id, opcion.matches, opcion.days), (pago, True, 70))
        pago.action_draft()
        with self.assertRaises(UserError):
            opcion.action_elegir()
        self.env.cr.flush()
        self.assertEqual(linea.payment_link_state, 'no_payment')

        folio_venta, factura_venta = venta(self)
        emitido = rep(self, (1000.0, 'MXN', '1', [docto(folio_venta, 1000.0)]), direction='issued')
        with self.assertRaises(UserError):
            emitido.dsi_payment_line_ids.with_user(self.usuario).action_elegir_pago()

    def test_tolerancia_de_cinco_centavos(self):
        folio, factura = compra(self, 2000.0)
        pagar(self, factura, importe=1000.05)
        cuadra = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0, 2000.0)])).dsi_payment_line_ids
        self.assertEqual(cuadra.payment_link_state, 'linked')
        self.assertEqual(cuadra.payment_amount_applied, 1000.05)

        folio, factura = compra(self, 2000.0)
        pagar(self, factura, importe=1000.06)
        no_cuadra = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0, 2000.0)])).dsi_payment_line_ids
        self.assertEqual(no_cuadra.payment_link_state, 'amount_mismatch')
        self.assertFalse(no_cuadra.account_payment_id)
        self.assertIn("1,000.00", no_cuadra.payment_link_note)
        self.assertIn("1,000.06", no_cuadra.payment_link_note)

    def test_fecha_fuera_de_la_ventana_y_la_pista(self):
        """El pago cuadra pero es de 39 días antes: «Por elegir». Lo liga el
        «Asiento del pago» puesto a mano en el REP (que no se escribe solo), o
        una ventana más grande en el parámetro."""
        folio, factura = compra(self)
        pago = pagar(self, factura, fecha='2026-09-01')
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)], {'fecha': '2026-10-10'}))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'to_choose')
        self.assertIn("39 días", linea.payment_link_note)

        rep_doc.move_id = pago.move_id
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        rep_doc.move_id = False
        self.assertEqual(linea.payment_link_state, 'to_choose')

        self._poner_parametro(PARAMETRO_VENTANA, 'diez')
        with self.assertLogs('odoo.addons.dsi_cfdi_vault.models.rep_pagos', level='INFO') as log:
            self.assertEqual(self.Linea._ventana_de_dias(), 10)
        self.assertIn("diez", log.output[0])
        self._poner_parametro(PARAMETRO_VENTANA, '60')
        rep_doc._ligar_reps()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertFalse(rep_doc.move_id, "la Bóveda no pone el asiento del pago")

    def test_factura_pagada_con_el_extracto(self):
        """Sin account.payment: la liga va al asiento del movimiento del banco,
        con la copia y el botón «REP» en él, y sin documentos CFDI de Odoo."""
        folio, factura = compra(self)
        movimiento = pagar_con_extracto(self, factura, 1000.0, '2026-10-10')
        asiento = movimiento.move_id
        documentos = asiento.l10n_mx_edi_document_ids
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids

        self.assertEqual(linea.payment_link_state, 'linked')
        self.assertFalse(linea.account_payment_id)
        self.assertEqual(linea.payment_move_id, asiento)
        copia = linea.payment_attachment_id
        self.assertEqual((copia.res_model, copia.res_id), ('account.move', asiento.id))
        self.assertEqual(asiento.dsi_rep_count, 1)
        self.assertEqual(self.Linea.search(asiento.action_view_rep_lines()['domain']), linea)
        self.env.cr.flush()
        self.assertEqual(asiento.l10n_mx_edi_document_ids, documentos)
        self.assertFalse(asiento.l10n_mx_edi_cfdi_uuid)

    def test_factura_en_dolares_pagada_en_pesos(self):
        """Compra de 100 USD a 17 y pago de 1,800 MXN a 18: la diferencia
        cambiaria no es un pago. El REP cuadra en dólares (regla de la
        factura) o, con el tipo de cambio del proveedor, en pesos (regla del
        pago)."""
        tipo_de_cambio(self, '2026-06-15', 17.0)
        tipo_de_cambio(self, '2026-10-10', 18.0)
        folio, factura = compra(self, 100.0, moneda=self.usd)
        pago = pagar(self, factura, importe=1800.0, moneda=self.mxn)
        self.assertEqual(factura.payment_state, 'paid')
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual(len(unidades), 1)
        e = unidades[0]
        self.assertEqual((e['clave'], e['importe'], e['moneda'], e['importe_pago'], e['moneda_pago']),
                         (f'pago:{pago.id}', 100.0, self.usd, 1800.0, self.mxn))
        self.assertEqual((e['monto'], e['moneda_monto'], e['saldo_anterior']), (1800.0, self.mxn, 100.0))

        rep_doc = rep(self, (1800.0, 'MXN', '1', [docto(folio, 100.0, moneda='USD', equivalencia='0.0555555556')]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertEqual(linea._regla_del_importe(e), 'factura')
        self.assertEqual(linea.payment_amount_applied, 100.0)

        # El proveedor usó otro tipo de cambio: en dólares no cuadra, en pesos sí.
        folio, factura = compra(self, 100.0, moneda=self.usd)
        pago = pagar(self, factura, importe=1800.0, moneda=self.mxn)
        rep_doc = rep(self, (1800.0, 'MXN', '1', [docto(folio, 99.72, moneda='USD', equivalencia='0.0554016620')]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertEqual(linea._regla_del_importe(factura._dsi_pagos_aplicados()[factura][0]), 'pago')

    def test_notas_de_credito_no_son_pagos_pero_cuentan_en_el_saldo(self):
        folio, factura = compra(self)
        self._nota_de_credito(200.0, date(2026, 9, 30), factura)
        pago = pagar(self, factura)
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([(e['clave'], e['importe'], e['saldo_anterior']) for e in unidades],
                         [(f'pago:{pago.id}', 800.0, 800.0)])

        rep_doc = rep(self, (800.0, 'MXN', '1', [docto(folio, 800.0, 800.0)]))
        self.assertEqual(rep_doc.dsi_payment_line_ids.account_payment_id, pago)
        self.assertEqual(rep_doc.dsi_payment_line_ids.payment_link_state, 'linked')

    def test_contrato_de_pagos_aplicados(self):
        """Lo que la discrepancia de compras PPD lee de la ayuda: las claves,
        el orden, la fecha, el saldo y qué pagos ya tienen su REP vigente."""
        folio, factura = compra(self)
        self._nota_de_credito(100.0, date(2026, 9, 1), factura)
        pago = pagar(self, factura, importe=400.0, fecha='2026-10-05')
        movimiento = pagar_con_extracto(self, factura, 500.0, '2026-10-12')
        borrador = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.proveedor.id, 'invoice_date': date(2026, 6, 15),
            'invoice_line_ids': [Command.create({'name': "x", 'quantity': 1, 'price_unit': 10.0})],
        })
        asiento = self.env['account.move'].create({'move_type': 'entry', 'line_ids': []})

        self._envejecer(factura)
        resultado = (factura | borrador | asiento)._dsi_pagos_aplicados()
        self.assertEqual(set(resultado), {factura, borrador, asiento})
        self.assertEqual((resultado[borrador], resultado[asiento.sudo()]), ([], []))
        unidades = resultado[factura]
        self.assertEqual([e['clave'] for e in unidades], [f'pago:{pago.id}', f'asiento:{movimiento.move_id.id}'])
        for e in unidades:
            self.assertEqual(set(e), CLAVES_DEL_CONTRATO)
            self.assertFalse(e['tiene_rep'])
            self.assertFalse(e['lineas_rep'])
        del_pago, del_banco = unidades
        self.assertEqual(
            (del_pago['pago'], del_pago['asiento'], del_pago['extracto'], del_pago['diario'], del_pago['fecha'],
             del_pago['fecha_banco'], del_pago['importe'], del_pago['moneda'], del_pago['importe_pago'],
             del_pago['moneda_pago'], del_pago['monto'], del_pago['moneda_monto'], del_pago['sin_asiento'],
             del_pago['saldo_anterior'], len(del_pago['parciales'])),
            (pago, pago.move_id, self.env['account.bank.statement.line'], self.banco, date(2026, 10, 5),
             None, 400.0, self.mxn, 400.0, self.mxn, 400.0, self.mxn, False, 900.0, 1))
        self.assertEqual(
            (del_banco['pago'], del_banco['asiento'], del_banco['extracto'], del_banco['fecha'],
             del_banco['fecha_banco'], del_banco['importe'], del_banco['monto'], del_banco['saldo_anterior']),
            (self.env['account.payment'], movimiento.move_id, movimiento, date(2026, 10, 12),
             date(2026, 10, 12), 500.0, 500.0, 500.0))
        self.assertTrue(self._sin_escribir(factura), "la ayuda no escribe")
        otra_vez = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([(e['clave'], e['importe'], e['saldo_anterior']) for e in otra_vez],
                         [(e['clave'], e['importe'], e['saldo_anterior']) for e in unidades])
        self.assertNotIn('tiene_rep', factura._dsi_pagos_aplicados(con_reps=False)[factura][0])

        rep_doc = rep(
            self,
            (400.0, 'MXN', '1', [docto(folio, 400.0, 900.0, parcialidad='1')], {'fecha': '2026-10-05'}),
            (500.0, 'MXN', '1', [docto(folio, 500.0, 500.0, parcialidad='2')], {'fecha': '2026-10-12'}))
        lineas = rep_doc.dsi_payment_line_ids.sorted('pago_idx')
        self.assertEqual(lineas.mapped('payment_link_state'), ['linked', 'linked'])
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['tiene_rep'] for e in unidades], [True, True])
        self.assertEqual([e['lineas_rep'] for e in unidades], list(lineas))

        # Un REP cancelado no cuenta, aunque su liga no se haya revisado aún.
        sat_dice(self, rep_doc.with_context(dsi_deteccion_diferida=True), 'cancelled')
        self.assertEqual(lineas.mapped('payment_link_state'), ['linked', 'linked'])
        self.assertEqual([e['tiene_rep'] for e in factura._dsi_pagos_aplicados()[factura]], [False, False])

    def test_desconciliar_quita_la_liga_y_la_copia_se_queda(self):
        """En 19 un pago en borrador conserva su conciliación, pero ya no está
        aplicado: la liga se va al confirmar y la copia se queda en el pago.
        Al volver a publicarlo se liga otra vez con la misma copia."""
        folio, factura = compra(self)
        pago = pagar(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        copia = linea.payment_attachment_id
        self.assertTrue(copia)

        pago.action_draft()
        self.env.cr.flush()
        self.assertEqual(linea.payment_link_state, 'no_payment')
        self.assertFalse(linea.account_payment_id)
        self.assertFalse(linea.payment_attachment_id)
        self.assertEqual(copias_en(self, pago, rep_doc), copia, "la copia nunca se borra")
        self.assertEqual(pago.dsi_rep_count, 0)

        pago.action_post()
        if factura.payment_state not in ('paid', 'in_payment'):
            (pago.move_id.line_ids | factura.line_ids).filtered(
                lambda l: l.account_type == 'liability_payable' and not l.reconciled).reconcile()
        self.env.cr.flush()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertEqual(linea.payment_attachment_id, copia)
        self.assertEqual(copias_en(self, pago, rep_doc), copia)

    def test_rep_cancelado_libera_el_pago_para_su_sustituto(self):
        folio, factura = compra(self)
        pago = pagar(self, factura)
        original = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        sustituto = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea_o, linea_s = original.dsi_payment_line_ids, sustituto.dsi_payment_line_ids
        self.assertEqual((linea_o.payment_link_state, linea_o.account_payment_id), ('linked', pago))
        self.assertEqual(linea_s.payment_link_state, 'to_choose')
        self.assertIn(original.display_name, linea_s.payment_link_note)

        sat_dice(self, original, 'cancelled')
        self.assertEqual(linea_o.payment_link_state, 'not_applicable')
        self.assertFalse(linea_o.account_payment_id)
        self.assertEqual((linea_s.payment_link_state, linea_s.account_payment_id), ('linked', pago))
        self.assertEqual(pago.dsi_rep_count, 1)

    def test_rep_emitido_y_compensacion_no_aplican(self):
        folio_venta, factura_venta = venta(self)
        cobro = pagar(self, factura_venta)
        emitido = rep(self, (1000.0, 'MXN', '1', [docto(folio_venta, 1000.0)]), direction='issued')
        linea = emitido.dsi_payment_line_ids
        self.assertEqual(linea.invoice_id, factura_venta)
        self.assertEqual(linea.payment_link_state, 'not_applicable')
        self.assertIn("REP emitido", linea.payment_link_note)
        self.assertFalse(copias_en(self, cobro, emitido))

        folio, factura = compra(self)
        compensacion = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)], {'forma': '17'}))
        linea = compensacion.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'not_applicable')
        self.assertIn("17", linea.payment_link_note)
        self.assertFalse((linea | emitido.dsi_payment_line_ids).payment_attachment_id)

    def test_pago_sin_asiento_y_luego_el_banco(self):
        """Con el módulo accountant el pago no tiene asiento hasta conciliar el
        banco: se liga al pago, y al conciliarlo gana el asiento del banco sin
        cambiar de clave ni de copia."""
        folio, factura = compra(self)
        pago = pagar_sin_asiento(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertFalse(linea.payment_move_id)
        copia = linea.payment_attachment_id
        self.assertEqual((copia.res_model, copia.res_id), ('account.payment', pago.id))

        movimiento = pagar_con_extracto(self, factura, 1000.0, '2026-10-11')
        self.assertEqual(pago.state, _estado_de_pago_conciliado(self.env))
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['clave'] for e in unidades], [f'pago:{pago.id}'])
        self.assertEqual((unidades[0]['asiento'], unidades[0]['fecha'], unidades[0]['sin_asiento']),
                         (movimiento.move_id, pago.date, False))
        self.assertEqual((linea.payment_link_state, linea.payment_move_id), ('linked', movimiento.move_id))
        self.assertEqual(linea.payment_attachment_id, copia)
        self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)
        self.assertFalse(copias_en(self, movimiento.move_id, rep_doc))

    def test_pago_agrupado_sin_asiento_se_liga_por_el_nodo(self):
        """Odoo no dice cuánto aplicó a cada factura un pago sin asiento de
        varias: se liga si el Monto del nodo es el del pago y el nodo paga sus
        mismas facturas."""
        folio1, compra1 = compra(self, 1000.0)
        folio2, compra2 = compra(self, 500.0)
        pago = pagar_sin_asiento(self, compra1 | compra2, agrupado=True)
        self.assertEqual(len(pago), 1)
        unidad = compra1._dsi_pagos_aplicados()[compra1][0]
        self.assertEqual((unidad['sin_asiento'], unidad['importe'], unidad['saldo_anterior']), (True, None, None))

        rep_doc = rep(self, (1500.0, 'MXN', '1', [docto(folio1, 1000.0), docto(folio2, 500.0)]))
        lineas = rep_doc.dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertIn("agrupado sin asiento", lineas[0].payment_link_note)
        self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)

    def test_pago_agrupado_sin_asiento_y_el_banco_contra_sus_facturas(self):
        """Pago sin asiento de A (1,000) y B (500), y el movimiento del banco
        conciliado contra las dos facturas, sin payment_lines_ids (en 18 no
        existe): es el suyo, porque les aplicó el importe del pago. La unidad
        de cada factura conserva la clave del pago, la línea gana el asiento
        del banco y no hay otra copia. En 18 Odoo deja el pago «en proceso»
        (compara cada parcial con el pago completo) y el lote de pagos lo
        marca conciliado; en 19 y 20 ya queda conciliado. Las dos valen."""
        folio_a, compra_a = compra(self, 1000.0)
        folio_b, compra_b = compra(self, 500.0)
        pago = pagar_sin_asiento(self, compra_a | compra_b, agrupado=True)
        rep_doc = rep(self, (1500.0, 'MXN', '1', [docto(folio_a, 1000.0), docto(folio_b, 500.0)]))
        lineas = rep_doc.dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        copia = lineas.payment_attachment_id
        self.assertEqual((copia.res_model, copia.res_id), ('account.payment', pago.id))

        movimiento = pagar_con_extracto(self, compra_a | compra_b, 1500.0, '2026-10-11')

        def sigue_en_el_pago():
            for factura, importe in ((compra_a, 1000.0), (compra_b, 500.0)):
                unidades = factura._dsi_pagos_aplicados()[factura]
                self.assertEqual([e['clave'] for e in unidades], [f'pago:{pago.id}'])
                self.assertEqual(
                    (unidades[0]['asiento'], unidades[0]['importe'], unidades[0]['fecha'], unidades[0]['tiene_rep']),
                    (movimiento.move_id, importe, pago.date, True))
            self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
            self.assertEqual((lineas.account_payment_id, lineas.payment_move_id), (pago, movimiento.move_id))
            self.assertEqual(lineas.payment_attachment_id, copia)
            self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)
            self.assertFalse(copias_en(self, movimiento.move_id, rep_doc))
            self.assertEqual(pago.dsi_rep_count, 1)
            self.assertFalse(set(lineas.ids) & set(self.Linea._lineas_con_liga_de_pago_por_revisar()))

        sigue_en_el_pago()
        if pago.state != _estado_de_pago_conciliado(self.env):
            pago.action_validate()
            self.env.cr.flush()
            sigue_en_el_pago()

    def test_una_parcialidad_por_el_banco_no_es_el_pago_sin_asiento_que_sigue(self):
        """Factura de 2,000: la primera parcialidad de 1,000 se concilió directo
        con el banco y la segunda es un pago sin asiento de 1,000 aún sin
        conciliar. Son dos pagos aunque tengan el mismo importe: a la factura
        todavía le queda saldo para el segundo."""
        folio, factura = compra(self, 2000.0)
        movimiento = pagar_con_extracto(self, factura, 1000.0, '2026-10-01')
        pago = pagar_sin_asiento(self, factura, fecha='2026-10-20')
        self.assertEqual(pago.amount, 1000.0)
        self.assertNotEqual(pago.state, _estado_de_pago_conciliado(self.env))
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['clave'] for e in unidades], [f'asiento:{movimiento.move_id.id}', f'pago:{pago.id}'])

        rep1 = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0, 2000.0, parcialidad='1')], {'fecha': '2026-10-01'}))
        rep2 = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0, 1000.0, parcialidad='2')], {'fecha': '2026-10-20'}))
        self.assertEqual((rep1.dsi_payment_line_ids.payment_link_state, rep1.dsi_payment_line_ids.payment_move_id),
                         ('linked', movimiento.move_id))
        self.assertEqual((rep2.dsi_payment_line_ids.payment_link_state, rep2.dsi_payment_line_ids.account_payment_id),
                         ('linked', pago))

    # -- Notas de crédito pagadas con la factura ----------------------------

    def test_pago_sin_asiento_de_una_factura_con_su_nota_de_credito(self):
        """Factura de 1,000 y su nota de crédito de 100 pagadas con un pago sin
        asiento de 900: es el pago de una sola factura y le aplica 900. La nota
        no es otra factura del pago."""
        folio, factura = compra(self, 1000.0)
        nota = self._nota_suelta(100.0, date(2026, 9, 30))
        pago = pagar_sin_asiento(self, factura | nota)
        self.assertEqual(pago.amount, 900.0)
        unidad = factura._dsi_pagos_aplicados()[factura][0]
        self.assertEqual((unidad['sin_asiento'], unidad['importe'], unidad['importe_pago']), (True, 900.0, 900.0))

        linea = rep(self, (900.0, 'MXN', '1', [docto(folio, 900.0, 900.0)])).dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertFalse(linea.payment_link_note)
        self.assertEqual(linea.payment_amount_applied, 900.0)
        self.assertTrue(self._tiene_rep(factura, pago))

    def test_pago_sin_asiento_con_su_nota_y_luego_el_banco(self):
        """Al conciliar con el banco el pago sin asiento de una factura y su nota
        de crédito, la nota tampoco cuenta: la unidad conserva la clave del pago,
        la línea gana el asiento del banco y no hay otra copia."""
        folio, factura = compra(self, 1000.0)
        nota = self._nota_suelta(100.0, date(2026, 9, 30))
        pago = pagar_sin_asiento(self, factura | nota)
        rep_doc = rep(self, (900.0, 'MXN', '1', [docto(folio, 900.0, 900.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))

        movimiento = pagar_con_extracto(self, factura, 900.0, '2026-10-11')
        unidades = factura._dsi_pagos_aplicados()[factura]
        self.assertEqual([e['clave'] for e in unidades], [f'pago:{pago.id}'])
        self.assertEqual(unidades[0]['asiento'], movimiento.move_id)
        self.assertEqual((linea.payment_link_state, linea.account_payment_id, linea.payment_move_id),
                         ('linked', pago, movimiento.move_id))
        self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)
        self.assertFalse(copias_en(self, movimiento.move_id, rep_doc))

    def test_pago_sin_asiento_de_dos_notas_no_dice_cuanto_toco_a_cada_una(self):
        """Un pago sin asiento que devuelve dos notas de crédito no aplicó su
        total a cada una: sin importe, como el pago sin asiento de dos facturas."""
        nota1 = self._nota_suelta(100.0, date(2026, 9, 30))
        nota2 = self._nota_suelta(50.0, date(2026, 9, 30))
        self.banco.inbound_payment_method_line_ids.payment_account_id = False
        pago = pagar_sin_asiento(self, nota1 | nota2, agrupado=True)
        self.assertEqual((len(pago), pago.amount), (1, 150.0))
        for nota in nota1 | nota2:
            unidad = nota._dsi_pagos_aplicados()[nota][0]
            self.assertEqual((unidad['clave'], unidad['importe'], unidad['importe_pago']),
                             (f'pago:{pago.id}', None, None))

    def test_la_devolucion_de_una_nota_mayor_no_es_el_pago_de_la_factura(self):
        """Factura de 100 y nota de crédito de 1,000 en un pago sin asiento: el
        proveedor devuelve 900. Ese pago no le aplicó 900 a la factura."""
        _folio, factura = compra(self, 100.0)
        nota = self._nota_suelta(1000.0, date(2026, 9, 30))
        self.banco.inbound_payment_method_line_ids.payment_account_id = False
        pago = pagar_sin_asiento(self, factura | nota)
        self.assertEqual((pago.payment_type, pago.amount), ('inbound', 900.0))
        unidad = factura._dsi_pagos_aplicados()[factura][0]
        self.assertEqual((unidad['clave'], unidad['importe'], unidad['importe_pago']),
                         (f'pago:{pago.id}', None, None))

    def test_pago_agrupado_sin_asiento_con_nota_de_credito_cuadra_por_el_nodo(self):
        """El nodo paga las mismas facturas que el pago sin asiento: la nota de
        crédito que va en el pago no es una de ellas."""
        folio1, compra1 = compra(self, 1000.0)
        folio2, compra2 = compra(self, 500.0)
        nota = self._nota_suelta(100.0, date(2026, 9, 30))
        pago = pagar_sin_asiento(self, compra1 | compra2 | nota)
        self.assertEqual(pago.amount, 1400.0)

        rep_doc = rep(self, (1400.0, 'MXN', '1', [docto(folio1, 900.0, 900.0), docto(folio2, 500.0)]))
        lineas = rep_doc.dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertIn("agrupado sin asiento", lineas[0].payment_link_note)
        self.assertTrue(self._tiene_rep(compra1, pago))
        self.assertTrue(self._tiene_rep(compra2, pago))

    # -- Odoo reparte el pago distinto que el REP (reglas de respaldo) -------

    def test_pago_agrupado_que_odoo_reparte_distinto_que_el_rep(self):
        """Pago de 1,400 de A (1,000) y B (500) con una nota de crédito de 100:
        Odoo aplicó la nota a una (a B en 19: A 1,000 y B 400; a A en 20: A
        900 y B 500) y el proveedor la tomó de la otra. Ninguna factura cuadra
        por lo aplicado; cuadra el Monto del nodo con el total del pago, que
        paga las dos facturas."""
        folio_a, compra_a = compra(self, 1000.0, fecha=date(2026, 6, 15))
        folio_b, compra_b = compra(self, 500.0, fecha=date(2026, 7, 15))
        nota = self._nota_suelta(100.0, date(2026, 9, 30))
        pago = pagar(self, compra_a | compra_b | nota)
        self.assertEqual(pago.amount, 1400.0)
        aplicado = tuple(
            [e['importe'] for e in factura._dsi_pagos_aplicados()[factura]] for factura in (compra_a, compra_b))
        self.assertIn(aplicado, (([1000.0], [400.0]), ([900.0], [500.0])))
        if aplicado == ([1000.0], [400.0]):
            doctos = [docto(folio_a, 900.0, 900.0), docto(folio_b, 500.0)]
        else:
            doctos = [docto(folio_a, 1000.0), docto(folio_b, 400.0, 400.0)]

        rep_doc = rep(self, (1400.0, 'MXN', '1', doctos))
        lineas = rep_doc.dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertIn("monto del pago", lineas[0].payment_link_note)
        self.assertEqual(sorted(lineas.mapped('payment_amount_applied')), sorted(aplicado[0] + aplicado[1]))
        self.assertEqual(len(copias_en(self, pago, rep_doc)), 1)
        self.assertTrue(self._tiene_rep(compra_a, pago))
        self.assertTrue(self._tiene_rep(compra_b, pago))

    def test_pago_con_descuento_que_salda_la_factura(self):
        """Pago de 980 que salda la factura de 1,000 con el descuento como
        quebranto («marcar como pagada»): Odoo le aplica 1,000 y el REP dice
        980. Cuadra el Monto del nodo con el total del pago."""
        folio, factura = compra(self, 1000.0)
        cuenta = self.env['account.account'].search([
            ('company_ids', 'in', self.company.ids), ('account_type', '=', 'income_other')], limit=1)
        pago = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=factura.ids).create({
                'payment_date': '2026-10-10', 'journal_id': self.banco.id, 'amount': 980.0,
                'payment_difference_handling': 'reconcile', 'writeoff_account_id': cuenta.id,
                'writeoff_label': "Descuento por pronto pago",
            })._create_payments()
        self.env.cr.flush()
        self.assertEqual([e['importe'] for e in factura._dsi_pagos_aplicados()[factura]], [1000.0])

        linea = rep(self, (980.0, 'MXN', '1', [docto(folio, 980.0, 1000.0)])).dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertIn("monto del pago", linea.payment_link_note)
        self.assertEqual(linea.payment_amount_applied, 1000.0)
        self.assertTrue(self._tiene_rep(factura, pago))

    def test_pago_agrupado_sin_asiento_que_cubre_los_saldos(self):
        """Pago sin asiento de 1,500 de A (1,000) y B (500), y un REP por
        factura: alcanza para el saldo de las dos, así que al conciliarlo Odoo
        le aplicará a cada una su saldo, que es lo que dice su REP."""
        folio_a, compra_a = compra(self, 1000.0)
        folio_b, compra_b = compra(self, 500.0)
        pago = pagar_sin_asiento(self, compra_a | compra_b)
        rep_a = rep(self, (1000.0, 'MXN', '1', [docto(folio_a, 1000.0)]))
        rep_b = rep(self, (500.0, 'MXN', '1', [docto(folio_b, 500.0)]))

        lineas = (rep_a | rep_b).dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'linked'})
        self.assertEqual(lineas.account_payment_id, pago)
        self.assertIn("saldo de la factura", lineas[0].payment_link_note)
        self.assertEqual(sorted(lineas.mapped('payment_amount_applied')), [500.0, 1000.0])
        self.assertEqual(len(copias_en(self, pago, rep_a)), 1)
        self.assertEqual(len(copias_en(self, pago, rep_b)), 1)
        self.assertTrue(self._tiene_rep(compra_a, pago))
        self.assertTrue(self._tiene_rep(compra_b, pago))

    def test_el_respaldo_no_liga_lo_ambiguo(self):
        """Ligar mal es peor que no ligar. El respaldo no liga: con dos pagos
        que lo cumplen, fuera de la ventana de días, con una factura del nodo
        que el pago no paga, ni con un pago sin asiento que no alcanza para
        los saldos. Y nunca le quita a otro REP el pago que le cuadra por lo
        aplicado."""
        # Dos pagos de 1,000 el mismo día y un REP de 1,000 que aplica 990.
        folio, factura = compra(self, 3000.0)
        pago1 = pagar(self, factura, importe=1000.0)
        pago2 = pagar(self, factura, importe=1000.0)
        linea = rep(self, (1000.0, 'MXN', '1', [docto(folio, 990.0, 3000.0)])).dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'amount_mismatch')
        self.assertFalse(self._tiene_rep(factura, pago1) or self._tiene_rep(factura, pago2))

        # Uno solo, pero a 70 días de la FechaPago.
        folio, factura = compra(self, 3000.0)
        pago = pagar(self, factura, importe=1000.0, fecha='2026-08-01')
        linea = rep(self, (1000.0, 'MXN', '1', [docto(folio, 990.0, 3000.0)])).dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'amount_mismatch')
        self.assertFalse(self._tiene_rep(factura, pago))

        # El Monto cuadra con el pago de A y B, pero el nodo también paga C.
        folio_a, compra_a = compra(self, 1000.0)
        _folio_b, compra_b = compra(self, 500.0)
        folio_c, _compra_c = compra(self, 600.0)
        pago = pagar(self, compra_a | compra_b)
        self.assertEqual(pago.amount, 1500.0)
        lineas = rep(self, (1500.0, 'MXN', '1', [docto(folio_a, 900.0), docto(folio_c, 600.0)])).dsi_payment_line_ids
        self.assertEqual(sorted(lineas.mapped('payment_link_state')), ['amount_mismatch', 'no_payment'])
        self.assertFalse(lineas.account_payment_id)
        self.assertFalse(self._tiene_rep(compra_a, pago))

        # Pago sin asiento de 1,200 para A (1,000) y B (500): no alcanza.
        folio_a, compra_a = compra(self, 1000.0)
        _folio_b, compra_b = compra(self, 500.0)
        pago = pagar_sin_asiento(self, compra_a | compra_b, importe=1200.0)
        linea = rep(self, (1000.0, 'MXN', '1', [docto(folio_a, 1000.0)])).dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'amount_mismatch')

        # Un pago de 1,000: un REP dice 1,000 y otro, con Monto 1,000, dice 990.
        folio, factura = compra(self)
        pago = pagar(self, factura)
        exacto = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]), ligar=False)
        por_monto = rep(self, (1000.0, 'MXN', '1', [docto(folio, 990.0)]), ligar=False)
        (exacto | por_monto)._ligar_reps()
        self.assertEqual(
            (exacto.dsi_payment_line_ids.payment_link_state, exacto.dsi_payment_line_ids.account_payment_id),
            ('linked', pago))
        self.assertFalse(exacto.dsi_payment_line_ids.payment_link_note)
        self.assertEqual(por_monto.dsi_payment_line_ids.payment_link_state, 'amount_mismatch')

    def test_el_monto_no_cuadra_con_facturas_a_las_que_el_pago_no_aplico(self):
        """«Registrar pago» deja en el pago todas las facturas elegidas aunque a
        alguna no le toque nada. Dos pagos de 1,000 a cuatro días: Odoo aplica
        el primero todo a A y nada a B, y el segundo a B. El proveedor reparte
        cada uno 500 y 500. El REP del segundo no cuadra por el monto con el
        primero (no le aplicó nada a B), ni cuando el del primero se cancela
        para sustituirlo: quedan para el contador."""
        folio_a, compra_a = compra(self, 1000.0, fecha=date(2026, 6, 15))
        folio_b, compra_b = compra(self, 1000.0, fecha=date(2026, 7, 15))
        pago1 = pagar(self, compra_a | compra_b, importe=1000.0, fecha='2026-10-01')
        pagar(self, compra_b, importe=1000.0, fecha='2026-10-05')
        self.assertEqual(pago1.invoice_ids, compra_a | compra_b)
        nodo1 = (1000.0, 'MXN', '1', [docto(folio_a, 500.0, 1000.0), docto(folio_b, 500.0, 1000.0)],
                 {'fecha': '2026-10-01'})
        rep2 = rep(self, (1000.0, 'MXN', '1', [
            docto(folio_a, 500.0, 500.0, parcialidad='2'), docto(folio_b, 500.0, 500.0, parcialidad='2'),
        ], {'fecha': '2026-10-05'}))
        rep1 = rep(self, nodo1)
        lineas = (rep1 | rep2).dsi_payment_line_ids
        self.assertEqual(set(lineas.mapped('payment_link_state')), {'amount_mismatch'})
        self.assertFalse(lineas.account_payment_id)
        self.assertFalse(self._tiene_rep(compra_a, pago1))

        sat_dice(self, rep1, 'cancelled')
        sustituto = rep(self, nodo1)
        self.assertFalse((rep2 | sustituto).dsi_payment_line_ids.account_payment_id)

        # Un pago sin asiento de C y D que aún no alcanza para los dos saldos:
        # no se sabe a cuál le tocará algo, y el REP que lo da todo a D no
        # cuadra por el monto.
        _folio_c, compra_c = compra(self, 1000.0, fecha=date(2026, 6, 15))
        folio_d, compra_d = compra(self, 1000.0, fecha=date(2026, 7, 15))
        pagar_sin_asiento(self, compra_c | compra_d, importe=1000.0, fecha='2026-10-01')
        linea = rep(self, (1000.0, 'MXN', '1', [docto(folio_d, 1000.0, 1000.0)],
                          {'fecha': '2026-10-01'})).dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id.id), ('amount_mismatch', False))

    def test_la_siguiente_parcialidad_no_le_quita_al_respaldo_su_pago(self):
        """Pago agrupado de 3,500: Odoo aplica 3,000 a A y 500 a B, el
        proveedor 2,500 y 1,000, y se liga por el monto. La siguiente
        parcialidad de B, de 1,000 y 50 días después, cuadra por lo aplicado
        con lo que el primer REP dice de B, pero fuera de su ventana: el primer
        REP conserva su pago y el de la parcialidad liga el suyo."""
        folio_a, compra_a = compra(self, 3000.0, fecha=date(2026, 6, 15))
        folio_b, compra_b = compra(self, 2000.0, fecha=date(2026, 7, 15))
        pago1 = pagar(self, compra_a | compra_b, importe=3500.0, fecha='2026-10-01')
        rep1 = rep(self, (3500.0, 'MXN', '1', [docto(folio_a, 2500.0, 3000.0), docto(folio_b, 1000.0, 2000.0)],
                          {'fecha': '2026-10-01'}))
        linea_b = rep1.dsi_payment_line_ids.filtered(lambda linea: linea.invoice_id == compra_b)
        self.assertEqual(set(rep1.dsi_payment_line_ids.mapped('payment_link_state')), {'linked'})
        self.assertEqual(rep1.dsi_payment_line_ids.account_payment_id, pago1)

        pago3 = pagar(self, compra_b, importe=1000.0, fecha='2026-11-20')
        self.assertEqual((linea_b.payment_link_state, linea_b.account_payment_id), ('linked', pago1))
        rep3 = rep(self, (1000.0, 'MXN', '1', [docto(folio_b, 1000.0, 1000.0, parcialidad='2')],
                          {'fecha': '2026-11-20'}))
        linea3 = rep3.dsi_payment_line_ids
        self.assertEqual((linea_b.payment_link_state, linea_b.account_payment_id), ('linked', pago1))
        self.assertEqual((linea3.payment_link_state, linea3.account_payment_id), ('linked', pago3))
        self.assertTrue(self._tiene_rep(compra_b, pago1))
        self.assertTrue(self._tiene_rep(compra_b, pago3))
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].browse(linea_b.action_elegir_pago()['res_id'])
        self.assertTrue(asistente.option_ids.filtered(lambda o: o.account_payment_id == pago1).matches)

    def test_el_asistente_marca_lo_que_cuadra_por_el_respaldo(self):
        folio, factura = compra(self, 1000.0)
        cuenta = self.env['account.account'].search([
            ('company_ids', 'in', self.company.ids), ('account_type', '=', 'income_other')], limit=1)
        pago = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=factura.ids).create({
                'payment_date': '2026-10-10', 'journal_id': self.banco.id, 'amount': 980.0,
                'payment_difference_handling': 'reconcile', 'writeoff_account_id': cuenta.id,
            })._create_payments()
        self.env.cr.flush()
        linea = rep(self, (980.0, 'MXN', '1', [docto(folio, 980.0, 1000.0)])).dsi_payment_line_ids
        asistente = self.env['dsi.cfdi.rep.pago.wizard'].browse(linea.action_elegir_pago()['res_id'])
        self.assertEqual((asistente.option_ids.account_payment_id, asistente.option_ids.matches), (pago, True))

    # -- Mantenerla al día -------------------------------------------------

    def test_la_actualizacion_liga_los_reps_ya_guardados_una_vez(self):
        folio, factura = compra(self)
        pago = pagar(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE dsi_cfdi_payment_line SET payment_link_state = NULL, account_payment_id = NULL, "
            "payment_move_id = NULL WHERE id = %s", linea.id))
        self.env.invalidate_all()

        self.Linea._backfill_ligas_con_pagos()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))

        Adjunto = self.env['ir.attachment']
        adjuntos = Adjunto.search_count([])
        self._envejecer(linea)
        self.Linea._backfill_ligas_con_pagos()
        self.assertTrue(self._sin_escribir(linea))
        self.assertEqual(Adjunto.search_count([]), adjuntos)

    def test_la_revision_diaria_repara_una_liga_vieja(self):
        """Si el precommit se perdió (un savepoint que se deshizo), la revisión
        diaria lo recoge. Va justo después de la liga con las facturas."""
        detectores = self.Inconsistencia._detectores()
        self.assertEqual(detectores[detectores.index('_detectar_reps_sin_factura') + 1], '_revisar_ligas_con_pagos')
        folio, factura = compra(self)
        pago = pagar(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'linked')

        pago.action_draft()
        self.env.cr.precommit.clear()
        self.env.flush_all()
        self.assertEqual(linea.payment_link_state, 'linked')
        self.assertIn(linea.id, self.Linea._lineas_con_liga_de_pago_por_revisar())
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(linea.payment_link_state, 'no_payment')

    def test_la_factura_que_vuelve_a_publicarse_sin_pagos_sale_de_no_aplica(self):
        """La factura pasa a borrador y la revisión diaria deja su REP «No
        aplica». Al publicarla otra vez, todavía sin pagos, la revisión la
        vuelve a ver aunque no tenga nada pagado: queda «Sin pago en Odoo» y
        ya no vuelve a salir. En 18 pasa también con una factura pagada: Odoo
        la desconcilia al pasarla a borrador."""
        folio, factura = compra(self)
        linea = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)])).dsi_payment_line_ids
        self.assertEqual(linea.payment_link_state, 'no_payment')

        factura.button_draft()
        self.env.cr.flush()
        self.Inconsistencia._revisar_ligas_con_pagos()
        self.assertEqual((linea.payment_link_state, linea.payment_link_note),
                         ('not_applicable', "La factura no está publicada."))

        factura.action_post()
        self.env.cr.flush()
        self.assertEqual(factura.payment_state, 'not_paid')
        self.assertIn(linea.id, self.Linea._lineas_con_liga_de_pago_por_revisar())
        self.Inconsistencia._revisar_ligas_con_pagos()
        self.assertEqual((linea.payment_link_state, linea.payment_link_note),
                         ('no_payment', "La factura no tiene pagos en Odoo."))
        self.assertNotIn(linea.id, self.Linea._lineas_con_liga_de_pago_por_revisar())

        # Una forma de pago sin movimiento del banco (compensación) se queda
        # «No aplica» con su motivo: no la vuelve a ver cada día.
        compensacion = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)], {'forma': '17'})).dsi_payment_line_ids
        self.assertEqual(compensacion.payment_link_state, 'not_applicable')
        self.assertNotIn(compensacion.id, self.Linea._lineas_con_liga_de_pago_por_revisar())

    def test_un_error_al_religar_no_detiene_el_pago(self):
        """Un choque con otra transacción es un aviso y la revisión se vuelve a
        programar; cualquier otro error queda en el log. El pago se registra
        igual."""
        folio, factura = compra(self)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        programadas = self._revisiones_programadas()
        choque = psycopg2.errors.SerializationFailure("concurrent update")
        with patch.object(type(self.Linea), '_ligar_con_sus_pagos', side_effect=choque), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency', level='WARNING') as log:
            pago = pagar(self, factura)
        self.assertEqual([r.levelname for r in log.records], ['WARNING'])
        self.assertIn('_ligar_reps_con_pagos', log.output[0])
        self.assertEqual(pago.move_id.state, 'posted')
        self.assertEqual(factura.payment_state, 'paid')
        self.assertEqual(self._revisiones_programadas(), programadas + 1)
        self.assertEqual(rep_doc.dsi_payment_line_ids.payment_link_state, 'no_payment')

        folio, factura = compra(self)
        rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        programadas = self._revisiones_programadas()
        with patch.object(type(self.Linea), '_ligar_con_sus_pagos', side_effect=ValueError("falla")), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.rep_pagos', level='ERROR') as log:
            pago = pagar(self, factura)
        self.assertEqual([r.levelname for r in log.records], ['ERROR'])
        self.assertEqual(pago.move_id.state, 'posted')
        self.assertEqual(self._revisiones_programadas(), programadas + 1)

    def test_si_la_busqueda_al_religar_falla_el_pago_se_registra(self):
        """La búsqueda de las líneas que religar también va en el manejo de
        errores, en su savepoint: con el código nuevo desplegado y la base sin
        actualizar falla («column … does not exist»), y aun así el pago se
        confirma y la transacción sigue viva."""
        folio, factura = compra(self)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        programadas = self._revisiones_programadas()
        buscar = type(self.Linea).search

        def busqueda_sin_columna(modelo, domain, *args, **kwargs):
            if any(isinstance(hoja, (list, tuple)) and hoja[0] == 'account_payment_id' for hoja in domain):
                modelo.env.cr.execute(SQL("SELECT columna_que_no_existe FROM dsi_cfdi_payment_line"))
            return buscar(modelo, domain, *args, **kwargs)

        with patch.object(type(self.Linea), 'search', busqueda_sin_columna), \
                self.assertLogs('odoo.sql_db', level='ERROR'), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.rep_pagos', level='ERROR') as log:
            pago = pagar(self, factura)
        self.assertEqual([r.levelname for r in log.records], ['ERROR'])
        self.assertEqual(pago.move_id.state, 'posted')
        self.assertEqual(factura.payment_state, 'paid')
        self.assertEqual(self._revisiones_programadas(), programadas + 1)
        self.assertEqual(rep_doc.dsi_payment_line_ids.payment_link_state, 'no_payment')

        self.Inconsistencia._revisar_ligas_con_pagos()
        self.assertEqual(rep_doc.dsi_payment_line_ids.payment_link_state, 'linked')

    def test_la_revision_ve_el_parcial_de_los_dos_lados(self):
        """Una liga al día no se vuelve a revisar, tenga la factura al debe o al
        haber del parcial; sin parcial, sí."""
        folio, factura = compra(self)
        pago = pagar(self, factura, importe=600.0)
        movimiento = pagar_con_extracto(self, factura, 400.0, '2026-10-10')
        rep_doc = rep(
            self,
            (600.0, 'MXN', '1', [docto(folio, 600.0, 1000.0, parcialidad='1')]),
            (400.0, 'MXN', '1', [docto(folio, 400.0, 400.0, parcialidad='2')]))
        lineas = rep_doc.dsi_payment_line_ids
        self.assertEqual(lineas.mapped('payment_link_state'), ['linked', 'linked'])
        self.assertEqual(lineas.payment_move_id, pago.move_id | movimiento.move_id)
        self.assertFalse(set(lineas.ids) & set(self.Linea._lineas_con_liga_de_pago_por_revisar()))

        parciales = factura._dsi_pagos_aplicados()[factura][0]['parciales'] | \
            factura._dsi_pagos_aplicados()[factura][1]['parciales']
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE account_partial_reconcile SET debit_move_id = credit_move_id, credit_move_id = debit_move_id "
            "WHERE id IN %s", tuple(parciales.ids)))
        self.env.invalidate_all()
        self.assertFalse(set(lineas.ids) & set(self.Linea._lineas_con_liga_de_pago_por_revisar()))

        self.env.cr.execute(SQL("DELETE FROM account_partial_reconcile WHERE id IN %s", tuple(parciales.ids)))
        self.env.invalidate_all()
        self.assertEqual(set(lineas.ids) & set(self.Linea._lineas_con_liga_de_pago_por_revisar()), set(lineas.ids))

    def test_la_liga_a_mano_que_deja_de_aplicar_lo_dice_en_el_rep(self):
        """«Elegir pago» la liga a mano con el segundo pago; la factura vuelve a
        borrador y queda «No aplica» (en 18 al desconciliarse, en 19 y 20 con
        la revisión diaria). El REP lo dice, con el pago que tenía y el motivo;
        volver a publicarla no la devuelve."""
        folio, factura = compra(self)
        pago1 = pagar(self, factura, importe=500.0)
        pago2 = pagar(self, factura, importe=500.0)
        rep_doc = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self._elegir_a_mano(linea, pago2)

        factura.button_draft()
        self.env.cr.flush()
        self.Inconsistencia._revisar_ligas_con_pagos()
        self.assertEqual(linea.payment_link_state, 'not_applicable')
        mensaje = self._mensajes_de_liga_perdida(rep_doc)
        self.assertEqual(len(mensaje), 1)
        self.assertIn(pago2.display_name, mensaje.body)
        self.assertIn("La factura no está publicada.", mensaje.body)

        factura.action_post()
        if factura.payment_state == 'not_paid':
            # Odoo 18 desconcilia la factura al pasarla a borrador (button_draft
            # llama a remove_move_reconcile); 19 y 20 conservan sus parciales.
            # Ahí el contador la vuelve a conciliar con sus pagos.
            (factura.line_ids | pago1.move_id.line_ids | pago2.move_id.line_ids).filtered(
                lambda l: l.account_type == 'liability_payable').reconcile()
        self.env.cr.flush()
        self.Inconsistencia._revisar_ligas_con_pagos()
        self.assertEqual(linea.payment_link_state, 'linked')
        self.assertEqual(len(self._mensajes_de_liga_perdida(rep_doc)), 1)

    def test_la_liga_a_mano_con_un_pago_borrado_lo_nombra(self):
        """El pago ligado a mano se pasa a borrador y se borra: cuando se
        religa, la línea ya no lo tiene (ondelete set null), y el mensaje del
        REP lo nombra igual."""
        folio, factura = compra(self)
        pago1 = pagar(self, factura, importe=500.0)
        pago2 = pagar(self, factura, importe=500.0)
        nombre = pago2.display_name
        rep_doc = rep(self, (500.0, 'MXN', '1', [docto(folio, 500.0, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self._elegir_a_mano(linea, pago2)

        pago2.action_draft()
        pago2.unlink()
        self.env.cr.flush()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago1))
        mensaje = self._mensajes_de_liga_perdida(rep_doc)
        self.assertEqual(len(mensaje), 1)
        self.assertIn(nombre, mensaje.body)
        self.assertNotIn("False", mensaje.body)

    # -- Vistas, permisos y licencia ---------------------------------------

    def test_vistas_del_pago_y_permisos(self):
        folio, factura = compra(self)
        pago = pagar(self, factura)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))

        Pago = self.env['account.payment'].with_user(self.usuario)
        arch = etree.fromstring(Pago.get_views([(False, 'form')])['views']['form']['arch'])
        self.assertTrue(arch.xpath("//button[@name='action_view_rep_lines']"))
        self.assertTrue(arch.xpath("//notebook[@name='payment_notebook']/page[@name='page_dsi_rep']"))
        del_usuario = pago.with_user(self.usuario)
        self.assertEqual(del_usuario.dsi_rep_count, 1)
        self.assertEqual(del_usuario.dsi_rep_line_ids, rep_doc.dsi_payment_line_ids)
        accion = del_usuario.action_view_rep_lines()
        self.assertEqual(self.Linea.with_user(self.usuario).search(accion['domain']), rep_doc.dsi_payment_line_ids)
        arch = etree.fromstring(self.Doc.with_user(self.usuario).get_views([(False, 'form')])['views']['form']['arch'])
        self.assertTrue(arch.xpath("//field[@name='dsi_payment_line_ids']/list/field[@name='account_payment_id']"))
        arch = etree.fromstring(self.Linea.with_user(self.usuario).get_views([(False, 'form')])['views']['form']['arch'])
        self.assertTrue(arch.xpath("//header/button[@name='action_elegir_pago']"))

        vistas = self.env['account.payment'].with_user(self.facturacion).get_views([(False, 'form')])
        campos = vistas['models']['account.payment']
        campos = campos.get('fields', campos)
        self.assertFalse([campo for campo in campos if campo.startswith('dsi_rep')])
        self.assertFalse(etree.fromstring(vistas['views']['form']['arch']).xpath("//page[@name='page_dsi_rep']"))
        pago.with_user(self.facturacion).read(list(campos))

    def test_sin_licencia_no_se_elige_a_mano_pero_se_liga_solo(self):
        folio, factura = compra(self)
        rep_doc = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        linea = rep_doc.dsi_payment_line_ids
        self.licencia.token = False
        with self.assertRaises(RedirectWarning) as error:
            linea.with_user(self.usuario).action_elegir_pago()
        self.assertIn("relacionar un REP con su pago en Odoo", str(error.exception))

        pago = pagar(self, factura)
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))

    def test_la_lista_sin_asiento_de_pago_excluye_los_ligados(self):
        """Un REP cuyo documento relacionado ya está ligado a un pago de Odoo
        ya tiene su pago; los que esperan elegir salen en su propia lista."""
        folio, factura = compra(self)
        pagar(self, factura)
        ligado = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        suelto = rep(self, (300.0, 'MXN', '1', [docto(self._uuid(), 300.0)]))
        folio, factura = compra(self)
        pagar(self, factura, fecha='2026-08-01')
        por_elegir = rep(self, (1000.0, 'MXN', '1', [docto(folio, 1000.0)]))
        reps = ligado | suelto | por_elegir

        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_payment_no_move')
        en_la_lista = self.Doc.with_user(self.usuario).search(safe_eval(accion.domain) + [('id', 'in', reps.ids)])
        self.assertEqual(en_la_lista, suelto | por_elegir)

        accion = self.env.ref('dsi_cfdi_vault.action_dsi_cfdi_payment_line_por_ligar')
        lineas = self.Linea.with_user(self.usuario).search(
            safe_eval(accion.domain) + [('payment_doc_id', 'in', reps.ids)])
        self.assertEqual(lineas, por_elegir.dsi_payment_line_ids)

    def test_rep_xml_acepta_fecha_y_forma_del_nodo(self):
        """La fábrica de REP de test_rep_facturas.py, con la FechaPago y la
        FormaDePagoP de cada nodo."""
        uuid = self._uuid()
        doc = guardar(self, rep_xml(uuid, [
            (100.0, 'MXN', '1', [docto(self._uuid(), 100.0)], {'fecha': '2026-09-02', 'forma': '17'}),
            (50.0, 'USD', '18', [docto(self._uuid(), 50.0, moneda='USD')]),
        ]))
        lineas = doc.dsi_payment_line_ids.sorted('pago_idx')
        self.assertEqual([(l.fecha_pago, l.forma_pago) for l in lineas],
                         [(date(2026, 9, 2), '17'), (date(2026, 10, 10), '03')])
        self.assertEqual(doc.amount_total, 1000.0)
