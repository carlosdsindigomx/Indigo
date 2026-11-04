"""Los pagos (REP) ligados a las facturas que pagan.

Existe porque la Bóveda guardaba el CFDI de Pago solo con su monto: no leía qué
facturas pagaba, y en la factura no se veía nada. Ahora cada DoctoRelacionado
es una línea propia ligada a su factura, con una copia del XML en la factura y,
si el REP es recibido, un documento «Pago recibido» en la pestaña CFDI de las
compras que paga.

Lo que más cuidan estas pruebas:

  * leer Pagos 1.0 y 2.0 tal como llegan: TipoCambioDR o EquivalenciaDR, sin
    NumParcialidad, folio fiscal en minúsculas;
  * el documento «Pago recibido» no le cambia a la factura su folio fiscal ni
    su estado CFDI o SAT, y no trae botones de Odoo (cancelar con el PAC de la
    empresa, reintentar, imprimir);
  * un REP emitido no lleva documento: en una venta timbrada, «Actualizar
    pagos» de Odoo lo borraría;
  * un REP recibido solo se liga a compras y uno emitido, solo a ventas, de la
    misma empresa;
  * el REP y su factura pueden llegar en cualquier orden, y ligar dos veces da
    lo mismo (una copia del XML por factura, un documento por REP);
  * corregir la empresa o la dirección del REP lo vuelve a ligar, y no se
    lleva el documento de la otra empresa;
  * «Pago (REP) de factura fuera de Odoo» solo para REP desde la fecha de
    corte; los anteriores, en la lista en vivo.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_estado_boveda.py: la conversión pasa por el import de CFDI de Odoo.
"""
import contextlib
import io
import zipfile
from datetime import date
from unittest.mock import patch

from freezegun import freeze_time
from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mimetypes
from odoo.tools.safe_eval import safe_eval

from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_cfdi_vault.models.cfdi_payment_line import PARAMETRO_CORTE
from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import RFC_EMISOR, RFC_RECEPTOR
from .test_factura_como_odoo import cfdi_xml

GRUPOS_BOVEDA = 'base.group_user,dsi_cfdi_vault.group_cfdi_vault_user'
RFC_CLIENTE = 'CLI010101AA1'


def odoo_sin_libmagic():
    """Sin libmagic (python-magic sin la biblioteca del sistema, como en un Mac
    sin Homebrew), Odoo 20 no puede publicar en una factura el XML que adjuntó
    un usuario que no es administrador: Odoo le pone text/plain y, al elegir
    el adjunto principal, account.move le pasa `att.raw` a `guess_mimetype`.
    En 20 eso es un BinaryValue y su respaldo sin libmagic lo recorre como
    bytes (TypeError). Le pasa igual a Odoo solo, sin la Bóveda; con libmagic
    (Odoo.sh) no. Aquí se le da el contenido en bytes para poder probar la
    conversión como Usuario de la Bóveda en cualquier entorno. En 18 y 19
    `raw` ya son bytes y esto no cambia nada."""
    if getattr(mimetypes, 'magic', None) is not None:
        return contextlib.nullcontext()
    original = mimetypes.guess_mimetype
    return patch('odoo.addons.account.models.account_move.guess_mimetype',
                 lambda datos, *args, **kwargs: original(getattr(datos, 'content', datos), *args, **kwargs))


def docto(uuid, pagado, saldo_anterior=None, parcialidad='1', moneda='MXN', equivalencia='1', version='4.0'):
    """Los atributos de un DoctoRelacionado. En Pagos 1.0 la equivalencia es
    TipoCambioDR y solo va si la moneda es otra; en 2.0, EquivalenciaDR,
    siempre. Con parcialidad=None no lleva NumParcialidad."""
    saldo_anterior = pagado if saldo_anterior is None else saldo_anterior
    atributos = {'IdDocumento': uuid, 'MonedaDR': moneda}
    if version == '3.3':
        atributos['MetodoDePagoDR'] = 'PPD'
        if equivalencia != '1':
            atributos['TipoCambioDR'] = equivalencia
    else:
        atributos['EquivalenciaDR'] = equivalencia
        atributos['ObjetoImpDR'] = '01'
    if parcialidad:
        atributos['NumParcialidad'] = parcialidad
    if pagado is not None:
        atributos.update({
            'ImpSaldoAnt': f"{saldo_anterior:.2f}",
            'ImpPagado': f"{pagado:.2f}",
            'ImpSaldoInsoluto': f"{saldo_anterior - pagado:.2f}",
        })
    return atributos


