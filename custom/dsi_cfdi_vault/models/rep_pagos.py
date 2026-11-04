"""Los pagos (REP) recibidos ligados a su pago de Odoo.

Antes el REP solo se ligaba a la factura que paga: en el pago de Odoo no se
veía nada, y con varios pagos en una factura nadie decía cuál era el del REP.
Ahora cada documento relacionado de un REP recibido se liga al pago de Odoo
que aplicó ese mismo importe a su factura (un account.payment, o el
movimiento del banco conciliado directo con ella), el XML del REP queda
también en ese pago, y lo ambiguo queda «Por elegir» para que lo decida el
contador con «Elegir pago».

La liga es por documento relacionado y no por nodo Pago: un pago agrupado de
Odoo aplica a cada factura su parcial, y un pago que Odoo dividió en uno por
factura tiene un parcial en cada una; los dos casos se resuelven igual. Si
Odoo repartió el pago entre las facturas distinto que el proveedor (una nota
de crédito, un descuento por pronto pago), cuadra el total del pago con el
Monto del REP: son las reglas de respaldo, que solo ligan si no hay duda.

Qué es un pago aplicado lo dice `account.move._dsi_pagos_aplicados`, con la
regla de l10n_mx_edi (`_l10n_mx_edi_is_cfdi_payment`): un asiento con
origin_payment_id o statement_line_id. Las notas de crédito, compensaciones,
diferencias cambiarias y asientos manuales reducen el saldo, pero no son
pagos. La misma ayuda la usa la discrepancia de compras PPD pagadas sin REP.

La liga se mantiene al día sola: al importar o volver a ligar un REP, al
cancelarlo, y cuando la contabilidad cambia (pagos, conciliaciones), con un
precommit que solo junta ids y religa una vez al confirmar la transacción. Lo
que se escape lo recoge la revisión diaria.
"""
import logging
from collections import Counter, defaultdict
from datetime import date

from odoo import api, fields, models, tools
from odoo.exceptions import UserError
from odoo.tools import SQL, float_compare, split_every

from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param

from .cfdi_conceptos import _bytes_de_adjunto
from .cfdi_document import ERRORES_DE_CONCURRENCIA
from .cfdi_payment_line import LOTE_DE_REPS

_logger = logging.getLogger(__name__)

# Cinco centavos, la misma tolerancia que la conciliación por importe
# (MATCH_AMOUNT_TOLERANCE) y que el total al convertir (TOLERANCIA_TOTAL): el
# REP lleva dos decimales y el pago de Odoo puede haber redondeado distinto.
TOLERANCIA_DE_IMPORTE = 0.05

# Días entre la FechaPago del REP y la fecha del pago en Odoo para ligar solo.
# Un SPEI va el mismo día; un cheque o un pago registrado tarde, unos días; y
# dos parcialidades mensuales del mismo importe quedan separadas. Lo que cuadra
# fuera de la ventana queda «Por elegir», salvo que el REP tenga a mano ese
# «Asiento del pago».
VENTANA_DE_DIAS = 10
# Opcional, un entero (días) para cambiar la ventana sin actualizar el módulo.
PARAMETRO_VENTANA = 'dsi_cfdi_vault.rep_pago_ventana_dias'

# Los estados con pago. «A mano» lo eligió el contador y la Bóveda no lo
# cambia mientras ese pago siga aplicado a la factura.
ESTADOS_LIGADOS = ('linked', 'manual')
# Los que esperan algo (un pago, o que el contador elija): la revisión diaria
# los vuelve a ver cuando la factura ya tiene algo pagado.
ESTADOS_PENDIENTES = ('to_choose', 'amount_mismatch', 'no_payment')

# FormaDePagoP sin movimiento del banco (c_FormaPago del SAT): dación en pago,
# subrogación, consignación, condonación, compensación, novación, confusión,
# remisión de deuda, prescripción y a satisfacción del acreedor. Sin pago que
# cuadre no es «Sin pago en Odoo»: no aplica. Si alguno cuadra, se liga.
FORMAS_SIN_PAGO = {'12', '13', '14', '15', '17', '23', '24', '25', '26', '27'}

# Los estados de account.payment que no aplican nada. Los demás se calculan con
# la selección (_estados_de_pago_aplicado): en 18 y 19 son in_process y paid;
# en 20, paid y reconciled.
ESTADOS_DE_PAGO_SIN_APLICAR = ('draft', 'canceled', 'rejected')

# La cola del precommit: ids de asientos y de pagos que cambiaron en la
# transacción; se religa una sola vez, al confirmarla.
COLA_DE_RELIGADO = 'dsi_cfdi_vault.religar_pagos'

# Compras por consulta al buscar sus pagos aplicados: la consulta de parciales
# lleva sus ids en un IN.
LOTE_DE_FACTURAS = 200

# Lo que cambia, en un account.payment, a qué factura aplica y con qué importe
# o fecha: al escribirlo se religan sus REP (AccountPayment.write).
CAMPOS_DEL_PAGO = {'state', 'invoice_ids', 'amount', 'currency_id', 'date', 'move_id'}

# Las notas de crédito que se pagan junto con sus facturas: restan del pago,
# no reciben nada de él (_facturas_del_pago).
REEMBOLSOS = ('in_refund', 'out_refund')


def _estados_de_pago_aplicado(env):
    """Los estados de account.payment de un pago que ya aplicó a la factura,
    de la selección y no fijos: en 20 desapareció in_process y apareció
    reconciled."""
    return tuple(
        valor for valor, _e in env['account.payment']._fields['state']._description_selection(env)
        if valor not in ESTADOS_DE_PAGO_SIN_APLICAR)


def _estado_de_pago_conciliado(env):
    """El estado de un pago sin asiento que ya se concilió con el banco:
    'reconciled' en 20, 'paid' en 18 y 19."""
    valores = [valor for valor, _e in env['account.payment']._fields['state']._description_selection(env)]
    return 'reconciled' if 'reconciled' in valores else 'paid'


def _tolerancia(moneda):
    return max(moneda.rounding, TOLERANCIA_DE_IMPORTE) if moneda else TOLERANCIA_DE_IMPORTE


def _facturas_del_pago(pago, factura=None):
    """Las facturas publicadas que paga el account.payment `pago`, sin las
    notas de crédito que se pagaron con ellas: una factura de 1,000 con su nota
    de 100 se paga con 900, y el pago es de una sola factura. Con `factura`,
    esa cuenta aunque sea una nota de crédito (un pago que solo la devuelve)."""
    return pago.invoice_ids.filtered(
        lambda f: f.state == 'posted' and (f.move_type not in REEMBOLSOS or f == factura))


def _de_una_sola_factura(pago, factura):
    """Si el pago completo es de `factura`: es la única factura que paga, sin
    contar las notas de crédito que se pagaron con ella. Un pago que solo
    devuelve notas de crédito es de una sola si devuelve solo esa; si devuelve
    dos, no se sabe cuánto le tocó a cada una. Un pago en sentido contrario al
    de la factura (el proveedor devuelve porque la nota es mayor) no es suyo."""
    if pago.payment_type != ('outbound' if factura.is_outbound() else 'inbound'):
        return False
    facturas = _facturas_del_pago(pago)
    if facturas:
        return facturas == factura
    return pago.invoice_ids.filtered(lambda f: f.state == 'posted') == factura


def _cuadra(importe, otro, tolerancia):
    """Si la diferencia está dentro de la tolerancia, comparada a seis
    decimales: en binario 1,000 - 999.95 da 0.0500000000001 y quedaba fuera."""
    return float_compare(abs(importe - otro), tolerancia, precision_digits=6) <= 0


def _es_su_movimiento_del_banco(e, pago, factura):
    """Si el movimiento del banco conciliado directo `e` (de
    _dsi_pagos_aplicados, sin account.payment) es el del pago sin asiento
    `pago` de `factura`: de una sola factura, le aplicó el importe del pago;
    de varias, está conciliado con todas las facturas del pago y les aplicó
    en total su importe, en la moneda del pago."""
    tolerancia = _tolerancia(pago.currency_id)
    if e['moneda_pago'] != pago.currency_id:
        return False
    if _de_una_sola_factura(pago, factura):
        return _cuadra(e['importe_pago'], pago.amount, tolerancia)
    facturas = _facturas_del_pago(pago)
    if len(facturas) < 2 or factura not in facturas:
        return False
    lineas = e['asiento'].line_ids
    aplicado, tocadas = 0.0, facturas.browse()
    for parcial in lineas.matched_debit_ids | lineas.matched_credit_ids:
        if parcial.debit_move_id in lineas:
            otra, importe, moneda = parcial.credit_move_id, parcial.debit_amount_currency, parcial.debit_currency_id
        else:
            otra, importe, moneda = parcial.debit_move_id, parcial.credit_amount_currency, parcial.credit_currency_id
        if otra.move_id in facturas:
            if moneda != pago.currency_id:
                return False
            aplicado += importe
            tocadas |= otra.move_id
    return tocadas == facturas and _cuadra(aplicado, pago.amount, tolerancia)


