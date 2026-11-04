"""«Actualizar SAT» automático en las ventas con solicitud de cancelación.

Existe porque una venta cuya cancelación se pidió desde Odoo (con el PAC en
producción) se queda publicada, con el documento «solicitud de cancelación»,
hasta que alguien aprieta «Actualizar SAT»: Odoo solo revisa por su cuenta las
solicitudes de hasta 60 días. Cuando los metadatos del SAT o la consulta
pública de la Bóveda ya la dan cancelada, la Bóveda ejecuta ese botón.

Lo que más cuidan estas pruebas:

  * el flujo de Odoo tal cual: el timbrado y la solicitud de cancelación con
    el PAC en producción simulado, y la consulta al SAT simulada, como
    test_cfdi_invoice_documents.py de l10n_mx_edi;
  * a quién sí: ventas con solicitud (también si Odoo ya la muestra
    «Timbrada» porque el SAT la dio por vigente), de empresas con licencia,
    avisadas por los metadatos o por la consulta pública de la Bóveda;
  * a quién no: a una compra ni a una venta sin solicitud, que quedan como
    «Estatus SAT distinto», ni a una venta que se volvió a timbrar por la
    solicitud de su CFDI anterior, que tampoco le da una discrepancia si
    sigue ligado en la Bóveda;
  * el resultado: venta cancelada; «Cancelado con factura contabilizada» si
    una fecha de bloqueo no deja cancelar el asiento (también si su XML
    estaba en la Bóveda sin ligar); o la «Cancelación por actualizar»
    abierta con lo que dijo cada lado;
  * no volver a consultar antes de 20 horas, que una venta que falla no
    detenga a las demás ni se vuelva a pedir sin fin (tampoco si falla al
    conciliar su XML) y que lo que se pide durante una corrida no se
    pierda.

Es subclase de las pruebas comunes de l10n_mx_edi (TestMxEdiCommon): empresa
de México en pesos, su certificado de prueba (vigente en 2016 y 2017, por eso
freeze_time) y los simuladores del PAC y del SAT. Nunca se habla con el SAT ni
con el PAC.
"""
from datetime import date
from unittest.mock import patch

import psycopg2
from freezegun import freeze_time

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.dsi_cfdi_sat_sync.models.account_move import DISPARO_DE_LA_TRANSACCION
from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_cfdi_vault.tests.test_factura_como_odoo import cfdi_xml
from odoo.addons.dsi_license_client.tests.common import activar_licencia
from odoo.addons.l10n_mx_edi.tests.common import TestMxEdiCommon

from .test_metadatos import linea, paquete, txt

LOG_FACTURA = 'odoo.addons.dsi_cfdi_sat_sync.models.account_move'
CANCELADO_EL = '2017-01-01 09:30:00'