def rep_xml(uuid, pagos, version='4.0', emisor=RFC_EMISOR, receptor=RFC_RECEPTOR, fecha='2026-10-13'):
    """Un CFDI de Pago con sus DoctoRelacionado. `pagos` es una lista de
    (monto, moneda, tipo de cambio, [docto(...)]), con un quinto elemento
    opcional {'fecha': 'AAAA-MM-DD', 'forma': 'NN'} para la FechaPago y la
    FormaDePagoP del nodo (por omisión 2026-10-10 y 03); con version='3.3'
    arma Pagos 1.0, que no tiene Totales."""
    if version == '3.3':
        ns_cfdi, ns_pago, p = 'http://www.sat.gob.mx/cfd/3', 'http://www.sat.gob.mx/Pagos', 'pago10'
    else:
        ns_cfdi, ns_pago, p = 'http://www.sat.gob.mx/cfd/4', 'http://www.sat.gob.mx/Pagos20', 'pago20'
    nodos = []
    for monto, moneda, tipo_cambio, doctos, *resto in pagos:
        extra = resto[0] if resto else {}
        hijos = ''.join(
            f'<{p}:DoctoRelacionado ' + ' '.join(f'{k}="{v}"' for k, v in d.items()) + '/>' for d in doctos)
        nodos.append(
            f'<{p}:Pago FechaPago="{extra.get("fecha", "2026-10-10")}T12:00:00" '
            f'FormaDePagoP="{extra.get("forma", "03")}" MonedaP="{moneda}" '
            f'TipoCambioP="{tipo_cambio}" Monto="{monto:.2f}">{hijos}</{p}:Pago>')
    totales = '' if version == '3.3' else (
        f'<{p}:Totales MontoTotalPagos="{sum(nodo[0] * float(nodo[2]) for nodo in pagos):.2f}"/>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="{ns_cfdi}" xmlns:{p}="{ns_pago}"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="{version}" Fecha="{fecha}T15:07:09" SubTotal="0" Total="0"
                  Moneda="XXX" TipoDeComprobante="P">
  <cfdi:Emisor Rfc="{emisor}" Nombre="Emisor del pago"/>
  <cfdi:Receptor Rfc="{receptor}" Nombre="Receptor del pago"/>
  <cfdi:Conceptos>
    <cfdi:Concepto Cantidad="1" ClaveProdServ="84111506" Descripcion="Pago" Importe="0" ValorUnitario="0"/>
  </cfdi:Conceptos>
  <cfdi:Complemento>
    <{p}:Pagos Version="{'1.0' if version == '3.3' else '2.0'}">{totales}{''.join(nodos)}</{p}:Pagos>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/>
  </cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


@tagged('post_install', '-at_install')
class TestRepFacturas(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
        ], order='sequence, id', limit=1)
        mx = cls.env.ref('base.mx')
        cls.proveedor = cls.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1, 'country_id': mx.id,
        })
        cls.cliente = cls.env['res.partner'].create({
            'name': "Cliente de Prueba", 'vat': RFC_CLIENTE, 'customer_rank': 1, 'country_id': mx.id,
        })
        cls.usuario = new_test_user(
            cls.env, login='boveda-rep', groups=GRUPOS_BOVEDA,
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.Doc = cls.env['cfdi.document']
        cls.Linea = cls.env['dsi.cfdi.payment.line']
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.Documento = cls.env['l10n_mx_edi.document']
        # La fecha de corte la fija la instalación (hoy). Aquí, una fija: los
        # REP de octubre de 2026 van después y los de septiembre, antes.
        # Odoo 20 cambió set_param por accesores tipados.
        cp = cls.env['ir.config_parameter'].sudo()
        (cp.set_str if hasattr(cp, 'set_str') else cp.set_param)(PARAMETRO_CORTE, '2026-10-01')
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return f"CCCCCCCC-5555-4000-8000-{self.contador:012d}"

    def _guardar(self, xml, direction='received', company=None, sat_status='not_verified'):
        """El CFDI en la Bóveda, como lo deja la importación: sus líneas de
        pago se leen al guardarlo."""
        company = company or self.company
        vals = self.Doc._parse_cfdi_xml(xml)
        adjunto = self.env['ir.attachment'].create({
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
        doc = self.Doc.create(datos)
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _rep(self, *pagados, direction='received', company=None, fecha='2026-10-13', version='4.0'):
        """REP de un solo pago con un DoctoRelacionado por (folio fiscal,
        importe) de `pagados`."""
        uuid = self._uuid()
        emisor, receptor = (RFC_EMISOR, RFC_RECEPTOR) if direction == 'received' else (RFC_RECEPTOR, RFC_CLIENTE)
        total = sum(importe for _folio, importe in pagados)
        xml = rep_xml(uuid, [(total, 'MXN', '1', [docto(f, i, version=version) for f, i in pagados])],
                      version=version, emisor=emisor, receptor=receptor, fecha=fecha)
        return self._guardar(xml, direction=direction, company=company)

    def _ingreso(self, uuid=None, direction='received', company=None, rfc_receptor=RFC_RECEPTOR):
        uuid = uuid or self._uuid()
        return self._guardar(cfdi_xml(uuid, rfc_receptor=rfc_receptor).encode(), direction=direction,
                             company=company, sat_status='valid')

    def _compra(self, uuid=None):
        """CFDI de ingreso recibido convertido en factura de proveedor."""
        doc = self._ingreso(uuid)
        return doc, doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)

    def _documento_odoo(self, factura, estado, sat_state, uuid):
        """El documento CFDI de Odoo de la factura: el que deja su timbrado o
        su import. De su XML sale el folio fiscal."""
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'application/xml',
            'res_model': 'account.move', 'res_id': factura.id,
        })
        valores = {
            'move_id': factura.id,
            'invoice_ids': [Command.set(factura.ids)],
            'state': estado,
            'sat_state': sat_state,
            'attachment_id': adjunto.id,
            'datetime': fields.Datetime.now(),
        }
        if 'company_id' in self.Documento._fields:
            valores['company_id'] = factura.company_id.id
        return self.Documento.create(valores)

    def _factura(self, move_type, partner):
        factura = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': partner.id,
            'invoice_date': date(2026, 6, 15),
            'invoice_line_ids': [Command.create({
                'name': "Servicio", 'quantity': 1, 'price_unit': 1000.0, 'tax_ids': [Command.clear()],
            })],
        })
        factura.action_post()
        return factura

    def _venta_timbrada(self, uuid=None):
        uuid = uuid or self._uuid()
        venta = self._factura('out_invoice', self.cliente)
        self._documento_odoo(venta, 'invoice_sent', 'valid', uuid)
        return uuid, venta

    def _compra_nativa(self, uuid=None):
        """Compra que entró por el import de Odoo, sin pasar por la Bóveda."""
        uuid = uuid or self._uuid()
        compra = self._factura('in_invoice', self.proveedor)
        self._documento_odoo(compra, 'invoice_received', 'valid', uuid)
        return uuid, compra

    def _importar(self, *xmls, usuario=None):
        """El asistente «Importar CFDI» con un ZIP, en el orden dado."""
        contenido = io.BytesIO()
        with zipfile.ZipFile(contenido, 'w') as zf:
            for n, xml in enumerate(xmls):
                zf.writestr(f"{n:02d}.xml", xml)
        Asistente = self.env['cfdi.import.wizard']
        if usuario:
            Asistente = Asistente.with_user(usuario)
        adjunto = Asistente.env['ir.attachment'].create({'name': 'lote.zip', 'raw': contenido.getvalue()})
        asistente = Asistente.create({'company_id': self.company.id, 'attachment_ids': [Command.set(adjunto.ids)]})
        asistente.action_import()
        return asistente.result_ids.cfdi_document_id

    def _copias(self, factura, rep):
        """Los adjuntos de la factura con el XML del REP."""
        contenido = _bytes_de_adjunto(rep.xml_attachment_id)
        return self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'), ('res_id', '=', factura.id),
        ]).filtered(lambda a: _bytes_de_adjunto(a) == contenido)

    def _estado_cfdi(self, factura):
        return (factura.l10n_mx_edi_cfdi_uuid, factura.l10n_mx_edi_cfdi_state,
                factura.l10n_mx_edi_cfdi_sat_state, factura.l10n_mx_edi_cfdi_attachment_id)

    def _sat_dice(self, docs, valor):
        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, _uuid):
            return {'value': valor}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            docs._fetch_and_store_sat_status()

    def _sin_factura(self, reps):
        return self.Inconsistencia.search([
            ('kind', '=', 'rep_sin_factura'), ('cfdi_document_id', 'in', reps.ids)])

    # -- Lectura del complemento ------------------------------------------

    def test_pagos_2_0_lee_cada_documento_relacionado(self):
        """Dos nodos Pago: el primero paga dos facturas (una con el folio en
        minúsculas, otra sin NumParcialidad) y el segundo, una en pesos y
        otra en dólares con la equivalencia en los diez decimales que permite
        el SAT."""
        f1, f2, f3, f4 = (self._uuid() for _i in range(4))
        xml = rep_xml(self._uuid(), [
            (300.0, 'MXN', '1', [docto(f1.lower(), 100.0, 250.0, parcialidad='2'),
                                 docto(f2, 200.0, parcialidad=None)]),
            (50.0, 'USD', '18.5', [docto(f3, 1000.0, 1000.0, moneda='MXN', equivalencia='20'),
                                   docto(f4, 6264.0, moneda='USD', equivalencia='0.0504032258')]),
        ])
        rep = self._guardar(xml)
        lineas = rep.dsi_payment_line_ids.sorted(lambda l: (l.pago_idx, l.docto_idx))
        self.assertEqual(
            [(l.pago_idx, l.docto_idx, l.id_documento, l.num_parcialidad) for l in lineas],
            [(1, 1, f1, 2), (1, 2, f2, 0), (2, 1, f3, 1), (2, 2, f4, 1)])
        uno, _dos, tres, cuatro = lineas
        lineas.invalidate_recordset()
        self.assertEqual(cuatro.equivalencia_dr, 0.0504032258, "sin redondear a seis decimales")
        self.assertEqual((uno.imp_saldo_ant, uno.imp_pagado, uno.imp_saldo_insoluto), (250.0, 100.0, 150.0))
        self.assertEqual((uno.fecha_pago, uno.forma_pago, uno.moneda_p, uno.monto_pago),
                         (date(2026, 10, 10), '03', 'MXN', 300.0))
        self.assertEqual((tres.moneda_p, tres.currency_p_id, tres.tipo_cambio_p, tres.monto_pago),
                         ('USD', self.usd, 18.5, 50.0))
        self.assertEqual((tres.moneda_dr, tres.currency_id.name, tres.equivalencia_dr), ('MXN', 'MXN', 20.0))
        self.assertEqual(set(lineas.mapped('link_state')), {'missing'})
        self.assertEqual(lineas.company_id, self.company)
        self.assertEqual(set(lineas.mapped('direction')), {'received'})

    def test_pagos_1_0_toma_el_tipo_de_cambio_dr(self):
        """Pagos 1.0: TipoCambioDR en lugar de EquivalenciaDR, sin
        NumParcialidad, folio en minúsculas, y un pago de un solo documento
        sin ImpPagado (vale el monto del pago en la moneda de la factura)."""
        f1, f2 = self._uuid(), self._uuid()
        xml = rep_xml(self._uuid(), [
            (100.0, 'USD', '18.5', [docto(f1.lower(), 1900.0, 1900.0, moneda='MXN', equivalencia='19.0',
                                          parcialidad=None, version='3.3')]),
            (40.0, 'USD', '18.5', [docto(f2, None, moneda='USD', parcialidad='3', version='3.3')]),
        ], version='3.3')
        rep = self._guardar(xml)
        uno, dos = rep.dsi_payment_line_ids.sorted('pago_idx')
        self.assertEqual((uno.id_documento, uno.num_parcialidad, uno.equivalencia_dr, uno.imp_pagado),
                         (f1, 0, 19.0, 1900.0))
        self.assertEqual((dos.id_documento, dos.num_parcialidad, dos.equivalencia_dr, dos.imp_pagado),
                         (f2, 3, 1.0, 40.0))
        self.assertEqual(dos.currency_id, self.usd)

    def test_un_rep_sin_documentos_relacionados_no_crea_lineas(self):
        rep = self._guardar(rep_xml(self._uuid(), []))
        self.assertEqual(rep.move_type, 'P')
        self.assertFalse(rep.dsi_payment_line_ids)
        rep._ligar_reps()
        self.assertFalse(rep.dsi_edi_document_id)

    # -- REP recibido: documento «Pago recibido» ----------------------------

    def test_rep_recibido_que_paga_dos_compras_es_un_solo_documento(self):
        _doc1, compra1 = self._compra()
        _doc2, compra2 = self._compra()
        antes = {compra: self._estado_cfdi(compra) for compra in compra1 | compra2}
        # Uno de los folios, en minúsculas, como lo timbran algunos PAC.
        rep = self._rep((compra1.l10n_mx_edi_cfdi_uuid.lower(), 500.0), (compra2.l10n_mx_edi_cfdi_uuid, 600.0))
        rep._ligar_reps()

        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, compra1 | compra2)
        documento = rep.dsi_edi_document_id
        self.assertEqual(len(documento), 1)
        self.assertEqual(documento.state, 'payment_received')
        self.assertEqual(documento.invoice_ids, compra1 | compra2)
        self.assertFalse(documento.move_id)
        self.assertEqual(documento.attachment_uuid.upper(), rep.uuid)
        # Su propia copia del XML, tal cual (sin reformatear).
        self.assertEqual(documento.attachment_id.res_model, 'l10n_mx_edi.document')
        self.assertEqual(documento.attachment_id.res_id, documento.id)
        self.assertEqual(_bytes_de_adjunto(documento.attachment_id), _bytes_de_adjunto(rep.xml_attachment_id))
        self.assertEqual(self.Documento.search_count([('state', '=', 'payment_received')]), 1)
        for compra in compra1 | compra2:
            compra.invalidate_recordset()
            self.assertIn(documento, compra.l10n_mx_edi_document_ids, "sale en la pestaña CFDI")
            self.assertEqual(self._estado_cfdi(compra), antes[compra], "folio, estado CFDI y SAT no cambian")
            self.assertEqual(compra.dsi_rep_count, 1)

    def test_el_documento_no_trae_botones_de_odoo(self):
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        self.assertFalse(documento.cancel_button_needed)
        self.assertFalse(documento.retry_button_needed)
        self.assertFalse(documento.show_button_needed)
        if 'print_button_needed' in documento._fields:
            self.assertFalse(documento.print_button_needed)
        # Ni Odoo lo consulta en el SAT: eso lo hace la Bóveda.
        if hasattr(self.Documento, '_get_update_sat_status_domains'):
            dominios = self.Documento._get_update_sat_status_domains(from_cron=False)
            self.assertFalse(any(documento.filtered_domain(dominio) for dominio in dominios))

    def test_el_pago_recibido_no_sale_en_los_documentos_cfdi_de_odoo(self):
        """Odoo 20 tiene el menú Contabilidad › Auditoría › México › Documentos
        CFDI, con lo que timbró, importó o descargó. Los «Pago recibido» salían
        ahí: Odoo guarda el dominio de esa acción al cargar l10n_mx_edi, antes
        que la Bóveda. En 18 y 19 ese menú no existe."""
        accion = self.env.ref('l10n_mx_edi.action_l10n_mx_edi_document', raise_if_not_found=False)
        if not accion:
            self.skipTest("Odoo 18 y 19 no tienen el menú Documentos CFDI de México.")
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        recibido = rep.dsi_edi_document_id
        de_la_compra = compra.l10n_mx_edi_invoice_document_ids.filtered(lambda d: d.state == 'invoice_received')
        self.assertEqual(recibido.state, 'payment_received')

        def en_el_menu():
            return self.Documento.search(safe_eval(accion.domain))

        self.assertNotIn(recibido, en_el_menu())
        self.assertIn(de_la_compra, en_el_menu(), "lo de Odoo sí sale")

        # Si l10n_mx_edi vuelve a cargar su acción, sale otra vez; la Bóveda se
        # actualiza con él (depende de él) y lo vuelve a quitar.
        de_odoo = [hoja for hoja in self.Documento._get_action_l10n_mx_edi_document_domain()
                   if hoja != ('state', '!=', 'payment_received')]
        accion.domain = str(de_odoo)
        self.assertIn(recibido, en_el_menu())
        self.Documento._quitar_pagos_recibidos_del_menu_de_odoo()
        self.assertNotIn(recibido, en_el_menu())
        self.assertIn(de_la_compra, en_el_menu())

    def test_una_copia_del_xml_por_factura_y_no_se_duplica(self):
        """Dos parcialidades de la misma compra en el mismo REP: una sola copia
        en esa compra. Ligar otra vez no agrega nada, ni mensajes."""
        _doc1, compra1 = self._compra()
        _doc2, compra2 = self._compra()
        uuid = self._uuid()
        folio1 = compra1.l10n_mx_edi_cfdi_uuid
        rep = self._guardar(rep_xml(uuid, [
            (400.0, 'MXN', '1', [docto(folio1, 400.0, 1160.0, parcialidad='1')]),
            (360.0, 'MXN', '1', [docto(folio1, 360.0, 760.0, parcialidad='2'),
                                 docto(compra2.l10n_mx_edi_cfdi_uuid, 1160.0)]),
        ]))
        mensajes = {compra: len(compra.message_ids) for compra in compra1 | compra2}
        rep._ligar_reps()
        copia1, copia2 = self._copias(compra1, rep), self._copias(compra2, rep)
        self.assertEqual((len(copia1), len(copia2)), (1, 1))
        lineas = rep.dsi_payment_line_ids
        self.assertEqual(lineas.filtered(lambda l: l.invoice_id == compra1).invoice_attachment_id, copia1)
        self.assertEqual(lineas.filtered(lambda l: l.invoice_id == compra2).invoice_attachment_id, copia2)
        documento = rep.dsi_edi_document_id

        rep._ligar_reps()
        self.Doc._backfill_lineas_de_pago()
        self.assertEqual(self._copias(compra1, rep), copia1)
        self.assertEqual(self._copias(compra2, rep), copia2)
        self.assertEqual(rep.dsi_edi_document_id, documento)
        self.assertEqual(self.Documento.search_count([('state', '=', 'payment_received')]), 1)
        self.assertEqual({compra: len(compra.message_ids) for compra in compra1 | compra2}, mensajes,
                         "sin mensajes en el chatter")

    def test_la_copia_del_rep_no_entra_a_documents(self):
        """Con Documents, cada XML que se adjunta a una factura entra a la
        carpeta de contabilidad: un REP que paga dos compras serían dos
        documentos con el mismo XML. La copia se crea con `no_document`, que
        sin Documents no hace nada."""
        _doc1, compra1 = self._compra()
        _doc2, compra2 = self._compra()
        rep = self._rep((compra1.l10n_mx_edi_cfdi_uuid, 500.0), (compra2.l10n_mx_edi_cfdi_uuid, 600.0))
        Adjunto = type(self.env['ir.attachment'])
        original = Adjunto.create
        en_facturas = []

        def espia(this, vals_list):
            for vals in [vals_list] if isinstance(vals_list, dict) else vals_list:
                if vals.get('res_model') == 'account.move':
                    en_facturas.append(this.env.context.get('no_document'))
            return original(this, vals_list)

        hay_documents = 'documents.document' in self.env
        documentos = hay_documents and self.env['documents.document'].search_count([])
        with patch.object(Adjunto, 'create', espia):
            rep._ligar_reps()
        self.assertEqual(len(rep.dsi_payment_line_ids.invoice_attachment_id), 2)
        self.assertEqual(en_facturas, [True, True])
        if hay_documents:
            self.assertEqual(self.env['documents.document'].search_count([]), documentos)

    def test_borrar_el_rep_y_volver_a_importarlo_reutiliza_su_documento(self):
        """Al borrar el CFDI de Pago su «Pago recibido» se queda en la compra.
        Si el REP vuelve a entrar (asistente o descarga), se reutiliza ese y
        la compra no queda con dos."""
        _doc, compra = self._compra()
        xml = rep_xml(self._uuid(), [(500.0, 'MXN', '1', [docto(compra.l10n_mx_edi_cfdi_uuid, 500.0)])])
        rep = self._importar(xml)
        documento = rep.dsi_edi_document_id
        self.assertEqual(documento.invoice_ids, compra)
        rep.unlink()
        self.assertTrue(documento.exists())

        otra_vez = self._importar(xml)
        self.assertEqual(otra_vez.dsi_payment_line_ids.invoice_id, compra)
        self.assertEqual(otra_vez.dsi_edi_document_id, documento)
        compra.invalidate_recordset()
        self.assertEqual(
            compra.l10n_mx_edi_invoice_document_ids.filtered(lambda d: d.state == 'payment_received'), documento)

    def test_la_fecha_del_documento_es_la_del_rep_y_no_se_mueve(self):
        """En la pestaña CFDI la fecha del «Pago recibido» es la del timbrado
        del REP (aquí, sin FechaTimbrado, la del comprobante: 15:07:09 en el
        centro de México, 21:07:09 en UTC), y no cambia cuando el REP se liga
        después a otra compra."""
        _doc1, compra1 = self._compra()
        uuid2 = self._uuid()
        rep = self._rep((compra1.l10n_mx_edi_cfdi_uuid, 500.0), (uuid2, 100.0), fecha='2026-10-02')
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        fecha = fields.Datetime.to_datetime('2026-10-02 21:07:09')
        self.assertEqual(documento.datetime, fecha)

        _doc2, compra2 = self._compra(uuid2)
        self.assertEqual(documento.invoice_ids, compra1 | compra2)
        self.assertEqual(documento.datetime, fecha)

    # -- El REP antes que su factura ----------------------------------------

    def test_rep_que_llega_antes_que_su_factura_en_el_mismo_lote(self):
        """El REP va primero en el ZIP y la factura que paga, después. Queda
        ligado a su CFDI en la Bóveda; al convertirlo en factura, a la
        factura. Todo como Usuario de la Bóveda, que solo lee las líneas."""
        uuid_factura, uuid_rep = self._uuid(), self._uuid()
        xml_rep = rep_xml(uuid_rep, [(500.0, 'MXN', '1', [docto(uuid_factura.lower(), 500.0, 1160.0)])])
        docs = self._importar(xml_rep, cfdi_xml(uuid_factura).encode(), usuario=self.usuario)
        rep = docs.filtered(lambda d: d.move_type == 'P')
        ingreso = docs - rep
        linea = rep.dsi_payment_line_ids
        self.assertEqual(linea.related_cfdi_id, ingreso)
        self.assertFalse(linea.invoice_id)
        self.assertEqual(linea.link_state, 'in_vault')
        self.assertEqual(ingreso.dsi_rep_line_ids, linea)
        self.assertFalse(rep.dsi_edi_document_id, "sin factura no hay documento")

        with odoo_sin_libmagic():
            compra = ingreso.with_user(self.usuario)._crear_factura_proveedor(
                journal=self.journal, partner=self.proveedor)
        self.assertEqual((linea.invoice_id, linea.link_state), (compra, 'linked'))
        self.assertEqual(rep.dsi_edi_document_id.invoice_ids, compra)
        self.assertEqual(len(self._copias(compra, rep)), 1)

    def test_rep_que_llega_antes_se_liga_al_llegar_su_factura(self):
        """El REP solo; después, en otra importación, el CFDI de la factura;
        al final la compra que entra por el import de Odoo (sin la Bóveda):
        la revisión diaria la liga."""
        uuid_factura = self._uuid()
        rep = self._rep((uuid_factura, 500.0))
        rep._ligar_reps()
        linea = rep.dsi_payment_line_ids
        self.assertEqual(linea.link_state, 'missing')

        ingreso = self._importar(cfdi_xml(uuid_factura).encode())
        self.assertEqual((linea.related_cfdi_id, linea.link_state), (ingreso, 'in_vault'))

        _uuid, compra = self._compra_nativa(uuid_factura)
        self.assertFalse(linea.invoice_id, "nada de la Bóveda se enteró")
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(linea.invoice_id, compra)
        self.assertEqual(rep.dsi_edi_document_id.invoice_ids, compra)

    def test_la_revision_diaria_pasa_la_liga_de_la_compra_cancelada_a_la_vigente(self):
        """La compra ligada se cancela y entra otra con el mismo folio por el
        import de Odoo: nada de la Bóveda se entera, y la revisión diaria
        pasa la línea y el «Pago recibido» a la vigente."""
        uuid = self._uuid()
        _uuid, cancelada = self._compra_nativa(uuid)
        rep = self._rep((uuid, 500.0))
        rep._ligar_reps()
        linea = rep.dsi_payment_line_ids
        self.assertEqual(linea.invoice_id, cancelada)
        cancelada.button_draft()
        cancelada.button_cancel()
        _uuid, vigente = self._compra_nativa(uuid)

        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(linea.invoice_id, vigente)
        self.assertEqual(rep.dsi_edi_document_id.invoice_ids, vigente)
        self.assertEqual(len(self._copias(vigente, rep)), 1)

    def test_una_liga_vigente_no_se_vuelve_a_revisar_a_diario(self):
        """Solo vuelven a ligarse las sospechosas: la ligada por su folio y la
        ligada por el CFDI de la Bóveda (la factura no lleva el folio) se
        quedan fuera."""
        uuid, compra = self._compra_nativa()
        por_folio = self._rep((uuid, 500.0))
        ingreso, convertida = self._compra()
        por_cfdi = self._rep((ingreso.uuid, 100.0))
        (por_folio | por_cfdi)._ligar_reps()
        self.assertEqual(por_folio.dsi_payment_line_ids.invoice_id, compra)
        self.assertEqual(por_cfdi.dsi_payment_line_ids.invoice_id, convertida)
        # Como si la factura convertida no llevara el folio (anterior a 0.2.0).
        self.env.cr.execute(
            "UPDATE account_move SET l10n_mx_edi_cfdi_uuid = NULL WHERE id = %s", [convertida.id])
        self.env.invalidate_all()
        self.assertFalse(self.Inconsistencia._lineas_con_otra_factura())

    # -- Estatus SAT del REP ------------------------------------------------

    def test_rep_cancelado_deja_el_documento_cancelado(self):
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        self.assertEqual(documento.sat_state, 'not_defined', "sin verificar en la Bóveda")
        antes = self._estado_cfdi(compra)

        self._sat_dice(rep, 'valid')
        self.assertEqual(documento.sat_state, 'valid')
        self._sat_dice(rep, 'cancelled')
        self.assertTrue(documento.exists(), "nunca se borra")
        self.assertEqual((documento.state, documento.sat_state), ('payment_received', 'cancelled'))
        self.assertEqual(documento.invoice_ids, compra)
        compra.invalidate_recordset()
        self.assertEqual(self._estado_cfdi(compra), antes, "la compra sigue vigente")
        self.assertEqual(compra.dsi_vault_sat_state, 'valid')

    # -- REP emitido ----------------------------------------------------------

    def test_rep_emitido_no_lleva_documento_y_actualizar_pagos_no_borra_nada(self):
        uuid, venta = self._venta_timbrada()
        self.assertEqual(venta.l10n_mx_edi_cfdi_state, 'sent')
        documentos = venta.l10n_mx_edi_invoice_document_ids
        rep = self._rep((uuid, 1000.0), direction='issued')
        rep._ligar_reps()
        linea = rep.dsi_payment_line_ids
        self.assertEqual(linea.invoice_id, venta)
        self.assertFalse(rep.dsi_edi_document_id)
        self.assertEqual(venta.l10n_mx_edi_invoice_document_ids, documentos, "ningún documento nuevo")
        copia = self._copias(venta, rep)
        self.assertEqual(len(copia), 1)

        self.assertFalse(venta.l10n_mx_edi_update_payments_needed)
        venta.l10n_mx_edi_cfdi_invoice_try_update_payments()
        self.assertTrue(copia.exists())
        self.assertEqual(linea.invoice_id, venta)
        self.assertEqual(venta.l10n_mx_edi_invoice_document_ids, documentos)

    def test_un_rep_recibido_no_se_liga_a_una_venta_ni_uno_emitido_a_una_compra(self):
        uuid_venta, _venta = self._venta_timbrada()
        recibido = self._rep((uuid_venta, 100.0))
        _ingreso, compra = self._compra()
        # El emitido encuentra el CFDI de la compra en la Bóveda, y aun así no
        # se liga a ella por respaldo.
        emitido = self._rep((compra.l10n_mx_edi_cfdi_uuid, 100.0), direction='issued')
        (recibido | emitido)._ligar_reps()
        self.assertFalse(recibido.dsi_payment_line_ids.invoice_id)
        self.assertFalse(emitido.dsi_payment_line_ids.invoice_id)
        self.assertEqual(emitido.dsi_payment_line_ids.link_state, 'in_vault')
        self.assertFalse((recibido | emitido).dsi_edi_document_id)

    def test_corregir_la_direccion_del_rep_lo_vuelve_a_ligar(self):
        """La dirección se corrige en el formulario y decide si el REP paga
        compras o ventas. El REP recibido ligado a su compra que se corrige a
        «Emitido» se desliga al momento: su «Pago recibido» se queda sin la
        compra y el botón «REP» ya no lo cuenta. Al regresarlo, vuelve."""
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        linea = rep.dsi_payment_line_ids

        rep.with_user(self.usuario).write({'direction': 'issued'})
        self.assertEqual(linea.direction, 'issued')
        self.assertFalse(linea.invoice_id)
        self.assertFalse(documento.invoice_ids)
        compra.invalidate_recordset()
        self.assertEqual(compra.dsi_rep_count, 0)

        rep.with_user(self.usuario).write({'direction': 'received'})
        self.assertEqual(linea.invoice_id, compra)
        self.assertEqual(rep.dsi_edi_document_id, documento)
        self.assertEqual(documento.invoice_ids, compra)

    def test_la_revision_diaria_desliga_la_factura_del_otro_lado(self):
        """Si la dirección cambió sin volver a ligar (un flujo en lote), la
        revisión diaria encuentra la línea ligada a una factura del otro lado
        y la desliga."""
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        linea = rep.dsi_payment_line_ids
        rep.with_context(dsi_sin_sync=True).direction = 'issued'
        self.assertEqual(linea.invoice_id, compra)
        self.assertEqual(self.Inconsistencia._lineas_con_otra_factura(), linea.ids)

        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertFalse(linea.invoice_id)
        self.assertFalse(rep.dsi_edi_document_id.invoice_ids)

    def test_rep_emitido_con_el_rfc_de_la_empresa_con_prefijo_mx(self):
        """Una base que viene de 18 tiene el RFC de la empresa como
        MXEKU9003173C9. El REP que emitió la empresa es emitido y se liga a
        su venta; antes se comparaba el texto tal cual y quedaba recibido,
        sin venta y con un «Pago (REP) de factura fuera de Odoo» falso."""
        uuid_venta, venta = self._venta_timbrada()
        self.env.cr.execute("UPDATE res_partner SET vat = %s WHERE id = %s",
                            ['MX' + RFC_RECEPTOR, self.company.partner_id.id])
        self.env.invalidate_all()
        xml = rep_xml(self._uuid(), [(1000.0, 'MXN', '1', [docto(uuid_venta, 1000.0)])],
                      emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE)
        rep = self._importar(xml)
        self.assertEqual(rep.direction, 'issued')
        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, venta)
        self.assertFalse(self._sin_factura(rep))

    def test_la_actualizacion_corrige_la_direccion_de_los_ya_guardados(self):
        """Lo que dejó la regla anterior: un REP emitido por la empresa
        guardado como recibido, con el RFC de la empresa con prefijo MX. La
        actualización le corrige la dirección y lo liga a su venta; correrla
        otra vez no cambia nada."""
        uuid_venta, venta = self._venta_timbrada()
        self.env.cr.execute("UPDATE res_partner SET vat = %s WHERE id = %s",
                            ['MX' + RFC_RECEPTOR, self.company.partner_id.id])
        self.env.invalidate_all()
        rep = self._rep((uuid_venta, 1000.0), direction='issued')
        recibido = self._rep((self._uuid(), 10.0))
        rep.with_context(dsi_sin_sync=True).direction = 'received'
        rep._ligar_reps()
        self.assertFalse(rep.dsi_payment_line_ids.invoice_id)

        self.Doc._corregir_direcciones()
        self.assertEqual((rep.direction, recibido.direction), ('issued', 'received'))
        self.assertEqual(rep.dsi_payment_line_ids.direction, 'issued')
        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, venta)
        with patch.object(type(self.Doc), 'write') as escritura:
            self.Doc._corregir_direcciones()
        escritura.assert_not_called()

    def _compra_en_sucursal(self, uuid):
        """Una sucursal sin RFC propio (recibe con el de la matriz) con una
        compra que lleva ese folio fiscal."""
        # Con país: en Odoo 20 una compra solo lleva folio fiscal en una
        # empresa de México, y la sucursal no lo hereda de la matriz.
        sucursal = self.env['res.company'].create({
            'name': "Sucursal Norte", 'parent_id': self.company.id, 'country_id': self.env.ref('base.mx').id,
        })
        self.env.user.company_ids |= sucursal
        compra = self.env['account.move'].with_company(sucursal).create({
            'move_type': 'in_invoice', 'company_id': sucursal.id, 'journal_id': self.journal.id,
            'partner_id': self.proveedor.id, 'invoice_date': date(2026, 6, 15),
        })
        self._documento_odoo(compra, 'invoice_received', 'valid', uuid)
        return sucursal, compra

    def test_sucursal_con_la_compra_no_es_factura_fuera_de_odoo(self):
        """La Bóveda en la matriz y la compra registrada en una sucursal sin
        RFC propio. El REP no se liga a ella (la liga es dentro de la empresa
        del REP), pero la factura sí está en Odoo: no es un «Pago (REP) de
        factura fuera de Odoo», ni en «Por revisar» ni en la lista en vivo."""
        uuid = self._uuid()
        self._compra_en_sucursal(uuid)
        rep = self._rep((uuid, 500.0), (self._uuid(), 100.0))
        rep._ligar_reps()
        self.assertFalse(rep.dsi_payment_line_ids.invoice_id)
        en_sucursal = rep.dsi_payment_line_ids.filtered(lambda l: l.id_documento == uuid)
        falta = rep.dsi_payment_line_ids - en_sucursal
        self.assertEqual(en_sucursal.link_state, 'other_company')
        self.assertEqual(falta.link_state, 'missing')
        self.assertEqual(self._sin_factura(rep).payment_line_id, falta, "solo el que de verdad no está en Odoo")
        accion = self.env.ref('dsi_cfdi_vault.action_dsi_cfdi_payment_line_no_invoice')
        en_la_lista = self.Linea.search(safe_eval(accion.domain) + [('payment_doc_id', '=', rep.id)])
        self.assertEqual(en_la_lista, falta)

    def test_la_compra_que_entra_en_la_sucursal_resuelve_el_rep_de_la_matriz(self):
        """El REP llega antes a la Bóveda de la matriz y su factura falta.
        Cuando la sucursal vincula su CFDI a la compra, el REP de la matriz
        se revisa en ese momento (no hasta la revisión diaria)."""
        uuid = self._uuid()
        rep = self._rep((uuid, 500.0))
        rep._ligar_reps()
        registro = self._sin_factura(rep)
        self.assertEqual(registro.state, 'open')

        sucursal, compra = self._compra_en_sucursal(uuid)
        ingreso = self._ingreso(uuid, company=sucursal)
        ingreso.move_id = compra
        self.assertEqual(rep.dsi_payment_line_ids.link_state, 'other_company')
        self.assertEqual(registro.state, 'resolved')

    def test_multiempresa(self):
        """Un REP solo se liga a facturas de su empresa. La empresa B no lleva
        catálogo de cuentas (cargarlo es lento): basta una compra en borrador
        con su documento CFDI de Odoo, que le da el folio fiscal."""
        empresa_b = self.env['res.company'].create({
            'name': "Empresa B MX", 'country_id': self.env.ref('base.mx').id,
            'currency_id': self.env.ref('base.MXN').id,
        })
        diario_b = self.env['account.journal'].create({
            'name': "Compras B", 'code': 'CMPB', 'type': 'purchase', 'company_id': empresa_b.id,
        })
        compra_b = self.env['account.move'].with_company(empresa_b).create({
            'move_type': 'in_invoice', 'journal_id': diario_b.id, 'partner_id': self.proveedor.id,
            'invoice_date': date(2026, 6, 15),
        })
        uuid = self._uuid()
        self._documento_odoo(compra_b, 'invoice_received', 'valid', uuid)
        self.assertEqual((compra_b.company_id, compra_b.l10n_mx_edi_cfdi_uuid), (empresa_b, uuid))

        rep_a = self._rep((uuid, 100.0))
        rep_b = self._rep((uuid, 100.0), company=empresa_b)
        (rep_a | rep_b)._ligar_reps()
        self.assertFalse(rep_a.dsi_payment_line_ids.invoice_id)
        self.assertFalse(rep_a.dsi_edi_document_id)
        self.assertEqual(rep_b.dsi_payment_line_ids.invoice_id, compra_b)
        self.assertEqual(rep_b.dsi_edi_document_id.invoice_ids, compra_b)
        self.assertEqual(rep_b.dsi_payment_line_ids.company_id, empresa_b)
        # El usuario de la empresa A no ve las líneas de B, ni el REP de B.
        visibles = self.Linea.with_user(self.usuario).search([('id_documento', '=', uuid)])
        self.assertEqual(visibles, rep_a.dsi_payment_line_ids)
        self.assertEqual(
            self.env['cfdi.document'].with_user(self.usuario).search([('id', 'in', (rep_a | rep_b).ids)]), rep_a)
        with self.assertRaises(AccessError):
            rep_b.with_user(self.usuario).read(['uuid'])

        # Si el REP de B se pasa a otra empresa, ya no paga la compra de B.
        empresa_c = self.env['res.company'].create({'name': "Empresa C MX"})
        rep_b.company_id = empresa_c
        self.assertEqual(rep_b.dsi_payment_line_ids.company_id, empresa_c)
        self.assertFalse(rep_b.dsi_payment_line_ids.invoice_id)
        self.assertFalse(rep_b.dsi_edi_document_id.invoice_ids)

    def test_el_rep_que_cambia_de_empresa_no_se_lleva_su_documento(self):
        """El REP recibido en A, con su «Pago recibido», se pasa a B, donde
        está la compra con el mismo folio. El documento de A se queda sin
        compras y B tiene el suyo: en Odoo 20 el documento lleva empresa, no
        se le puede cambiar y sus compras tienen que ser de ella. Si el REP
        regresa a A, se reutiliza el de A."""
        uuid = self._uuid()
        _uuid, compra_a = self._compra_nativa(uuid)
        rep = self._rep((uuid, 100.0))
        rep._ligar_reps()
        documento_a = rep.dsi_edi_document_id
        self.assertEqual(documento_a.invoice_ids, compra_a)

        empresa_b = self.env['res.company'].create({
            'name': "Empresa B MX", 'country_id': self.env.ref('base.mx').id,
            'currency_id': self.env.ref('base.MXN').id,
        })
        diario_b = self.env['account.journal'].create({
            'name': "Compras B", 'code': 'CMPB', 'type': 'purchase', 'company_id': empresa_b.id,
        })
        compra_b = self.env['account.move'].with_company(empresa_b).create({
            'move_type': 'in_invoice', 'journal_id': diario_b.id, 'partner_id': self.proveedor.id,
            'invoice_date': date(2026, 6, 15),
        })
        self._documento_odoo(compra_b, 'invoice_received', 'valid', uuid)

        rep.company_id = empresa_b
        documento_b = rep.dsi_edi_document_id
        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, compra_b)
        self.assertNotEqual(documento_b, documento_a)
        self.assertEqual(documento_b.invoice_ids, compra_b)
        self.assertEqual(documento_b.attachment_id.company_id, empresa_b)
        if 'company_id' in self.Documento._fields:
            self.assertEqual((documento_a.company_id, documento_b.company_id), (self.company, empresa_b))
        self.assertFalse(documento_a.invoice_ids)

        rep.company_id = self.company
        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, compra_a)
        self.assertEqual(rep.dsi_edi_document_id, documento_a)
        self.assertEqual(documento_a.invoice_ids, compra_a)
        self.assertFalse(documento_b.invoice_ids)
        self.assertEqual(self.Documento.search_count([('state', '=', 'payment_received')]), 2)

    # -- Migración ------------------------------------------------------------

    def test_el_llenado_de_los_ya_guardados_dos_veces_da_lo_mismo(self):
        _doc1, compra1 = self._compra()
        uuid_venta, venta = self._venta_timbrada()
        reps = self._rep((compra1.l10n_mx_edi_cfdi_uuid, 500.0), (self._uuid(), 10.0))
        reps |= self._rep((uuid_venta, 1000.0), direction='issued')
        reps |= self._rep((self._uuid(), 20.0), fecha='2026-09-15')
        # Como estaban antes de 0.2.0: sin líneas.
        reps.dsi_payment_line_ids.unlink()

        def conteos():
            lineas = self.Linea.search([('payment_doc_id', 'in', reps.ids)])
            return (
                sorted(lineas.ids), len(lineas), len(lineas.filtered('invoice_id')),
                self.Documento.search_count([('state', '=', 'payment_received')]),
                len(self._copias(compra1, reps[0])) + len(self._copias(venta, reps[1])),
                self.Inconsistencia.search_count([('kind', '=', 'rep_sin_factura')]),
            )

        self.Doc._backfill_lineas_de_pago()
        primera = conteos()
        self.assertEqual(primera[1:], (4, 2, 1, 2, 1))
        self.Doc._backfill_lineas_de_pago()
        self.assertEqual(conteos(), primera)

    def test_el_rep_vinculado_a_mano_a_la_factura_que_paga_se_desvincula(self):
        """Antes de 0.2.0 no había dónde poner la factura que paga un REP y
        alguien lo vinculó a ella. Al llenar las líneas, la liga queda en las
        líneas y el REP se desvincula, con nota. Si la factura vinculada no es
        una de las que paga, se deja como está."""
        _doc1, compra1 = self._compra()
        _doc2, compra2 = self._compra()
        rep = self._rep((compra1.l10n_mx_edi_cfdi_uuid, 500.0))
        otro = self._rep((self._uuid(), 50.0))
        rep.write({'move_id': compra1.id, 'match_state': 'manual', 'match_method': 'manual'})
        otro.write({'move_id': compra2.id, 'match_state': 'manual', 'match_method': 'manual'})
        (rep | otro).dsi_payment_line_ids.unlink()

        self.Doc._backfill_lineas_de_pago()
        self.assertEqual((rep.move_id, rep.match_state), (self.env['account.move'], 'pending'))
        self.assertEqual(rep.dsi_payment_line_ids.invoice_id, compra1)
        self.assertTrue(any(compra1.display_name in str(m.body) for m in rep.message_ids))
        self.assertEqual(otro.move_id, compra2)

    # -- La factura se borra ------------------------------------------------

    def test_borrar_la_factura_deja_la_linea_sin_factura_y_se_vuelve_a_ligar(self):
        ingreso, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        linea = rep.dsi_payment_line_ids

        compra.unlink()
        self.assertFalse(linea.invoice_id)
        self.assertEqual(linea.link_state, 'in_vault')
        self.assertTrue(documento.exists(), "el documento no se borra")
        self.assertFalse(documento.invoice_ids)

        otra = ingreso._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        self.assertEqual((linea.invoice_id, linea.link_state), (otra, 'linked'))
        self.assertEqual(rep.dsi_edi_document_id, documento)
        self.assertEqual(documento.invoice_ids, otra)
        self.assertEqual(linea.invoice_attachment_id, self._copias(otra, rep))

    # -- «Pago (REP) de factura fuera de Odoo» --------------------------------

    def test_rep_sin_factura_solo_desde_el_corte_y_se_resuelve_al_ligar(self):
        uuid_factura = self._uuid()
        antes_del_corte = self._rep((uuid_factura, 500.0), fecha='2026-09-15')
        despues = self._rep((uuid_factura, 300.0), (self._uuid(), 10.0))
        (antes_del_corte | despues)._ligar_reps()
        self.assertFalse(self._sin_factura(antes_del_corte))
        registros = self._sin_factura(despues)
        self.assertEqual(len(registros), 2, "uno por documento relacionado")
        self.assertEqual(registros.payment_line_id, despues.dsi_payment_line_ids)
        self.assertEqual(set(registros.mapped('state')), {'open'})
        self.assertEqual(set(registros.mapped('severity')), {'warning'})
        primero = registros.filtered(lambda r: r.payment_line_id.id_documento == uuid_factura)
        self.assertIn("Tampoco está en la Bóveda", primero.observation)
        # Cada fila dice qué factura falta y de qué proveedor: un REP que paga
        # dos facturas fuera de Odoo no da dos filas iguales.
        self.assertEqual(primero.uuid, uuid_factura)
        self.assertEqual(sorted(registros.mapped('uuid')), sorted(despues.dsi_payment_line_ids.mapped('id_documento')))
        self.assertEqual(len(set(registros.mapped('display_name'))), 2)
        self.assertEqual(registros.partner_id, self.proveedor)
        self.assertEqual(registros.cfdi_document_id, despues)

        # Llega su CFDI: sigue sin factura (la huella no cambia).
        ingreso = self._ingreso(uuid_factura)
        self.assertEqual(self._sin_factura(despues), registros)
        self.assertEqual(primero.state, 'open')
        # Se convierte en factura: ese se resuelve solo; el otro sigue.
        ingreso._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        self.assertEqual(primero.state, 'resolved')
        self.assertEqual((registros - primero).state, 'open')
        # Si el REP se cancela, ya no cuenta.
        self._sat_dice(despues, 'cancelled')
        self.assertEqual((registros - primero).state, 'resolved')

    def test_el_corte_es_la_fecha_de_mexico(self):
        """El corte lo fija la actualización, que corre como el sistema y sin
        zona horaria. A las 20:55 en México ya es el día siguiente en UTC, y el
        corte quedaba un día adelante: los REP de ese día ya no se guardaban."""
        self.env['ir.config_parameter'].sudo().search([('key', '=', PARAMETRO_CORTE)]).unlink()
        with freeze_time('2026-10-28 02:55:00'):
            corte = self.Linea.with_context(tz=False)._corte_rep_sin_factura()
        self.assertEqual(corte, date(2026, 10, 27))
        self.assertEqual(_param(self.env, PARAMETRO_CORTE), '2026-10-27')

    def test_la_lista_en_vivo_tiene_los_anteriores_al_corte(self):
        """Sin los REP cancelados ni los archivados, como «Por revisar»."""
        _doc, compra = self._compra()
        viejo = self._rep((self._uuid(), 500.0), (compra.l10n_mx_edi_cfdi_uuid, 10.0), fecha='2026-09-15')
        cancelado = self._rep((self._uuid(), 50.0), fecha='2026-09-15')
        archivado = self._rep((self._uuid(), 60.0), fecha='2026-09-15')
        (viejo | cancelado | archivado)._ligar_reps()
        self._sat_dice(cancelado, 'cancelled')
        archivado.active = False
        accion = self.env.ref('dsi_cfdi_vault.action_dsi_cfdi_payment_line_no_invoice')
        en_la_lista = self.Linea.with_user(self.usuario).search(safe_eval(accion.domain))
        self.assertEqual(en_la_lista, viejo.dsi_payment_line_ids.filtered(lambda l: not l.invoice_id))
        self.assertFalse(self._sin_factura(viejo), "antes del corte no se guarda")

    def test_la_lista_sin_asiento_de_pago(self):
        """«Pagos (REP) sin asiento de pago»: los que no están vinculados a
        un asiento, también los que alguien vinculó a mano a una factura."""
        _doc, compra = self._compra()
        sin_nada = self._rep((self._uuid(), 10.0))
        con_factura = self._rep((self._uuid(), 20.0))
        con_asiento = self._rep((self._uuid(), 30.0))
        cancelado = self._rep((self._uuid(), 40.0))
        con_factura.move_id = compra
        con_asiento.move_id = self.env['account.move'].create({'move_type': 'entry', 'company_id': self.company.id})
        self._sat_dice(cancelado, 'cancelled')
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_payment_no_move')
        reps = sin_nada | con_factura | con_asiento | cancelado
        en_la_lista = self.Doc.search(safe_eval(accion.domain) + [('id', 'in', reps.ids)])
        self.assertEqual(en_la_lista, sin_nada | con_factura, "un REP cancelado no necesita asiento")

    def test_el_formulario_del_rep_no_empuja_a_vincularlo_a_una_factura(self):
        """En un CFDI de Pago el vínculo es el asiento del pago: «Ver
        sugerencias» (que propone facturas) no sale, el campo se llama
        «Asiento del pago» y solo ofrece asientos. El «Pago recibido» no se
        abre desde ahí: en 18 y 19 el documento de Odoo no tiene formulario
        propio y el genérico deja editarlo todo."""
        arch = etree.fromstring(self.Doc.with_user(self.usuario).get_views([(False, 'form')])['views']['form']['arch'])
        sugerencias = arch.xpath("//button[@name='action_open_match_suggestions']")[0]
        self.assertIn("move_type == 'P'", sugerencias.get('invisible'))
        asiento = arch.xpath("//field[@name='move_id'][@string='Asiento del pago']")[0]
        self.assertEqual(safe_eval(asiento.get('domain')), [('move_type', '=', 'entry')])
        self.assertEqual(asiento.get('invisible'), "move_type != 'P'")
        documento = arch.xpath("//field[@name='dsi_edi_document_id']")
        self.assertEqual([campo.get('invisible') for campo in documento], ['1'])

    # -- Permisos y vistas ----------------------------------------------------

    def test_el_usuario_de_la_boveda_lee_las_lineas_pero_no_las_escribe(self):
        rep = self._rep((self._uuid(), 500.0))
        linea = rep.dsi_payment_line_ids.with_user(self.usuario)
        self.assertTrue(linea.id_documento)
        with self.assertRaises(AccessError):
            linea.write({'imp_pagado': 1.0})
        with self.assertRaises(AccessError):
            self.Linea.with_user(self.usuario).create({'payment_doc_id': rep.id, 'id_documento': 'X'})

    def test_un_usuario_de_facturacion_sin_la_boveda_abre_la_factura(self):
        """No ve el botón «REP» (es de la Bóveda), pero abre la compra y en su
        pestaña CFDI ve y descarga el «Pago recibido» sin permisos de la
        Bóveda."""
        _doc, compra = self._compra()
        rep = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep._ligar_reps()
        facturacion = new_test_user(
            self.env, login='solo-facturacion-rep', groups='base.group_user,account.group_account_invoice',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        Factura = compra.with_user(facturacion).with_context(allowed_company_ids=self.company.ids)

        vistas = Factura.get_views([(False, 'form')])
        campos = vistas['models']['account.move']
        campos = campos.get('fields', campos)
        self.assertNotIn('dsi_rep_count', campos)
        self.assertNotIn('dsi_rep_line_ids', campos)
        valores = Factura.read(list(campos))[0]
        self.assertIn(rep.dsi_edi_document_id.id, valores['l10n_mx_edi_document_ids'])
        documento = Factura.l10n_mx_edi_document_ids.filtered(lambda d: d.state == 'payment_received')
        documento.read(['state', 'sat_state', 'attachment_uuid', 'cancel_button_needed', 'show_button_needed'])
        self.assertTrue(_bytes_de_adjunto(documento.attachment_id))
        boton = etree.fromstring(vistas['views']['form']['arch']).xpath("//button[@name='action_view_rep_lines']")
        self.assertFalse(boton)

    def test_el_boton_rep_de_la_factura(self):
        _doc, compra = self._compra()
        rep1 = self._rep((compra.l10n_mx_edi_cfdi_uuid, 500.0))
        rep2 = self._rep((compra.l10n_mx_edi_cfdi_uuid, 660.0))
        (rep1 | rep2)._ligar_reps()
        compra = compra.with_user(self.usuario)
        self.assertEqual(compra.dsi_rep_count, 2)
        accion = compra.action_view_rep_lines()
        self.assertEqual(self.Linea.search(accion['domain']), (rep1 | rep2).dsi_payment_line_ids)

    def test_un_rep_cancelado_y_su_sustituto_no_cuentan_doble(self):
        """El proveedor cancela su REP y emite otro por lo mismo. El botón
        cuenta uno, su lista abre sin el cancelado (quitando el filtro se ve)
        y la pestaña «Pagos (REP)» del CFDI suma lo pagado una vez. En la
        pestaña CFDI de la compra quedan los dos, el primero cancelado."""
        ingreso, compra = self._compra()
        folio = compra.l10n_mx_edi_cfdi_uuid
        original = self._rep((folio, 1160.0))
        sustituto = self._rep((folio, 1160.0))
        (original | sustituto)._ligar_reps()
        self._sat_dice(original, 'cancelled')
        self._sat_dice(sustituto, 'valid')

        compra = compra.with_user(self.usuario)
        self.assertEqual(compra.dsi_rep_count, 1)
        accion = compra.action_view_rep_lines()
        filtro = etree.fromstring(self.Linea.get_views([(False, 'search')])['views']['search']['arch']).xpath(
            "//filter[@name='filter_not_canceled']")[0]
        self.assertTrue(accion['context'].get('search_default_filter_not_canceled'))
        lineas = self.Linea.with_user(self.usuario).search(accion['domain'] + safe_eval(filtro.get('domain')))
        self.assertEqual(lineas, sustituto.dsi_payment_line_ids)
        self.assertEqual(sum(lineas.mapped('imp_pagado')), 1160.0)
        ingreso.invalidate_recordset()
        self.assertEqual(ingreso.dsi_rep_line_ids, sustituto.dsi_payment_line_ids)
        self.assertEqual(
            sorted(compra.sudo().l10n_mx_edi_document_ids.filtered(
                lambda d: d.state == 'payment_received').mapped('sat_state')), ['cancelled', 'valid'])
