"""Discrepancia «Compra PPD pagada sin REP».

Existe porque nadie decía qué pagos de compras PPD se quedaron sin su REP: la
Bóveda ligaba los REP que llegaban, pero un pago sin el suyo no se veía en
ningún lado, y el proveedor solo tiene hasta el día 5 del mes siguiente para
emitirlo. Ahora cada pago aplicado a una compra PPD sin REP vigente ligado es
una discrepancia desde el día 6, contado en la hora de México.

Lo que más cuidan estas pruebas:

  * el plazo: nada hasta el día 5 y desde el 6 en la hora de México (no en
    UTC), también a fin de mes y de año, contado desde el movimiento del
    banco si se conoce y es posterior al pago (sin cambiar de registro);
  * qué es una compra PPD: el CFDI de ingreso vigente de la Bóveda con PPD;
    ni PUE, ni sin CFDI, ni ventas;
  * qué es un pago: uno por account.payment o movimiento del banco y por
    compra (parcialidades, pagos de varias compras, pago sin asiento que
    después se concilia); las notas de crédito, compensaciones y diferencias
    cambiarias no son pagos;
  * se resuelve en el momento al ligarse el REP (a mano o por la regla de la
    liga), y vuelve si el REP se cancela o se archiva; lo demás lo ve la
    revisión diaria, con el motivo de cada resolución;
  * el corte opcional, que nunca se fija solo, y uno mal escrito no toca nada;
  * la observación dice qué hacer: elegir el pago de un REP sin ligar,
    importar el REP que está en otra empresa del mismo RFC, o pedirlo. Es
    una pista en el mismo registro: no lo sustituye ni le quita su revisión,
    ni toca los registros de los demás pagos de la compra;
  * la consulta de la revisión diaria no deja fuera nada de lo que registra
    Python (tampoco con el plazo en una cuenta por cobrar), y la revisión del
    día 6 se programa una sola vez;
  * las tandas de la revisión diaria: una que falla no deshace las demás ni
    resuelve sus registros, y la caché no crece de una a otra;
  * lo que ve cada quien: filtros, menú, el botón del asiento del pago, la
    otra empresa y quien solo factura; la compra no queda «Por revisar».

Usa una empresa mexicana en pesos con el catálogo de México
(tests/common_pagos.py), y compras convertidas desde su CFDI en la Bóveda,
que es de donde sale el método de pago.
"""
from datetime import date, datetime
from unittest.mock import patch

from freezegun import freeze_time
from lxml import etree

from odoo import Command, tools
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL
from odoo.tools.safe_eval import safe_eval

from odoo.addons.dsi_cfdi_vault.models import ppd_sin_rep
from odoo.addons.dsi_cfdi_vault.models.ppd_sin_rep import PARAMETRO_CORTE_PPD, _limite_del_rep
from odoo.addons.dsi_cfdi_vault.models.rep_pagos import ESTADOS_PENDIENTES, _estado_de_pago_conciliado
from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param

from .common_pagos import (
    compra as compra_de_odoo, empresa_mx, guardar, pagar, pagar_con_extracto, pagar_sin_asiento, rep, sat_dice,
    tipo_de_cambio, venta,
)
from .test_factura_como_odoo import cfdi_xml
from .test_rep_facturas import GRUPOS_BOVEDA, docto, rep_xml

# El «hoy» de cada prueba, también el de los ganchos: los pagos de octubre ya
# vencieron. Sin esto el resultado dependería del día en que corre la prueba.
DESPUES_DEL_DIA_6 = '2026-11-20 18:00:00'


