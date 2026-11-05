"""Los CFDI de nómina (N) relacionados con su póliza.

Existe porque la Bóveda no tenía cómo llevar una N a la póliza que la
contabiliza: solo un vínculo a mano sin dominio, que dejaba ligarla a una
factura, y la conciliación por RFC, total y fecha la ligaba sola a una factura
del empleado. Ahora «Relacionar con póliza» busca la póliza por la fecha de
pago del complemento, liga todas las seleccionadas y deja una copia de cada
XML en los adjuntos de la póliza.

Lo que más cuidan estas pruebas:

  * la póliza se busca por la FechaPago del complemento nomina12, no por la
    Fecha del comprobante, que llega semanas después, y un comentario en el
    XML no tumba la lectura;
  * el diario habitual y la cercanía a la fecha de pago se ordenan antes del
    límite de búsqueda, y los asientos CABA y de diferencia cambiaria nunca
    son candidatos;
  * una copia por XML, sin duplicar al volver a relacionar y reutilizando el
    XML que ya estaba en la póliza;
  * las copias nunca se borran (desvincular, reemplazar, bitácora de
    auditoría) y la nota en la póliza va sin adjuntos: Odoo no importa nada;
  * empresa, tipo y póliza cancelada se validan en el servidor, también al
    ligar a mano; una liga de antes a otra empresa no copia el XML ni estorba;
  * una N no se liga sola a una factura por importe.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_rep_facturas.py, y una segunda empresa sin catálogo.
"""
import io
import logging
import zipfile
from datetime import date
from unittest.mock import patch

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, RedirectWarning, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL
from odoo.tools.safe_eval import safe_eval

from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import RFC_RECEPTOR
from .test_factura_como_odoo import cfdi_xml
from .test_rep_facturas import GRUPOS_BOVEDA

EMPLEADO = 'PEGJ800101AB1'
ASISTENTE = 'odoo.addons.dsi_cfdi_vault.wizards.cfdi_nomina_poliza_wizard'
NOMINA = 'odoo.addons.dsi_cfdi_vault.models.cfdi_nomina'
DOCUMENTO = 'odoo.addons.dsi_cfdi_vault.models.cfdi_document'


def nomina_xml(uuid, total='9251.60', fecha='2026-06-18',
               nominas=(('O', '2026-05-15', '2026-05-01', '2026-05-15'),),
               emisor=RFC_RECEPTOR, empleado=EMPLEADO):
    """Un CFDI de Nómina 4.0 con un nodo nomina12:Nomina por cada (tipo,
    fecha de pago, periodo desde, periodo hasta) de `nominas`; con
    nominas=() sale sin complemento. Lo emite la empresa (RFC_RECEPTOR) y el
    receptor es el empleado. El nomina12:Receptor va a propósito: tiene el
    mismo nombre local que cfdi:Receptor."""
    nodos = ''.join(f'''
    <nomina12:Nomina Version="1.2" TipoNomina="{tipo}" FechaPago="{pago}" FechaInicialPago="{inicial}"
        FechaFinalPago="{final}" NumDiasPagados="15.000" TotalPercepciones="{total}" TotalDeducciones="0.00">
      <nomina12:Receptor Curp="PEGJ800101HNLRRN09" NumEmpleado="1" TipoContrato="01" TipoRegimen="02"
          PeriodicidadPago="04" ClaveEntFed="NLE"/>
    </nomina12:Nomina>''' for tipo, pago, inicial, final in nominas)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:nomina12="http://www.sat.gob.mx/nomina12"
    xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" Version="4.0" Fecha="{fecha}T08:00:00"
    SubTotal="{total}" Total="{total}" Moneda="MXN" TipoDeComprobante="N" MetodoPago="PUE" Exportacion="01">
  <cfdi:Emisor Rfc="{emisor}" Nombre="Mi Empresa" RegimenFiscal="601"/>
  <cfdi:Receptor Rfc="{empleado}" Nombre="Empleado de Prueba" UsoCFDI="CN01"/>
  <cfdi:Conceptos><cfdi:Concepto ClaveProdServ="84111505" Cantidad="1" ClaveUnidad="ACT"
      Descripcion="Pago de nómina" ValorUnitario="{total}" Importe="{total}" ObjetoImp="01"/></cfdi:Conceptos>
  <cfdi:Complemento>{nodos}
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/>
  </cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