@tagged('post_install', '-at_install')
class TestCancelacionEmitidas(TestMxEdiCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        # PAC en producción: así la cancelación queda como solicitud.
        cls.company.write({
            'l10n_mx_edi_pac_test_env': False,
            'l10n_mx_edi_pac_username': 'test',
            'l10n_mx_edi_pac_password': 'test',
        })
        activar_licencia(cls, cls.company)
        cls.credential = cls.env['sat.credential'].sudo().create({
            'company_id': cls.company.id, 'rfc': cls.company.vat, 'state': 'validated',
        })
        cls.Factura = cls.env.registry['account.move']
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency'].sudo()
        cls.cron = cls.env.ref('dsi_cfdi_sat_sync.ir_cron_actualizar_cancelaciones_emitidas')
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _venta(self):
        """Venta timbrada en Odoo (PAC simulado). En 20 el ayudante de la
        venta mexicana se llama _create_invoice_mx."""
        crear = getattr(self, '_create_invoice_mx', None) or self._create_invoice
        venta = crear(invoice_date_due='2017-02-01')
        with self.with_mocked_pac_sign_success():
            venta._l10n_mx_edi_cfdi_invoice_try_send()
        self.assertEqual(venta.l10n_mx_edi_cfdi_state, 'sent')
        return venta

    def _pedir_cancelacion(self, venta):
        """La cancelación pedida desde Odoo, como el asistente: con el PAC en
        producción queda el documento «solicitud de cancelación» sin
        respuesta del SAT y el asiento publicado."""
        with self.with_mocked_pac_cancel_success():
            self.env['l10n_mx_edi.invoice.cancel'] \
                .with_context(venta.button_request_cancel()['context']) \
                .create({'cancellation_reason': '02'}) \
                .action_cancel_invoice()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'cancel_requested'))
        return venta

    def _venta_con_solicitud(self):
        return self._pedir_cancelacion(self._venta())

    def _metadatos(self, venta, estatus='0', direction='issued', cancelado=CANCELADO_EL):
        """Los metadatos de la venta por el camino de la descarga, sin hablar
        con el SAT."""
        solicitud = self.env['sat.download.request'].sudo().create({
            'company_id': self.company.id,
            'credential_id': self.credential.id,
            'direction': direction,
            'request_type': 'metadata',
            'estado_comprobante': 'todos',
            'date_from': date(2017, 1, 1),
            'date_to': date(2017, 1, 1),
            'state': 'downloading',
            'package_ids_json': '["PAQUETE_DE_PRUEBA_01"]',
        })
        contenido = txt(linea(
            venta.l10n_mx_edi_cfdi_uuid, estatus=estatus, cancelado=cancelado if estatus == '0' else '',
            emisor=self.company.vat, receptor=venta.partner_id.vat, monto=f"{venta.amount_total:.2f}"))
        solicitud._procesar_paquete_zip(paquete(**{'AAAAAAAA-0000-0000-0000-000000000000_01': contenido}))
        solicitud._procesar_metadatos()
        self.assertEqual(solicitud.metadata_count, 1)

    def _cfdi_de_la_boveda(self, venta):
        """El XML de la venta guardado en la Bóveda como emitido, sin ligar a
        la factura: como lo deja la importación de un XML que no se concilió."""
        Doc = self.env['cfdi.document'].sudo()
        xml = _bytes_de_adjunto(venta.l10n_mx_edi_cfdi_attachment_id)
        vals = Doc._parse_cfdi_xml(xml)
        adjunto = self.env['ir.attachment'].sudo().create({
            'name': f"{vals['uuid']}.xml", 'raw': xml, 'mimetype': 'text/xml',
        })
        datos = {k: v for k, v in vals.items() if k != 'parse_state'}
        datos.update({
            'company_id': self.company.id, 'direction': 'issued', 'xml_attachment_id': adjunto.id,
            'currency_id': vals['currency_id'] or self.company.currency_id.id,
        })
        doc = Doc.create(datos)
        self.assertEqual(doc.uuid, venta.l10n_mx_edi_cfdi_uuid.upper())
        self.assertFalse(doc.move_id)
        return doc

    def _compra_timbrada(self):
        """Compra con su CFDI recibido registrado como lo hace Odoo al
        importarlo (documento «recibido»), publicada."""
        type(self).contador += 1
        uuid = f"EEEEEEEE-7777-4000-8000-{self.contador:012d}"
        compra = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_mx.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({
                'name': "Servicio", 'quantity': 1, 'price_unit': 1000.0, 'tax_ids': [Command.clear()],
            })],
        })
        compra.action_post()
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'application/xml',
            'res_model': 'account.move', 'res_id': compra.id,
        })
        Documento = self.env['l10n_mx_edi.document']
        valores = {
            'move_id': compra.id, 'invoice_ids': [Command.set(compra.ids)], 'state': 'invoice_received',
            'sat_state': 'valid', 'attachment_id': adjunto.id, 'datetime': fields.Datetime.now(),
        }
        if 'company_id' in Documento._fields:
            valores['company_id'] = self.company.id
        Documento.sudo().create(valores)
        self.assertEqual(compra.l10n_mx_edi_cfdi_uuid, uuid)
        return compra

    def _actualizar_sat(self, sat_dice, **kwargs):
        """La tarea de «Actualizar SAT» con el SAT simulado. Devuelve el espía
        de l10n_mx_edi_cfdi_try_sat, que sigue haciendo lo de Odoo."""
        original = self.Factura.l10n_mx_edi_cfdi_try_sat
        with self.with_mocked_sat_call(lambda _documento: sat_dice), \
                patch.object(self.Factura, 'l10n_mx_edi_cfdi_try_sat', autospec=True, side_effect=original) as espia:
            self.env['account.move']._cron_actualizar_cancelaciones_emitidas(**kwargs)
        return espia

    def _registros(self, factura, kind=None):
        dominio = [('move_id', '=', factura.id)]
        if kind:
            dominio.append(('kind', '=', kind))
        return self.Inconsistencia.search(dominio)

    def _vivos(self, factura):
        """Las discrepancias sin resolver de la factura, {tipo: registro}."""
        return {r.kind: r for r in self._registros(factura).filtered(lambda r: r.state != 'resolved')}

    def _disparos(self):
        return self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', self.cron.id)])

    def _revision_diaria(self):
        self.Inconsistencia._cron_detectar_inconsistencias()

    def _corrida_nueva(self):
        """Lo que ve una corrida nueva de la tarea, después del commit de la
        anterior: sin disparos pendientes ni la memoria de la transacción."""
        self.env['ir.cron.trigger'].sudo().search([('cron_id', '=', self.cron.id)]).unlink()
        self.env.cr.precommit.data.pop(DISPARO_DE_LA_TRANSACCION, None)

    def _sat_por_folio(self, cancelados):
        """La consulta pública simulada por folio fiscal: cancelado si está en
        `cancelados`, vigente si no. La usan «Actualizar SAT» y la Bóveda."""
        def consulta(_documento, _emisor, _receptor, _total, uuid):
            return {'value': 'cancelled' if (uuid or '').upper() in cancelados else 'valid'}
        return patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta)

    def _refacturar(self, rechazada):
        """Venta timbrada (CFDI anterior) con su XML ligado en la Bóveda; se
        cancela (antes, si `rechazada`, con una solicitud que el SAT
        rechazó), se regresa a borrador y se vuelve a timbrar (CFDI nuevo,
        vigente). Devuelve (venta, XML anterior en la Bóveda, folio anterior)."""
        venta = self._venta()
        anterior = venta.l10n_mx_edi_cfdi_uuid.upper()
        doc = self._cfdi_de_la_boveda(venta)
        doc.write({'move_id': venta.id})
        with self._sat_por_folio(set()):
            venta.l10n_mx_edi_cfdi_try_sat()
            doc._fetch_and_store_sat_status()
        if rechazada:
            self._pedir_cancelacion(venta)
            with self._sat_por_folio(set()):
                venta.l10n_mx_edi_cfdi_try_sat()
        self._pedir_cancelacion(venta)
        with self._sat_por_folio({anterior}):
            venta.l10n_mx_edi_cfdi_try_sat()
            doc._fetch_and_store_sat_status()
        self.assertEqual((venta.state, doc.sat_status), ('cancel', 'canceled'))
        venta.button_draft()
        venta.action_post()
        with self.with_mocked_pac_sign_success():
            venta._l10n_mx_edi_cfdi_invoice_try_send()
        self.assertNotEqual(venta.l10n_mx_edi_cfdi_uuid.upper(), anterior)
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))
        return venta, doc, anterior

    # -- Lo que se actualiza ------------------------------------------------

    @freeze_time('2017-01-01')
    def test_los_metadatos_cancelados_cancelan_la_venta(self):
        venta = self._venta_con_solicitud()
        disparos = self._disparos()

        self._metadatos(venta)
        self.assertEqual(venta.dsi_vault_sat_state, 'cancelled')
        vivos = self._vivos(venta)
        self.assertEqual(list(vivos), ['cancelacion_por_actualizar'], "gana sobre «Estatus SAT distinto»")
        pendiente = vivos['cancelacion_por_actualizar']
        self.assertEqual((pendiente.fingerprint, pendiente.source, pendiente.severity),
                         ('consulta=not_defined|por_intentar', 'metadata', 'critical'))
        self.assertIn("metadatos: cancelada el", pendiente.observation)
        self.assertIn("en las próximas horas", pendiente.observation)
        self.assertGreater(self._disparos(), disparos, "los metadatos piden la tarea")

        espia = self._actualizar_sat('cancelled')
        espia.assert_called_once()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state, venta.l10n_mx_edi_cfdi_sat_state),
                         ('cancel', 'cancel', 'cancelled'))
        self.assertTrue(venta.sudo().dsi_try_sat_last)
        self.assertEqual(pendiente.state, 'resolved')
        self.assertFalse(self._vivos(venta))
        self.assertFalse(venta.dsi_vault_sat_mismatch)

        # Ya no es candidata: otra corrida no la vuelve a consultar.
        with freeze_time('2017-01-03'):
            self._actualizar_sat('cancelled').assert_not_called()

    @freeze_time('2017-01-01')
    def test_si_el_sat_aun_no_la_confirma_queda_abierta(self):
        venta = self._venta_con_solicitud()
        self._metadatos(venta)
        pendiente = self._registros(venta, 'cancelacion_por_actualizar')

        self._actualizar_sat('valid').assert_called_once()
        # Odoo la vuelve a mostrar «Timbrada»: el SAT dio la solicitud por vigente.
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))
        self.assertEqual(pendiente.state, 'resolved', "cambió lo que dice: la sustituye otra")
        abierta = self._vivos(venta)['cancelacion_por_actualizar']
        self.assertEqual((abierta.fingerprint, abierta.source), ('consulta=valid|intentada', 'try_sat'))
        self.assertIn("metadatos: cancelada el", abierta.observation)
        self.assertIn("consulta pública: ", abierta.observation)
        self.assertIn("ya ejecutó «Actualizar SAT»", abierta.observation)
        self.assertIn("pudo rechazar la cancelación", abierta.observation)
        self.assertEqual(list(self._vivos(venta)), ['cancelacion_por_actualizar'])

        # La revisión diaria la deja como está.
        self._revision_diaria()
        self.assertEqual(self._vivos(venta), {'cancelacion_por_actualizar': abierta})

    @freeze_time('2017-01-01')
    def test_la_solicitud_que_el_sat_dio_por_vigente_tambien_se_actualiza(self):
        """El SAT rechazó la solicitud: Odoo deja la venta «Timbrada» con la
        solicitud vigente. Si después el SAT la cancela (el cliente la aceptó
        o se volvió a pedir), la venta sigue siendo de las que se actualizan:
        no se filtra por «Cancelación solicitada».

        Con el CFDI ya verificado vigente antes de la solicitud, como lo deja
        el cron de Odoo, «Actualizar SAT» consulta los dos documentos y Odoo
        deja dos documentos «cancelado» (el segundo con motivo 02): es lo que
        hace su botón, y aquí solo se revisa el resultado."""
        venta = self._venta()
        with self.with_mocked_sat_call(lambda _documento: 'valid'):
            venta.l10n_mx_edi_cfdi_try_sat()
        self._pedir_cancelacion(venta)
        with self.with_mocked_sat_call(lambda _documento: 'valid'):
            venta.l10n_mx_edi_cfdi_try_sat()
        solicitud = venta._dsi_solicitud_de_cancelacion()
        self.assertEqual((venta.l10n_mx_edi_cfdi_state, solicitud.sat_state), ('sent', 'valid'))

        self._metadatos(venta)
        self.assertEqual(self._vivos(venta)['cancelacion_por_actualizar'].fingerprint,
                         'consulta=valid|por_intentar')
        self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('cancel', 'cancel'))
        self.assertFalse(self._vivos(venta))

    @freeze_time('2017-01-01')
    def test_con_fecha_de_bloqueo_queda_cancelado_contabilizado(self):
        venta = self._venta_con_solicitud()
        self._metadatos(venta)
        self.company.sudo().fiscalyear_lock_date = date(2017, 1, 1)

        self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state, venta.l10n_mx_edi_cfdi_sat_state),
                         ('posted', 'cancel', 'cancelled'), "la fecha de bloqueo no deja cancelar el asiento")
        vivos = self._vivos(venta)
        self.assertEqual(list(vivos), ['cancelado_contabilizado'])
        self.assertEqual(vivos['cancelado_contabilizado'].source, 'try_sat')
        self.assertEqual(self._registros(venta, 'cancelacion_por_actualizar').state, 'resolved')

        # Ya no es candidata ni la revisión diaria la regresa.
        self._revision_diaria()
        self.assertEqual(list(self._vivos(venta)), ['cancelado_contabilizado'])

    @freeze_time('2017-01-01')
    def test_la_consulta_publica_de_la_boveda_tambien_la_dispara(self):
        """Sin metadatos: la consulta pública diaria de la Bóveda encuentra
        cancelado el CFDI emitido (que ni siquiera está ligado a la venta)."""
        venta = self._venta_con_solicitud()
        doc = self._cfdi_de_la_boveda(venta)
        disparos = self._disparos()

        with self.with_mocked_sat_call(lambda _documento: 'cancelled'):
            doc._fetch_and_store_sat_status()
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('canceled', 'consulta'))
        self.assertGreater(self._disparos(), disparos, "la consulta pública pide la tarea")
        self.assertFalse(self.env['dsi.cfdi.sat.metadata'].sudo().search([('uuid', '=', doc.uuid)]))

        # La revisión diaria la registra aunque el CFDI no esté ligado.
        self._revision_diaria()
        pendiente = self._vivos(venta)['cancelacion_por_actualizar']
        self.assertEqual(pendiente.cfdi_document_id, doc)
        self.assertIn("Bóveda: cancelada según su consulta pública", pendiente.observation)

        self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('cancel', 'cancel'))
        self.assertEqual(pendiente.state, 'resolved')

    @freeze_time('2017-01-01')
    def test_con_fecha_de_bloqueo_y_el_cfdi_sin_ligar_tambien_queda_contabilizado(self):
        """El aviso es de la consulta pública sobre el XML de la venta, que
        está en la Bóveda sin ligar, y no hay metadatos: la venta no tenía
        Estado bóveda y, con la fecha de bloqueo, no quedaba ninguna
        discrepancia. Antes de «Actualizar SAT» la Bóveda concilia ese CFDI
        con la venta por su folio."""
        venta = self._venta_con_solicitud()
        doc = self._cfdi_de_la_boveda(venta)
        with self.with_mocked_sat_call(lambda _documento: 'cancelled'):
            doc._fetch_and_store_sat_status()
        self._revision_diaria()
        self.assertEqual(list(self._vivos(venta)), ['cancelacion_por_actualizar'])
        self.company.sudo().fiscalyear_lock_date = date(2017, 1, 1)

        self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual((doc.move_id, doc.match_method), (venta, 'uuid_exact'))
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state, venta.l10n_mx_edi_cfdi_sat_state),
                         ('posted', 'cancel', 'cancelled'))
        self.assertEqual(venta.dsi_vault_sat_state, 'cancelled')
        self.assertEqual(list(self._vivos(venta)), ['cancelado_contabilizado'])
        self._revision_diaria()
        self.assertEqual(list(self._vivos(venta)), ['cancelado_contabilizado'])

    @freeze_time('2017-01-01')
    def test_una_venta_refacturada_no_se_cancela_por_la_solicitud_anterior(self):
        """Una solicitud que el SAT rechazó se queda para siempre. Si después
        la venta se canceló, se regresó a borrador y se volvió a timbrar, esa
        solicitud es del CFDI anterior: «Actualizar SAT» la consultaba, Odoo
        registraba la cancelación del CFDI anterior como la más reciente y
        cancelaba la venta con su CFDI nuevo vigente."""
        venta = self._venta()
        anterior = venta.l10n_mx_edi_cfdi_uuid
        with self.with_mocked_sat_call(lambda _documento: 'valid'):
            venta.l10n_mx_edi_cfdi_try_sat()
        doc = self._cfdi_de_la_boveda(venta)
        doc.write({'move_id': venta.id})
        # Primera solicitud: el SAT la rechaza. La segunda sí, y Odoo cancela.
        self._pedir_cancelacion(venta)
        with self.with_mocked_sat_call(lambda _documento: 'valid'):
            venta.l10n_mx_edi_cfdi_try_sat()
        self._pedir_cancelacion(venta)
        with self.with_mocked_sat_call(lambda _documento: 'cancelled'):
            venta.l10n_mx_edi_cfdi_try_sat()
            doc._fetch_and_store_sat_status()
        self.assertEqual((venta.state, doc.sat_status), ('cancel', 'canceled'))
        self._metadatos(venta)
        # Se vuelve a timbrar: CFDI nuevo, vigente.
        venta.button_draft()
        venta.action_post()
        with self.with_mocked_pac_sign_success():
            venta._l10n_mx_edi_cfdi_invoice_try_send()
        self.assertNotEqual(venta.l10n_mx_edi_cfdi_uuid, anterior)
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))
        self.assertIn('invoice_cancel_requested', venta.l10n_mx_edi_invoice_document_ids.mapped('state'),
                      "la solicitud rechazada del CFDI anterior sigue ahí")

        self._revision_diaria()
        espia = self._actualizar_sat('cancelled')
        espia.assert_not_called()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))
        self.assertFalse(self._vivos(venta), "ni «Estatus SAT distinto» por el CFDI anterior ligado")
        self.assertFalse(venta.dsi_vault_sat_state)
        self.assertFalse(venta._dsi_solicitud_de_cancelacion())
        # Los metadatos del CFDI anterior ya no son de la venta: la revisión
        # diaria los desliga.
        self.assertFalse(venta._dsi_metadato_del_sat())
        metadato = self.env['dsi.cfdi.sat.metadata'].sudo().search([('uuid', '=', anterior.upper())])
        self.assertEqual((metadato.estatus, metadato.move_id), ('0', self.env['account.move']))

    @freeze_time('2017-01-01')
    def test_la_venta_refacturada_no_toma_el_estado_del_cfdi_anterior(self):
        """El XML anterior, cancelado, sigue ligado a la venta que se volvió a
        timbrar. Le daba su Estado bóveda cancelado: un «Estatus SAT
        distinto» crítico que nunca se resolvía (la cancelación es
        definitiva) y aconsejaba «Actualizar SAT»."""
        venta, doc, anterior = self._refacturar(rechazada=False)
        self._revision_diaria()
        self.assertEqual(doc.move_id, venta, "el XML anterior sigue ligado")
        self.assertFalse(venta.dsi_vault_sat_state)
        self.assertFalse(venta.dsi_vault_sat_mismatch)
        self.assertFalse(self._vivos(venta))

        # Una semana de verificaciones y revisiones no lo cambia.
        with freeze_time('2017-01-08'):
            with self._sat_por_folio({anterior}):
                doc._fetch_and_store_sat_status()
            self._revision_diaria()
        self.assertFalse(self._vivos(venta))

        # Los metadatos del CFDI de hoy sí le dan su Estado bóveda.
        self._metadatos(venta, estatus='1')
        self.assertEqual(venta.dsi_vault_sat_state, 'valid')
        self.assertFalse(self._vivos(venta))

    @freeze_time('2017-01-01')
    def test_la_refacturada_con_una_solicitud_rechazada_no_aconseja_actualizar_sat(self):
        """Como la anterior, pero el CFDI anterior tuvo antes una solicitud
        que el SAT rechazó y que se queda para siempre. La discrepancia
        aconsejaba «Actualizar SAT»: el botón de Odoo consulta esa solicitud
        y cancelaba la venta con su CFDI nuevo vigente."""
        venta, _doc, _anterior = self._refacturar(rechazada=True)
        self._revision_diaria()
        self.assertFalse(self._vivos(venta))
        self._actualizar_sat('cancelled').assert_not_called()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))

    @freeze_time('2017-01-01')
    def test_vigente_en_los_metadatos_y_cancelada_en_odoo_sin_xml_no_es_diferencia(self):
        """Venta sin su XML en la Bóveda, con metadatos que la dan vigente
        (de antes de cancelarla) y cancelada bien en Odoo. Quedaba «Estatus
        SAT distinto» para siempre: no hay CFDI que la Bóveda pueda volver a
        verificar, y el refresco de 90 días no vuelve a pedir lo que se
        canceló después."""
        venta = self._venta()
        self._metadatos(venta, estatus='1')
        self.assertEqual(venta.dsi_vault_sat_state, 'valid')
        self._pedir_cancelacion(venta)
        with self.with_mocked_sat_call(lambda _documento: 'cancelled'):
            venta.l10n_mx_edi_cfdi_try_sat()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_sat_state), ('cancel', 'cancelled'))
        self.assertFalse(venta.dsi_vault_sat_mismatch)
        self._revision_diaria()
        self.assertFalse(self._vivos(venta))
        self.assertFalse(self.Inconsistencia._cfdi_por_volver_a_verificar())

    # -- A quién no ------------------------------------------------------------

    @freeze_time('2017-01-01')
    def test_a_una_compra_nunca_se_le_ejecuta(self):
        compra = self._compra_timbrada()
        asiento = compra.line_ids.read(['account_id', 'debit', 'credit'])
        with patch.object(self.Factura, 'l10n_mx_edi_cfdi_try_sat', autospec=True) as try_sat:
            self._metadatos(compra, direction='received')
            self.env['account.move']._cron_actualizar_cancelaciones_emitidas()
            self._revision_diaria()
        try_sat.assert_not_called()
        self.assertEqual(compra.state, 'posted')
        self.assertEqual(compra.line_ids.read(['account_id', 'debit', 'credit']), asiento)
        self.assertEqual(list(self._vivos(compra)), ['sat_distinto'])

    @freeze_time('2017-01-01')
    def test_a_una_venta_sin_solicitud_no_se_le_ejecuta(self):
        venta = self._venta()
        with patch.object(self.Factura, 'l10n_mx_edi_cfdi_try_sat', autospec=True) as try_sat:
            self._metadatos(venta)
            self.env['account.move']._cron_actualizar_cancelaciones_emitidas()
            self._revision_diaria()
        try_sat.assert_not_called()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'sent'))
        distinto = self._vivos(venta)
        self.assertEqual(list(distinto), ['sat_distinto'])
        self.assertIn("«Actualizar SAT» en la factura registra la cancelación", distinto['sat_distinto'].observation)

    @freeze_time('2017-01-01')
    def test_sin_licencia_no_se_ejecuta(self):
        venta = self._venta_con_solicitud()
        licencia = self.env['dsi.license.client.state']._get_or_create(self.company)
        licencia.sudo().write({'token': False})
        self.assertFalse(licencia.is_feature_enabled('vault'))
        disparos = self._disparos()

        self._metadatos(venta)
        self.assertEqual(self._disparos(), disparos, "sin licencia no se pide la tarea")
        self._actualizar_sat('cancelled').assert_not_called()
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'cancel_requested'))
        pendiente = self._vivos(venta)['cancelacion_por_actualizar']
        self.assertEqual(pendiente.fingerprint, 'consulta=not_defined|sin_licencia')
        self.assertIn("Sin una licencia vigente", pendiente.observation)

    @freeze_time('2017-01-01')
    def test_en_una_empresa_que_no_es_de_mexico_en_pesos_lo_explica(self):
        """En 19 y 20 «Actualizar SAT» no hace nada si la empresa no es de
        México con moneda MXN; en 18 no existe esa revisión."""
        if not hasattr(self.Factura, '_l10n_mx_edi_is_cfdi_invoice'):
            self.skipTest("Odoo 18 consulta el SAT en cualquier empresa")
        venta = self._venta_con_solicitud()
        with patch.object(self.Factura, '_l10n_mx_edi_is_cfdi_invoice', lambda _factura: False):
            self._metadatos(venta)
            self._actualizar_sat('cancelled').assert_not_called()
        pendiente = self._vivos(venta)['cancelacion_por_actualizar']
        self.assertEqual(pendiente.fingerprint, 'consulta=not_defined|no_aplica')
        self.assertIn("solo consulta el SAT en empresas de México con moneda MXN", pendiente.observation)

    # -- Cuántas veces y fallas --------------------------------------------------

    @freeze_time('2017-01-01')
    def test_no_repite_la_consulta_antes_de_20_horas(self):
        venta = self._venta_con_solicitud()
        self._metadatos(venta)

        with freeze_time('2017-01-01 10:00:00'):
            self._actualizar_sat('valid').assert_called_once()
            self._actualizar_sat('valid').assert_not_called()
        # Un aviso nuevo (otra lectura de metadatos, la consulta pública) no
        # la adelanta.
        with freeze_time('2017-01-02 05:59:00'):
            self._metadatos(venta)
            self._revision_diaria()
            self._actualizar_sat('valid').assert_not_called()
        with freeze_time('2017-01-02 06:00:01'):
            self._actualizar_sat('valid').assert_called_once()
        self.assertEqual(venta.sudo().dsi_try_sat_last, fields.Datetime.to_datetime('2017-01-02 06:00:01'))

    @freeze_time('2017-01-01')
    def test_una_venta_que_falla_no_detiene_a_las_demas(self):
        primera = self._venta_con_solicitud()
        segunda = self._venta_con_solicitud()
        self._metadatos(primera)
        self._metadatos(segunda)
        original = self.Factura.l10n_mx_edi_cfdi_try_sat

        def try_sat(factura):
            if factura == primera:
                raise UserError("falla simulada")
            return original(factura)

        with self.with_mocked_sat_call(lambda _documento: 'cancelled'), \
                patch.object(self.Factura, 'l10n_mx_edi_cfdi_try_sat', autospec=True, side_effect=try_sat) as espia, \
                self.assertLogs(LOG_FACTURA, level='ERROR') as log:
            self.env['account.move']._cron_actualizar_cancelaciones_emitidas()
        self.assertEqual(espia.call_count, 2)
        self.assertIn(f"falló en la factura {primera.name}", log.output[0])
        self.assertEqual((primera.state, primera.l10n_mx_edi_cfdi_state), ('posted', 'cancel_requested'))
        self.assertTrue(primera.sudo().dsi_try_sat_last, "no se reintenta en cada corrida")
        self.assertEqual(self._vivos(primera)['cancelacion_por_actualizar'].fingerprint,
                         'consulta=not_defined|intentada')
        self.assertEqual((segunda.state, segunda.l10n_mx_edi_cfdi_state), ('cancel', 'cancel'))
        self.assertFalse(self._vivos(segunda))

    @freeze_time('2017-01-01')
    def test_un_choque_con_otra_transaccion_es_un_aviso(self):
        venta = self._venta_con_solicitud()
        self._metadatos(venta)

        def choque(_factura):
            raise psycopg2.errors.SerializationFailure("choque simulado")

        with patch.object(self.Factura, 'l10n_mx_edi_cfdi_try_sat', autospec=True, side_effect=choque), \
                self.assertLogs(LOG_FACTURA, level='WARNING') as log:
            self.env['account.move']._cron_actualizar_cancelaciones_emitidas()
        self.assertEqual([registro.levelname for registro in log.records], ['WARNING'])
        self.assertIn("chocó con otra transacción (SerializationFailure)", log.output[0])
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('posted', 'cancel_requested'))
        self.assertEqual(list(self._vivos(venta)), ['cancelacion_por_actualizar'])

    @freeze_time('2017-01-01')
    def test_una_falla_al_conciliar_no_deja_la_tarea_en_bucle(self):
        """Conciliar el XML de la venta iba antes de guardar la hora del
        intento: si fallaba, la venta quedaba sin intento, su discrepancia
        volvía a pedir la tarea y la tarea volvía a fallar, sin fin. Y una
        falla al conciliar no impide «Actualizar SAT»."""
        venta = self._venta_con_solicitud()
        doc = self._cfdi_de_la_boveda(venta)
        self._metadatos(venta)

        # Falla todo lo de la conciliación: no hay «Actualizar SAT», pero
        # queda el intento y no se vuelve a pedir la tarea.
        self._corrida_nueva()
        with patch.object(self.Factura, '_dsi_conciliar_su_cfdi', autospec=True,
                          side_effect=ValueError("falla al conciliar")), \
                self.assertLogs(LOG_FACTURA, level='ERROR'):
            self._actualizar_sat('cancelled').assert_not_called()
        self.assertTrue(venta.sudo().dsi_try_sat_last)
        self.assertEqual(self._disparos(), 0)
        self.assertEqual(self._vivos(venta)['cancelacion_por_actualizar'].fingerprint,
                         'consulta=not_defined|intentada')

        # Al día siguiente falla solo la conciliación: se sigue sin ella.
        with freeze_time('2017-01-02'):
            self._corrida_nueva()
            with patch.object(self.env.registry['cfdi.document'], 'action_match_automatically',
                              side_effect=ValueError("falla al conciliar")), \
                    self.assertLogs(LOG_FACTURA, level='ERROR') as log:
                self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual(len(log.records), 1)
        self.assertIn(f"falló conciliar el CFDI {doc.uuid}", log.output[0])
        self.assertFalse(doc.move_id)
        self.assertEqual((venta.state, venta.l10n_mx_edi_cfdi_state), ('cancel', 'cancel'))
        self.assertFalse(self._vivos(venta))
        self.assertEqual(self._disparos(), 0)

    @freeze_time('2017-01-01')
    def test_sin_tiempo_se_programa_otra_corrida(self):
        """También si la corrida la inició un disparo: Odoo lo borra en la
        transacción de la corrida, que se confirma al terminar, y mientras
        tanto lo ven las demás. Antes, al verlo, no se pedía otro y lo que
        faltaba esperaba a la corrida del día siguiente."""
        venta = self._venta_con_solicitud()
        self._metadatos(venta)
        Disparo = self.env['ir.cron.trigger'].sudo()
        Disparo.search([('cron_id', '=', self.cron.id)]).unlink()
        self.cron.sudo()._trigger()   # el que inició esta corrida

        self._actualizar_sat('cancelled', segundos=-1).assert_not_called()
        self.assertEqual(self._disparos(), 2, "uno nuevo para lo que faltó")
        self._actualizar_sat('cancelled').assert_called_once()
        self.assertEqual(venta.state, 'cancel')

    def test_los_avisos_piden_la_tarea_una_vez_por_transaccion(self):
        """Un disparo pendiente de otra transacción (el de una corrida en
        curso) no basta: se pide uno propio, una sola vez por transacción. Si
        un savepoint lo deshizo, se vuelve a pedir."""
        Disparo = self.env['ir.cron.trigger'].sudo()
        Disparo.search([('cron_id', '=', self.cron.id)]).unlink()
        self.cron.sudo()._trigger()
        Factura = self.env['account.move']
        Factura._dsi_programar_actualizar_sat()
        Factura._dsi_programar_actualizar_sat()
        self.assertEqual(self._disparos(), 2)

        Disparo.search([('cron_id', '=', self.cron.id)]).unlink()
        Factura._dsi_programar_actualizar_sat()
        self.assertEqual(self._disparos(), 1)