@tagged('post_install', '-at_install')
class TestPpdSinRep(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        empresa_mx(cls, 'boveda-ppd')
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.Linea = cls.env['dsi.cfdi.payment.line']
        cls.contador = 0

    def setUp(self):
        super().setUp()
        # Sin corte: todo el histórico. Las pruebas del corte lo ponen.
        self.env['ir.config_parameter'].sudo().search([('key', '=', PARAMETRO_CORTE_PPD)]).unlink()
        reloj = freeze_time(DESPUES_DEL_DIA_6)
        reloj.start()
        self.addCleanup(reloj.stop)

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return "ACACACAC-2222-4000-8000-%012d" % self.contador

    def _poner_parametro(self, clave, valor):
        cp = self.env['ir.config_parameter'].sudo()
        (cp.set_str if hasattr(cp, 'set_str') else cp.set_param)(clave, valor)

    def _compra_ppd(self, metodo='PPD', moneda='MXN', tipo_cambio=''):
        """El CFDI en la Bóveda y la compra convertida desde él, publicada:
        1,000 + IVA = 1,160. Devuelve (cfdi, compra)."""
        uuid = self._uuid()
        cfdi = guardar(self, cfdi_xml(
            uuid, moneda=moneda, tipo_cambio=tipo_cambio, metodo_pago=metodo,
            forma_pago='99' if metodo == 'PPD' else '03').encode())
        factura = cfdi._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        factura.action_post()
        return cfdi, factura

    def _rep_de(self, cfdi, pagado, fecha, ligar=True, saldo_anterior=None):
        """Un REP del proveedor con un solo documento relacionado: la compra
        de `cfdi`."""
        return rep(self, (pagado, 'MXN', '1', [docto(cfdi.uuid, pagado, saldo_anterior=saldo_anterior)],
                          {'fecha': fecha}), ligar=ligar)

    def _ligar(self, linea, compra, pago):
        """Liga a mano la línea con el pago, sin pasar por la regla de la liga,
        y avisa como lo hace ella."""
        linea.sudo().write({
            'invoice_id': compra.id,
            'account_payment_id': pago.id,
            'payment_move_id': pago.move_id.id,
            'payment_link_state': 'manual',
        })
        self.Inconsistencia._tras_ligar_pagos(compra, 'rep')

    def _dejar_sin_ligar(self, linea, compra, estado='amount_mismatch'):
        """Deja la línea con su compra pero sin pago, como la liga cuando no
        encuentra uno que cuadre, sin pasar por su regla, y avisa como lo hace
        ella."""
        linea.sudo().write({'invoice_id': compra.id, 'payment_link_state': estado})
        self.Inconsistencia._tras_ligar_pagos(compra, 'rep')

    def _en_cache(self, registros, campo):
        """Los de `registros` que tienen `campo` en la caché del ORM: en 19 y
        20 con la caché del campo (en 20 env.cache es obsoleto y avisa); en
        18, con env.cache."""
        campo = registros._fields[campo]
        if hasattr(campo, '_get_cache'):
            return registros.filtered(lambda r: r.id in campo._get_cache(r.env))
        return registros.filtered(lambda r: r.env.cache.contains(r, campo))

    def _revisar(self, utc=DESPUES_DEL_DIA_6):
        with freeze_time(utc):
            self.Inconsistencia._detectar_ppd_sin_rep()

    def _registros(self, compras, *estados):
        dominio = [('kind', '=', 'ppd_sin_rep'), ('move_id', 'in', compras.ids)]
        if estados:
            dominio.append(('state', 'in', estados))
        return self.Inconsistencia.search(dominio)

    def _mensajes(self, registro):
        """El historial, con los mensajes de seguimiento ya escritos (los
        escribe el precommit)."""
        self.env.cr.flush()
        return " ".join(str(mensaje.body) for mensaje in registro.message_ids)

    def _compras_de_la_consulta(self, corte=None):
        return {compra_id for compra_id, _fecha in self.Inconsistencia._pagos_ppd_sin_rep_por_sql(corte)}

    def _triggers(self, cuando=None):
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        dominio = [('cron_id', '=', cron.id)]
        if cuando:
            dominio.append(('call_at', '=', cuando))
        return self.env['ir.cron.trigger'].search_count(dominio)

    def _por_revisar(self, factura):
        if 'review_state' in factura._fields:
            return factura.review_state in ('todo', 'anomaly')
        return not factura.checked

    def _revisar_factura(self, factura):
        if 'review_state' in factura._fields:
            factura.review_state = 'reviewed'
        else:
            factura.checked = True

    def _nota_de_credito(self, compra, importe, fecha):
        nota = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.proveedor.id,
            'invoice_date': fecha,
            'invoice_line_ids': [Command.create({
                'name': "Descuento", 'quantity': 1, 'price_unit': importe, 'tax_ids': [Command.clear()],
            })],
        })
        nota.action_post()
        (compra.line_ids | nota.line_ids).filtered(lambda l: l.account_type == 'liability_payable').reconcile()
        return nota

    def _volver_a_publicar(self, pago, compra):
        """Publica otra vez un pago que se regresó a borrador y, si hiciera
        falta, lo concilia con la compra."""
        pago.action_post()
        pendientes = (pago.move_id.line_ids | compra.line_ids).filtered(
            lambda l: l.account_type == 'liability_payable' and not l.reconciled)
        if len(pendientes) > 1:
            pendientes.reconcile()
        self.env.cr.flush()

    # -- El plazo --------------------------------------------------------

    def test_pago_sin_rep_aparece_desde_el_dia_6(self):
        cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')

        self._revisar('2026-11-05 18:00:00')
        self.assertFalse(self._registros(compra), "el día 5 todavía está en plazo")

        self._revisar('2026-11-06 12:00:00')
        registro = self._registros(compra)
        self.assertRecordValues(registro, [{
            'state': 'open',
            'severity': 'warning',
            'source': 'cron',
            'company_id': self.company.id,
            'move_id': compra.id,
            'account_payment_id': pago.id,
            'payment_move_id': pago.move_id.id,
            'cfdi_document_id': cfdi.id,
            'partner_id': compra.commercial_partner_id.id,
            'uuid': cfdi.uuid,
            'dedupe_key': f"ppd_sin_rep:0:{compra.id}:0:pago:{pago.id}",
            'fingerprint': 'fecha=2026-10-15',
            'vault_value': "PPD, sin REP de este pago",
        }])
        self.assertIn(pago.display_name, registro.odoo_value)
        self.assertIn("1,160.00", registro.odoo_value)
        self.assertIn(tools.format_date(self.env, date(2026, 11, 5)), registro.observation)
        self.assertIn("Pídeselo al proveedor", registro.observation)
        self.assertNotIn("aún no tiene asiento", registro.observation)
        self.assertIn(pago.display_name, registro.display_name, "dos pagos de una compra no se llaman igual")

    def test_el_dia_6_es_el_de_mexico(self):
        """El cron corre en UTC: a las 05:30 UTC del día 6 en México todavía
        son las 23:30 del 5."""
        _cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        self._revisar('2026-11-06 05:30:00')
        self.assertFalse(self._registros(compra))
        self._revisar('2026-11-06 06:30:00')
        self.assertEqual(len(self._registros(compra)), 1)

    def test_limite_a_fin_de_mes_y_de_anio(self):
        self.assertEqual(_limite_del_rep(date(2026, 1, 31)), date(2026, 2, 5))
        self.assertEqual(_limite_del_rep(date(2026, 12, 20)), date(2027, 1, 5))
        self.assertEqual(_limite_del_rep(date(2026, 10, 1)), date(2026, 11, 5))

        _cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-01-31')
        self._revisar('2026-02-05 18:00:00')
        self.assertFalse(self._registros(compra))
        self._revisar('2026-02-06 12:00:00')
        self.assertEqual(self._registros(compra).fingerprint, 'fecha=2026-01-31')

    def test_el_plazo_corre_desde_la_fecha_del_banco(self):
        """Un pago registrado el 31 de octubre que el banco cobra el 3 de
        noviembre: el proveedor lo recibió el 3 (la FechaPago de su REP) y
        tiene hasta el 5 de diciembre. Cuenta la más tardía entre la fecha
        del pago y la de su movimiento del banco, si ya se conoce; sin
        movimiento del banco, la del pago."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar_sin_asiento(self, compra, fecha='2026-10-31')
        pagar_con_extracto(self, compra, 1160.0, '2026-11-03')
        aplicado, = compra._dsi_pagos_aplicados()[compra]
        self.assertEqual((aplicado['pago'], aplicado['fecha'], aplicado['fecha_banco']),
                         (pago, date(2026, 10, 31), date(2026, 11, 3)))
        _cfdi, sin_banco = self._compra_ppd()
        pagar(self, sin_banco, fecha='2026-10-31')

        self._revisar('2026-11-06 12:00:00')
        self.assertFalse(self._registros(compra), "el plazo corre desde el 3 de noviembre")
        self.assertEqual(len(self._registros(sin_banco)), 1, "sin movimiento del banco cuenta la fecha del pago")
        self.assertEqual(self._triggers(datetime(2026, 12, 6, 6, 5)), 1, "vence el 6 de diciembre")

        self._revisar('2026-12-05 18:00:00')
        self.assertFalse(self._registros(compra))
        self._revisar('2026-12-06 12:00:00')
        registro = self._registros(compra)
        self.assertRecordValues(registro, [{
            'state': 'open',
            'account_payment_id': pago.id,
            'fingerprint': 'fecha=2026-10-31',
        }])
        self.assertIn(tools.format_date(self.env, date(2026, 12, 5)), registro.observation)
        self.assertIn(tools.format_date(self.env, date(2026, 11, 3)), registro.observation,
                      "dice desde qué fecha corre el plazo")

    def test_la_fecha_del_banco_que_se_conoce_despues(self):
        """El registro ya existe cuando se concilia el banco. Si con la fecha
        del banco el pago todavía está en plazo, se resuelve con ese motivo;
        si sigue vencido, es el mismo registro (la huella es la fecha del
        pago), revisado si lo estaba, y su observación dice ahora el límite
        que cuenta y desde qué fecha."""
        _cfdi, en_plazo = self._compra_ppd()
        pagar_sin_asiento(self, en_plazo, fecha='2026-10-31')
        _cfdi, vencida = self._compra_ppd()
        pagar_sin_asiento(self, vencida, fecha='2026-09-15')
        self._revisar('2026-11-06 12:00:00')
        registro = self._registros(en_plazo)
        revisado = self._registros(vencida)
        self.assertEqual((registro.state, revisado.state), ('open', 'open'))
        self.assertIn(tools.format_date(self.env, date(2026, 10, 5)), revisado.observation)
        revisado.with_user(self.usuario).action_marcar_revisado()

        pagar_con_extracto(self, en_plazo, 1160.0, '2026-11-03')
        pagar_con_extracto(self, vencida, 1160.0, '2026-10-03')
        self._revisar('2026-11-07 12:00:00')
        self.assertEqual(registro.state, 'resolved')
        self.assertIn(f"tiene hasta el {tools.format_date(self.env, date(2026, 12, 5))}", self._mensajes(registro))
        self.assertEqual(self._registros(vencida), revisado, "no nace otro")
        self.assertRecordValues(revisado, [{
            'state': 'reviewed',
            'reviewed_by_id': self.usuario.id,
            'fingerprint': 'fecha=2026-09-15',
        }])
        self.assertIn(tools.format_date(self.env, date(2026, 11, 5)), revisado.observation)
        self.assertIn(tools.format_date(self.env, date(2026, 10, 3)), revisado.observation)
        self.assertNotIn("Cambiaron los valores", self._mensajes(revisado))

    # -- Qué compras cuentan ---------------------------------------------

    def test_compra_pue_no_cuenta(self):
        cfdi, compra = self._compra_ppd(metodo='PUE')
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        self.assertFalse(compra._dsi_cfdi_ppd())
        self.assertFalse(self._registros(compra))
        self.assertNotIn(compra.id, self._compras_de_la_consulta())

    def test_compra_sin_su_cfdi_en_la_boveda_no_cuenta(self):
        """Una compra registrada solo en Odoo: no hay de dónde sacar el método
        de pago."""
        _folio, compra = compra_de_odoo(self, 1160.0)
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        self.Inconsistencia._sincronizar_ppd_sin_rep(compra, 'cron')
        self.assertFalse(compra._dsi_cfdi_ppd())
        self.assertFalse(self._registros(compra))

    def test_cfdi_de_la_compra_cancelado_o_archivado_la_resuelve(self):
        cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(registro.state, 'open')

        sat_dice(self, cfdi, 'cancelled')
        self._revisar()
        self.assertEqual(registro.state, 'resolved')
        self.assertIn("ya no es una compra PPD", self._mensajes(registro))

        cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(registro.state, 'open')
        cfdi.active = False
        self._revisar()
        self.assertEqual(registro.state, 'resolved')
        self.assertIn("ya no es una compra PPD", self._mensajes(registro))

    def test_venta_ppd_no_cuenta(self):
        """El REP de una venta lo emite la empresa: no es una compra sin REP."""
        folio, factura = venta(self, 1160.0)
        cfdi = guardar(self, cfdi_xml(folio, metodo_pago='PPD', forma_pago='99').encode(), direction='issued')
        cfdi.move_id = factura
        pagar(self, factura, fecha='2026-10-15')
        self.assertEqual(factura._dsi_cfdi_de_la_boveda(), cfdi)
        self.assertFalse(factura._dsi_cfdi_ppd())
        self._revisar()
        self.Inconsistencia._sincronizar_ppd_sin_rep(factura, 'cron')
        self.assertFalse(self._registros(factura))
        self.assertNotIn(factura.id, self._compras_de_la_consulta())

    # -- El REP ----------------------------------------------------------

    def test_rep_ligado_al_pago_la_resuelve_al_momento(self):
        """Sin esperar a la revisión diaria: el punto de extensión de la liga
        la resuelve en cuanto el REP queda ligado a ese pago."""
        cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(registro.state, 'open')

        rep_doc = self._rep_de(cfdi, 1160.0, '2026-10-15', ligar=False)
        self._ligar(rep_doc.dsi_payment_line_ids, compra, pago)
        self.assertEqual(registro.state, 'resolved')
        self.assertIn(f"Ya tiene su REP, ligado a este pago: {rep_doc.uuid}", self._mensajes(registro))

    def test_la_liga_automatica_de_u1_la_resuelve(self):
        """Importar el REP con su FechaPago y su importe: la liga lo deja con
        su pago y la discrepancia se resuelve en la misma operación."""
        cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)

        rep_doc = self._rep_de(cfdi, 1160.0, '2026-10-15')
        linea = rep_doc.dsi_payment_line_ids
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('linked', pago))
        self.assertEqual(registro.state, 'resolved')
        self.assertIn("Ya tiene su REP", self._mensajes(registro))
        self._revisar()
        self.assertFalse(self._registros(compra, 'open', 'reviewed'))
        self.assertNotIn(compra.id, self._compras_de_la_consulta())

    def test_rep_cancelado_no_cubre_y_la_reabre(self):
        cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        rep_doc = self._rep_de(cfdi, 1160.0, '2026-10-15')
        self.assertEqual(registro.state, 'resolved')

        sat_dice(self, rep_doc, 'cancelled')
        self.assertEqual(registro.state, 'open')
        self.assertIn("Volvió a aparecer", self._mensajes(registro))
        self.assertEqual(self._registros(compra), registro, "es el mismo registro, reabierto")

    def test_rep_archivado_no_cubre(self):
        cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        rep_doc = self._rep_de(cfdi, 1160.0, '2026-10-15')
        self.assertEqual(registro.state, 'resolved')
        self.assertNotIn(compra.id, self._compras_de_la_consulta())

        rep_doc.active = False
        self.assertEqual(registro.state, 'open')
        self.assertIn(compra.id, self._compras_de_la_consulta())

    # -- Qué es un pago ---------------------------------------------------

    def test_parcialidades_una_por_pago(self):
        """Dos parcialidades y el REP solo de la primera: la segunda sale sola,
        en cuanto se registra (el precommit de la liga, porque la compra ya
        tiene REP), con origen «Pago en Odoo»."""
        cfdi, compra = self._compra_ppd()
        primero = pagar(self, compra, importe=600.0, fecha='2026-10-01')
        rep_doc = self._rep_de(cfdi, 600.0, '2026-10-01', saldo_anterior=1160.0)
        self.assertEqual(rep_doc.dsi_payment_line_ids.account_payment_id, primero)
        self.assertFalse(self._registros(compra))

        segundo = pagar(self, compra, importe=560.0, fecha='2026-10-15')
        registro = self._registros(compra)
        self.assertEqual(len(registro), 1)
        self.assertEqual((registro.account_payment_id, registro.source), (segundo, 'pago'))
        self.assertIn("560.00", registro.odoo_value)
        self._revisar()
        self.assertEqual(self._registros(compra), registro)

    def test_un_pago_de_varias_compras_da_una_por_compra(self):
        """El REP es por documento relacionado: un pago de dos compras con el
        REP de una sola deja sin REP a la otra."""
        cfdi1, compra1 = self._compra_ppd()
        _cfdi2, compra2 = self._compra_ppd()
        pago = pagar(self, compra1 | compra2, fecha='2026-10-15', agrupado=True)
        self.assertEqual(len(pago), 1)
        self._rep_de(cfdi1, 1160.0, '2026-10-15')
        self._revisar()
        self.assertFalse(self._registros(compra1))
        registro = self._registros(compra2)
        self.assertEqual(len(registro), 1)
        self.assertEqual(registro.account_payment_id, pago)
        self.assertEqual(registro.dedupe_key, f"ppd_sin_rep:0:{compra2.id}:0:pago:{pago.id}")

    def test_nota_de_credito_no_es_pago(self):
        cfdi, compra = self._compra_ppd()
        self._nota_de_credito(compra, 160.0, '2026-09-30')
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(len(registro), 1)
        self.assertEqual(registro.account_payment_id, pago)
        self.assertIn("1,000.00", registro.odoo_value)
        filas = [fecha for compra_id, fecha in self.Inconsistencia._pagos_ppd_sin_rep_por_sql(None)
                 if compra_id == compra.id]
        self.assertEqual(filas, [date(2026, 10, 15)])

    def test_diferencia_cambiaria_no_es_pago(self):
        """Compra de 1,160 USD a 17 pagada en pesos a 18: Odoo registra la
        diferencia cambiaria, que no es un pago."""
        tipo_de_cambio(self, '2026-06-15', 17.0)
        tipo_de_cambio(self, '2026-10-15', 18.0)
        _cfdi, compra = self._compra_ppd(moneda='USD', tipo_cambio='17')
        self.assertEqual(compra.currency_id, self.usd)
        pago = pagar(self, compra, importe=20880.0, fecha='2026-10-15', moneda=self.mxn)
        self.assertIn(compra.payment_state, ('paid', 'in_payment'))
        lineas = compra.line_ids | pago.move_id.line_ids
        # Puede haber más de una: la de la compra y la del IVA en base de
        # efectivo (CABA) del catálogo de México.
        cambiarias = (lineas.matched_debit_ids | lineas.matched_credit_ids).exchange_move_id
        self.assertTrue(cambiarias, "Odoo registró la diferencia cambiaria")
        self.assertEqual(set(cambiarias.mapped('state')), {'posted'})

        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(len(registro), 1)
        self.assertEqual(registro.payment_move_id, pago.move_id)
        self.assertNotIn(registro.payment_move_id, cambiarias)
        filas = [fecha for compra_id, fecha in self.Inconsistencia._pagos_ppd_sin_rep_por_sql(None)
                 if compra_id == compra.id]
        self.assertEqual(filas, [date(2026, 10, 15)])

    def test_compensacion_no_es_pago(self):
        """Un asiento manual contra la cuenta por pagar (compensación con lo
        que el proveedor debe): salda la compra, pero no es un pago."""
        _cfdi, compra = self._compra_ppd()
        por_pagar = compra.line_ids.filtered(lambda l: l.account_type == 'liability_payable')
        diario = self.env['account.journal'].search([
            ('type', '=', 'general'), ('company_id', '=', self.company.id)], limit=1)
        por_cobrar = self.proveedor.with_company(self.company).property_account_receivable_id
        compensacion = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-10-15',
            'journal_id': diario.id,
            'line_ids': [
                Command.create({'account_id': por_pagar.account_id.id, 'partner_id': self.proveedor.id,
                                'debit': 1160.0}),
                Command.create({'account_id': por_cobrar.id, 'partner_id': self.proveedor.id,
                                'credit': 1160.0}),
            ],
        })
        compensacion.action_post()
        (compensacion.line_ids.filtered(lambda l: l.account_id == por_pagar.account_id) | por_pagar).reconcile()
        self.assertTrue(por_pagar.reconciled)

        self._revisar()
        self.assertEqual(compra._dsi_pagos_aplicados()[compra], [])
        self.assertFalse(self._registros(compra))
        self.assertNotIn(compra.id, self._compras_de_la_consulta())

    def test_extracto_conciliado_directo(self):
        """Un movimiento del banco conciliado directo con la compra, sin
        account.payment: la clave es la de su asiento, y el asiento del banco
        muestra la discrepancia en su botón."""
        _cfdi, compra = self._compra_ppd()
        movimiento = pagar_con_extracto(self, compra, 1160.0, '2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertFalse(registro.account_payment_id)
        self.assertEqual(registro.payment_move_id, movimiento.move_id)
        self.assertEqual(registro.dedupe_key, f"ppd_sin_rep:0:{compra.id}:0:asiento:{movimiento.move_id.id}")
        self.assertIn(movimiento.move_id.display_name, registro.odoo_value)
        asiento = movimiento.move_id.with_user(self.usuario)
        self.assertEqual((asiento.dsi_inconsistency_total, asiento.dsi_inconsistency_count), (1, 1))

    def test_compra_con_su_plazo_en_una_cuenta_por_cobrar(self):
        """La línea de plazo de la compra (y la del pago) en una cuenta por
        cobrar. Odoo ya no deja capturarlo (_check_payable_receivable), pero
        puede venir de una base migrada de antes. La ayuda cuenta sus pagos,
        y la consulta de la revisión diaria también la tiene que traer: es
        una criba que no deja fuera nada de lo que registra Python."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        por_cobrar = self.proveedor.with_company(self.company).property_account_receivable_id
        lineas = (compra.line_ids | pago.move_id.line_ids).filtered(lambda l: l.account_type == 'liability_payable')
        self.assertEqual(len(lineas), 2)
        # Por SQL, como la dejó la migración: el ORM ya no lo permite.
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE account_move_line SET account_id = %s WHERE id IN %s", por_cobrar.id, tuple(lineas.ids)))
        self.env.invalidate_all()
        plazo = compra.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        self.assertEqual(plazo.account_id.account_type, 'asset_receivable')
        self.assertEqual([e['pago'] for e in compra._dsi_pagos_aplicados()[compra]], [pago])

        self.assertIn(compra.id, self._compras_de_la_consulta())
        self._revisar()
        self.assertEqual(self._registros(compra).account_payment_id, pago)

    def test_pago_sin_asiento_y_luego_el_banco_no_cambia_el_registro(self):
        """Con el módulo accountant el pago no tiene asiento hasta conciliar el
        banco. Cuenta desde su fecha de registro, y al conciliarlo sigue siendo
        el mismo registro: misma clave (la del pago) y misma huella (su
        fecha)."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar_sin_asiento(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        self.assertEqual((registro.account_payment_id, registro.payment_move_id),
                         (pago, self.env['account.move']))
        self.assertEqual(registro.dedupe_key, f"ppd_sin_rep:0:{compra.id}:0:pago:{pago.id}")
        self.assertIn("aún no tiene asiento", registro.observation)

        pagar_con_extracto(self, compra, 1160.0, '2026-10-17')
        self.assertEqual(pago.state, _estado_de_pago_conciliado(self.env))
        self._revisar()
        self.assertEqual(self._registros(compra), registro)
        self.assertEqual((registro.state, registro.fingerprint), ('open', 'fecha=2026-10-15'))
        self.assertNotIn("Cambiaron los valores", self._mensajes(registro))

    def test_pago_agrupado_sin_asiento_y_el_banco_contra_sus_compras_no_cambia_el_registro(self):
        """Pago sin asiento de dos compras y el banco conciliado contra las dos,
        sin payment_lines_ids (en 18 no existe): sigue siendo el mismo registro
        de cada compra, con su revisión, esté el pago «en proceso» (18) o ya
        conciliado. No aparece otro por el movimiento del banco, y el REP
        ligado a ese pago los resuelve."""
        cfdi1, compra1 = self._compra_ppd()
        cfdi2, compra2 = self._compra_ppd()
        compras = compra1 | compra2
        pago = pagar_sin_asiento(self, compras, fecha='2026-10-15', agrupado=True)
        self.assertEqual((len(pago), pago.amount), (1, 2320.0))
        self._revisar()
        registros = self._registros(compras)
        self.assertEqual(sorted(registros.mapped('dedupe_key')), sorted(
            f"ppd_sin_rep:0:{compra.id}:0:pago:{pago.id}" for compra in compras))
        registros.with_user(self.usuario).action_marcar_revisado()

        pagar_con_extracto(self, compras, 2320.0, '2026-10-17')

        def sigue_igual():
            self._revisar()
            self.assertEqual(self._registros(compras), registros)
            self.assertEqual(set(registros.mapped('state')), {'reviewed'})

        sigue_igual()
        if pago.state != _estado_de_pago_conciliado(self.env):
            # Como el lote de pagos de 18, que lo valida al conciliar el banco.
            pago.action_validate()
            self.env.cr.flush()
            sigue_igual()

        rep(self, (2320.0, 'MXN', '1', [docto(cfdi1.uuid, 1160.0), docto(cfdi2.uuid, 1160.0)],
                   {'fecha': '2026-10-15'}))
        self.assertEqual(self._registros(compras), registros)
        self.assertEqual(set(registros.mapped('state')), {'resolved'})
        self.assertIn("Ya tiene su REP", self._mensajes(registros[0]))

    def test_pago_desconciliado_la_resuelve_y_al_volver_se_reabre(self):
        """En 19 un pago en borrador conserva su conciliación, pero ya no está
        aplicado. La compra no tiene REP: lo ve la revisión diaria."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)

        pago.action_draft()
        self.env.cr.flush()
        self._revisar()
        self.assertEqual(registro.state, 'resolved')
        self.assertIn("ya no está aplicado", self._mensajes(registro))

        self._volver_a_publicar(pago, compra)
        self._revisar()
        self.assertEqual(registro.state, 'open')
        self.assertIn("Volvió a aparecer", self._mensajes(registro))
        self.assertEqual(self._registros(compra), registro)

    def test_cambio_de_fecha_del_pago_la_sustituye(self):
        """La fecha es la huella: otra fecha es otro registro, y el anterior se
        resuelve. Con una fecha que todavía está en plazo, se resuelve con el
        motivo del plazo."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)

        pago.action_draft()
        pago.date = '2026-10-01'
        self._volver_a_publicar(pago, compra)
        self._revisar()
        self.assertEqual(registro.state, 'resolved')
        self.assertIn("Cambiaron los valores", self._mensajes(registro))
        nuevo = self._registros(compra, 'open')
        self.assertEqual((nuevo.dedupe_key, nuevo.fingerprint), (registro.dedupe_key, 'fecha=2026-10-01'))

        pago.action_draft()
        pago.date = '2026-11-10'
        self._volver_a_publicar(pago, compra)
        self._revisar()
        self.assertEqual(nuevo.state, 'resolved')
        self.assertIn(f"tiene hasta el {tools.format_date(self.env, date(2026, 12, 5))}", self._mensajes(nuevo))
        self.assertFalse(self._registros(compra, 'open', 'reviewed'))

    # -- El corte --------------------------------------------------------

    def test_sin_parametro_revisa_todo_el_historico(self):
        _cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2024-03-10')
        self.assertEqual(self.Inconsistencia._corte_ppd_sin_rep(), (True, None))
        self._revisar()
        self.assertEqual(self._registros(compra).fingerprint, 'fecha=2024-03-10')
        self.assertFalse(_param(self.env, PARAMETRO_CORTE_PPD), "el corte nunca se fija solo")

    def test_fecha_de_corte(self):
        self._poner_parametro(PARAMETRO_CORTE_PPD, '2026-10-01')
        _cfdi, antes = self._compra_ppd()
        pagar(self, antes, fecha='2026-09-30')
        _cfdi, desde = self._compra_ppd()
        pagar(self, desde, fecha='2026-10-01')
        self._revisar()
        self.assertFalse(self._registros(antes))
        registro = self._registros(desde)
        self.assertEqual(registro.state, 'open')
        en_la_consulta = self._compras_de_la_consulta(date(2026, 10, 1))
        self.assertIn(desde.id, en_la_consulta)
        self.assertNotIn(antes.id, en_la_consulta)

        self._poner_parametro(PARAMETRO_CORTE_PPD, '2026-10-02')
        self._revisar()
        self.assertEqual(registro.state, 'resolved')
        mensajes = self._mensajes(registro)
        self.assertIn("anterior a la fecha de corte", mensajes)
        self.assertIn(PARAMETRO_CORTE_PPD, mensajes)

    def test_parametro_que_no_es_fecha_no_toca_nada(self):
        """Un corte mal escrito no se ignora ni se adivina: no se crea ni se
        resuelve nada de este tipo hasta corregirlo, y queda un aviso."""
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        _cfdi, otra = self._compra_ppd()
        pagar(self, otra, fecha='2026-10-16')
        pago.action_draft()
        self.env.cr.flush()

        self._poner_parametro(PARAMETRO_CORTE_PPD, '01/10/2026')
        with self.assertLogs('odoo.addons.dsi_cfdi_vault.models.ppd_sin_rep', level='WARNING') as log:
            self._revisar()
        self.assertEqual(len(log.records), 1)
        self.assertIn("01/10/2026", log.output[0])
        self.assertEqual(registro.state, 'open', "no se resolvió")
        self.assertFalse(self._registros(otra), "ni se creó otra")

    # -- Revisión y pistas ----------------------------------------------

    def test_revisada_sigue_revisada_y_se_resuelve_con_su_revisor(self):
        cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        registro.with_user(self.usuario).action_marcar_revisado()
        self._revisar()
        self.assertEqual(registro.state, 'reviewed')
        self.assertEqual(registro.reviewed_by_id, self.usuario)

        rep_doc = self._rep_de(cfdi, 1160.0, '2026-10-15', ligar=False)
        self._ligar(rep_doc.dsi_payment_line_ids, compra, pago)
        self.assertEqual(registro.state, 'resolved')
        mensajes = self._mensajes(registro)
        self.assertIn("Ya tiene su REP", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario.name}", mensajes)

    def test_rep_sin_ligar_lo_dice_en_el_mismo_registro(self):
        """Llega un REP de la compra que la liga no puede asignar (otro
        importe). Es una pista, no otro hallazgo: el mismo registro, revisado
        o no, dice en su observación que hay un REP sin ligar y que basta con
        «Elegir pago». Sigue revisado; al elegir el pago, se resuelve."""
        cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)
        registro.with_user(self.usuario).action_marcar_revisado()

        rep_doc = self._rep_de(cfdi, 500.0, '2026-10-15', saldo_anterior=1160.0)
        linea = rep_doc.dsi_payment_line_ids
        self.assertIn(linea.payment_link_state, ESTADOS_PENDIENTES)
        self.assertEqual(self._registros(compra), registro, "no nace otro registro")
        self.assertRecordValues(registro, [{
            'state': 'reviewed',
            'reviewed_by_id': self.usuario.id,
            'fingerprint': 'fecha=2026-10-15',
            'vault_value': "PPD, con REP sin ligar a un pago",
        }])
        self.assertIn(rep_doc.uuid, registro.observation)
        self.assertIn("Elegir pago", registro.observation)
        self.assertNotIn("Cambiaron los valores", self._mensajes(registro))

        asistente = self.env['dsi.cfdi.rep.pago.wizard'].with_user(self.usuario).browse(
            linea.with_user(self.usuario).action_elegir_pago()['res_id'])
        asistente.option_ids.filtered(lambda o: o.account_payment_id == pago).action_elegir()
        self.assertEqual((linea.payment_link_state, linea.account_payment_id), ('manual', pago))
        self.assertEqual(registro.state, 'resolved')
        mensajes = self._mensajes(registro)
        self.assertIn("Ya tiene su REP", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario.name}", mensajes)

    def test_rep_sin_ligar_de_un_pago_no_toca_los_registros_de_los_demas(self):
        """Dos parcialidades sin REP y llega el de la segunda con un importe
        que no cuadra. El registro revisado de la primera sigue siendo el
        mismo y revisado: ni se sustituye al llegar el REP, ni se reabre como
        pendiente (sin su revisión) cuando el contador elige el pago de la
        segunda. Con doce parcialidades, cada REP sin ligar revolvía las
        otras once."""
        cfdi, compra = self._compra_ppd()
        p1 = pagar(self, compra, importe=600.0, fecha='2026-09-15')
        p2 = pagar(self, compra, importe=560.0, fecha='2026-10-15')
        self._revisar()
        r1 = self._registros(compra).filtered(lambda r: r.account_payment_id == p1)
        r2 = self._registros(compra).filtered(lambda r: r.account_payment_id == p2)
        self.assertEqual((r1.state, r2.state), ('open', 'open'))
        r1.with_user(self.usuario).action_marcar_revisado()

        rep_doc = self._rep_de(cfdi, 500.0, '2026-10-15', saldo_anterior=560.0, ligar=False)
        linea = rep_doc.dsi_payment_line_ids
        self._dejar_sin_ligar(linea, compra)
        self.assertEqual(self._registros(compra), r1 | r2, "no nace ninguno")
        self.assertRecordValues(r1, [{
            'state': 'reviewed', 'reviewed_by_id': self.usuario.id, 'fingerprint': 'fecha=2026-09-15',
        }])
        self.assertRecordValues(r2, [{'state': 'open', 'fingerprint': 'fecha=2026-10-15'}])
        for registro in r1 | r2:
            self.assertIn(rep_doc.uuid, registro.observation)
            self.assertIn("Elegir pago", registro.observation)

        self._ligar(linea, compra, p2)
        self.assertEqual(r2.state, 'resolved')
        self.assertIn("Ya tiene su REP", self._mensajes(r2))
        self.assertEqual(self._registros(compra), r1 | r2)
        self.assertRecordValues(r1, [{'state': 'reviewed', 'reviewed_by_id': self.usuario.id}])
        self.assertNotIn(rep_doc.uuid, r1.observation, "la pista ya no está")
        self.assertIn("Pídeselo al proveedor", r1.observation)
        mensajes = self._mensajes(r1)
        self.assertNotIn("Cambiaron los valores", mensajes)
        self.assertNotIn("Volvió a aparecer", mensajes)

    def test_rep_en_otra_empresa_del_mismo_rfc_lo_dice(self):
        """El REP está en la Bóveda de una sucursal sin RFC propio (recibe con
        el de la matriz) y la compra en la matriz: no se liga, porque la liga
        es dentro de la empresa. La observación dice dónde está."""
        sucursal = self.env['res.company'].create({
            'name': "Sucursal Norte", 'parent_id': self.company.id, 'country_id': self.env.ref('base.mx').id,
        })
        self.env.user.company_ids |= sucursal
        cfdi, compra = self._compra_ppd()
        pagar(self, compra, fecha='2026-10-15')
        rep_doc = guardar(self, rep_xml(self._uuid(), [
            (1160.0, 'MXN', '1', [docto(cfdi.uuid, 1160.0)], {'fecha': '2026-10-15'})]), company=sucursal)
        rep_doc._ligar_reps()
        self.assertTrue(rep_doc.dsi_payment_line_ids.invoice_in_other_company)

        self._revisar()
        registro = self._registros(compra)
        self.assertEqual(registro.fingerprint, 'fecha=2026-10-15', "no es un REP sin ligar de la compra")
        self.assertIn(f"La Bóveda de {sucursal.name} tiene REP de esta factura", registro.observation)
        self.assertIn(f"impórtalo también en {self.company.name}", registro.observation)

    # -- Quién ve qué -----------------------------------------------------

    def test_cada_empresa_ve_solo_las_suyas(self):
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        registro = self._registros(compra)

        empresa_b = self.env['res.company'].create({'name': "Empresa B de prueba (PPD)"})
        usuario_b = new_test_user(
            self.env, login='boveda-ppd-b', groups=GRUPOS_BOVEDA,
            company_id=empresa_b.id, company_ids=[Command.set(empresa_b.ids)])
        de_b = self.Inconsistencia.with_user(usuario_b).with_context(allowed_company_ids=empresa_b.ids)
        self.assertNotIn(registro, de_b.search([('kind', '=', 'ppd_sin_rep')]))
        with self.assertRaises(AccessError):
            registro.with_user(usuario_b).with_context(allowed_company_ids=empresa_b.ids).read(['state'])

        del_usuario = registro.with_user(self.usuario)
        valores = del_usuario.read(['account_payment_id', 'payment_move_id', 'display_name'])[0]
        self.assertEqual(valores['account_payment_id'][0], pago.id)
        self.assertEqual(valores['payment_move_id'][0], pago.move_id.id)

    def test_la_compra_no_queda_por_revisar(self):
        """Su asiento está bien: lo que falta es un documento del proveedor
        sobre otro asiento, el del pago."""
        _cfdi, compra = self._compra_ppd()
        self._revisar_factura(compra)
        pagar(self, compra, fecha='2026-10-15')
        self._revisar()
        self.assertTrue(self._registros(compra))
        self.assertFalse(self._por_revisar(compra))

    def test_filtros_menu_y_boton_del_asiento_del_pago(self):
        _cfdi, compra = self._compra_ppd()
        pago = pagar(self, compra, fecha='2026-10-15')
        _cfdi, sin_pagar = self._compra_ppd()
        self._revisar()
        registro = self._registros(compra)

        Factura = self.env['account.move'].with_user(self.usuario)
        vista = self.env.ref('account.view_account_invoice_filter')
        arch = etree.fromstring(Factura.get_view(vista.id, 'search')['arch'])
        filtro = arch.xpath("//filter[@name='filter_dsi_ppd_sin_rep']")
        self.assertTrue(filtro)
        dominio = safe_eval(filtro[0].get('domain'))
        self.assertEqual(Factura.search(dominio + [('id', 'in', (compra | sin_pagar).ids)]), compra)
        registro.action_marcar_revisado()
        self.assertEqual(Factura.search(dominio + [('id', 'in', compra.ids)]), compra, "también las revisadas")

        accion = self.env.ref('dsi_cfdi_vault.action_dsi_cfdi_inconsistency_ppd_sin_rep')
        self.assertEqual(self.env.ref('dsi_cfdi_vault.menu_dsi_cfdi_inconsistency_ppd_sin_rep').action, accion)
        self.assertEqual(self.Inconsistencia.with_user(self.usuario).search(safe_eval(accion.domain)), registro)
        busqueda = etree.fromstring(self.Inconsistencia.with_user(self.usuario).get_view(
            accion.search_view_id.id, 'search')['arch'])
        for nombre in safe_eval(accion.context):
            self.assertTrue(busqueda.xpath(f"//filter[@name='{nombre.removeprefix('search_default_')}']"), nombre)

        asiento = pago.move_id.with_user(self.usuario)
        self.assertEqual(asiento.dsi_inconsistency_total, 1)
        self.assertEqual(self.Inconsistencia.with_user(self.usuario).search(
            asiento.action_view_inconsistencies()['domain']), registro)
        self.assertEqual(compra.with_user(self.usuario).dsi_inconsistency_total, 1, "la compra sigue contándola")

        sin_boveda = Factura.with_user(self.facturacion)
        vistas = sin_boveda.get_views([(False, 'form')])
        campos = vistas['models']['account.move']
        campos = campos.get('fields', campos)
        self.assertFalse([campo for campo in campos if campo.startswith('dsi_inconsistency')])
        compra.with_user(self.facturacion).read(['name', 'payment_state'])
        arch = etree.fromstring(sin_boveda.get_view(vista.id, 'search')['arch'])
        self.assertFalse(arch.xpath("//filter[@name='filter_dsi_ppd_sin_rep']"))

    # -- La revisión diaria ----------------------------------------------

    def test_la_consulta_no_deja_fuera_ninguna_y_el_trigger_del_dia_6_no_se_duplica(self):
        """La consulta de la revisión diaria es una criba: puede traer de más,
        nunca de menos. Y si hay pagos por vencer, la revisión se programa una
        sola vez para el día 6 a las 00:05 de México."""
        detectores = self.Inconsistencia._detectores()
        self.assertEqual(detectores[-1], '_detectar_ppd_sin_rep')
        self.assertLess(detectores.index('_revisar_ligas_con_pagos'), detectores.index('_detectar_ppd_sin_rep'))

        _cfdi, sin_rep = self._compra_ppd()
        pagar(self, sin_rep, fecha='2026-09-15')
        cfdi, cubierta = self._compra_ppd()
        pagar(self, cubierta, fecha='2026-09-15')
        self._rep_de(cfdi, 1160.0, '2026-09-15')
        _cfdi, con_nota = self._compra_ppd()
        self._nota_de_credito(con_nota, 1160.0, '2026-09-15')
        _cfdi, por_el_banco = self._compra_ppd()
        pagar_con_extracto(self, por_el_banco, 1160.0, '2026-09-25')
        _cfdi, pue = self._compra_ppd(metodo='PUE')
        pagar(self, pue, fecha='2026-09-15')
        _cfdi, sin_asiento = self._compra_ppd()
        pagar_sin_asiento(self, sin_asiento, fecha='2026-09-20')

        antes = self._triggers()
        self._revisar('2026-11-03 18:00:00')
        self.assertEqual(self._triggers(), antes, "sin pagos por vencer no se programa nada")
        en_la_consulta = self._compras_de_la_consulta()
        hallazgos = self.Inconsistencia.search([('kind', '=', 'ppd_sin_rep'), ('state', '=', 'open')]).move_id
        self.assertEqual(hallazgos, sin_rep | por_el_banco | sin_asiento)
        self.assertLessEqual(set(hallazgos.ids), en_la_consulta)
        self.assertFalse({cubierta.id, con_nota.id, pue.id} & en_la_consulta)

        _cfdi, reciente = self._compra_ppd()
        pagar(self, reciente, fecha='2026-11-02')
        dia_6 = datetime(2026, 11, 6, 6, 5)
        self._revisar('2026-11-03 18:00:00')
        self.assertEqual(self._triggers(dia_6), 1)
        self._revisar('2026-11-03 20:00:00')
        self.assertEqual(self._triggers(dia_6), 1, "no se duplica")
        self.assertFalse(self._registros(reciente))

        # Ya en el día 6 de México, el siguiente es el del mes que sigue.
        self.Inconsistencia._programar_vencimiento_de_reps(date(2026, 11, 6))
        self.assertEqual(self._triggers(datetime(2026, 12, 6, 6, 5)), 1)

    def test_una_tanda_que_falla_no_deshace_las_demas(self):
        """Cada tanda de la revisión diaria va en su savepoint: una compra que
        falla deshace solo su tanda, con un aviso en el log, y las demás
        quedan. Los registros de la tanda que falló siguen como estaban: no
        se resuelven por no haberse visto."""
        _cfdi, a = self._compra_ppd()
        pagar(self, a, fecha='2026-10-15')
        _cfdi, b = self._compra_ppd()
        pago_b = pagar(self, b, fecha='2026-10-15')
        _cfdi, c = self._compra_ppd()
        pagar(self, c, fecha='2026-10-15')
        self._revisar()
        antes = self._registros(a | b | c)
        self.assertEqual(antes.mapped('state'), ['open'] * 3)

        # Lo que vería la revisión: el pago de B ya no está aplicado (se
        # resolvería) y D tiene un pago sin REP (nacería).
        pago_b.action_draft()
        self.env.cr.flush()
        _cfdi, d = self._compra_ppd()
        pagar(self, d, fecha='2026-10-15')

        original = type(self.env['account.move'])._dsi_cfdi_ppd

        def falla_en_b(compra):
            if compra.id == b.id:
                raise ValueError("falla forzada en la compra B")
            return original(compra)

        with patch.object(ppd_sin_rep, 'LOTE_DE_COMPRAS_PPD', 1), \
                patch.object(type(self.env['account.move']), '_dsi_cfdi_ppd', falla_en_b), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.ppd_sin_rep', level='WARNING') as log:
            self._revisar()
        self.assertEqual(len(log.records), 1)
        self.assertEqual(log.records[0].levelname, 'WARNING')
        self.assertIn(str(b.id), log.output[0])
        self.assertIn("falla forzada", log.output[0])
        self.assertEqual(antes.mapped('state'), ['open'] * 3, "ninguno se resolvió en esa corrida")
        self.assertEqual(len(self._registros(d, 'open')), 1, "las demás tandas quedan")

        # Ya sin la falla, la siguiente revisión resuelve el de B.
        self._revisar()
        self.assertEqual(self._registros(b).state, 'resolved')
        self.assertIn("ya no está aplicado", self._mensajes(self._registros(b)))

    def test_la_cache_no_crece_de_tanda_en_tanda(self):
        """La revisión diaria suelta la caché del ORM al terminar cada tanda:
        sin esto, con miles de compras, la memoria crecía con cada una."""
        compras = self.env['account.move']
        for _i in range(3):
            _cfdi, compra = self._compra_ppd()
            pagar(self, compra, fecha='2026-10-15')
            compras |= compra
        original = type(self.Inconsistencia)._sincronizar_ppd_sin_rep
        al_empezar = []

        def midiendo(modelo, facturas, source):
            al_empezar.append((facturas, self._en_cache(compras, 'move_type')))
            return original(modelo, facturas, source)

        self.env.invalidate_all()
        with patch.object(ppd_sin_rep, 'LOTE_DE_COMPRAS_PPD', 1), \
                patch.object(type(self.Inconsistencia), '_sincronizar_ppd_sin_rep', midiendo):
            self._revisar()
        self.assertEqual(len(al_empezar), 3)
        anteriores = self.env['account.move']
        for tanda, en_cache in al_empezar:
            self.assertFalse(en_cache & anteriores, "la tanda empieza con las compras de las anteriores en la caché")
            anteriores |= tanda
        self.assertEqual(len(self._registros(compras)), 3)
