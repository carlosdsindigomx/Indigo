"""Ayudantes de las pruebas de pagos (REP ↔ pago de Odoo, compras PPD).

Solo funciones, sin clases TestCase: si las tuviera, el cargador de pruebas
correría sus pruebas también desde cada archivo que las importa. Por eso este
archivo no va en tests/__init__.py.

Arman lo mismo que test_rep_facturas.py (empresa mexicana en pesos con el
catálogo de México), más los pagos de Odoo: «Registrar pago», un movimiento
del banco conciliado directo con la factura y un pago sin asiento como el del
módulo accountant.
"""
from datetime import date, timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests.common import new_test_user
from odoo.tools import float_compare

from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_cfdi_vault.models.cfdi_payment_line import PARAMETRO_CORTE
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import RFC_EMISOR, RFC_RECEPTOR
from .test_factura_como_odoo import cfdi_xml
from .test_rep_facturas import GRUPOS_BOVEDA, RFC_CLIENTE, rep_xml


def empresa_mx(cls, login):
    """En setUpClass: la empresa MX en pesos con el catálogo de México, USD
    activo, la licencia vigente, el diario de compras y el banco, el proveedor
    y el cliente, un Usuario de la Bóveda (`usuario`) y uno que solo factura
    (`facturacion`), y el corte de «Pago (REP) de factura fuera de Odoo» fijo
    en 2026-10-01."""
    mxn = cls.env.ref('base.MXN')
    mxn.active = True
    cls.mxn = mxn
    cls.usd = cls.env.ref('base.USD')
    cls.usd.active = True
    cls.company = cls.env['res.company'].create({
        'name': f"Empresa MX de prueba ({login})",
        'country_id': cls.env.ref('base.mx').id,
        'currency_id': mxn.id,
    })
    cls.company.partner_id.vat = RFC_RECEPTOR
    cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
    cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
    cls.licencia = activar_licencia(cls, cls.company)
    cls.journal = cls.env['account.journal'].search([
        ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
    ], order='sequence, id', limit=1)
    cls.banco = cls.env['account.journal'].search([
        ('type', '=', 'bank'), ('company_id', '=', cls.company.id),
    ], order='sequence, id', limit=1)
    mx = cls.env.ref('base.mx')
    cls.proveedor = cls.env['res.partner'].create({
        'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1, 'country_id': mx.id,
    })
    cls.cliente = cls.env['res.partner'].create({
        'name': "Cliente de Prueba", 'vat': RFC_CLIENTE, 'customer_rank': 1, 'country_id': mx.id,
    })
    cls.usuario = new_test_user(
        cls.env, login=login, groups=GRUPOS_BOVEDA,
        company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
    cls.facturacion = new_test_user(
        cls.env, login=f'{login}-facturacion', groups='base.group_user,account.group_account_invoice',
        company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
    # Odoo 20 cambió set_param por accesores tipados.
    cp = cls.env['ir.config_parameter'].sudo()
    (cp.set_str if hasattr(cp, 'set_str') else cp.set_param)(PARAMETRO_CORTE, '2026-10-01')


def _documento_odoo(case, factura, uuid, estado='invoice_received'):
    """El documento CFDI de Odoo de la factura, como lo deja su import (o su
    timbrado, en una venta): de su XML sale el folio fiscal."""
    adjunto = case.env['ir.attachment'].create({
        'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'application/xml',
        'res_model': 'account.move', 'res_id': factura.id,
    })
    Documento = case.env['l10n_mx_edi.document']
    valores = {
        'move_id': factura.id,
        'invoice_ids': [Command.set(factura.ids)],
        'state': estado,
        'sat_state': 'valid',
        'attachment_id': adjunto.id,
        'datetime': fields.Datetime.now(),
    }
    if 'company_id' in Documento._fields:
        valores['company_id'] = factura.company_id.id
    return Documento.create(valores)


def _factura(case, move_type, partner, importe, moneda=None, fecha=date(2026, 6, 15), uuid=None,
             estado='invoice_received'):
    valores = {
        'move_type': move_type,
        'partner_id': partner.id,
        'invoice_date': fecha,
        'invoice_line_ids': [Command.create({
            'name': "Servicio", 'quantity': 1, 'price_unit': importe, 'tax_ids': [Command.clear()],
        })],
    }
    if moneda:
        valores['currency_id'] = moneda.id
    factura = case.env['account.move'].create(valores)
    factura.action_post()
    uuid = uuid or case._uuid()
    _documento_odoo(case, factura, uuid, estado)
    return uuid, factura


def compra(case, importe=1000.0, moneda=None, fecha=date(2026, 6, 15), uuid=None):
    """Una factura de proveedor publicada, sin impuestos, con su folio fiscal
    (el documento de Odoo de su import). Devuelve (folio, compra)."""
    return _factura(case, 'in_invoice', case.proveedor, importe, moneda=moneda, fecha=fecha, uuid=uuid)


def venta(case, importe=1000.0, fecha=date(2026, 6, 15), uuid=None):
    """Una factura de cliente publicada y timbrada. Devuelve (folio, venta)."""
    return _factura(case, 'out_invoice', case.cliente, importe, fecha=fecha, uuid=uuid, estado='invoice_sent')


def guardar(case, xml, direction='received', company=None, sat_status='valid'):
    """El CFDI en la Bóveda, como lo deja la importación: sus líneas de pago
    se leen al guardarlo."""
    company = company or case.company
    Doc = case.env['cfdi.document']
    vals = Doc._parse_cfdi_xml(xml)
    adjunto = case.env['ir.attachment'].create({
        'name': f"{vals['uuid']}.xml", 'raw': xml, 'mimetype': 'text/xml',
    })
    datos = {k: v for k, v in vals.items() if k != 'parse_state'}
    datos.update({
        'company_id': company.id,
        'direction': direction,
        'xml_attachment_id': adjunto.id,
        'currency_id': vals['currency_id'] or company.currency_id.id,
        'sat_status': sat_status,
    })
    doc = Doc.create(datos)
    adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
    return doc


def rep(case, *nodos, fecha='2026-10-13', direction='received', ligar=True):
    """Un REP con estos nodos Pago, guardado y ligado. Cada nodo es (monto,
    moneda, tipo de cambio, [docto(...)], {'fecha': ..., 'forma': ...}); el
    último elemento es opcional. Con ligar=False solo se guarda."""
    emisor, receptor = (RFC_EMISOR, RFC_RECEPTOR) if direction == 'received' else (RFC_RECEPTOR, RFC_CLIENTE)
    doc = guardar(case, rep_xml(case._uuid(), list(nodos), emisor=emisor, receptor=receptor, fecha=fecha),
                  direction=direction)
    if ligar:
        doc._ligar_reps()
    return doc


def pagar(case, facturas, importe=None, fecha='2026-10-10', agrupado=True, moneda=None):
    """«Registrar pago» de Odoo sobre `facturas`, desde el banco, y el
    precommit (la liga con los REP) como al confirmar la transacción.
    Devuelve los pagos."""
    valores = {'payment_date': fecha, 'journal_id': case.banco.id, 'group_payment': agrupado}
    if importe is not None:
        valores.update(amount=importe, payment_difference_handling='open')
    if moneda:
        valores['currency_id'] = moneda.id
    pagos = case.env['account.payment.register'].with_context(
        active_model='account.move', active_ids=facturas.ids).create(valores)._create_payments()
    case.env.cr.flush()
    return pagos


def pagar_con_extracto(case, factura, importe, fecha):
    """Un movimiento del banco conciliado directo con la factura (o con varias
    del mismo proveedor), sin account.payment (patrón de
    account/tests/common.py, pay_with_statement_line). Devuelve el
    movimiento."""
    movimiento = case.env['account.bank.statement.line'].create({
        'journal_id': case.banco.id,
        'amount': -importe,
        'date': fecha,
        'partner_id': factura.partner_id.id,
        'payment_ref': "Pago por el banco",
    })
    _liquidez, suspenso, _otras = movimiento.with_context(skip_account_move_synchronization=True)._seek_for_lines()
    por_pagar = factura.line_ids.filtered(lambda l: l.account_type in ('asset_receivable', 'liability_payable'))
    suspenso.account_id = por_pagar.account_id
    (suspenso + por_pagar).reconcile()
    case.env.cr.flush()
    return movimiento


def pagar_sin_asiento(case, facturas, **kw):
    """Un pago sin asiento, como los deja el módulo accountant hasta conciliar
    el banco: sin cuenta de pagos pendientes y con la factura «en proceso de
    pago»."""
    case.banco.outbound_payment_method_line_ids.payment_account_id = False
    with patch.object(type(case.env['account.move']), '_get_invoice_in_payment_state', lambda self: 'in_payment'):
        pagos = pagar(case, facturas, **kw)
    case.assertFalse(pagos.move_id)
    return pagos


def tipo_de_cambio(case, fecha, mxn_por_usd):
    """Pesos por dólar que rigen desde esa fecha, para la empresa. En 20 el
    tipo de cambio de un día rige desde el siguiente (Odoo solo toma los de
    antes de la fecha): si el de `fecha` no rige ese mismo día, queda con la
    fecha del día anterior."""
    tasa = case.env['res.currency.rate'].create({
        'name': fecha,
        'currency_id': case.usd.id,
        'company_id': case.company.id,
        'inverse_company_rate': mxn_por_usd,
    })
    dia = fields.Date.to_date(fecha)
    rige = case.env['res.currency']._get_conversion_rate(case.mxn, case.usd, case.company, dia)
    if float_compare(rige, tasa.rate, precision_digits=6):
        tasa.name = dia - timedelta(days=1)
    return tasa


def copias_en(case, registro, rep_doc):
    """Los adjuntos de `registro` con el XML del REP."""
    contenido = _bytes_de_adjunto(rep_doc.xml_attachment_id)
    return case.env['ir.attachment'].search([
        ('res_model', '=', registro._name), ('res_id', '=', registro.id),
    ]).filtered(lambda a: _bytes_de_adjunto(a) == contenido)


def sat_dice(case, docs, valor):
    """La verificación SAT de `docs` con la respuesta `valor`."""
    def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, _uuid):
        return {'value': valor}
    with patch.object(case.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
        docs._fetch_and_store_sat_status()