@tagged('post_install', '-at_install')
class TestNominaPoliza(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        mx = cls.env.ref('base.mx')
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de nómina", 'country_id': mx.id, 'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.otra = cls.env['res.company'].create({
            'name': "Otra empresa de nómina", 'country_id': mx.id, 'currency_id': mxn.id,
        })
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.usuario = new_test_user(
            cls.env, login='boveda-nomina', groups=GRUPOS_BOVEDA,
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.Doc = cls.env['cfdi.document']
        cls.Asistente = cls.env['dsi.cfdi.nomina.poliza.wizard']
        cls.empleado = cls.env['res.partner'].create({'name': "Empleado de Prueba", 'vat': EMPLEADO})

        # Diario y cuentas de la póliza de nómina, en cada empresa: gasto =
        # neto + ISR, ISR retenido y nómina por pagar (el neto).
        cls.cuentas = {}
        cls.diarios = {}
        for empresa in (cls.company, cls.otra):
            Cuenta = cls.env['account.account'].with_company(empresa)
            cls.cuentas[empresa] = tuple(Cuenta.create({
                'name': nombre, 'code': codigo, 'account_type': tipo, 'company_ids': [Command.set(empresa.ids)],
            }) for nombre, codigo, tipo in (
                ("Sueldos de prueba", '699.99.01', 'expense'),
                ("ISR retenido de prueba", '299.99.01', 'liability_current'),
                ("Nómina por pagar de prueba", '299.99.02', 'liability_current'),
            ))
            cls.diarios[empresa] = cls.env['account.journal'].with_company(empresa).create({
                'name': "Nómina", 'code': 'NOM', 'type': 'general', 'company_id': empresa.id,
            })
        cls.diario = cls.diarios[cls.company]
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return f"ADADADAD-3333-4000-8000-{self.contador:012d}"

    def _guardar(self, xml, direction='issued', company=None, sat_status='valid', **extra):
        """El CFDI en la Bóveda, como lo deja la importación; `extra` son
        valores más para el create (una importación CSV que trae la póliza)."""
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
            **extra,
        })
        doc = self.Doc.create(datos)
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _nomina(self, company=None, sat_status='valid', **kwargs):
        return self._guardar(nomina_xml(self._uuid(), **kwargs), company=company, sat_status=sat_status)

    def _poliza(self, fecha, neto, isr=100.0, ref="Nómina 1a quincena mayo", journal=None, state='posted',
                company=None, etiqueta="Nómina por pagar"):
        """Póliza de provisión: cargo a sueldos por neto + ISR, abono al ISR
        retenido y a nómina por pagar por el neto."""
        company = company or self.company
        gasto, retenido, por_pagar = self.cuentas[company]
        poliza = self.env['account.move'].with_company(company).create({
            'move_type': 'entry',
            'date': fecha,
            'ref': ref,
            'journal_id': (journal or self.diarios[company]).id,
            'line_ids': [
                Command.create({'name': "Sueldos", 'account_id': gasto.id, 'debit': neto + isr}),
                Command.create({'name': "ISR retenido", 'account_id': retenido.id, 'credit': isr}),
                Command.create({'name': etiqueta, 'account_id': por_pagar.id, 'credit': neto}),
            ],
        })
        if state == 'posted':
            poliza.action_post()
        elif state == 'cancel':
            poliza.button_cancel()
        return poliza

    def _factura_del_empleado(self, fecha='2026-06-18', total=9251.60):
        """Una factura de proveedor del RFC del empleado (un reembolso), con el
        mismo total y fecha que su recibo de nómina."""
        factura = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.empleado.id,
            'invoice_date': fecha,
            'invoice_line_ids': [Command.create({
                'name': "Reembolso", 'quantity': 1, 'price_unit': total, 'tax_ids': [Command.clear()],
            })],
        })
        factura.action_post()
        return factura

    def _abrir(self, docs, user=None):
        """«Relacionar con póliza» como el usuario: el asistente que abre."""
        accion = docs.with_user(user or self.usuario).action_relacionar_con_poliza()
        self.assertEqual(accion['res_model'], 'dsi.cfdi.nomina.poliza.wizard')
        self.assertEqual(accion['target'], 'new')
        return self.Asistente.with_user(user or self.usuario).browse(accion['res_id'])

    def _relacionar(self, docs, poliza, user=None, **valores):
        asistente = self._abrir(docs, user)
        asistente.write(dict(valores, move_id=poliza.id))
        return asistente, asistente.action_relacionar()

    def _copias(self, poliza):
        """Los adjuntos de la póliza (las pólizas de prueba no traen otros).
        Con sudo: los de una póliza de la otra empresa no se ven desde esta."""
        return self.env['ir.attachment'].sudo().search(
            [('res_model', '=', 'account.move'), ('res_id', '=', poliza.id)], order='id')

    def _sat_dice(self, docs, valor):
        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, _uuid):
            return {'value': valor}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            docs._fetch_and_store_sat_status()

    # -- Lectura del complemento ------------------------------------------

    def test_lee_tipo_fecha_de_pago_y_periodo_del_complemento(self):
        uuid = self._uuid()
        vals = self.Doc._parse_cfdi_xml(nomina_xml(uuid))
        self.assertEqual(vals['dsi_nomina_tipo'], 'O')
        self.assertEqual(vals['dsi_nomina_fecha_pago'], date(2026, 5, 15))
        self.assertEqual(vals['dsi_nomina_fecha_inicial'], date(2026, 5, 1))
        self.assertEqual(vals['dsi_nomina_fecha_final'], date(2026, 5, 15))
        # La Fecha del comprobante sigue siendo la de emisión, y el receptor
        # es el de cfdi:Receptor, no el nodo del complemento.
        self.assertEqual(vals['invoice_date'], '2026-06-18')
        self.assertEqual(vals['partner_rfc_receiver'], EMPLEADO)
        self.assertEqual(vals['amount_total'], 9251.60)

        doc = self._guardar(nomina_xml(uuid))
        self.assertEqual(
            (doc.dsi_nomina_tipo, doc.dsi_nomina_fecha_pago, doc.dsi_nomina_fecha_inicial, doc.dsi_nomina_fecha_final),
            ('O', date(2026, 5, 15), date(2026, 5, 1), date(2026, 5, 15)))
        # Un CFDI que no es de nómina no lleva estos datos.
        self.assertNotIn('dsi_nomina_fecha_pago', self.Doc._parse_cfdi_xml(cfdi_xml(self._uuid()).encode()))

    def test_dos_nodos_de_nomina_toman_la_primera_fecha_y_todo_el_periodo(self):
        doc = self._nomina(nominas=(
            ('O', '2026-05-31', '2026-05-16', '2026-05-31'),
            ('E', '2026-05-20', '2026-05-20', '2026-05-20'),
        ))
        self.assertEqual(doc.dsi_nomina_tipo, 'O')
        self.assertEqual(doc.dsi_nomina_fecha_pago, date(2026, 5, 20))
        self.assertEqual((doc.dsi_nomina_fecha_inicial, doc.dsi_nomina_fecha_final),
                         (date(2026, 5, 16), date(2026, 5, 31)))
        extraordinaria = self._nomina(nominas=(('E', '2026-05-20', '2026-05-20', '2026-05-20'),))
        self.assertEqual(extraordinaria.dsi_nomina_tipo, 'E')

    def test_un_comentario_o_una_instruccion_en_el_xml_no_tumban_la_lectura(self):
        """root.iter() también da los comentarios y las instrucciones de
        proceso, cuyo tag no es texto. Un sistema de nómina que deja un
        comentario en la Addenda tumbaba la importación del ZIP completo, y
        en la descarga del SAT el paquete fallaba en cada corrida. Antes del
        timbre ya pasaba con cualquier CFDI."""
        for extra in (b'<!-- generado por el sistema de nomina -->', b'<?sistema-nomina version="3"?>'):
            xml = nomina_xml(self._uuid()).replace(
                b'</cfdi:Complemento>', b'</cfdi:Complemento><cfdi:Addenda>' + extra + b'</cfdi:Addenda>')
            vals = self.Doc._parse_cfdi_xml(xml)
            self.assertEqual(vals['parse_state'], 'ok')
            self.assertEqual((vals['dsi_nomina_tipo'], vals['dsi_nomina_fecha_pago'], vals['dsi_nomina_fecha_final']),
                             ('O', date(2026, 5, 15), date(2026, 5, 15)))
            # Antes del Emisor y del timbre.
            vals = self.Doc._parse_cfdi_xml(nomina_xml(self._uuid()).replace(b'<cfdi:Emisor', extra + b'<cfdi:Emisor'))
            self.assertEqual((vals['parse_state'], vals['partner_rfc_receiver'], vals['dsi_nomina_fecha_pago']),
                             ('ok', EMPLEADO, date(2026, 5, 15)))
            ingreso = cfdi_xml(self._uuid()).replace('<cfdi:Emisor', extra.decode() + '<cfdi:Emisor').encode()
            vals = self.Doc._parse_cfdi_xml(ingreso)
            self.assertEqual((vals['parse_state'], vals['move_type'], vals['amount_total']), ('ok', 'I', 1160.0))

    def test_si_el_complemento_de_nomina_no_se_lee_el_cfdi_se_guarda_igual(self):
        """Los datos de nómina solo sirven para buscar la póliza: si fallan,
        el CFDI se lee sin ellos (el asistente usa la Fecha del comprobante)
        y queda en el log como información."""
        uuid = self._uuid()
        with patch(f'{DOCUMENTO}._datos_de_nomina', side_effect=ValueError("nodo inesperado")), \
                self.assertLogs(DOCUMENTO, level='INFO') as registro:
            vals = self.Doc._parse_cfdi_xml(nomina_xml(uuid))
        self.assertEqual((vals['parse_state'], vals['uuid'], vals['move_type']), ('ok', uuid, 'N'))
        self.assertNotIn('dsi_nomina_fecha_pago', vals)
        self.assertFalse([r for r in registro.records if r.levelno >= logging.WARNING])
        self.assertTrue(any(uuid in r.getMessage() and "nodo inesperado" in r.getMessage() for r in registro.records))

    def test_un_zip_con_una_nomina_comentada_se_importa_completo(self):
        nomina = nomina_xml(self._uuid()).replace(
            b'</cfdi:Complemento>', b'</cfdi:Complemento><cfdi:Addenda><!-- x --></cfdi:Addenda>')
        archivo = io.BytesIO()
        with zipfile.ZipFile(archivo, 'w') as zf:
            zf.writestr('nomina.xml', nomina)
            zf.writestr('ingreso.xml', cfdi_xml(self._uuid()))
        zip_adjunto = self.env['ir.attachment'].create({'name': 'cfdi.zip', 'raw': archivo.getvalue()})
        asistente = self.env['cfdi.import.wizard'].create({
            'company_id': self.company.id, 'attachment_ids': [Command.set(zip_adjunto.ids)]})
        asistente.action_import()
        self.assertEqual((asistente.imported_count, asistente.error_count), (2, 0))
        importada = asistente.result_ids.cfdi_document_id.filtered(lambda d: d.move_type == 'N')
        self.assertEqual(importada.dsi_nomina_fecha_pago, date(2026, 5, 15))

    def test_una_nomina_sin_complemento_se_guarda_sin_fecha_de_pago(self):
        """Sin complemento la póliza se busca alrededor de la Fecha del
        comprobante."""
        doc = self._nomina(nominas=())
        self.assertFalse(doc.dsi_nomina_fecha_pago)
        self.assertFalse(doc.dsi_nomina_tipo)
        asistente = self._abrir(doc)
        self.assertEqual((asistente.date_from, asistente.date_to), (date(2026, 6, 11), date(2026, 6, 25)))
        self.assertEqual(asistente.payment_date_from, date(2026, 6, 18))

    def test_al_actualizar_se_llenan_las_nominas_guardadas_y_no_se_repite(self):
        doc = self._nomina()
        sin_complemento = self._nomina(nominas=())
        ilegible = self._nomina(nominas=())
        ilegible.xml_attachment_id.raw = b'<cfdi:Comprobante roto'
        doc.write({'dsi_nomina_tipo': False, 'dsi_nomina_fecha_pago': False,
                   'dsi_nomina_fecha_inicial': False, 'dsi_nomina_fecha_final': False})

        # Un XML que no se puede leer es información, no una advertencia.
        with self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_nomina', level='INFO') as registro:
            self.Doc._backfill_datos_de_nomina()
        self.assertFalse([r for r in registro.records if r.levelno >= logging.WARNING])
        self.assertTrue(any(ilegible.uuid in r.getMessage() for r in registro.records))
        self.assertEqual(
            (doc.dsi_nomina_tipo, doc.dsi_nomina_fecha_pago, doc.dsi_nomina_fecha_inicial, doc.dsi_nomina_fecha_final),
            ('O', date(2026, 5, 15), date(2026, 5, 1), date(2026, 5, 15)))
        self.assertFalse(sin_complemento.dsi_nomina_fecha_pago)

        # Otra vez: ya no escribe nada (ni la que no trae complemento).
        with patch.object(type(self.Doc), 'write', side_effect=AssertionError("no debe escribir")), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_nomina', level='INFO'):
            self.Doc._backfill_datos_de_nomina()

    def test_al_actualizar_los_xml_no_se_quedan_en_memoria_entre_tandas(self):
        """Una base con años de nómina son decenas de miles de XML: cada tanda
        suelta la caché, con el contenido de los que leyó, antes de la
        siguiente. Con tandas de uno, al leer cada XML ya no debe estar en
        caché ninguno de los anteriores."""
        docs = self._nomina() | self._nomina() | self._nomina()
        docs.write({'dsi_nomina_tipo': False, 'dsi_nomina_fecha_pago': False,
                    'dsi_nomina_fecha_inicial': False, 'dsi_nomina_fecha_final': False})
        original = _bytes_de_adjunto
        leidos, retenidos = [], []

        def leer(adjunto):
            retenidos.extend(previo for previo in leidos if 'raw' in previo._cache)
            leidos.append(adjunto)
            return original(adjunto)

        with patch(f'{NOMINA}.LOTE_DEL_LLENADO', 1), patch(f'{NOMINA}._bytes_de_adjunto', leer), \
                self.assertLogs(NOMINA, level='INFO'):
            self.Doc._backfill_datos_de_nomina()
        self.assertEqual(len(leidos), 3)
        self.assertFalse(retenidos, "el XML de una tanda anterior sigue en caché")
        for doc in docs:
            self.assertEqual(doc.dsi_nomina_fecha_pago, date(2026, 5, 15))

    # -- La acción --------------------------------------------------------

    def test_la_accion_rechaza_si_ninguno_es_de_nomina(self):
        ingreso = self._guardar(cfdi_xml(self._uuid()).encode(), direction='received')
        with self.assertRaises(UserError) as error:
            ingreso.with_user(self.usuario).action_relacionar_con_poliza()
        self.assertIn("no son CFDI de nómina", str(error.exception))

    def test_relaciona_solo_las_nominas_y_dice_que_dejo_fuera(self):
        nominas = self._nomina() | self._nomina()
        ingreso = self._guardar(cfdi_xml(self._uuid()).encode(), direction='received')
        cancelada = self._nomina(sat_status='canceled')
        asistente = self._abrir(nominas | ingreso | cancelada)
        self.assertEqual(asistente.cfdi_ids, nominas)
        self.assertEqual(asistente.cfdi_count, 2)
        self.assertIn("1 porque no son CFDI de nómina", asistente.skipped_note)
        self.assertIn("1 porque están cancelados en el SAT", asistente.skipped_note)
        # Sin descartes no hay nota.
        self.assertFalse(self._abrir(nominas).skipped_note)

    def test_no_mezcla_empresas(self):
        docs = self._nomina() | self._nomina(company=self.otra)
        with self.assertRaises(UserError) as error:
            docs.action_relacionar_con_poliza()
        self.assertIn("una sola empresa", str(error.exception))

    def test_la_accion_de_servidor_abre_el_asistente(self):
        docs = self._nomina() | self._nomina()
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_relacionar_con_poliza')
        self.assertEqual(accion.binding_model_id.model, 'cfdi.document')
        self.assertEqual(accion.binding_view_types, 'list')
        resultado = accion.with_user(self.usuario).with_context(
            active_model='cfdi.document', active_ids=docs.ids, active_id=docs[0].id).run()
        self.assertEqual(resultado['res_model'], 'dsi.cfdi.nomina.poliza.wizard')
        self.assertEqual(self.Asistente.browse(resultado['res_id']).cfdi_ids, docs)

    # -- Búsqueda ---------------------------------------------------------

    def test_propone_por_fecha_de_pago_y_no_por_fecha_de_emision(self):
        """La N se timbró el 18 de junio por el pago del 15 de mayo: la póliza
        está en mayo."""
        doc = self._nomina()
        del_pago = self._poliza('2026-05-15', 9251.60)
        de_la_emision = self._poliza('2026-06-18', 9251.60)
        asistente = self._abrir(doc)
        self.assertEqual((asistente.date_from, asistente.date_to), (date(2026, 5, 8), date(2026, 5, 22)))
        self.assertIn(del_pago, asistente.line_ids.move_id)
        self.assertNotIn(de_la_emision, asistente.line_ids.move_id)

    def test_la_poliza_con_el_neto_va_primero(self):
        docs = self._nomina(total='1000.00') | self._nomina(total='2500.50')
        sin_el_neto = self._poliza('2026-05-15', 5000.0)
        con_el_neto = self._poliza('2026-05-12', 3500.50)
        asistente = self._abrir(docs)
        self.assertEqual(asistente.amount, 3500.50)
        self.assertEqual(asistente.net_total, 3500.50)
        primera = asistente.line_ids[0]
        self.assertEqual(primera.move_id, con_el_neto)
        self.assertTrue(primera.amount_matches)
        self.assertEqual(primera.difference, 0.0)
        self.assertEqual(primera.total_debit, 3600.50)
        otra = asistente.line_ids.filtered(lambda linea: linea.move_id == sin_el_neto)
        self.assertFalse(otra.amount_matches)
        self.assertEqual(otra.closest_amount, 5000.0)

    def test_la_poliza_con_el_neto_no_se_pierde_por_el_limite(self):
        doc = self._nomina()
        con_el_neto = self._poliza('2026-05-10', 9251.60)
        cercanas = [self._poliza(f'2026-05-{dia}', 100.0) for dia in (16, 17, 18)]
        with patch(f'{ASISTENTE}.LIMITE_DE_BUSQUEDA', 1):
            asistente = self._abrir(doc)
            # Sin el neto entra la más cercana a la fecha de pago (el 15),
            # no la más reciente.
            self.assertEqual(asistente.line_ids.move_id, con_el_neto | cercanas[0])
            self.assertEqual(asistente.line_ids[0].move_id, con_el_neto)
        asistente.only_matching_amount = True
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, con_el_neto)

    def test_el_diario_habitual_se_ordena_antes_del_limite(self):
        """Con una selección parcial (la póliza es de toda la quincena) o el
        neto repartido en varias cuentas, la póliza correcta no tiene el neto
        y solo entra por el límite. En una base real los pagos, los extractos
        y los asientos de base de efectivo son asientos contables, decenas al
        día: si el orden se aplica después de cortar a las más recientes, la
        póliza se queda fuera."""
        previa = self._nomina(nominas=(('O', '2026-04-30', '2026-04-16', '2026-04-30'),))
        self._relacionar(previa, self._poliza('2026-04-30', 9251.60, ref="abril"))
        doc = self._nomina(total='1000.00')
        # Fechada al fin de la ventana: la del diario de nómina, aunque sea la más lejana.
        correcta = self._poliza('2026-05-20', 3000.0)
        otro = self.env['account.journal'].create({
            'name': "Bancos varios", 'code': 'BMX', 'type': 'general', 'company_id': self.company.id})
        otras = [self._poliza(f'2026-05-{dia}', 50.0, ref="Movimiento", journal=otro) for dia in (15, 16, 21, 22)]
        with patch(f'{ASISTENTE}.LIMITE_DE_BUSQUEDA', 2):
            asistente = self._abrir(doc)
        self.assertFalse(asistente.line_ids.filtered('amount_matches'))
        self.assertEqual(asistente.line_ids.move_id, correcta | otras[0])
        self.assertEqual(asistente.line_ids[0].move_id, correcta)

    def test_la_cercania_a_la_fecha_de_pago_se_ordena_antes_del_limite(self):
        """Sin diario habitual (la primera vez), la más cercana a la fecha de
        pago, no la más reciente; a la misma distancia, la más nueva."""
        doc = self._nomina(total='1000.00')
        correcta = self._poliza('2026-05-14', 3000.0)
        empate = self._poliza('2026-05-16', 2000.0)
        recientes = [self._poliza(f'2026-05-{dia}', 50.0) for dia in (19, 20, 21, 22)]
        with patch(f'{ASISTENTE}.LIMITE_DE_BUSQUEDA', 2):
            asistente = self._abrir(doc)
        self.assertFalse(asistente.line_ids.move_id & self.env['account.move'].browse([p.id for p in recientes]))
        self.assertEqual(asistente.line_ids.move_id, correcta | empate)
        self.assertEqual(asistente.line_ids[0].move_id, empate)

    def test_no_propone_asientos_de_base_de_efectivo_ni_de_diferencia_cambiaria(self):
        """Odoo los crea solos al conciliar y nunca son una póliza de nómina,
        aunque traigan el neto. Los movimientos del banco sí: la nómina se
        puede contabilizar desde el extracto."""
        doc = self._nomina()
        correcta = self._poliza('2026-05-15', 9251.60)
        caba = self._poliza('2026-05-15', 9251.60, ref="IVA en base de efectivo", state='draft')
        caba.tax_cash_basis_origin_move_id = self._factura_del_empleado(fecha='2026-04-01')
        caba.action_post()
        cambiario = self.company.currency_exchange_journal_id
        self.assertTrue(cambiario, "el catálogo de México crea el diario de diferencias cambiarias")
        cambiaria = self._poliza('2026-05-15', 9251.60, ref="Diferencia cambiaria", journal=cambiario)
        banco = self.env['account.journal'].search(
            [('company_id', '=', self.company.id), ('type', '=', 'bank')], limit=1)
        del_banco = self.env['account.bank.statement.line'].create({
            'journal_id': banco.id, 'date': '2026-05-15', 'amount': -9251.60, 'payment_ref': "Pago de nómina",
        }).move_id
        nunca = caba | cambiaria

        asistente = self._abrir(doc)
        self.assertIn(correcta, asistente.line_ids.move_id)
        self.assertIn(del_banco, asistente.line_ids.move_id)
        self.assertFalse(asistente.line_ids.move_id & nunca)
        # Tampoco por la ruta del neto.
        asistente.only_matching_amount = True
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, correcta | del_banco)
        # Quien filtra por el diario de diferencias cambiarias sí las ve.
        asistente.write({'only_matching_amount': False, 'journal_id': cambiario.id})
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, cambiaria)

    def test_el_diario_cambiario_no_se_excluye_si_es_el_de_la_nomina(self):
        """Una empresa que puso como diario de diferencias cambiarias el mismo
        donde contabiliza la nómina: en cuanto relaciona una, ese diario es
        el habitual y sus pólizas vuelven a proponerse."""
        self.company.currency_exchange_journal_id = self.diario
        previa = self._nomina(nominas=(('O', '2026-04-30', '2026-04-16', '2026-04-30'),))
        abril = self._poliza('2026-04-30', 9251.60, ref="abril")
        self.assertNotIn(abril, self._abrir(previa).line_ids.move_id)
        self._relacionar(previa, abril)
        doc = self._nomina()
        correcta = self._poliza('2026-05-15', 9251.60)
        self.assertIn(correcta, self._abrir(doc).line_ids.move_id)

    def test_filtros_de_diario_referencia_y_borradores(self):
        doc = self._nomina()
        otro_diario = self.env['account.journal'].create({
            'name': "Otros", 'code': 'OTR', 'type': 'general', 'company_id': self.company.id,
        })
        en_nomina = self._poliza('2026-05-15', 500.0, ref="Quincena 1 mayo")
        en_otro = self._poliza('2026-05-15', 600.0, ref="Otra cosa", journal=otro_diario, etiqueta="NOM-QNA-09")
        borrador = self._poliza('2026-05-15', 700.0, state='draft')
        cancelada = self._poliza('2026-05-15', 800.0, state='cancel')
        de_otra_empresa = self._poliza('2026-05-15', 900.0, company=self.otra, state='draft')
        factura = self._factura_del_empleado(fecha='2026-05-15')
        nunca = cancelada | de_otra_empresa | factura

        asistente = self._abrir(doc)
        self.assertEqual(asistente.line_ids.move_id, en_nomina | en_otro)

        asistente.journal_id = self.diario
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, en_nomina)

        asistente.journal_id = False
        asistente.reference = "quincena 1"
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, en_nomina, "por la referencia")
        asistente.reference = "QNA-09"
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, en_otro, "por la etiqueta de una línea")

        asistente.reference = False
        asistente.include_draft = True
        asistente.action_buscar()
        self.assertEqual(asistente.line_ids.move_id, en_nomina | en_otro | borrador)
        self.assertFalse(asistente.line_ids.move_id & nunca)

        asistente.date_from = date(2026, 6, 1)
        with self.assertRaises(UserError):
            asistente.action_buscar()

    # -- Relacionar -------------------------------------------------------

    def test_relacionar_liga_todas_y_copia_cada_xml_una_vez(self):
        docs = self._nomina(total='1000.00') | self._nomina(total='2000.00') | self._nomina(total='3000.00')
        poliza = self._poliza('2026-05-15', 6000.0)
        mensajes = poliza.message_ids
        asistente = self._abrir(docs)
        linea = asistente.line_ids.filtered(lambda l: l.move_id == poliza)
        self.assertTrue(linea.amount_matches)
        accion = linea.action_elegir()
        self.assertEqual((accion['res_id'], accion['target']), (asistente.id, 'new'))
        self.assertEqual(asistente.move_id, poliza)

        resultado = asistente.action_relacionar()
        self.assertEqual(resultado['params']['type'], 'success')
        self.assertEqual(resultado['params']['next'], {'type': 'ir.actions.act_window_close'})
        for doc in docs:
            self.assertEqual((doc.move_id, doc.match_state, doc.match_method), (poliza, 'manual', 'manual'))
        self.assertEqual(poliza.cfdi_document_count, 3)
        poliza.invalidate_recordset(['message_ids'])

        copias = self._copias(poliza)
        self.assertEqual(len(copias), 3)
        self.assertEqual(docs.dsi_poliza_attachment_id, copias)
        for doc in docs:
            copia = doc.dsi_poliza_attachment_id
            self.assertEqual(_bytes_de_adjunto(copia), _bytes_de_adjunto(doc.xml_attachment_id))
            self.assertEqual(copia.name, f"Nomina-{EMPLEADO}-{doc.uuid}.xml")
            # Como cualquier XML que adjunta quien no administra la base: Odoo
            # lo guarda como texto (ir.attachment._check_contents mira al
            # usuario real, no a sudo). Como administrador queda XML.
            self.assertEqual(copia.mimetype, 'text/plain')
            self.assertEqual(copia.company_id, self.company)
        self.assertFalse(poliza.message_main_attachment_id, "un XML nunca es el adjunto principal")

        nuevos = poliza.message_ids - mensajes
        self.assertEqual(len(nuevos), 1, "una sola nota por ejecución")
        self.assertEqual(nuevos.subtype_id, self.env.ref('mail.mt_note'))
        self.assertFalse(nuevos.attachment_ids)
        self.assertIn("Se relacionaron 3 CFDI de nómina", str(nuevos.body))

    def test_relacionar_dos_veces_no_duplica_y_reusa_el_xml_que_ya_estaba(self):
        docs = self._nomina() | self._nomina() | self._nomina()
        poliza = self._poliza('2026-05-15', 100.0, state='draft')
        # El sistema de nómina ya había subido el XML del primero.
        subido = self.env['ir.attachment'].create({
            'name': 'recibo.xml', 'raw': _bytes_de_adjunto(docs[0].xml_attachment_id),
            'mimetype': 'application/xml', 'res_model': 'account.move', 'res_id': poliza.id,
        })
        self._relacionar(docs, poliza)
        self.assertEqual(len(self._copias(poliza)), 3)
        self.assertEqual(docs[0].dsi_poliza_attachment_id, subido)

        # Otra vez: la póliza viene elegida y no se duplica nada ni hay otra nota.
        mensajes = poliza.message_ids
        asistente = self._abrir(docs)
        self.assertEqual(asistente.move_id, poliza)
        resultado = asistente.action_relacionar()
        self.assertEqual(resultado['params']['type'], 'success')
        self.assertIn("3 ya estaban en ella", resultado['params']['message'])
        self.assertEqual(len(self._copias(poliza)), 3)
        poliza.invalidate_recordset(['message_ids'])
        self.assertEqual(poliza.message_ids, mensajes)

        # Si alguien borra una copia de la póliza en borrador, la siguiente
        # corrida la repone.
        docs[1].dsi_poliza_attachment_id.unlink()
        self.assertFalse(docs[1].dsi_poliza_attachment_id)
        self._relacionar(docs, poliza)
        self.assertEqual(len(self._copias(poliza)), 3)
        self.assertEqual(docs.dsi_poliza_attachment_id, self._copias(poliza))

    def test_no_reemplaza_otra_poliza_sin_marcarlo(self):
        doc = self._nomina()
        poliza_a = self._poliza('2026-05-15', 9251.60, ref="A")
        poliza_b = self._poliza('2026-05-15', 9251.60, ref="B")
        self._relacionar(doc, poliza_a)
        copia_a = doc.dsi_poliza_attachment_id

        # Con una sin póliza en la selección, no viene elegida ninguna.
        self.assertFalse(self._abrir(doc | self._nomina()).move_id)
        asistente = self._abrir(doc)
        self.assertEqual(asistente.move_id, poliza_a)
        self.assertEqual(asistente.other_move_count, 0)
        asistente.move_id = poliza_b
        self.assertEqual(asistente.other_move_count, 1)
        resultado = asistente.action_relacionar()
        self.assertEqual(resultado['params']['type'], 'warning')
        self.assertIn("Reemplazar", resultado['params']['message'])
        self.assertEqual(doc.move_id, poliza_a)
        self.assertFalse(self._copias(poliza_b))

        mensajes_a = poliza_a.message_ids
        asistente.replace_existing = True
        resultado = asistente.action_relacionar()
        self.assertEqual(resultado['params']['type'], 'success')
        self.assertEqual(doc.move_id, poliza_b)
        self.assertEqual(doc.dsi_poliza_attachment_id, self._copias(poliza_b))
        self.assertTrue(copia_a.exists(), "la copia de la póliza anterior se queda")
        self.assertEqual(self._copias(poliza_a), copia_a)
        poliza_a.invalidate_recordset(['message_ids'])
        nota = poliza_a.message_ids - mensajes_a
        self.assertEqual(len(nota), 1)
        self.assertIn("se relacionaron ahora con", str(nota.body))
        self.assertFalse(nota.attachment_ids)

    def test_la_poliza_se_valida_en_el_servidor(self):
        doc = self._nomina()
        asistente = self._abrir(doc)
        factura = self._factura_del_empleado()
        cancelada = self._poliza('2026-05-15', 9251.60, state='cancel')
        de_otra_empresa = self._poliza('2026-05-15', 9251.60, company=self.otra, state='draft')
        for move, texto in ((self.env['account.move'], "Elige la póliza"), (factura, "no es una póliza"),
                            (cancelada, "está cancelada")):
            asistente.write({'move_id': move.id})
            with self.assertRaises(UserError) as error:
                asistente.action_relacionar()
            self.assertIn(texto, str(error.exception))
        # La de otra empresa: el Usuario de la Bóveda ni siquiera la puede
        # tocar; quien tiene las dos empresas, la ve rechazada.
        asistente.write({'move_id': de_otra_empresa.id})
        with self.assertRaises(AccessError):
            asistente.action_relacionar()
        with self.assertRaises(UserError) as error:
            asistente.sudo().action_relacionar()
        self.assertIn("es de otra empresa", str(error.exception))
        self.assertFalse(doc.move_id)
        self.assertFalse(doc.dsi_poliza_attachment_id)

    def test_el_mensaje_en_la_poliza_no_importa_el_xml(self):
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        with patch.object(type(self.env['account.move']), '_extend_with_attachments',
                          side_effect=AssertionError("Odoo importó el XML")):
            _asistente, resultado = self._relacionar(doc, poliza)
        self.assertEqual(resultado['params']['type'], 'success')
        self.assertEqual(poliza.move_type, 'entry')
        self.assertEqual(self.env['account.move'].search_count([('partner_id', '=', self.empleado.id)]), 0)

    def test_las_copias_llevan_no_document(self):
        """Con Documents instalado, cada XML sería un documento en la carpeta
        de contabilidad. Sin Documents no cambia nada."""
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        Adjunto = type(self.env['ir.attachment'])
        original = Adjunto.create
        creados = []

        def create(this, vals_list):
            lista = vals_list if isinstance(vals_list, list) else [vals_list]
            creados.extend((this.env.context.get('no_document'), vals) for vals in lista)
            return original(this, vals_list)

        with patch.object(Adjunto, 'create', create):
            self._relacionar(doc, poliza)
        en_la_poliza = [no_document for no_document, vals in creados if vals.get('res_model') == 'account.move']
        self.assertEqual(en_la_poliza, [True])

    # -- Formulario, restricción y Desvincular -----------------------------

    def test_ligar_a_mano_en_el_formulario_tambien_copia_el_xml(self):
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        doc.with_user(self.usuario).write({'move_id': poliza.id, 'match_state': 'manual', 'match_method': 'manual'})
        copias = self._copias(poliza)
        self.assertEqual(len(copias), 1)
        self.assertEqual(doc.dsi_poliza_attachment_id, copias)
        self.assertEqual(copias.create_uid, self.usuario)
        # Ligada por el sistema (la conciliación por UUID, la actualización):
        # la copia queda como XML.
        otra = self._nomina()
        otra.write({'move_id': poliza.id, 'match_state': 'manual', 'match_method': 'manual'})
        self.assertEqual(otra.dsi_poliza_attachment_id.mimetype, 'application/xml')
        self.assertEqual(len(self._copias(poliza)), 2)

    def test_una_nomina_solo_se_liga_a_una_poliza_de_su_empresa(self):
        doc = self._nomina()
        factura = self._factura_del_empleado()
        de_otra_empresa = self._poliza('2026-05-15', 9251.60, company=self.otra, state='draft')
        with self.assertRaises(ValidationError) as error:
            doc.write({'move_id': factura.id})
        self.assertIn("no con una factura", str(error.exception))
        with self.assertRaises(ValidationError) as error:
            doc.write({'move_id': de_otra_empresa.id})
        self.assertIn("es de otra empresa", str(error.exception))
        self.assertFalse(doc.move_id)
        self.assertFalse(self._copias(de_otra_empresa))

    def test_una_liga_previa_a_otra_empresa_no_copia_el_xml_ni_estorba(self):
        """Antes de 0.3.0 move_id no revisaba la empresa. Una N ligada así a
        un asiento de otra empresa no deja su XML en él (se iría a la otra
        empresa), la actualización no la desliga, y quien no ve esa empresa
        la relaciona con su póliza como si estuviera libre, con o sin
        «Reemplazar»."""
        poliza = self._poliza('2026-05-15', 9251.60)
        for reemplazar in (False, True):
            doc = self._nomina()
            ajena = self._poliza('2026-05-15', 9251.60, company=self.otra, state='draft')
            self.env.cr.execute(SQL(
                "UPDATE cfdi_document SET move_id = %s, match_state = 'manual', match_method = 'manual' "
                "WHERE id = %s", ajena.id, doc.id))
            doc.invalidate_recordset()
            self.Doc._copiar_nominas_a_sus_polizas()
            doc._copiar_xml_de_nomina_a_su_poliza()
            self.assertFalse(self._copias(ajena))
            self.assertFalse(doc.dsi_poliza_attachment_id)
            self.assertEqual(doc.move_id, ajena, "la actualización no desliga nada")

            # Cada paso como una petición nueva: sin soltar la caché, el
            # usuario leería la póliza ajena que la prueba ya leyó como
            # administrador, sin que Odoo revise el acceso.
            self.env.invalidate_all()
            asistente = self._abrir(doc)
            self.assertFalse(asistente.move_id)
            asistente.write({'move_id': poliza.id, 'replace_existing': reemplazar})
            self.env.invalidate_all()
            self.assertEqual(asistente.other_move_count, 0)
            resultado = asistente.action_relacionar()
            self.assertEqual(resultado['params']['type'], 'success')
            self.assertEqual(doc.move_id, poliza)
            self.assertEqual(doc.dsi_poliza_attachment_id.res_id, poliza.id)
            self.assertFalse(self._copias(ajena))
            self.assertFalse(ajena.sudo().message_ids.filtered(lambda m: "se relacionaron ahora" in str(m.body)))

    def test_no_se_liga_a_una_poliza_cancelada_pero_la_liga_que_ya_estaba_sigue(self):
        """Ni a mano, ni por RPC o importación CSV, ni por el folio fiscal.
        Solo cuando la liga se pone o cambia: si la póliza se cancela
        después, el CFDI se sigue pudiendo guardar."""
        doc = self._nomina()
        cancelada = self._poliza('2026-05-15', 9251.60, state='cancel')
        with self.assertRaises(ValidationError) as error:
            doc.with_user(self.usuario).write({'move_id': cancelada.id})
        self.assertIn("está cancelada", str(error.exception))
        with self.assertRaises(ValidationError):
            self._guardar(nomina_xml(self._uuid()), move_id=cancelada.id)
        self.assertFalse(doc.move_id)
        self.assertFalse(self._copias(cancelada))

        # Con el folio fiscal de un recibo: la cancelada no se liga ni tumba
        # la conciliación; una vigente sí.
        con_folio = self._poliza('2026-05-15', 9251.60, state='cancel')
        self.env.cr.execute(SQL(
            "UPDATE account_move SET l10n_mx_edi_cfdi_uuid = %s WHERE id = %s", doc.uuid, con_folio.id))
        con_folio.invalidate_recordset(['l10n_mx_edi_cfdi_uuid'])
        doc.action_match_automatically()
        self.assertFalse(doc.move_id)
        vigente = self._poliza('2026-05-15', 9251.60, ref="Nómina de Odoo")
        self.env.cr.execute(SQL(
            "UPDATE account_move SET l10n_mx_edi_cfdi_uuid = %s WHERE id = %s", doc.uuid, vigente.id))
        vigente.invalidate_recordset(['l10n_mx_edi_cfdi_uuid'])
        doc.action_match_automatically()
        self.assertEqual((doc.move_id, doc.match_method), (vigente, 'uuid_exact'))
        self.assertEqual(doc.dsi_poliza_attachment_id, self._copias(vigente))

        # La póliza se cancela después: la liga se queda y el CFDI se sigue
        # guardando, también con los campos que revisa la restricción.
        en_borrador = self._poliza('2026-05-15', 9251.60, state='draft')
        otra = self._nomina()
        self._relacionar(otra, en_borrador)
        en_borrador.button_cancel()
        otra.with_user(self.usuario).write({'match_state': 'matched'})
        otra.write({'move_id': en_borrador.id, 'move_type': 'N', 'company_id': self.company.id})
        self.assertEqual(otra.move_id, en_borrador)
        # Y se mueve a una vigente con el asistente.
        self._relacionar(otra, vigente)
        self.assertEqual(otra.move_id, vigente)

    def test_desvincular_deja_la_copia_en_la_poliza(self):
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        self._relacionar(doc, poliza)
        copia = doc.dsi_poliza_attachment_id
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_payroll_no_move')
        sin_poliza = self.Doc.with_user(self.usuario)
        self.assertNotIn(doc, sin_poliza.search(safe_eval(accion.domain)))

        doc.with_user(self.usuario).action_unlink_move()
        self.assertFalse(doc.move_id)
        self.assertFalse(doc.dsi_poliza_attachment_id)
        self.assertTrue(copia.exists())
        self.assertEqual((copia.res_model, copia.res_id), ('account.move', poliza.id))
        self.assertIn(doc, sin_poliza.search(safe_eval(accion.domain)))

        # La que está en una póliza cancelada sigue pendiente; una cancelada
        # en el SAT no necesita póliza.
        en_borrador = self._poliza('2026-05-15', 10.0, state='draft')
        en_poliza_cancelada = self._nomina()
        self._relacionar(en_poliza_cancelada, en_borrador)
        en_borrador.button_cancel()
        cancelada = self._nomina(sat_status='canceled')
        en_la_lista = sin_poliza.search(safe_eval(accion.domain))
        self.assertIn(en_poliza_cancelada, en_la_lista)
        self.assertNotIn(cancelada, en_la_lista)

    def test_con_bitacora_de_auditoria_desvincular_no_intenta_borrar(self):
        if 'restrictive_audit_trail' not in self.company._fields:
            self.skipTest("Esta versión de Odoo no tiene bitácora de auditoría de adjuntos")
        self.company.restrictive_audit_trail = True
        doc = self._nomina()
        poliza_a = self._poliza('2026-05-15', 9251.60, ref="A")
        poliza_b = self._poliza('2026-05-15', 9251.60, ref="B")
        self._relacionar(doc, poliza_a)
        copia_a = doc.dsi_poliza_attachment_id
        doc.with_user(self.usuario).action_unlink_move()
        self.assertTrue(copia_a.exists())
        # Volver a la misma reutiliza la copia; reemplazar la deja ahí.
        self._relacionar(doc, poliza_a)
        self.assertEqual(doc.dsi_poliza_attachment_id, copia_a)
        self._relacionar(doc, poliza_b, replace_existing=True)
        self.assertEqual(doc.move_id, poliza_b)
        self.assertTrue(copia_a.exists())
        self.assertEqual(len(self._copias(poliza_b)), 1)

    def test_una_nomina_no_se_liga_sola_a_una_factura_por_importe(self):
        """El receptor de una N emitida es el empleado: por RFC, total y fecha
        se ligaba a un reembolso suyo."""
        factura = self._factura_del_empleado()
        doc = self._nomina()
        doc.action_match_automatically()
        self.assertFalse(doc.move_id)
        self.assertEqual(doc.match_state, 'pending')
        self.assertEqual(doc._find_match_candidates(), [])

        # Las que se ligaron así antes de 0.3.0 se desligan al actualizar, con
        # una nota; la ligada a mano se deja. Por SQL: la restricción ya no
        # deja guardarlas.
        por_importe, a_mano = self._nomina(), self._nomina()
        for nomina, metodo in ((por_importe, 'rfc_amount_date'), (a_mano, 'manual')):
            self.env.cr.execute(SQL(
                "UPDATE cfdi_document SET move_id = %s, match_state = 'matched', match_method = %s WHERE id = %s",
                factura.id, metodo, nomina.id))
        (por_importe | a_mano).invalidate_recordset()
        self.Doc._desligar_nominas_de_facturas()
        self.assertFalse(por_importe.move_id)
        self.assertEqual((por_importe.match_state, por_importe.match_method), ('pending', False))
        por_importe.invalidate_recordset(['message_ids'])
        self.assertTrue(any("Relacionar con póliza" in str(m.body) and factura.display_name in str(m.body)
                            for m in por_importe.message_ids))
        self.assertEqual(a_mano.move_id, factura)

    def test_nomina_cancelada_ligada_sale_como_discrepancia(self):
        """Hay que desligarla y relacionar la que la sustituye."""
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        self._relacionar(doc, poliza)
        self.assertFalse(doc.is_discrepancy)
        self._sat_dice(doc, 'cancelled')
        self.assertTrue(doc.is_discrepancy)
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_discrepancy')
        self.assertIn(doc, self.Doc.search(safe_eval(accion.domain)))
        # Y ya no se puede volver a relacionar.
        with self.assertRaises(UserError):
            doc.with_user(self.usuario).action_relacionar_con_poliza()

    # -- Permisos, vistas y licencia ---------------------------------------

    def test_el_usuario_de_la_boveda_relaciona_y_quien_solo_factura_no(self):
        docs = self._nomina() | self._nomina()
        poliza = self._poliza('2026-05-15', 100.0)
        _asistente, resultado = self._relacionar(docs, poliza, user=self.usuario)
        self.assertEqual(resultado['params']['type'], 'success')
        self.assertEqual(docs.move_id, poliza)
        self.assertEqual(self._copias(poliza).create_uid, self.usuario)

        facturacion = new_test_user(
            self.env, login='solo-facturacion-nomina', groups='base.group_user,account.group_account_invoice',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        with self.assertRaises(AccessError):
            self.Doc.with_user(facturacion).search([('id', 'in', docs.ids)])
        with self.assertRaises(AccessError):
            docs.with_user(facturacion).action_relacionar_con_poliza()
        with self.assertRaises(AccessError):
            self.Asistente.with_user(facturacion).create({
                'company_id': self.company.id, 'date_from': date(2026, 5, 1), 'date_to': date(2026, 5, 31)})

    def test_el_formulario_dice_poliza_en_las_nominas(self):
        arch = etree.fromstring(self.Doc.with_user(self.usuario).get_views([(False, 'form')])['views']['form']['arch'])
        poliza = arch.xpath("//field[@name='move_id'][@string='Póliza']")
        self.assertEqual(len(poliza), 1)
        self.assertEqual(poliza[0].get('invisible'), "move_type != 'N'")
        self.assertIn("('move_type', '=', 'entry')", poliza[0].get('domain'))
        sugerencias = arch.xpath("//button[@name='action_open_match_suggestions']")[0]
        self.assertIn("move_type == 'N'", sugerencias.get('invisible'))
        self.assertIn("move_type == 'P'", sugerencias.get('invisible'))
        self.assertTrue(arch.xpath("//button[@name='action_relacionar_con_poliza']"))
        self.assertEqual(len(arch.xpath("//button[@name='action_unlink_move']")), 2)

        # La lista de nómina y el asistente abren como el Usuario de la Bóveda.
        accion = self.env.ref('dsi_cfdi_vault.action_cfdi_document_payroll_no_move')
        self.assertEqual(accion.view_id, self.env.ref('dsi_cfdi_vault.view_cfdi_document_list_nomina'))
        vistas = self.Doc.with_user(self.usuario).get_views(
            [(accion.view_id.id, 'list'), (accion.search_view_id.id, 'search')])
        campos = vistas['models']['cfdi.document']
        campos = campos.get('fields', campos)
        self.assertIn('dsi_nomina_fecha_pago', campos)
        lista = etree.fromstring(vistas['views']['list']['arch'])
        self.assertEqual(lista.xpath("//field[@name='move_id']")[0].get('string'), "Póliza")
        self.Asistente.with_user(self.usuario).get_views([(False, 'form')])

    def test_sin_licencia_no_se_relaciona_nomina_con_poliza(self):
        doc = self._nomina()
        poliza = self._poliza('2026-05-15', 9251.60)
        asistente = self._abrir(doc)
        asistente.move_id = poliza
        self.env['dsi.license.client.state']._get_or_create(self.company).token = False
        with self.assertRaises(RedirectWarning) as error:
            doc.with_user(self.usuario).action_relacionar_con_poliza()
        self.assertIn("relacionar CFDI de nómina con pólizas", str(error.exception))
        # Tampoco con el asistente que ya estaba abierto.
        with self.assertRaises(RedirectWarning):
            asistente.action_relacionar()
        self.assertFalse(doc.move_id)
        # Ligar a mano sigue libre, como antes, y también copia el XML.
        doc.with_user(self.usuario).write({'move_id': poliza.id})
        self.assertTrue(doc.dsi_poliza_attachment_id)
