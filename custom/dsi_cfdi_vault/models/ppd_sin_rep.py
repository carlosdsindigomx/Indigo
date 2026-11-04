"""Discrepancia «Compra PPD pagada sin REP» (ppd_sin_rep).

Una compra PPD se paga después de facturarse, y por cada pago el proveedor
debe emitir su CFDI de Pago (REP) a más tardar el día 5 del mes siguiente al
pago. La Bóveda ligaba los REP que llegaban, pero nadie decía qué pagos se
habían quedado sin el suyo.

Ahora, por cada pago aplicado a una compra PPD que no tiene un REP vigente
ligado, se guarda una discrepancia desde el día 6 del mes siguiente al pago,
contado en la hora de México. El plazo corre desde que el proveedor recibe el
pago (la FechaPago de su REP): si ya se conoce el movimiento del banco del
pago y es posterior (un cheque que se cobra días después), cuenta ese día y
no el del registro en Odoo. Qué es un pago aplicado lo dice
`account.move._dsi_pagos_aplicados` (rep_pagos.py): un account.payment o el
movimiento del banco conciliado directo con la compra. Las notas de crédito,
compensaciones y diferencias cambiarias no son pagos. Hay un registro por pago
y compra: un pago de varias compras da uno por compra (el REP es por documento
relacionado), y dos parcialidades dan dos.

El método de pago sale del CFDI de la Bóveda y no de la factura: en 19
l10n_mx_edi_payment_policy queda en False en las compras (cfdi_to_bill.py,
_valores_cfdi_de_factura). Una compra sin su CFDI en la Bóveda no se revisa.

Se resuelve sola, en el momento, cuando el REP se liga con ese pago
(_tras_ligar_pagos, el punto de extensión de la liga REP→pago). Lo que pasa
solo del lado de los pagos en una compra sin REP (un pago nuevo, uno que se
desconcilia) lo ve la revisión diaria. Si hay pagos por vencer, la revisión
se programa además para el día 6.

Revisa todo el histórico, salvo que se fije la fecha de corte opcional
PARAMETRO_CORTE_PPD. La compra no queda «Por revisar»: su asiento está bien,
lo que falta es un documento del proveedor sobre otro asiento, el del pago.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timezone

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, tools
from odoo.tools import SQL, split_every

from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param

from .cfdi_document import ERRORES_DE_CONCURRENCIA, _empresas_del_mismo_rfc
from .cfdi_inconsistency import _clave_del_hallazgo
from .cfdi_payment_line import ZONA_DEL_CFDI
from .rep_pagos import ESTADOS_LIGADOS, ESTADOS_PENDIENTES, _estados_de_pago_aplicado

_logger = logging.getLogger(__name__)

# Opcional, AAAA-MM-DD: solo los pagos desde esa fecha (la del pago en Odoo).
# Sin valor se revisa todo el histórico. A diferencia del corte de «Pago (REP)
# de factura fuera de Odoo», nunca se fija solo: quien no quiera el histórico
# lo pone antes de actualizar. Si lo pone después, lo anterior al corte se
# resuelve con ese motivo y queda como historial.
PARAMETRO_CORTE_PPD = 'dsi_cfdi_vault.ppd_sin_rep_desde'

# El proveedor emite el REP a más tardar este día del mes siguiente al pago
# (RMF). La discrepancia aparece desde el día siguiente. Si la regla cambia, se
# cambia aquí.
DIA_LIMITE_DEL_REP = 5

# Llevan REP las facturas y los recibos de proveedor (CFDI de ingreso). Una
# nota de crédito no.
TIPOS_DE_COMPRA_PPD = ('in_invoice', 'in_receipt')

# El payment_state de una compra que puede tener pagos aplicados. Solo acota
# la consulta de la revisión diaria. 'blocked' va porque una compra bloqueada
# también puede estar pagada.
ESTADOS_DE_PAGO_DE_LA_COMPRA = ('paid', 'in_payment', 'partial', 'blocked')

# Compras por tanda en la revisión diaria, cada tanda en su savepoint y con la
# caché soltada al terminarla. Cada una cuesta lo de _dsi_pagos_aplicados,
# unas cuantas consultas por tanda.
LOTE_DE_COMPRAS_PPD = 200

# Lo que se reescribe en un registro que sigue apareciendo (_sincronizar,
# en_sitio): la pista de qué hacer (un REP sin ligar de la compra, o el REP en
# otra empresa), el límite y desde qué fecha cuenta, y el importe aplicado.
# Cambian sin que cambie el hallazgo, que es el mismo pago sin su REP: no lo
# sustituyen por otro ni le quitan su revisión.
CAMPOS_EN_SITIO_PPD = ('vault_value', 'odoo_value', 'observation')


def _inicio_del_plazo(e):
    """Desde cuándo corre el plazo del REP del pago aplicado `e` (de
    _dsi_pagos_aplicados): desde que el proveedor recibe el pago. Es la fecha
    del pago o, si ya se conoce la de su movimiento del banco (fecha_banco) y
    es posterior, esa: un cheque registrado el 31 de octubre y cobrado el 3
    de noviembre lleva FechaPago 3 de noviembre en su REP, que vence el 5 de
    diciembre.

    La ayuda trae fecha_banco cuando el pago es un movimiento del banco
    conciliado con la compra (también el de un pago sin asiento ya
    conciliado). Un pago con asiento propio que después se concilia con el
    banco no la trae, y cuenta su fecha."""
    fecha_banco = e.get('fecha_banco')
    return max(e['fecha'], fecha_banco) if fecha_banco else e['fecha']


def _limite_del_rep(fecha):
    """El último día para emitir el REP de un pago recibido en `fecha`
    (_inicio_del_plazo): el día 5 del mes siguiente. El del 31 de enero vence
    el 5 de febrero, y el del 20 de diciembre, el 5 de enero del año
    siguiente."""
    return (fecha + relativedelta(months=1)).replace(day=DIA_LIMITE_DEL_REP)


def _hoy_en_mexico(env):
    """El día de hoy en México, como _corte_rep_sin_factura. El cron corre en
    UTC, y entre las 00:00 y las 05:59 UTC en México todavía es el día
    anterior: un pago que vence el día 5 no debe aparecer en la noche del 5."""
    return fields.Date.context_today(env['dsi.cfdi.inconsistency'].with_context(tz=ZONA_DEL_CFDI.key))


def _clave_ppd(compra, e):
    """La clave del hallazgo: la compra y la clave del pago aplicado `e`
    ('pago:<id>' o 'asiento:<id>'), p. ej. ppd_sin_rep:0:4711:0:pago:88.

    - No lleva el CFDI, para que no cambie si el CFDI se vuelve a ligar.
    - Lleva la compra, porque un pago de varias compras da un registro por
      compra.
    - Un pago sin asiento que después se concilia con el banco conserva la
      clave del pago (lo garantiza la ayuda): sigue siendo el mismo registro."""
    return f"{_clave_del_hallazgo('ppd_sin_rep', factura=compra)}:{e['clave']}"


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _dsi_cfdi_ppd(self):
        """El CFDI de ingreso PPD de la Bóveda de esta compra publicada, o
        vacío. Cuenta una factura o un recibo de proveedor publicado cuyo CFDI
        (el del Estado bóveda, _dsi_cfdi_de_la_boveda) cumple todo esto:

        - es de ingreso;
        - no está cancelado en el SAT;
        - dice MetodoPago PPD.

        El método de pago sale de la Bóveda y no de la factura: en 19
        l10n_mx_edi_payment_policy queda en False en las compras, y Odoo lo
        borra al primer cambio de proveedor o de fechas (cfdi_to_bill.py,
        _valores_cfdi_de_factura).

        Un CFDI cancelado no lleva REP: esa compra ya tiene su discrepancia de
        estatus, y el proveedor no puede emitir el REP de un CFDI cancelado."""
        self.ensure_one()
        vacio = self.env['cfdi.document'].sudo()
        if self.move_type not in TIPOS_DE_COMPRA_PPD or self.state != 'posted':
            return vacio
        cfdi = self.sudo()._dsi_cfdi_de_la_boveda()
        if (cfdi.move_type != 'I' or cfdi.sat_status == 'canceled'
                or (cfdi.payment_method or '').strip().upper() != 'PPD'):
            return vacio
        return cfdi


class CfdiInconsistency(models.Model):
    _inherit = 'dsi.cfdi.inconsistency'

    # -------------------------------------------------------------------
    # Desde la liga REP→pago
    # -------------------------------------------------------------------

    @api.model
    def _tras_ligar_pagos(self, facturas, source):
        """Después de religar REP con pagos, las compras PPD de esas facturas
        quedan al momento como dicen sus ligas. Esto cubre:

        - un REP que llega o se liga: importación, descarga, vinculación,
          «Elegir pago» o la revisión diaria;
        - un REP que se cancela (la discrepancia se reabre);
        - una conciliación o desconciliación en una compra que ya tiene REP
          (el precommit de la liga, con source 'pago').

        Un pago nuevo o deshecho en una compra sin REP no pasa por aquí: lo ve
        la revisión diaria.

        Va en su propio savepoint: un choque con otra transacción no deshace la
        liga. Con `dsi_deteccion_diferida` (el llenado al actualizar) no se
        hace nada: lo recoge la revisión diaria."""
        super()._tras_ligar_pagos(facturas, source)
        if facturas and not self.env.context.get('dsi_deteccion_diferida'):
            self._sincronizar_desde_gancho('_sincronizar_ppd_sin_rep', facturas, source)

    # -------------------------------------------------------------------
    # La regla
    # -------------------------------------------------------------------

    @api.model
    def _corte_ppd_sin_rep(self):
        """(es_válido, fecha o None) de PARAMETRO_CORTE_PPD.

        - Sin valor (no existe o está vacío): (True, None), todo el histórico.
        - Con un valor que no es AAAA-MM-DD: un aviso en el log y (False, None).
          Quien llama no crea ni resuelve nada de este tipo. Si ignorara el
          corte, llenaría «Por revisar» con el histórico que el cliente quiso
          dejar fuera; si adivinara otro, resolvería registros sin motivo."""
        valor = (_param(self.env, PARAMETRO_CORTE_PPD) or '').strip()
        if not valor:
            return True, None
        try:
            return True, fields.Date.to_date(valor)
        except ValueError:
            _logger.warning(
                "El parámetro %s tiene «%s», que no es una fecha (AAAA-MM-DD): no se revisan las compras PPD "
                "pagadas sin REP hasta corregirlo.", PARAMETRO_CORTE_PPD, valor)
            return False, None

    @api.model
    def _sincronizar_ppd_sin_rep(self, facturas, source):
        """Registros «Compra PPD pagada sin REP» de las `facturas` revisadas
        (las que no son compras se ignoran). Hay uno por cada pago aplicado a
        una compra PPD (_dsi_cfdi_ppd) que cumple todo esto:

        - no tiene un REP vigente ligado a ese pago;
        - es desde el corte (con la fecha del pago);
        - ya venció su plazo (_limite_del_rep desde _inicio_del_plazo, con el
          «hoy» de México).

        Lo que deja de aparecer se resuelve con su motivo
        (_resolver_ppd_con_motivo): el pago es anterior al corte, ya tiene su
        REP, todavía está en plazo, la compra ya no califica o el pago ya no
        está aplicado. Lo que sigue apareciendo es el mismo registro, y se le
        reescriben en sitio la pista y el límite (CAMPOS_EN_SITIO_PPD).

        No se compara el importe del REP con el del pago: eso lo decide la liga
        REP→pago, que solo liga sola cuando cuadra. Una liga a mano con otro
        importe es decisión del contador.

        Devuelve si algún pago de estas compras todavía está en plazo: la
        revisión diaria programa entonces la del día en que vence."""
        compras = facturas.sudo().exists().filtered(lambda f: f.move_type in TIPOS_DE_COMPRA_PPD)
        if not compras:
            return False
        valido, corte = self._corte_ppd_sin_rep()
        if not valido:
            return False
        env = self.env
        hoy = _hoy_en_mexico(env)
        cfdi_de = {compra: compra._dsi_cfdi_ppd() for compra in compras}
        con_ppd = compras.filtered(lambda compra: cfdi_de[compra])
        pagos_de = con_ppd._dsi_pagos_aplicados() if con_ppd else {}
        pistas = self._pistas_de_rep(con_ppd)

        hallazgos, motivos, en_plazo = [], {}, False
        for compra in con_ppd:
            for e in pagos_de[compra]:
                clave = _clave_ppd(compra, e)
                desde = _inicio_del_plazo(e)
                limite = _limite_del_rep(desde)
                if corte and e['fecha'] < corte:
                    motivos[clave] = env._(
                        "El pago del %(fecha)s es anterior a la fecha de corte %(corte)s (parámetro %(parametro)s).",
                        fecha=tools.format_date(env, e['fecha']), corte=tools.format_date(env, corte),
                        parametro=PARAMETRO_CORTE_PPD)
                elif e['tiene_rep']:
                    motivos[clave] = env._(
                        "Ya tiene su REP, ligado a este pago: %(uuids)s.",
                        uuids=", ".join(e['lineas_rep'].payment_doc_id.mapped('uuid')))
                elif hoy <= limite:
                    en_plazo = True
                    if desde != e['fecha']:
                        motivos[clave] = env._(
                            "El pago es del %(fecha)s y su movimiento del banco del %(banco)s: el proveedor tiene "
                            "hasta el %(limite)s para emitir su REP.",
                            fecha=tools.format_date(env, e['fecha']), banco=tools.format_date(env, desde),
                            limite=tools.format_date(env, limite))
                    else:
                        motivos[clave] = env._(
                            "El pago es del %(fecha)s: el proveedor tiene hasta el %(limite)s para emitir su REP.",
                            fecha=tools.format_date(env, e['fecha']), limite=tools.format_date(env, limite))
                else:
                    hallazgos.append(self._hallazgo_de_ppd_sin_rep(
                        compra, cfdi_de[compra], e, clave, limite, pistas.get(compra.id)))

        self._resolver_ppd_con_motivo(compras, con_ppd, hallazgos, motivos, source)
        self._sincronizar(
            'ppd_sin_rep', [('move_id', 'in', compras.ids)], hallazgos, source, en_sitio=CAMPOS_EN_SITIO_PPD)
        return en_plazo

    @api.model
    def _resolver_ppd_con_motivo(self, compras, con_ppd, hallazgos, motivos, source):
        """Resuelve con su motivo los registros pendientes o revisados de
        `compras` que ya no son hallazgo. _sincronizar solo diría «Ya no
        aparece»; aquí se dice por qué:

        - la compra ya no está en `con_ppd`: se canceló o volvió a borrador, o
          su CFDI se desligó, se archivó, se canceló o no es PPD;
        - su clave tiene motivo en `motivos`: anterior al corte, ya tiene su
          REP o todavía en plazo;
        - si no: ese pago ya no está aplicado a la compra (se desconcilió, se
          canceló, volvió a borrador o se borró).

        Los que siguen con la misma clave y otra huella se le dejan a
        _sincronizar, que dice «Cambiaron los valores» y crea el nuevo."""
        Inconsistencia = self.sudo()
        vistas = {valores['dedupe_key'] for valores in hallazgos}
        no_califica = self.env._(
            "La compra ya no es una compra PPD publicada con su CFDI vigente en la Bóveda.")
        ya_no_aplicado = self.env._("El pago ya no está aplicado a esta compra.")
        por_motivo = defaultdict(lambda: Inconsistencia)
        for registro in Inconsistencia.search([
                ('kind', '=', 'ppd_sin_rep'), ('state', 'in', ('open', 'reviewed')),
                ('move_id', 'in', compras.ids)]):
            if registro.dedupe_key in vistas:
                continue
            if registro.move_id not in con_ppd:
                motivo = no_califica
            else:
                motivo = motivos.get(registro.dedupe_key) or ya_no_aplicado
            por_motivo[motivo] |= registro
        for motivo, registros in por_motivo.items():
            registros._resolver_solas(source, motivo=motivo)

    @api.model
    def _pistas_de_rep(self, compras):
        """{compra.id: (líneas sin ligar, empresas con REP)} de las `compras`,
        para que la observación diga qué hacer:

        - las líneas de REP recibidos vigentes de la compra que no se ligaron
          a ningún pago (por elegir, con otro importe o sin pago en Odoo).
          Quizá una es la de este pago y basta con «Elegir pago»;
        - las otras empresas del mismo RFC (matriz o sucursal) en cuya Bóveda
          hay un REP vigente de esta factura. Ese REP no se liga porque la liga
          es dentro de la empresa: hay que importarlo también en la de la
          compra.

        Son dos búsquedas con sudo para toda la tanda; la segunda casi nunca
        trae nada."""
        Linea = self.env['dsi.cfdi.payment.line'].sudo().with_context(active_test=False)
        Empresa = self.env['res.company'].sudo()
        if not compras:
            return {}
        vigente = [('payment_doc_id.active', '=', True), ('payment_doc_id.sat_status', '!=', 'canceled')]

        sin_ligar = defaultdict(lambda: Linea)
        for linea in Linea.search([
                ('invoice_id', 'in', compras.ids), ('direction', '=', 'received'),
                ('payment_link_state', 'in', ESTADOS_PENDIENTES)] + vigente):
            sin_ligar[linea.invoice_id.id] |= linea

        # Las líneas de otra empresa guardan el folio fiscal en mayúsculas y
        # no tienen factura: se buscan por el folio del CFDI de la compra.
        folio_de = {compra.id: (compra._dsi_cfdi_de_la_boveda().uuid or '').upper() for compra in compras}
        en_otras = defaultdict(lambda: Empresa)
        de_otras = Linea.search([
            ('invoice_in_other_company', '=', True),
            ('id_documento', 'in', [folio for folio in folio_de.values() if folio]),
        ] + vigente)
        if de_otras:
            por_folio = de_otras.grouped('id_documento')
            del_mismo_rfc = {}
            for compra in compras:
                otras = por_folio.get(folio_de[compra.id])
                if not otras:
                    continue
                empresa = compra.company_id
                if empresa not in del_mismo_rfc:
                    del_mismo_rfc[empresa] = _empresas_del_mismo_rfc(empresa)
                en_otras[compra.id] = (otras.company_id - empresa) & del_mismo_rfc[empresa]

        return {compra.id: (sin_ligar[compra.id], en_otras[compra.id]) for compra in compras}

    @api.model
    def _hallazgo_de_ppd_sin_rep(self, compra, cfdi, e, clave, limite, pista=None):
        """La fotografía de un pago `e` (de _dsi_pagos_aplicados) de la compra
        PPD sin su REP, vencido el `limite`.

        La huella es solo la fecha del pago: identifica al pago junto con la
        clave, y otra fecha es otro pago. Lo demás es presentación y se
        reescribe en el mismo registro (CAMPOS_EN_SITIO_PPD), sin quitarle su
        revisión:

        - el importe: que cambie un parcial no debe volver pendiente algo ya
          revisado;
        - la pista de qué hacer. Un REP sin ligar es de la compra y no de un
          pago: si fuera parte de la huella, el REP sin ligar de una
          parcialidad sustituiría los registros de todas las demás, y al
          ligarse los reabriría sin su revisión;
        - el límite y desde cuándo cuenta: la fecha del banco de un pago se
          puede conocer después (_inicio_del_plazo), y no lo vuelve otro pago.

        Va en el idioma de la empresa, aunque la detecte un usuario: el texto
        se reescribe en sitio y, en el de cada quien, la revisión diaria y el
        usuario se lo reescribirían uno al otro (como las notas de la liga,
        _env_de_la_empresa)."""
        env = self.with_context(lang=tools.get_lang(self.env, compra.company_id.partner_id.lang).code).env
        sin_ligar, empresas = pista or (self.env['dsi.cfdi.payment.line'], self.env['res.company'])
        pago = e['pago'] or e['asiento']
        desde = _inicio_del_plazo(e)
        valores = {
            'compra': compra.display_name,
            'uuid': cfdi.uuid,
            'pago': pago.display_name,
            'fecha': tools.format_date(env, e['fecha']),
            'importe': tools.format_amount(env, e['importe'], compra.currency_id)
            if e['importe'] is not None else env._("importe sin determinar"),
        }
        observacion = env._(
            "La compra %(compra)s (CFDI %(uuid)s) es PPD y el pago %(pago)s del %(fecha)s le aplicó "
            "%(importe)s, pero ningún REP vigente está ligado a ese pago.", **valores)
        if desde != e['fecha']:
            observacion += " " + env._(
                "El proveedor tenía hasta el %(limite)s para emitirlo: el día %(dia)s del mes siguiente al "
                "%(banco)s, la fecha del movimiento del banco del pago, que es cuando lo recibió.",
                limite=tools.format_date(env, limite), dia=DIA_LIMITE_DEL_REP, banco=tools.format_date(env, desde))
        else:
            observacion += " " + env._(
                "El proveedor tenía hasta el %(limite)s para emitirlo (el día %(dia)s del mes siguiente al "
                "pago).", limite=tools.format_date(env, limite), dia=DIA_LIMITE_DEL_REP)
        if sin_ligar:
            observacion += " " + env._(
                "La compra tiene REP que no están ligados a ningún pago (%s): si uno es de este pago, usa "
                "«Elegir pago» y esto se resuelve solo.", ", ".join(sin_ligar.payment_doc_id.mapped('uuid')))
        elif empresas:
            observacion += " " + env._(
                "La Bóveda de %(empresas)s tiene REP de esta factura: impórtalo también en %(empresa)s para "
                "que se ligue al pago.", empresas=", ".join(empresas.mapped('name')), empresa=compra.company_id.name)
        else:
            observacion += " " + env._(
                "Pídeselo al proveedor: al importarlo o descargarlo del SAT se liga al pago y esto se "
                "resuelve solo.")
        if e['sin_asiento']:
            observacion += " " + env._(
                "El pago aún no tiene asiento contable (no se ha conciliado con el banco): cuenta su fecha "
                "de registro.")
        return {
            'company_id': compra.company_id.id,
            'dedupe_key': clave,
            'fingerprint': f"fecha={e['fecha'].isoformat()}",
            'cfdi_document_id': cfdi.id,
            'move_id': compra.id,
            'account_payment_id': e['pago'].id or False,
            'payment_move_id': e['asiento'].id or False,
            'partner_id': compra.commercial_partner_id.id,
            'uuid': cfdi.uuid,
            'vault_value': env._("PPD, con REP sin ligar a un pago") if sin_ligar else env._(
                "PPD, sin REP de este pago"),
            'odoo_value': env._("Pago %(pago)s del %(fecha)s: %(importe)s", **valores),
            'observation': observacion,
        }

    # -------------------------------------------------------------------
    # Revisión diaria
    # -------------------------------------------------------------------

    @api.model
    def _pagos_ppd_sin_rep_por_sql(self, corte):
        """[(compra_id, fecha del pago)] de los pagos aplicados a compras PPD
        sin un REP vigente ligado, desde el `corte` (o todos si es None).

        Es la primera criba de la revisión diaria, en una sola consulta: solo
        las compras que salen aquí (y las que ya tienen registro) pasan por
        Python, que es quien decide. Por eso devuelve de más, nunca de menos:

        - Pago aplicado con la misma regla que la ayuda: un asiento publicado
          con origin_payment_id o statement_line_id conciliado con la cuenta
          por pagar o por cobrar de la compra (las dos, como la ayuda: una
          compra migrada de antes de que Odoo lo impidiera puede tener su
          plazo en una por cobrar), más los pagos sin asiento ligados a la
          compra. Así quedan fuera las notas de crédito, compensaciones,
          diferencias cambiarias y CABA.
        - La fecha es la del pago si lo hay; si no, la del asiento, como en la
          ayuda. Un pago sin asiento ya conciliado sale dos veces (por el
          banco y por el pago), y al menos una coincide con la de Python. El
          plazo de Python puede correr desde una fecha del banco posterior
          (_inicio_del_plazo), nunca anterior: con la del pago, el vencido
          de aquí sigue siendo un superconjunto.
        - Sin REP: ninguna línea ligada de un REP vigente que apunte a ese
          pago o a ese asiento.

        account_type se lee de la cuenta: en la línea es un related sin
        guardar. Los estados de un pago aplicado salen de la selección (en 20
        cambiaron), igual que en la ayuda."""
        self.env.flush_all()
        self.env.cr.execute(SQL("""
            WITH compras AS (
                SELECT DISTINCT m.id
                  FROM cfdi_document cd
                  JOIN account_move m ON m.id = cd.move_id
                 WHERE cd.active AND cd.move_type = 'I' AND cd.sat_status != 'canceled'
                   AND upper(btrim(cd.payment_method)) = 'PPD'
                   AND m.move_type IN %(tipos)s AND m.state = 'posted'
                   AND m.payment_state IN %(estados_compra)s
            ),
            aplicados AS (
                SELECT c.id AS compra_id, cpm.id AS asiento_id, cpm.origin_payment_id AS pago_id,
                       COALESCE(pay.date, cpm.date) AS fecha
                  FROM compras c
                  JOIN account_move_line fl ON fl.move_id = c.id
                  JOIN account_account cta ON cta.id = fl.account_id
                                          AND cta.account_type IN ('asset_receivable', 'liability_payable')
                  JOIN LATERAL (
                        SELECT part.debit_move_id AS linea_id
                          FROM account_partial_reconcile part
                         WHERE part.credit_move_id = fl.id
                        UNION ALL
                        SELECT part.credit_move_id
                          FROM account_partial_reconcile part
                         WHERE part.debit_move_id = fl.id
                       ) contra ON TRUE
                  JOIN account_move_line cl ON cl.id = contra.linea_id
                  JOIN account_move cpm ON cpm.id = cl.move_id
             LEFT JOIN account_payment pay ON pay.id = cpm.origin_payment_id
                 WHERE cpm.id != c.id AND cpm.state = 'posted'
                   AND (cpm.origin_payment_id IS NOT NULL OR cpm.statement_line_id IS NOT NULL)
                UNION
                SELECT c.id, NULL, pay.id, pay.date
                  FROM compras c
                  JOIN account_move__account_payment rel ON rel.invoice_id = c.id
                  JOIN account_payment pay ON pay.id = rel.payment_id
                 WHERE pay.move_id IS NULL AND pay.state IN %(aplicados)s
            )
            SELECT a.compra_id, a.fecha
              FROM aplicados a
             WHERE (%(corte)s::date IS NULL OR a.fecha >= %(corte)s::date)
               AND NOT EXISTS (
                    SELECT 1
                      FROM dsi_cfdi_payment_line l
                      JOIN cfdi_document rep ON rep.id = l.payment_doc_id
                     WHERE l.invoice_id = a.compra_id AND l.payment_link_state IN %(ligados)s
                       AND rep.active AND rep.sat_status != 'canceled'
                       AND (l.account_payment_id = a.pago_id OR l.payment_move_id = a.asiento_id))
        """, tipos=TIPOS_DE_COMPRA_PPD, estados_compra=ESTADOS_DE_PAGO_DE_LA_COMPRA,
            aplicados=_estados_de_pago_aplicado(self.env), corte=corte, ligados=ESTADOS_LIGADOS))
        return self.env.cr.fetchall()

    @api.model
    def _detectar_ppd_sin_rep(self):
        """Las compras con algún pago sin REP ya vencido (según la consulta),
        más las que tenían un registro sin resolver, para resolverlo si ya no
        aparece. Es la única que ve lo que pasa solo del lado de los pagos en
        una compra sin REP: un pago nuevo o con fecha atrasada, uno que se
        desconcilia, se cancela o cambia de fecha, y la compra o su CFDI que
        dejan de calificar.

        Si hay pagos que todavía están en plazo (según la consulta o, si su
        plazo corre desde la fecha del banco, según Python), la revisión se
        programa para el día en que vencen (_programar_vencimiento_de_reps).

        Por tandas de LOTE_DE_COMPRAS_PPD, cada una en su savepoint:

        - una tanda que falla se deshace sola, queda un aviso en el log y
          sigue la siguiente. Cada tanda resuelve solo los registros de sus
          compras, que vio completas: los de la tanda que falló quedan como
          estaban, no se resuelven por no haberse visto;
        - un choque con otra transacción se relanza: lo convierte en aviso y
          revisión reprogramada _cron_detectar_inconsistencias, como en los
          demás detectores;
        - sin commit entre tandas: el detector corre dentro del savepoint de
          la revisión, y el avance con commit es entre detectores
          (_avance_del_cron);
        - al terminar cada una se suelta la caché del ORM (invalidate_all
          escribe antes lo pendiente): sin esto la memoria crecía con cada
          tanda. Después solo se usan ids y fechas, nada leído antes."""
        valido, corte = self._corte_ppd_sin_rep()
        if not valido:
            return
        hoy = _hoy_en_mexico(self.env)
        filas = self._pagos_ppd_sin_rep_por_sql(corte)
        en_plazo = any(_limite_del_rep(fecha) >= hoy for _compra_id, fecha in filas)
        vencidas = sorted({compra_id for compra_id, fecha in filas if _limite_del_rep(fecha) < hoy})
        Factura = self.env['account.move'].sudo()
        ids_de_compras = (Factura.browse(vencidas) | self.sudo().search([
            ('kind', '=', 'ppd_sin_rep'), ('state', '!=', 'resolved'),
        ]).move_id).ids
        for ids in split_every(LOTE_DE_COMPRAS_PPD, ids_de_compras):
            # Lo pendiente de antes se escribe fuera del savepoint: si eso
            # falla, no es la tanda la que falló.
            self.env.cr.flush()
            try:
                with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                    en_plazo |= bool(self._sincronizar_ppd_sin_rep(Factura.browse(ids), 'cron'))
            except ERRORES_DE_CONCURRENCIA:
                raise
            except Exception:  # pylint: disable=broad-except
                _logger.warning(
                    "Compras PPD pagadas sin REP: falló la revisión de la tanda de compras %s y se deshizo; "
                    "sus registros quedan como estaban y sigue la siguiente tanda.", list(ids), exc_info=True)
            self.env.invalidate_all()
        if en_plazo:
            self._programar_vencimiento_de_reps(hoy)

    @api.model
    def _programar_vencimiento_de_reps(self, hoy):
        """Pide la revisión de discrepancias para el próximo día 6 a las 00:05
        de México, cuando vencen los pagos que hoy están en plazo. Sin esto
        aparecerían en la primera revisión diaria cuyo «hoy» de México ya sea
        día 6: hasta un día tarde, según la hora del cron.

        Se pide una sola vez por fecha: ir.cron._trigger no evita duplicados, y
        esto corre en cada revisión."""
        dia = hoy.replace(day=DIA_LIMITE_DEL_REP + 1)
        if hoy >= dia:
            dia = (hoy + relativedelta(months=1)).replace(day=DIA_LIMITE_DEL_REP + 1)
        cuando = datetime.combine(dia, time(0, 5), tzinfo=ZONA_DEL_CFDI).astimezone(timezone.utc).replace(tzinfo=None)
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies', raise_if_not_found=False)
        if cron and not self.env['ir.cron.trigger'].sudo().search_count([
                ('cron_id', '=', cron.id), ('call_at', '=', cuando)]):
            cron.sudo()._trigger(cuando)

    @api.model
    def _detectores(self):
        """Al final: así ve las ligas de los REP con sus facturas y con sus
        pagos que la misma revisión acaba de dejar al día."""
        return list(super()._detectores()) + ['_detectar_ppd_sin_rep']