def _le_queda_saldo_para_el_pago(pago, factura):
    """Si a las facturas del pago sin asiento `pago` (de `factura`) todavía
    les queda saldo para todo su importe, o no se sabe (otra moneda): el
    pago aún puede aplicarles algo al conciliarlo, así que un movimiento del
    banco ya conciliado con ellas puede ser otro pago."""
    facturas = _facturas_del_pago(pago) or factura
    if any(f.currency_id != pago.currency_id for f in facturas):
        return True
    saldos = sum(abs(f.amount_residual) for f in facturas)
    return float_compare(saldos, pago.amount - _tolerancia(pago.currency_id), precision_digits=6) >= 0


def _env_de_la_empresa(env, empresa):
    """El entorno en el idioma de la empresa. Las notas de la liga se guardan y
    se comparan para no reescribir lo que no cambió: en el idioma de quien
    religa, la revisión diaria y el usuario se las reescribían uno al otro."""
    return env(context=dict(env.context, lang=tools.get_lang(env, empresa.partner_id.lang).code))


class CfdiPaymentLine(models.Model):
    _inherit = 'dsi.cfdi.payment.line'

    # La liga con el pago. Solo la escribe la Bóveda (_ligar_con_sus_pagos) o
    # el asistente «Elegir pago», siempre con sudo.
    account_payment_id = fields.Many2one(
        'account.payment', string="Pago en Odoo", ondelete='set null', index='btree_not_null',
        readonly=True, copy=False,
        help="El pago de Odoo que ampara este documento relacionado. Vacío si el pago es un movimiento "
             "del banco conciliado directo con la factura.")
    payment_move_id = fields.Many2one(
        'account.move', string="Asiento del pago", ondelete='set null', index='btree_not_null',
        readonly=True, copy=False,
        help="El asiento del pago o del movimiento del banco. Vacío si el pago todavía no tiene asiento.")
    # Sin valor por omisión: vacío es «sin evaluar», y es lo que toma el llenado
    # al actualizar (_backfill_ligas_con_pagos).
    payment_link_state = fields.Selection([
        ('linked', "Ligado"),
        ('manual', "Ligado a mano"),
        ('to_choose', "Por elegir"),
        ('amount_mismatch', "Importe distinto"),
        ('no_payment', "Sin pago en Odoo"),
        ('not_applicable', "No aplica"),
    ], string="Liga con el pago", index=True, readonly=True, copy=False,
        help="Ligado: un solo pago de Odoo aplicó a la factura lo que dice el REP, en una fecha cercana (o, si "
             "Odoo repartió el pago entre las facturas distinto que el REP, el total del pago es el monto del REP). "
             "Ligado a mano: lo eligió el contador con «Elegir pago». "
             "Por elegir: varios pagos cuadran, el que cuadra ya es de otro REP o su fecha está lejos de "
             "la del REP. "
             "Importe distinto: la factura tiene pagos, pero ninguno aplicó lo que dice el REP. "
             "Sin pago en Odoo: la factura todavía no tiene pagos. "
             "No aplica: REP emitido, cancelado o archivado, sin factura publicada en Odoo, o una forma de "
             "pago sin movimiento del banco (compensación, condonación…).")
    payment_link_note = fields.Char(string="Motivo", readonly=True, copy=False)
    invoice_currency_id = fields.Many2one(
        related='invoice_id.currency_id', string="Moneda de la factura en Odoo")
    payment_amount_applied = fields.Monetary(
        string="Aplicado en Odoo", currency_field='invoice_currency_id', readonly=True, copy=False,
        help="Lo que ese pago aplicó a la factura en Odoo, en la moneda de la factura.")
    payment_attachment_id = fields.Many2one(
        'ir.attachment', string="Copia del XML en el pago", ondelete='set null', readonly=True, copy=False)
    # El nombre del pago que eligió el contador, mientras la liga es a mano:
    # si el pago se borra, la liga queda vacía (ondelete set null) y el mensaje
    # de liga perdida lo nombra con esto.
    manual_payment_name = fields.Char(string="Pago elegido a mano", readonly=True, copy=False)

    # -------------------------------------------------------------------
    # La regla
    # -------------------------------------------------------------------

    def _clave_de_la_liga(self):
        """La clave del pago aplicado al que apunta la línea, como la de
        account.move._dsi_pagos_aplicados: la del account.payment si lo hay;
        si no, la del asiento; None si no tiene liga."""
        self.ensure_one()
        if self.account_payment_id:
            return 'pago:%d' % self.account_payment_id.id
        if self.payment_move_id:
            return 'asiento:%d' % self.payment_move_id.id
        return None

    @api.model
    def _ventana_de_dias(self):
        """Los días de PARAMETRO_VENTANA, o VENTANA_DE_DIAS si no está. Un
        valor que no es un entero no detiene nada: se usa la ventana de
        siempre y queda en el log (info: no es una falla de la Bóveda)."""
        valor = (_param(self.env, PARAMETRO_VENTANA) or '').strip()
        if not valor:
            return VENTANA_DE_DIAS
        try:
            dias = int(valor)
        except ValueError:
            dias = -1
        if dias >= 0:
            return dias
        _logger.info(
            "El parámetro %s tiene «%s», que no es un número de días: se usan %s.",
            PARAMETRO_VENTANA, valor, VENTANA_DE_DIAS)
        return VENTANA_DE_DIAS

    def _regla_del_importe(self, e, respaldo=False):
        """Por qué cuadra esta línea con el pago aplicado `e` (un dict de
        _dsi_pagos_aplicados de la factura de la línea), o None si no cuadra:

        - 'factura': ImpPagado contra lo que el pago aplicó a la factura, en la
          moneda de la factura.
        - 'pago': ImpPagado / EquivalenciaDR contra lo mismo en la moneda del
          lado del pago. Un pago en pesos de una factura en dólares cuadra
          aunque el tipo de cambio de Odoo no sea el del REP. La tolerancia
          suma el redondeo de ImpPagado (dos decimales) llevado a MonedaP.
        - 'nodo': un pago sin asiento de varias facturas (con el módulo
          accountant), del que Odoo no dice cuánto aplicó a cada una: cuadra si
          el Monto del nodo es el del pago y el nodo paga exactamente sus
          mismas facturas (sin las notas de crédito que van en el pago).

        Pagos 1.0 sin ImpPagado (en cero) solo puede cuadrar por el nodo.

        Con `respaldo`, para cuando Odoo repartió el pago distinto que el REP.
        Quien llama lo pide solo si ningún pago de la factura cuadra por las de
        arriba (_elegir_pagos), y nunca liga si más de un pago cumple el
        respaldo:

        - 'monto': el Monto del nodo es el total del account.payment, en su
          moneda, y todas las facturas del nodo están en Odoo y ese pago les
          aplicó algo (_facturas_que_toco). Un pago agrupado que aplicó una nota de crédito a otra factura
          que el proveedor, o un pago con quebranto o descuento por pronto
          pago que salda más de lo que se pagó.
        - 'saldo': un pago sin asiento de varias facturas que alcanza para el
          saldo de todas: al conciliarlo, Odoo le aplicará a cada una su saldo,
          y eso se compara como en 'factura' y 'pago' (_saldo_que_cubre)."""
        self.ensure_one()
        regla = self._regla_de_lo_aplicado(e['importe'], e['moneda'], e['importe_pago'], e['moneda_pago'])
        if regla:
            return regla
        if (e['sin_asiento'] and e['importe'] is None and self.currency_p_id
                and self.currency_p_id == e['pago'].currency_id
                and _cuadra(self.monto_pago, e['pago'].amount, _tolerancia(self.currency_p_id))):
            del_nodo = self._lineas_del_nodo().invoice_id
            if del_nodo and _facturas_del_pago(e['pago']) == del_nodo:
                return 'nodo'
        if not respaldo:
            return None
        if self._cuadra_por_el_monto_del_pago(e):
            return 'monto'
        saldo = self._saldo_que_cubre(e)
        if saldo is not None and self._regla_de_lo_aplicado(saldo, e['moneda'], saldo, e['pago'].currency_id):
            return 'saldo'
        return None

    def _regla_de_lo_aplicado(self, importe, moneda, importe_pago, moneda_pago):
        """'factura' o 'pago' de _regla_del_importe: ImpPagado contra lo que se
        aplicó a la factura, en su moneda (`importe`) o en la del lado del pago
        (`importe_pago`). None si no cuadra o no se sabe (en None)."""
        if not self.imp_pagado:
            return None
        if (importe is not None and self.currency_id and self.currency_id == moneda
                and _cuadra(self.imp_pagado, importe, _tolerancia(moneda))):
            return 'factura'
        if (importe_pago is not None and self.currency_p_id and self.currency_p_id == moneda_pago
                and self.equivalencia_dr > 0):
            en_moneda_del_pago = self.imp_pagado / self.equivalencia_dr
            tolerancia = _tolerancia(self.currency_p_id) + 0.005 / self.equivalencia_dr
            if _cuadra(en_moneda_del_pago, importe_pago, tolerancia):
                return 'pago'
        return None

    def _lineas_del_nodo(self):
        """Las líneas del mismo nodo Pago del REP, esta incluida."""
        self.ensure_one()
        return self.payment_doc_id.dsi_payment_line_ids.filtered(lambda linea: linea.pago_idx == self.pago_idx)

    def _cuadra_por_el_monto_del_pago(self, e):
        """El respaldo 'monto' de _regla_del_importe. Sin factura en Odoo para
        algún documento del nodo no cuadra: el Monto incluiría algo que el
        pago de Odoo no paga."""
        pago = e['pago']
        if not (pago and self.currency_p_id and self.currency_p_id == pago.currency_id
                and _cuadra(self.monto_pago, pago.amount, _tolerancia(pago.currency_id))):
            return False
        del_nodo = self._lineas_del_nodo()
        return all(linea.invoice_id for linea in del_nodo) and del_nodo.invoice_id <= self._facturas_que_toco(e)

    def _facturas_que_toco(self, e):
        """Las facturas del pago aplicado `e` a las que el pago sí les aplicó
        algo: «Registrar pago» deja en invoice_ids todas las elegidas aunque a
        alguna no le toque nada, y el REP de otro pago del mismo importe no debe
        cuadrar con este. Con asiento (el del pago o el del banco), las
        conciliadas con él. Un pago sin asiento aún sin conciliar le aplicará
        algo a todas solo si alcanza para el saldo de todas; si no, no se sabe a
        cuáles y no cuenta ninguna."""
        pago = e['pago']
        facturas = _facturas_del_pago(pago)
        if e['asiento']:
            lineas = e['asiento'].line_ids
            return facturas & (lineas.matched_debit_ids.debit_move_id | lineas.matched_credit_ids.credit_move_id).move_id
        if any(f.currency_id != pago.currency_id for f in facturas):
            return facturas.browse()
        saldos = sum(abs(f.amount_residual) for f in facturas)
        if float_compare(saldos, pago.amount + _tolerancia(pago.currency_id), precision_digits=6) > 0:
            return facturas.browse()
        return facturas

    def _saldo_que_cubre(self, e):
        """El respaldo 'saldo' de _regla_del_importe: lo que el pago sin asiento
        `e` de varias facturas le aplicará a la de esta línea al conciliarlo,
        su saldo, si alcanza para el saldo de todas. None si no se sabe: ya se
        concilió (el saldo ya no lo dice), hay otra moneda, o alguna de sus
        facturas tiene otro pago sin asiento pendiente (no se sabe cómo
        repartirá Odoo el saldo entre los dos)."""
        pago, factura = e['pago'], self.invoice_id
        conciliado = _estado_de_pago_conciliado(self.env)
        if not (e['sin_asiento'] and e['importe'] is None and pago and factura) or pago.state == conciliado:
            return None
        facturas = _facturas_del_pago(pago)
        if factura not in facturas or any(f.currency_id != pago.currency_id for f in facturas):
            return None
        aplicados = _estados_de_pago_aplicado(self.env)
        for f in facturas:
            pendientes = f.matched_payment_ids.filtered(
                lambda p: not p.move_id and p.state in aplicados and p.state != conciliado)
            if pendientes != pago:
                return None
        saldos = sum(abs(f.amount_residual) for f in facturas)
        if float_compare(saldos, pago.amount + _tolerancia(pago.currency_id), precision_digits=6) > 0:
            return None
        return abs(factura.amount_residual)

    def _importe_cuadra(self, e, respaldo=False):
        return bool(self._regla_del_importe(e, respaldo=respaldo))

    def _en_su_ventana(self, e, ventana):
        """Si el pago aplicado `e` está a `ventana` días o menos de la
        FechaPago, o es el «Asiento del pago» que alguien puso a mano en el REP."""
        self.ensure_one()
        rep = self.payment_doc_id
        pista = rep.move_id if rep.move_id.move_type == 'entry' else self.env['account.move']
        return self._dias_a(e) <= ventana or bool(pista and (e['asiento'] == pista or e['pago'].move_id == pista))

    def _dias_a(self, e):
        """Días entre la FechaPago y el pago aplicado `e`: la menor distancia
        a la fecha del pago o a la del movimiento del banco. Sin FechaPago no
        se puede medir y no aleja (0); un pago sin ninguna fecha, sí."""
        self.ensure_one()
        if not self.fecha_pago:
            return 0
        dias = [abs((fecha - self.fecha_pago).days) for fecha in (e['fecha'], e['fecha_banco']) if fecha]
        return min(dias) if dias else 10 ** 6

    def _desempatar(self, candidatos, pista):
        """Los pagos que quedan de `candidatos` (todos cuadran y están en la
        ventana), con estos filtros en orden. Cada uno se aplica solo si deja
        al menos uno:

        1. la pista: el «Asiento del pago» que alguien puso a mano en el REP;
        2. el saldo anterior: el de Odoo justo antes de ese pago contra el
           ImpSaldoAnt del REP (separa dos parcialidades iguales del mismo día);
        3. el Monto del nodo contra el total del pago;
        4. la fecha más cercana, si es una sola;
        5. la liga que ya tenía, para no moverla sin evidencia nueva. Va al
           final a propósito: lo de arriba sí puede corregir una liga.
        """
        self.ensure_one()

        def filtrar(quedan, condicion):
            pasan = [e for e in quedan if condicion(e)]
            return pasan or quedan

        quedan = list(candidatos)
        if len(quedan) > 1 and pista:
            quedan = filtrar(quedan, lambda e: e['asiento'] == pista or e['pago'].move_id == pista)
        if len(quedan) > 1 and self.imp_saldo_ant > 0:
            quedan = filtrar(quedan, lambda e: (
                self.currency_id == e['moneda'] and e['saldo_anterior'] is not None
                and _cuadra(e['saldo_anterior'], self.imp_saldo_ant, _tolerancia(e['moneda']))))
        if len(quedan) > 1:
            quedan = filtrar(quedan, lambda e: (
                e['moneda_monto'] == self.currency_p_id
                and _cuadra(e['monto'], self.monto_pago, _tolerancia(self.currency_p_id))))
        if len(quedan) > 1:
            dias = [self._dias_a(e) for e in quedan]
            if dias.count(min(dias)) == 1:
                quedan = [quedan[dias.index(min(dias))]]
        if len(quedan) > 1:
            actual = self._clave_de_la_liga()
            quedan = filtrar(quedan, lambda e: e['clave'] == actual)
        return quedan

    # -------------------------------------------------------------------
    # Ligar
    # -------------------------------------------------------------------

    def _ligar_con_sus_pagos(self, facturas=None):
        """Deja como deben estar la liga con el pago de estas líneas y de todas
        las demás de sus compras (y de `facturas`, las que tenían antes de un
        cambio de factura): compiten por los mismos pagos. Devuelve las
        facturas evaluadas.

        Escribe solo lo que cambia, en un write por grupo de valores iguales,
        y una línea que pierde su pago pierde también la referencia a su copia
        del XML; la copia se queda en el pago (como en las facturas, nunca se
        borra). Una liga a mano cuyo pago ya no está aplicado vuelve a
        automática con un mensaje en el REP; también la que deja de aplicar
        (factura en borrador, REP cancelado…), con su motivo. La elección del
        contador no se guarda: si vuelve a aplicar, se liga con la regla.

        Lo escribe la Bóveda: con sudo, y sin revisar la licencia (lo llaman
        los ganchos, la revisión diaria y la actualización)."""
        Linea = self.env['dsi.cfdi.payment.line'].sudo().with_context(active_test=False)
        Factura = self.env['account.move'].sudo()
        Pago, Asiento = self.env['account.payment'].sudo(), Factura
        lineas = Linea.browse(self.ids).exists()
        afectadas = (lineas.invoice_id | (facturas or Factura)).sudo().exists()
        compras = afectadas.filtered(lambda f: f.state == 'posted' and f.is_purchase_document(include_receipts=True))
        todas = lineas
        if compras:
            todas |= Linea.search([('invoice_id', 'in', compras.ids)])
        elegibles = todas.filtered(lambda linea: (
            linea.direction == 'received' and linea.invoice_id in compras
            and linea.payment_doc_id.active and linea.payment_doc_id.sat_status != 'canceled'))

        deseado = {}
        for linea in todas - elegibles:
            deseado[linea] = (Pago, Asiento, 'not_applicable', 0.0, self._motivo_no_aplica(linea))
        pagos = {}
        for ids in split_every(LOTE_DE_FACTURAS, compras.ids):
            pagos.update(Factura.browse(ids)._dsi_pagos_aplicados(con_reps=False))
        ventana = self._ventana_de_dias()
        for factura, grupo in elegibles.grouped('invoice_id').items():
            en_su_idioma = Linea.with_env(_env_de_la_empresa(Linea.env, factura.company_id))
            deseado.update(en_su_idioma._elegir_pagos(factura, grupo, pagos.get(factura, []), ventana))

        # Antes de escribir: las ligas a mano que se pierden, sea cual sea su
        # estado nuevo, con el pago que tenían, para decirlo en el REP. Si el
        # pago se borró, la liga ya está vacía: queda su nombre guardado.
        degradadas = [
            (linea, (linea.account_payment_id or linea.payment_move_id).display_name
             or linea.manual_payment_name or self.env._("(pago eliminado)"))
            for linea, (_p, _a, estado, _i, _n) in deseado.items()
            if linea.payment_link_state == 'manual' and estado != 'manual'
        ]
        cambios = defaultdict(lambda: Linea)
        for linea, (pago, asiento, estado, importe, nota) in deseado.items():
            valores = {
                'account_payment_id': pago.id or False,
                'payment_move_id': asiento.id or False,
                'payment_link_state': estado,
                'payment_amount_applied': importe,
                'payment_link_note': nota or False,
            }
            if estado not in ESTADOS_LIGADOS and linea.payment_attachment_id:
                valores['payment_attachment_id'] = False
            if estado != 'manual' and linea.manual_payment_name:
                valores['manual_payment_name'] = False
            if (linea.account_payment_id != pago or linea.payment_move_id != asiento
                    or linea.payment_link_state != estado or (linea.payment_link_note or False) != (nota or False)
                    or float_compare(linea.payment_amount_applied, importe, precision_digits=6)
                    or 'payment_attachment_id' in valores or 'manual_payment_name' in valores):
                cambios[tuple(sorted(valores.items()))] |= linea
        cambiadas = Linea
        for valores, grupo in cambios.items():
            grupo.write(dict(valores))
            cambiadas |= grupo

        for linea, pago_anterior in degradadas:
            if linea in elegibles:
                cuerpo = self.env._(
                    "Se quitó la liga a mano del documento %(folio)s con el pago %(pago)s: ya no está aplicado a "
                    "la factura %(factura)s. La Bóveda la volvió a evaluar.",
                    folio=linea.display_name, pago=pago_anterior, factura=linea.invoice_id.display_name)
            else:
                cuerpo = self.env._(
                    "Se quitó la liga a mano del documento %(folio)s con el pago %(pago)s: %(motivo)s",
                    folio=linea.display_name, pago=pago_anterior, motivo=linea.payment_link_note)
            linea.payment_doc_id.message_post(body=cuerpo)

        sin_copia = elegibles.filtered(
            lambda linea: linea.payment_link_state in ESTADOS_LIGADOS and not linea._copia_al_dia())
        (cambiadas | sin_copia).payment_doc_id._copiar_rep_a_sus_pagos()
        return afectadas

    def _motivo_no_aplica(self, linea):
        """Por qué una línea no se liga con ningún pago (en el idioma de su
        empresa: se guarda como su nota)."""
        env = _env_de_la_empresa(self.env, linea.company_id)
        rep = linea.payment_doc_id
        if linea.direction != 'received':
            return env._("REP emitido: la liga con el pago es solo para los REP recibidos.")
        if not rep.active or rep.sat_status == 'canceled':
            return env._("El REP está cancelado en el SAT o archivado.")
        if not linea.invoice_id:
            return env._("Sin factura en Odoo.")
        return env._("La factura no está publicada.")

    def _elegir_pagos(self, factura, lineas, unidades, ventana):
        """{línea: (pago, asiento, estado, importe, nota)} de las `lineas` de
        un REP vigente de la compra `factura` con sus pagos aplicados
        (`unidades`, de _dsi_pagos_aplicados). En el idioma de este entorno.

        1. Las «a mano» se quedan con su pago mientras siga aplicado a la
           factura (varias pueden compartir uno). Si ya no está, se evalúan
           como automáticas.
        2. Las automáticas, en rondas: cada una propone el pago que le queda
           después de cuadrar el importe, la ventana de días y los desempates.
           Gana la que lo pide sola, o, si varias lo piden, la única que ya lo
           tenía. El pago ganado sale de las demás y se repite hasta que nadie
           gana. Así dos REP no se quedan con el mismo pago.
        3. Las que quedan, con su motivo: sin pagos, importe distinto, el pago
           que cuadra ya lo tiene otro REP, fuera de fecha, o varios cuadran.

        Una línea sin ningún pago que cuadre por las reglas de siempre (Odoo
        repartió el pago distinto que el REP) prueba las de respaldo de
        _regla_del_importe, en las mismas rondas, con la ventana de días y sin
        los pagos ya tomados. Solo con los pagos que ninguna otra línea de la
        factura pide por las reglas de siempre (el respaldo no le quita su pago
        a nadie), y solo si queda uno: con dos o más no desempata, porque ligar
        mal es peor que no ligar, y la línea queda como sin respaldo.
        """
        env = self.env
        Pago, Asiento = self.env['account.payment'].sudo(), self.env['account.move'].sudo()
        moneda = factura.currency_id
        por_clave = {e['clave']: e for e in unidades}
        ocupadas = {}           # clave -> la primera línea que la tiene
        resultado = {}

        def nombre(e):
            return (e['pago'] or e['asiento']).display_name

        def importe_de(e):
            if e['importe'] is not None:
                return tools.format_amount(env, e['importe'], e['moneda'])
            return tools.format_amount(env, e['monto'], e['moneda_monto'])

        def lista(unidades_):
            texto = ", ".join(f"{nombre(e)} {importe_de(e)}" for e in unidades_[:5])
            return texto + (", …" if len(unidades_) > 5 else "")

        def pendiente(estado, nota):
            return (Pago, Asiento, estado, 0.0, nota)

        def ligada(linea, e):
            """La liga, con la regla en la nota cuando no es la de siempre."""
            regla = linea._regla_del_importe(e, respaldo=linea in con_respaldo)
            importe, nota = e['importe'], False
            if regla == 'nodo':
                nota = env._("Pago agrupado sin asiento: cuadra por el monto del nodo.")
            elif regla == 'monto':
                nota = env._(
                    "Cuadra por el monto del pago (%(monto)s): Odoo lo repartió entre las facturas distinto que "
                    "el REP.", monto=tools.format_amount(env, e['pago'].amount, e['pago'].currency_id))
            elif regla == 'saldo':
                importe = linea._saldo_que_cubre(e)
                nota = env._(
                    "Pago sin asiento de varias facturas: cuadra con el saldo de la factura, que es lo que le "
                    "aplicará al conciliarlo.")
            return (e['pago'], e['asiento'], 'linked', moneda.round(importe or 0.0), nota)

        orden = lineas.sorted(lambda linea: (
            linea.fecha_pago or date.min, linea.payment_doc_id.id, linea.pago_idx, linea.docto_idx, linea.id))
        automaticas = []
        for linea in orden:
            if linea.payment_link_state == 'manual':
                e = por_clave.get(linea._clave_de_la_liga()) or next((
                    e for e in unidades
                    if (linea.account_payment_id and e['pago'] == linea.account_payment_id)
                    or (linea.payment_move_id and e['asiento'] == linea.payment_move_id)), None)
                if e:
                    resultado[linea] = (e['pago'], e['asiento'], 'manual', moneda.round(e['importe'] or 0.0),
                                        linea.payment_link_note)
                    ocupadas.setdefault(e['clave'], linea)
                    continue
            automaticas.append(linea)

        # Las que prueban el respaldo, y los pagos que el respaldo no toca: los
        # que alguna otra cuadra por las reglas de siempre. Las dos, solo con
        # los pagos de su ventana: la siguiente parcialidad del mismo importe,
        # semanas después, no le quita a una línea su liga por el respaldo.
        con_respaldo = {linea for linea in automaticas if not any(
            linea._importe_cuadra(e) and linea._en_su_ventana(e, ventana) for e in unidades)}
        pedidas = {
            e['clave'] for linea in automaticas if linea not in con_respaldo
            for e in unidades if linea._importe_cuadra(e) and linea._en_su_ventana(e, ventana)}
        # Las de respaldo con más de un pago desde la primera ronda: aunque
        # otra tome uno, no se liga por descarte.
        ambiguas = set()

        def evaluar(linea):
            """(los que cuadran y están libres, los que además están en la
            ventana, los que quedan después de desempatar)."""
            rep = linea.payment_doc_id
            pista = rep.move_id if rep.move_id.move_type == 'entry' else Asiento
            respaldo = linea in con_respaldo
            cuadran = [
                e for e in unidades
                if e['clave'] not in ocupadas and not (respaldo and e['clave'] in pedidas)
                and linea._importe_cuadra(e, respaldo=respaldo)]
            en_ventana = [e for e in cuadran if linea._en_su_ventana(e, ventana)]
            if respaldo and (linea in ambiguas or len(en_ventana) > 1):
                ambiguas.add(linea)
                return cuadran, en_ventana, []
            return cuadran, en_ventana, linea._desempatar(en_ventana, pista)

        pendientes = list(automaticas)
        evaluacion = {linea: evaluar(linea) for linea in pendientes}
        while pendientes:
            solos = {linea: evaluacion[linea][2][0]['clave']
                     for linea in pendientes if len(evaluacion[linea][2]) == 1}
            pedidos = Counter(solos.values())
            ganan = {}
            for linea, clave in solos.items():
                if pedidos[clave] == 1:
                    ganan[linea] = clave
                elif linea._clave_de_la_liga() == clave and sum(
                        1 for otra, suya in solos.items() if suya == clave and otra._clave_de_la_liga() == clave) == 1:
                    ganan[linea] = clave
            if not ganan:
                break
            for linea, clave in ganan.items():
                resultado[linea] = ligada(linea, por_clave[clave])
                ocupadas.setdefault(clave, linea)
                pendientes.remove(linea)
            evaluacion = {linea: evaluar(linea) for linea in pendientes}

        # Las que nadie pudo ligar: quién más pide el pago que cada una quiere.
        piden = defaultdict(list)
        for linea in pendientes:
            if len(evaluacion[linea][2]) == 1:
                piden[evaluacion[linea][2][0]['clave']].append(linea)
        for linea in pendientes:
            cuadran, en_ventana, quedan = evaluacion[linea]
            forma_sin_pago = (linea.forma_pago or '').strip() in FORMAS_SIN_PAGO
            nota_forma = env._("Forma de pago %(forma)s: no hay pago del banco que ligar.", forma=linea.forma_pago)
            if not unidades:
                resultado[linea] = (pendiente('not_applicable', nota_forma) if forma_sin_pago else
                                    pendiente('no_payment', env._("La factura no tiene pagos en Odoo.")))
                continue
            # Por las reglas de siempre: la que no ligó por el respaldo queda
            # como sin él, con importe distinto.
            alguna = [e for e in unidades if linea._importe_cuadra(e)]
            if not alguna:
                resultado[linea] = pendiente('not_applicable', nota_forma) if forma_sin_pago else pendiente(
                    'amount_mismatch', env._(
                        "Ningún pago de la factura aplicó %(importe)s; pagos: %(lista)s.",
                        importe=tools.format_amount(env, linea.imp_pagado, linea.currency_id)
                        if linea.currency_id else f"{linea.imp_pagado:,.2f} {linea.moneda_dr or ''}".strip(),
                        lista=lista(unidades)))
                continue
            if not cuadran:
                e = alguna[0]
                otra = ocupadas.get(e['clave'])
                resultado[linea] = pendiente('to_choose', env._(
                    "El pago %(pago)s, que cuadra, ya lo tiene el REP %(rep)s.",
                    pago=nombre(e), rep=otra.payment_doc_id.display_name if otra else "?"))
                continue
            if not en_ventana:
                e = min(cuadran, key=linea._dias_a)
                resultado[linea] = pendiente('to_choose', env._(
                    "%(pago)s cuadra en importe, pero su fecha está a %(dias)s días de la del REP.",
                    pago=nombre(e), dias=linea._dias_a(e)))
                continue
            if not quedan:
                # Respaldo ambiguo de una línea que además cuadra por las
                # reglas de siempre con un pago fuera de su ventana.
                resultado[linea] = pendiente('to_choose', env._(
                    "Más de un pago de Odoo podría ser el suyo (%(lista)s); elige uno.", lista=lista(en_ventana)))
                continue
            if len(quedan) > 1:
                resultado[linea] = pendiente('to_choose', env._(
                    "%(n)s pagos de Odoo cuadran (%(lista)s); elige uno.", n=len(quedan), lista=lista(quedan)))
                continue
            otra = next((x for x in piden[quedan[0]['clave']] if x != linea), None)
            resultado[linea] = pendiente('to_choose', env._(
                "El pago %(pago)s también lo pide el REP %(rep)s.",
                pago=nombre(quedan[0]), rep=otra.payment_doc_id.display_name if otra else "?"))
        return resultado

    def _copia_al_dia(self):
        """Si la copia del XML de esta línea ligada está en su pago: el
        account.payment si lo tiene; si no, el asiento."""
        self.ensure_one()
        copia = self.payment_attachment_id
        if not copia:
            return False
        if self.account_payment_id:
            return (copia.res_model, copia.res_id) == ('account.payment', self.account_payment_id.id)
        return (copia.res_model, copia.res_id) == ('account.move', self.payment_move_id.id)

    # -------------------------------------------------------------------
    # Mantenerla al día: ganchos, actualización y revisión diaria
    # -------------------------------------------------------------------

    @api.model
    def _programar_religado(self, asientos=None, pagos=None):
        """Anota los asientos (facturas o asientos de pago) y los pagos que
        cambiaron, para religar una sola vez al confirmar la transacción
        (_religar_cola). No consulta nada: lo llaman cada pago, cada
        conciliación y cada desconciliación de la base, también las de ventas
        y las que no tienen nada que ver con un REP.

        En precommit y no al momento: «Registrar pago» crea el pago, lo
        publica, lo concilia y lo liga a la factura en pasos separados, y la
        liga solo se puede decidir al final. Y un error al religar nunca debe
        impedir registrar un pago."""
        if not (asientos or pagos):
            return
        precommit = self.env.cr.precommit
        cola = precommit.data.get(COLA_DE_RELIGADO)
        if cola is None:
            cola = precommit.data[COLA_DE_RELIGADO] = {'asientos': set(), 'pagos': set()}
            precommit.add(self.env['dsi.cfdi.payment.line'].sudo()._religar_cola)
        if asientos:
            cola['asientos'].update(asientos.ids)
        if pagos:
            cola['pagos'].update(pagos.ids)

    def _religar_cola(self):
        """El precommit de _programar_religado: religa las líneas de esas
        facturas y las ligadas a esos asientos o pagos. Sin líneas (casi
        siempre) es una sola búsqueda por índice, en su savepoint.

        Saca la cola ANTES de nada: _sincronizar_desde_gancho hace un flush,
        que corre los demás precommit y al terminar limpia los datos de todos.

        Va en el savepoint de _sincronizar_desde_gancho, que ya convierte un
        choque con otra transacción en un aviso y una revisión reprogramada.
        Cualquier otro error queda en el log y lo recoge la revisión diaria:
        el pago se confirma igual. La búsqueda también, en su propio
        savepoint: falla, por ejemplo, con el código nuevo ya desplegado y la
        base sin actualizar (una columna que no existe), y sin savepoint la
        transacción quedaría abortada y no se confirmaría ningún pago. Un
        choque al escribir lo pendiente de quien registró el pago (el flush
        antes de los savepoints) es suyo y le llega tal cual: Odoo reintenta
        la petición."""
        cola = self.env.cr.precommit.data.pop(COLA_DE_RELIGADO, None)
        if not cola:
            return
        Linea = self.sudo().with_context(active_test=False)
        asientos, pagos = list(cola['asientos']), list(cola['pagos'])
        Inconsistencia = self.env['dsi.cfdi.inconsistency']
        self.env.flush_all()
        try:
            # Sin flush: ya se hizo afuera, y el savepoint que lo hace, si se
            # deshace, descarta también los precommit que falten.
            with self.env.cr.savepoint(flush=False):
                lineas = Linea.search([
                    '|', '|', ('invoice_id', 'in', asientos), ('payment_move_id', 'in', asientos),
                    ('account_payment_id', 'in', pagos),
                ])
            if lineas:
                Inconsistencia._sincronizar_desde_gancho('_ligar_reps_con_pagos', lineas, 'pago')
        except ERRORES_DE_CONCURRENCIA:
            raise
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Pagos (REP): no se pudo religar con los pagos de Odoo; lo recoge la revisión diaria.")
            Inconsistencia._programar_revision()

    @api.model
    def _backfill_ligas_con_pagos(self):
        """La liga con el pago de los REP que ya estaban guardados. Corre al
        instalar o actualizar el módulo (data/cfdi_payment_lines.xml).

        Solo toma los REP con alguna línea sin evaluar (estado vacío): todas
        quedan con estado, así que correrla otra vez no hace nada. En tandas
        con flush y sin commit, como _backfill_lineas_de_pago, y vaciando la
        caché después de cada una: si no, guarda todo lo leído (facturas,
        pagos, parciales, adjuntos), unos 20 KB por línea. Con
        `dsi_deteccion_diferida`: las discrepancias de lo que se liga aquí
        las ve la revisión diaria."""
        Linea = self.sudo().with_context(active_test=False, dsi_deteccion_diferida=True)
        reps = Linea.search([('payment_link_state', '=', False)]).payment_doc_id.ids
        Inconsistencia = self.env['dsi.cfdi.inconsistency'].with_context(dsi_deteccion_diferida=True)
        for ids in split_every(LOTE_DE_REPS, reps):
            Inconsistencia._ligar_reps_con_pagos(Linea.search([('payment_doc_id', 'in', ids)]), 'rep')
            self.env.flush_all()
            self.env.invalidate_all()
        if reps:
            cuantas = dict(Linea._read_group(
                [('payment_doc_id', 'in', reps)], ['payment_link_state'], ['__count']))
            _logger.info(
                "Pagos (REP): %s ligados a su pago, %s por elegir, %s con importe distinto, %s sin pago en "
                "Odoo, %s no aplican.",
                cuantas.get('linked', 0) + cuantas.get('manual', 0), cuantas.get('to_choose', 0),
                cuantas.get('amount_mismatch', 0), cuantas.get('no_payment', 0), cuantas.get('not_applicable', 0))

    @api.model
    def _lineas_con_liga_de_pago_por_revisar(self):
        """Ids de las líneas cuya liga con el pago quizá ya no es la que toca,
        por algo que ningún gancho vio (una cola de precommit que se perdió,
        un pago borrado, un cambio por SQL). En SQL, con el patrón de
        _lineas_con_otra_factura:

        a) sin evaluar, o pendientes cuya compra ya tiene algo pagado;
        b) ligadas a un pago que ya no está aplicado a la factura: sin pago ni
           asiento (se borraron), un asiento que ya no está publicado o sin
           parcial con la factura (en 19 un pago en borrador conserva su
           conciliación), o un pago sin asiento que ya lo tiene, que ya no
           aplica o que ya no está ligado a la factura;
        c) un estado que ya no corresponde: con liga y el REP ya no es
           vigente, recibido o con su compra publicada; o «No aplica» y ya lo
           es: con algo pagado, o con una forma de pago fuera de
           FORMAS_SIN_PAGO (la compra que vuelve a publicarse sin pagos queda
           «Sin pago en Odoo»; una compensación sin pagos sigue «No aplica» y
           no se vuelve a ver cada día)."""
        for modelo in ('dsi.cfdi.payment.line', 'cfdi.document', 'account.move', 'account.payment',
                       'account.partial.reconcile', 'account.move.line'):
            self.env[modelo].flush_model()
        compras = tuple(self.env['account.move'].get_purchase_types(include_receipts=True))
        self.env.cr.execute(SQL("""
            SELECT l.id
              FROM dsi_cfdi_payment_line l
              JOIN cfdi_document rep ON rep.id = l.payment_doc_id
              JOIN account_move f ON f.id = l.invoice_id
             WHERE l.direction = 'received' AND rep.active AND rep.sat_status != 'canceled'
               AND f.state = 'posted' AND f.move_type IN %(compras)s
               AND (l.payment_link_state IS NULL
                    OR (l.payment_link_state IN %(pendientes)s
                        AND f.payment_state IN ('partial', 'in_payment', 'paid')))
            UNION
            SELECT l.id
              FROM dsi_cfdi_payment_line l
         LEFT JOIN account_payment p ON p.id = l.account_payment_id
         LEFT JOIN account_move pm ON pm.id = l.payment_move_id
             WHERE l.payment_link_state IN %(ligados)s
               AND ((l.account_payment_id IS NULL AND l.payment_move_id IS NULL)
                    OR (l.payment_move_id IS NOT NULL
                        AND (pm.state != 'posted' OR NOT EXISTS (
                            SELECT 1
                              FROM account_partial_reconcile part
                              JOIN account_move_line a ON a.id = part.debit_move_id
                              JOIN account_move_line b ON b.id = part.credit_move_id
                             WHERE (a.move_id = l.invoice_id AND b.move_id = l.payment_move_id)
                                OR (a.move_id = l.payment_move_id AND b.move_id = l.invoice_id))))
                    OR (l.payment_move_id IS NULL
                        AND (p.move_id IS NOT NULL OR p.state NOT IN %(aplicados)s OR NOT EXISTS (
                            SELECT 1
                              FROM account_move__account_payment rel
                             WHERE rel.invoice_id = l.invoice_id AND rel.payment_id = l.account_payment_id))))
            UNION
            SELECT l.id
              FROM dsi_cfdi_payment_line l
              JOIN cfdi_document rep ON rep.id = l.payment_doc_id
         LEFT JOIN account_move f ON f.id = l.invoice_id
             WHERE (l.payment_link_state IS NOT NULL AND l.payment_link_state != 'not_applicable'
                    AND (l.direction != 'received' OR NOT rep.active OR rep.sat_status = 'canceled'
                         OR l.invoice_id IS NULL OR f.state != 'posted'))
                OR (l.payment_link_state = 'not_applicable' AND l.direction = 'received' AND rep.active
                    AND rep.sat_status != 'canceled' AND f.state = 'posted' AND f.move_type IN %(compras)s
                    AND (f.payment_state IN ('partial', 'in_payment', 'paid')
                         OR btrim(COALESCE(l.forma_pago, '')) NOT IN %(formas_sin_pago)s))
        """, compras=compras, pendientes=ESTADOS_PENDIENTES, ligados=ESTADOS_LIGADOS,
            formas_sin_pago=tuple(sorted(FORMAS_SIN_PAGO)), aplicados=_estados_de_pago_aplicado(self.env)))
        return [fila[0] for fila in self.env.cr.fetchall()]

    # -------------------------------------------------------------------
    # Acciones del contador
    # -------------------------------------------------------------------

    def _revisar_que_se_liga_a_mano(self):
        self.ensure_one()
        self.check_access('read')
        if self.direction != 'received' or not self.invoice_id or self.payment_link_state == 'not_applicable':
            raise UserError(self.env._(
                "Solo los REP recibidos y vigentes, con su factura en Odoo, se ligan a un pago."))
        self.env['dsi.license.client.state']._check_feature(
            'vault', self.env._("relacionar un REP con su pago en Odoo"), self.company_id)

    def action_elegir_pago(self):
        """«Elegir pago»: los pagos aplicados a la factura, para ligar este
        documento relacionado a mano. El Usuario de la Bóveda solo lee las
        líneas: el asistente valida y escribe con sudo."""
        self._revisar_que_se_liga_a_mano()
        return self.env['dsi.cfdi.rep.pago.wizard']._abrir_para(self)

    def action_liga_automatica(self):
        """«Volver a automático»: deja que la Bóveda la ligue con la regla."""
        self.ensure_one()
        self.check_access('read')
        self.env['dsi.license.client.state']._check_feature(
            'vault', self.env._("relacionar un REP con su pago en Odoo"), self.company_id)
        if self.payment_link_state != 'manual':
            return False
        self.sudo().write({'payment_link_state': False, 'payment_link_note': False})
        self.env['dsi.cfdi.inconsistency']._ligar_reps_con_pagos(self, 'rep')
        estados = dict(self._fields['payment_link_state']._description_selection(self.env))
        self.payment_doc_id.sudo().message_post(body=self.env._(
            "Documento %(folio)s: se devolvió a la liga automática; quedó %(estado)s.",
            folio=self.display_name, estado=estados.get(self.payment_link_state, self.payment_link_state)))
        return False


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    def _copiar_rep_a_sus_pagos(self):
        """Una copia del XML de cada REP en cada pago al que están ligados sus
        documentos relacionados: el account.payment o, si no lo hay, el asiento
        del movimiento del banco. Una sola por REP y pago, aunque el REP pague
        varias de sus facturas.

        Igual que en las facturas (_copiar_rep_a_sus_facturas): se reutiliza
        la que ya tenga el mismo contenido; nunca se borra (con bitácora de
        auditoría Odoo no lo permite, y en una liga que cambió el XML también
        estuvo en el pago anterior); sin mensaje en el chatter (creada por el
        ORM no pasa por _post_add_create, que en un asiento importaría el XML
        como si fuera su factura); y con `no_document`, para que Documents no
        la meta a la carpeta de contabilidad."""
        Adjunto = self.env['ir.attachment'].sudo().with_context(no_document=True)
        for rep in self.sudo().filtered(lambda d: d.move_type == 'P'):
            contenido = None
            ligadas = rep.dsi_payment_line_ids.filtered(
                lambda linea: linea.payment_link_state in ESTADOS_LIGADOS
                and (linea.account_payment_id or linea.payment_move_id))
            for destino, grupo in ligadas.grouped(lambda linea: linea.account_payment_id or linea.payment_move_id).items():
                copia = grupo.payment_attachment_id.filtered(
                    lambda a, d=destino: a.res_model == d._name and a.res_id == d.id)[:1]
                if not copia:
                    contenido = contenido or _bytes_de_adjunto(rep.xml_attachment_id)
                    if not contenido:
                        break
                    copia = Adjunto.search([
                        ('res_model', '=', destino._name), ('res_id', '=', destino.id),
                        ('checksum', '=', Adjunto._compute_checksum(contenido)),
                    ], limit=1) or Adjunto.create({
                        'name': f"REP-{rep.uuid}.xml",
                        'raw': contenido,
                        'mimetype': 'application/xml',
                        'res_model': destino._name,
                        'res_id': destino.id,
                        'company_id': destino.company_id.id,
                    })
                grupo.filtered(lambda linea, c=copia: linea.payment_attachment_id != c).write(
                    {'payment_attachment_id': copia.id})


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _dsi_pagos_aplicados(self, con_reps=True):
        """Los pagos aplicados a cada factura de `self`: {factura: [dict]}, con
        una llave por cada registro (con o sin sudo, es la misma).

        Un pago aplicado es lo que Odoo aplicó a UNA factura desde un mismo
        origen: un account.payment (con asiento, o sin él con el módulo
        accountant) o el asiento de un movimiento del banco conciliado directo,
        con la regla de l10n_mx_edi (_l10n_mx_edi_is_cfdi_payment). Las notas
        de crédito, compensaciones, diferencias cambiarias, CABA y asientos
        manuales no son pagos, pero cuentan en el saldo anterior.

        Cada dict trae: clave ('pago:<id>' o 'asiento:<id>'), pago, asiento,
        extracto, diario, fecha (la del pago si lo hay: no cambia al conciliar
        con el banco un pago sin asiento), fecha_banco, importe y moneda (lo
        aplicado a la factura, en su moneda), importe_pago y moneda_pago (lo
        mismo en la moneda del lado del pago), monto y moneda_monto (el pago
        completo), parciales, sin_asiento, saldo_anterior y, con `con_reps`,
        lineas_rep y tiene_rep (las líneas de REP vigentes ligadas a ese
        pago). Importes en None cuando Odoo no dice cuánto aplicó: un pago sin
        asiento de varias facturas, o en otra moneda.

        Una factura sin publicar, o un asiento que no es factura, da []. Lee
        con sudo y no escribe nada; las consultas no dependen de cuántas
        facturas ni cuántos pagos: una de parciales (y otra de sus diferencias
        cambiarias, si las hay), las lecturas con prefetch y una búsqueda de
        líneas de REP. No usa _get_all_reconciled_invoice_partials: en 20
        cambió de forma.

        No decide PPD, plazos ni si un importe cuadra: eso es de quien llama.
        """
        resultado = {factura: [] for factura in self}
        facturas = self.sudo().filtered(lambda f: f.state == 'posted' and f.is_invoice(include_receipts=True))
        if not facturas:
            return resultado
        env = facturas.env
        env['account.partial.reconcile'].flush_model()
        env['account.move.line'].flush_model(['move_id', 'account_id', 'date'])
        env['account.account'].flush_model(['account_type'])
        # Un renglón por parcial de cada factura, del lado de su línea por
        # cobrar o por pagar: (factura, parcial, lo aplicado del lado de la
        # factura, lo aplicado del lado contrario, la moneda de ese lado, la
        # línea contraria, el asiento de diferencia cambiaria del parcial).
        env.cr.execute(SQL("""
            SELECT fl.move_id, part.id, part.debit_amount_currency, part.credit_amount_currency,
                   part.credit_currency_id, part.credit_move_id, part.exchange_move_id
              FROM account_partial_reconcile part
              JOIN account_move_line fl ON fl.id = part.debit_move_id
              JOIN account_account cta ON cta.id = fl.account_id
             WHERE fl.move_id IN %(facturas)s AND cta.account_type IN ('asset_receivable', 'liability_payable')
            UNION ALL
            SELECT fl.move_id, part.id, part.credit_amount_currency, part.debit_amount_currency,
                   part.debit_currency_id, part.debit_move_id, part.exchange_move_id
              FROM account_partial_reconcile part
              JOIN account_move_line fl ON fl.id = part.credit_move_id
              JOIN account_account cta ON cta.id = fl.account_id
             WHERE fl.move_id IN %(facturas)s AND cta.account_type IN ('asset_receivable', 'liability_payable')
        """, facturas=tuple(facturas.ids)))
        filas_de = defaultdict(list)
        for fila in env.cr.fetchall():
            filas_de[fila[0]].append(fila)
        # Una factura en dólares pagada en pesos: Odoo parte lo que el pago
        # aplicó en dos, el parcial con la factura (al tipo de cambio de la
        # factura) y el de su diferencia cambiaria, conciliada con la línea del
        # pago. Lo que el pago aplicó a la factura en su moneda es la suma de
        # los dos: sin el segundo, 1,800 pesos se veían como 1,700.
        cambiarias = {fila[6] for filas in filas_de.values() for fila in filas if fila[6]}
        ajuste_cambiario = defaultdict(float)
        if cambiarias:
            env.cr.execute(SQL("""
                SELECT ex.move_id, part.debit_move_id, part.debit_amount_currency
                  FROM account_partial_reconcile part
                  JOIN account_move_line ex ON ex.id = part.credit_move_id
                 WHERE ex.move_id IN %(cambiarias)s
                UNION ALL
                SELECT ex.move_id, part.credit_move_id, part.credit_amount_currency
                  FROM account_partial_reconcile part
                  JOIN account_move_line ex ON ex.id = part.debit_move_id
                 WHERE ex.move_id IN %(cambiarias)s
            """, cambiarias=tuple(cambiarias)))
            for cambiaria_id, linea_id, importe in env.cr.fetchall():
                ajuste_cambiario[(cambiaria_id, linea_id)] += importe or 0.0
        contras = {linea.id: linea for linea in env['account.move.line'].browse(
            {fila[5] for filas in filas_de.values() for fila in filas})}
        Pago, Asiento = env['account.payment'], env['account.move']
        Parcial, Moneda = env['account.partial.reconcile'], env['res.currency']
        Extracto = env['account.bank.statement.line']
        # El enlace extracto ↔ pago sin asiento: en 19 lo trae
        # account_accountant_batch_payment y en 20 account_batch_payment; en 18
        # no existe.
        con_lineas_de_pago = 'payment_lines_ids' in env['account.move.line']._fields
        aplicados = _estados_de_pago_aplicado(env)
        conciliado = _estado_de_pago_conciliado(env)

        def nueva(clave, pago, asiento, extracto, orden, factura):
            return {
                'clave': clave,
                'pago': pago,
                'asiento': asiento,
                'extracto': extracto,
                'diario': asiento.journal_id or pago.journal_id,
                'fecha': pago.date if pago else asiento.date,
                'fecha_banco': extracto.date if extracto else None,
                'importe': 0.0,
                'moneda': factura.currency_id,
                'importe_pago': 0.0,
                'moneda_pago': pago.currency_id,
                'monto': pago.amount if pago else abs(
                    extracto.amount_currency if extracto.foreign_currency_id else extracto.amount),
                'moneda_monto': pago.currency_id or extracto.foreign_currency_id or extracto.currency_id,
                'parciales': Parcial,
                'sin_asiento': False,
                'saldo_anterior': None,
                'orden': orden,
            }

        por_factura = {}
        for factura in facturas:
            por_clave, otros = {}, []
            for (_f, parcial_id, importe, importe_pago, moneda_pago_id, contra_id, cambiaria_id) in filas_de[factura.id]:
                contra = contras[contra_id]
                asiento = contra.move_id
                if asiento == factura or asiento.state != 'posted':
                    continue
                orden = (contra.date, parcial_id)
                pago, extracto = asiento.origin_payment_id, asiento.statement_line_id
                if not pago and extracto and con_lineas_de_pago:
                    candidatos = contra.payment_lines_ids.filtered(lambda p, f=factura: f in p.invoice_ids)
                    pago = candidatos if len(candidatos) == 1 else Pago
                if not (pago or extracto):
                    # Nota de crédito, compensación, diferencia cambiaria,
                    # CABA o asiento manual: solo cuenta en el saldo.
                    otros.append({'orden': orden, 'importe': importe or 0.0})
                    continue
                clave = f'pago:{pago.id}' if pago else f'asiento:{asiento.id}'
                e = por_clave.get(clave)
                if e is None:
                    e = por_clave[clave] = nueva(clave, pago, asiento, extracto, orden, factura)
                e['importe'] += importe or 0.0
                e['importe_pago'] += (importe_pago or 0.0) + ajuste_cambiario.pop((cambiaria_id, contra_id), 0.0)
                e['moneda_pago'] = Moneda.browse(moneda_pago_id)
                e['parciales'] |= Parcial.browse(parcial_id)
                e['orden'] = min(e['orden'], orden)

            # Los pagos sin asiento (módulo accountant): están en
            # matched_payment_ids sin parcial hasta conciliarlos con el banco.
            sin_asiento = factura.matched_payment_ids.filtered(
                lambda p: not p.move_id and p.state in aplicados).sorted('id')
            for pago in sin_asiento:
                clave = f'pago:{pago.id}'
                if clave in por_clave:
                    continue
                sueltos = [e for e in por_clave.values() if e['extracto'] and not e['pago']]
                # Su movimiento del banco, si ya lo concilió y no se sabe con
                # cuál (sin payment_lines_ids): si uno solo le aplicó a sus
                # facturas el importe del pago, es el suyo y toma su clave (la
                # del pago no cambia). Odoo marca conciliado el pago al
                # conciliar el banco con sus facturas, salvo el de varias
                # facturas en 18: compara cada parcial con el pago completo y
                # lo deja «en proceso». Por eso se busca también sin
                # conciliar, pero solo si a sus facturas ya no les queda saldo
                # para él; si les queda, ese movimiento puede ser otro pago
                # (una parcialidad anterior del mismo importe).
                gemelos = [e for e in sueltos if _es_su_movimiento_del_banco(e, pago, factura)]
                if gemelos and pago.state != conciliado and _le_queda_saldo_para_el_pago(pago, factura):
                    gemelos = []
                iguales = sin_asiento.filtered(lambda p, pago=pago: (
                    p.state == pago.state and p.currency_id == pago.currency_id
                    and _cuadra(p.amount, pago.amount, _tolerancia(pago.currency_id))))
                if len(gemelos) == 1 and len(iguales) == 1:
                    e = gemelos[0]
                    del por_clave[e['clave']]
                    e.update({
                        'clave': clave, 'pago': pago, 'fecha': pago.date, 'diario': e['asiento'].journal_id,
                        'monto': pago.amount, 'moneda_monto': pago.currency_id,
                    })
                    por_clave[clave] = e
                    continue
                if pago.state == conciliado and any(e['fecha'] >= pago.date for e in sueltos):
                    # Conciliado sin que se sepa con qué movimiento, y hay uno
                    # del banco de ese día o después: ya está contado ahí y no
                    # se cuenta dos veces.
                    continue
                # De una sola factura aunque lleve sus notas de crédito: lo
                # que aplica a la factura es el pago completo.
                una = _de_una_sola_factura(pago, factura)
                por_clave[clave] = {
                    **nueva(clave, pago, Asiento, Extracto, (pago.date, 0), factura),
                    'importe': pago.amount if una and pago.currency_id == factura.currency_id else None,
                    'importe_pago': pago.amount if una else None,
                    'sin_asiento': True,
                }

            # El saldo justo antes de cada pago, en orden desde el total:
            # también restan las notas de crédito y las demás.
            saldo = abs(factura.amount_total)
            for item in sorted([e for e in por_clave.values() if not e['sin_asiento']] + otros,
                               key=lambda i: i['orden']):
                if 'clave' in item:
                    item['saldo_anterior'] = factura.currency_id.round(saldo)
                saldo -= item['importe']
            for e in por_clave.values():
                e.pop('orden')
                if e['importe'] is not None:
                    e['importe'] = factura.currency_id.round(e['importe'])
                if e['importe_pago'] is not None and e['moneda_pago']:
                    e['importe_pago'] = e['moneda_pago'].round(e['importe_pago'])
            # Por fecha y, el mismo día, el asiento antes que el pago y por id
            # como número: como texto, 'pago:1000' iba antes que 'pago:999'.
            por_factura[factura] = sorted(por_clave.values(), key=lambda e: (
                e['fecha'], (e['pago'] or e['asiento'])._name, (e['pago'] or e['asiento']).id))

        if con_reps:
            Linea = env['dsi.cfdi.payment.line'].with_context(active_test=False)
            ligadas = defaultdict(lambda: Linea)
            for linea in Linea.search([
                    ('invoice_id', 'in', facturas.ids), ('payment_link_state', 'in', ESTADOS_LIGADOS),
                    ('payment_doc_id.active', '=', True), ('payment_doc_id.sat_status', '!=', 'canceled')]):
                ligadas[(linea.invoice_id.id, linea._clave_de_la_liga())] |= linea
            for factura, unidades in por_factura.items():
                for e in unidades:
                    e['lineas_rep'] = ligadas[(factura.id, e['clave'])]
                    e['tiene_rep'] = bool(e['lineas_rep'])
        for factura, unidades in por_factura.items():
            resultado[factura] = unidades
        return resultado

    def write(self, vals):
        """Odoo liga el pago a la factura (matched_payment_ids) después de
        crearlo y conciliarlo: un pago sin asiento solo se ve por ahí."""
        resultado = super().write(vals)
        if 'matched_payment_ids' in vals:
            self.env['dsi.cfdi.payment.line']._programar_religado(asientos=self)
        return resultado


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    # Solo para la Bóveda, como en la factura: quien no es Usuario de la
    # Bóveda no puede leer los REP, y sin el grupo no abriría el pago.
    dsi_rep_line_ids = fields.One2many(
        'dsi.cfdi.payment.line', 'account_payment_id', string="Pagos (REP)",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    dsi_rep_count = fields.Integer(
        string="Número de pagos (REP)", compute='_compute_dsi_rep_count',
        groups='dsi_cfdi_vault.group_cfdi_vault_user')

    # Los REP (no los documentos relacionados) ligados a este pago, sin los
    # cancelados.
    @api.depends('dsi_rep_line_ids.payment_sat_status', 'dsi_rep_line_ids.payment_link_state')
    def _compute_dsi_rep_count(self):
        for pago in self:
            pago.dsi_rep_count = len(pago.dsi_rep_line_ids.filtered(
                lambda linea: linea.payment_link_state in ESTADOS_LIGADOS
                and linea.payment_sat_status != 'canceled').payment_doc_id)

    def action_view_rep_lines(self):
        """Botón «REP» del pago: los documentos relacionados ligados a él,
        sin los REP cancelados (quitando el filtro se ven)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Pagos (REP)",
            'res_model': 'dsi.cfdi.payment.line',
            'view_mode': 'list,form',
            'domain': [('account_payment_id', '=', self.id)],
            'context': {'create': False, 'search_default_filter_not_canceled': 1},
        }

    # Los ganchos solo juntan ids (_programar_religado): la liga se decide al
    # confirmar la transacción.
    @api.model_create_multi
    def create(self, vals_list):
        pagos = super().create(vals_list)
        self.env['dsi.cfdi.payment.line']._programar_religado(asientos=pagos.sudo().invoice_ids, pagos=pagos)
        return pagos

    def write(self, vals):
        if not CAMPOS_DEL_PAGO & set(vals):
            return super().write(vals)
        antes = self.sudo().invoice_ids
        resultado = super().write(vals)
        self.env['dsi.cfdi.payment.line']._programar_religado(
            asientos=antes | self.sudo().invoice_ids | self.sudo().move_id, pagos=self)
        return resultado

    def unlink(self):
        self.env['dsi.cfdi.payment.line']._programar_religado(
            asientos=self.sudo().invoice_ids | self.sudo().move_id, pagos=self)
        return super().unlink()


class AccountPartialReconcile(models.Model):
    """Conciliar y desconciliar cambian qué pagos aplican a una factura. Solo
    se anotan los asientos de los dos lados (_programar_religado): una
    conciliación bancaria de cientos de facturas religa una vez, al final, y
    un error al religar no impide conciliar."""
    _inherit = 'account.partial.reconcile'

    @api.model_create_multi
    def create(self, vals_list):
        parciales = super().create(vals_list)
        lados = parciales.sudo()
        self.env['dsi.cfdi.payment.line']._programar_religado(
            asientos=(lados.debit_move_id | lados.credit_move_id).move_id)
        return parciales

    def unlink(self):
        lados = self.sudo()
        asientos = (lados.debit_move_id | lados.credit_move_id).move_id
        resultado = super().unlink()
        self.env['dsi.cfdi.payment.line']._programar_religado(asientos=asientos)
        return resultado


class CfdiInconsistency(models.Model):
    _inherit = 'dsi.cfdi.inconsistency'

    @api.model
    def _ligar_reps_con_pagos(self, lineas, source, facturas=None):
        """Liga con sus pagos las líneas de REP (y las demás de sus compras y
        de `facturas`), y después lo que cuelga de esa liga. Desde un gancho va
        con _sincronizar_desde_gancho, en su savepoint."""
        evaluadas = lineas._ligar_con_sus_pagos(facturas=facturas)
        self._tras_ligar_pagos(evaluadas, source)

    @api.model
    def _tras_ligar_pagos(self, facturas, source):
        """Punto de extensión: después de religar REP con pagos. La discrepancia
        de compras PPD pagadas sin REP se cuelga aquí."""

    @api.model
    def _revisar_ligas_con_pagos(self):
        """El detector de la revisión diaria: vuelve a ligar las líneas que un
        gancho no vio (_lineas_con_liga_de_pago_por_revisar), por tandas de
        REP con flush y la caché vacía después de cada una (como
        _backfill_ligas_con_pagos). Las líneas de cada REP se juntan antes,
        en una consulta: con la caché vacía, filtrarlas en cada tanda volvía
        a leerlas todas."""
        Linea = self.env['dsi.cfdi.payment.line'].sudo().with_context(active_test=False)
        ids = Linea._lineas_con_liga_de_pago_por_revisar()
        if not ids:
            return
        de_cada_rep = {
            rep.id: lineas
            for rep, lineas in Linea._read_group([('id', 'in', ids)], ['payment_doc_id'], ['id:array_agg'])}
        for tanda in split_every(LOTE_DE_REPS, list(de_cada_rep)):
            self._ligar_reps_con_pagos(Linea.browse([i for rep in tanda for i in de_cada_rep[rep]]), 'cron')
            self.env.flush_all()
            self.env.invalidate_all()

    @api.model
    def _detectores(self):
        """La liga con los pagos va justo después de la liga con las facturas
        (_detectar_reps_sin_factura), que puede cambiar la factura de una
        línea."""
        detectores = list(super()._detectores())
        posicion = (detectores.index('_detectar_reps_sin_factura') + 1
                    if '_detectar_reps_sin_factura' in detectores else len(detectores))
        detectores.insert(posicion, '_revisar_ligas_con_pagos')
        return detectores
