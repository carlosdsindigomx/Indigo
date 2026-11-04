"""Los metadatos de la descarga masiva del SAT: se leen, se guardan y dan el
estatus a la Bóveda y a las facturas.

Existe porque los metadatos se pedían (estatus «todos», lo único que trae
vigentes y cancelados juntos) y se tiraban: el paquete trae un TXT y solo se
abrían los XML. En la base de desarrollo había 13 solicitudes de metadatos
«procesadas» con 0 importados. Además cada periodo se pedía una sola vez, así
que una cancelación posterior nunca llegaba.

Lo que más cuidan estas pruebas:

  * leer el TXT como lo entrega el SAT (BOM, CRLF con un LF suelto dentro de
    un nombre, Latin-1, encabezado con otras mayúsculas o espacios, columnas
    nuevas, el archivo aparte de terceros) y nunca adivinar una línea rota:
    se cuenta y se registra;
  * la cancelación es definitiva: ni un archivo posterior ni la consulta
    pública la regresan a vigente;
  * el estatus llega al CFDI de la Bóveda (con su fecha y su origen), al
    documento «Pago recibido» de un REP y al Estado bóveda de la factura,
    también de la que no tiene su XML en la Bóveda, aunque el XML llegue
    después;
  * en una compra nunca se ejecuta «Actualizar SAT»: queda «Por revisar»;
  * el refresco semanal no mueve «Cubierto hasta», no repite parámetros y
    solo corre con licencia;
  * volver a leer los TXT adjuntos no cambia nada, y un TXT que se vuelve a
    descargar no se adjunta dos veces;
  * un choque al pasar los metadatos a un CFDI recién guardado no pierde el
    CFDI (ni la descarga entera);
  * llenar lo que nadie había verificado no deja mensajes, ni en el CFDI ni
    en la factura, y no va y viene contra la consulta pública.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_estado_boveda.py de la Bóveda: la conversión pasa por el import de Odoo.
"""
import io
import zipfile
from datetime import date, datetime, timedelta
from unittest.mock import patch

import psycopg2
from freezegun import freeze_time

from odoo import Command, fields
from odoo.exceptions import AccessError, RedirectWarning
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.dsi_cfdi_sat_sync.models.sat_metadata import leer_metadatos_txt, leer_paquete_de_metadatos
from odoo.addons.dsi_cfdi_vault.models.cfdi_conceptos import _bytes_de_adjunto
from odoo.addons.dsi_cfdi_vault.tests.test_conversion_factura import RFC_EMISOR, RFC_RECEPTOR
from odoo.addons.dsi_cfdi_vault.tests.test_factura_como_odoo import cfdi_xml
from odoo.addons.dsi_cfdi_vault.tests.test_inconsistencias import mensajes_que_registran
from odoo.addons.dsi_cfdi_vault.tests.test_rep_facturas import docto, rep_xml
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_descarga_concilia import descargar, paquete_zip

GRUPOS_BOVEDA = 'base.group_user,dsi_cfdi_vault.group_cfdi_vault_user'
RFC_CLIENTE = 'CLI010101AA1'
ENCABEZADO = ('Uuid~RfcEmisor~NombreEmisor~RfcReceptor~NombreReceptor~RfcPac~FechaEmision~'
              'FechaCertificacionSat~Monto~EfectoComprobante~Estatus~FechaCancelacion')
LOG_METADATOS = 'odoo.addons.dsi_cfdi_sat_sync.models.sat_metadata'
LOG_SOLICITUD = 'odoo.addons.dsi_cfdi_sat_sync.models.sat_download_request'


def linea(uuid, estatus='1', cancelado='', efecto='I', emisor=RFC_EMISOR, nombre="Proveedor de Prueba",
          receptor=RFC_RECEPTOR, monto='1160.00'):
    """Una línea del TXT, como la arma el SAT: con FechaCancelacion vacía la
    línea termina en «~»."""
    return '~'.join([uuid, emisor, nombre, receptor, "Empresa MX de prueba", 'SAT970701NN3',
                     '2026-06-15 12:00:00', '2026-06-15 12:01:00', monto, efecto, estatus, cancelado])


def txt(*lineas, encabezado=ENCABEZADO, fin='\r\n', codificacion='utf-8'):
    return (fin.join([encabezado, *lineas]) + fin).encode(codificacion)


def paquete(**archivos):
    """ZIP del SAT con estos TXT: {nombre sin extensión: bytes}."""
    contenido = io.BytesIO()
    with zipfile.ZipFile(contenido, 'w') as zf:
        for nombre, datos in archivos.items():
            zf.writestr(f"{nombre}.txt", datos)
    return contenido.getvalue()


def utc(*valores):
    """Una fecha del centro de México (UTC-6 todo el año desde 2022) en UTC."""
    return datetime(*valores) + timedelta(hours=6)


@tagged('post_install', '-at_install')
class TestMetadatos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.credential = cls.env['sat.credential'].create({
            'company_id': cls.company.id, 'rfc': RFC_RECEPTOR, 'state': 'validated',
        })
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
            cls.env, login='boveda-metadatos', groups=GRUPOS_BOVEDA,
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.Metadato = cls.env['dsi.cfdi.sat.metadata']
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _uuid(self):
        type(self).contador += 1
        return f"DDDDDDDD-6666-4000-8000-{self.contador:012d}"

    def _solicitud(self, direction='received'):
        return self.env['sat.download.request'].create({
            'company_id': self.company.id,
            'credential_id': self.credential.id,
            'direction': direction,
            'request_type': 'metadata',
            'estado_comprobante': 'todos',
            'date_from': date(2026, 6, 1),
            'date_to': date(2026, 6, 30),
            'state': 'downloading',
            'package_ids_json': '["PAQUETE_DE_PRUEBA_01"]',
        })

    def _procesar(self, *lineas, direction='received'):
        """Un paquete de metadatos por el camino de la descarga, sin hablar
        con el SAT: se adjunta (_procesar_paquete_zip) y se lee."""
        solicitud = self._solicitud(direction)
        solicitud._procesar_paquete_zip(paquete(**{'AAAAAAAA-0000-0000-0000-000000000000_01': txt(*lineas)}))
        solicitud._procesar_metadatos()
        return solicitud

    def _metadato(self, uuid):
        return self.Metadato.search([('company_id', '=', self.company.id), ('uuid', '=', uuid)])

    def _guardar(self, xml, direction='received', sat_status='not_verified'):
        """El CFDI en la Bóveda, como lo deja la importación."""
        Doc = self.env['cfdi.document']
        vals = Doc._parse_cfdi_xml(xml)
        adjunto = self.env['ir.attachment'].create({
            'name': f"{vals['uuid']}.xml", 'raw': xml, 'mimetype': 'text/xml',
        })
        datos = {k: v for k, v in vals.items() if k != 'parse_state'}
        datos.update({
            'company_id': self.company.id, 'direction': direction, 'xml_attachment_id': adjunto.id,
            'currency_id': vals['currency_id'] or self.company.currency_id.id, 'sat_status': sat_status,
        })
        doc = Doc.create(datos)
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _cfdi(self, uuid=None, sat_status='not_verified'):
        return self._guardar(cfdi_xml(uuid or self._uuid()).encode(), sat_status=sat_status)

    def _compra(self):
        """Compra convertida desde la Bóveda, publicada."""
        doc = self._cfdi(sat_status='valid')
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        factura.action_post()
        return doc, factura

    def _venta_timbrada(self, uuid):
        """Venta timbrada en Odoo: lleva el documento CFDI de Odoo con su folio.
        Su XML no está en la Bóveda."""
        venta = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.cliente.id,
            'invoice_date': date(2026, 6, 15),
            'invoice_line_ids': [Command.create({
                'name': "Servicio", 'quantity': 1, 'price_unit': 1000.0, 'tax_ids': [Command.clear()],
            })],
        })
        venta.action_post()
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': cfdi_xml(uuid).encode(), 'mimetype': 'application/xml',
            'res_model': 'account.move', 'res_id': venta.id,
        })
        Documento = self.env['l10n_mx_edi.document']
        valores = {
            'move_id': venta.id, 'invoice_ids': [Command.set(venta.ids)], 'state': 'invoice_sent',
            'sat_state': 'valid', 'attachment_id': adjunto.id, 'datetime': fields.Datetime.now(),
        }
        if 'company_id' in Documento._fields:
            valores['company_id'] = self.company.id
        Documento.create(valores)
        self.assertEqual(venta.l10n_mx_edi_cfdi_uuid, uuid)
        return venta

    def _compra_importada_por_odoo(self, uuid):
        """Compra que entra por el import de Odoo, no por la Bóveda: lleva el
        documento CFDI «recibido» con su folio."""
        compra = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.proveedor.id,
            'invoice_date': date(2026, 6, 15),
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
            'sat_state': 'not_defined', 'attachment_id': adjunto.id, 'datetime': fields.Datetime.now(),
        }
        if 'company_id' in Documento._fields:
            valores['company_id'] = self.company.id
        Documento.create(valores)
        self.assertEqual(compra.l10n_mx_edi_cfdi_uuid, uuid)
        return compra

    def _registros(self, facturas, kind='sat_distinto'):
        return self.Inconsistencia.search([('kind', '=', kind), ('move_id', 'in', facturas.ids)])

    def _por_revisar(self, factura):
        if 'review_state' in factura._fields:
            return factura.review_state in ('todo', 'anomaly')
        return not factura.checked

    def _sat_dice(self, docs, valor):
        def consulta(_modelo, _rfc_emisor, _rfc_receptor, _total, _uuid):
            return {'value': valor}
        with patch.object(self.env.registry['l10n_mx_edi.document'], '_fetch_sat_status', consulta):
            docs._fetch_and_store_sat_status()

    def _confirmar_historial(self):
        """Lo que hace el commit: escribir los mensajes de seguimiento."""
        self.env.flush_all()
        self.env.cr.precommit.run()

    def _try_sat(self):
        return patch.object(self.env.registry['account.move'], 'l10n_mx_edi_cfdi_try_sat', autospec=True)

    # -- Lectura del TXT ----------------------------------------------------

    def test_lee_el_txt_tal_como_lo_entrega_el_sat(self):
        uuid = self._uuid()
        cancelado = self._uuid()
        lineas = [linea(uuid.lower()), linea(cancelado, estatus='0', cancelado='2026-10-05 09:30:00')]
        casos = {
            'BOM, CRLF y línea final vacía': b'\xef\xbb\xbf' + txt(*lineas),
            'solo LF': txt(*lineas, fin='\n'),
            'Latin-1': txt(*lineas, codificacion='latin-1'),
            'encabezado con otras mayúsculas y espacios': txt(*lineas, encabezado=(
                ' UUID ~RFCEmisor~Nombre Emisor~rfcreceptor~NombreReceptor~RfcPac~FechaEmision~'
                'Fecha_Certificacion_Sat~MONTO~EfectoComprobante~ estatus ~FechaCancelación')),
        }
        for caso, contenido in casos.items():
            with self.subTest(caso=caso):
                lectura = leer_metadatos_txt(contenido)
                self.assertEqual(lectura['tipo'], 'metadatos')
                self.assertEqual(lectura['ilegibles'], [])
                vigente, baja = lectura['filas']
                self.assertEqual(vigente['uuid'], uuid, "el folio va en mayúsculas")
                self.assertEqual((vigente['estatus'], vigente['fecha_cancelacion']), ('1', False))
                self.assertEqual(vigente['fecha_emision'], utc(2026, 6, 15, 12, 0, 0), "en UTC")
                self.assertEqual(vigente['fecha_certificacion'], utc(2026, 6, 15, 12, 1, 0))
                self.assertEqual((vigente['monto'], vigente['efecto'], vigente['rfc_pac']),
                                 (1160.0, 'I', 'SAT970701NN3'))
                self.assertEqual((baja['estatus'], baja['fecha_cancelacion']), ('0', utc(2026, 10, 5, 9, 30, 0)))

        # Latin-1 con acentos: no se pierden.
        lectura = leer_metadatos_txt(txt(linea(uuid, nombre="Compañía Óptima"), codificacion='latin-1'))
        self.assertEqual(lectura['filas'][0]['nombre_emisor'], "Compañía Óptima")
        # Con CRLF, un LF suelto es un salto dentro del nombre, no otra línea.
        lectura = leer_metadatos_txt(txt(linea(uuid, nombre="Compañía\nÓptima"), linea(cancelado)))
        self.assertEqual(lectura['ilegibles'], [])
        self.assertEqual([f['nombre_emisor'] for f in lectura['filas']], ["Compañía Óptima", "Proveedor de Prueba"])

    def test_columnas_nuevas_y_terceros(self):
        uuid, otro = self._uuid(), self._uuid()
        lectura = leer_metadatos_txt(txt(
            linea(uuid) + '~AAAA010101AA1~PERSONA UNO~dato nuevo', linea(otro) + '~~~',
            encabezado=ENCABEZADO + '~RfcACuentaTerceros~NombreACuentaTerceros~ColumnaNueva'))
        primera, segunda = lectura['filas']
        self.assertEqual((primera['rfc_a_cuenta_terceros'], primera['nombre_a_cuenta_terceros']),
                         ('AAAA010101AA1', "PERSONA UNO"))
        self.assertEqual(primera['extra'], {'ColumnaNueva': "dato nuevo"})
        self.assertEqual((segunda['rfc_a_cuenta_terceros'], segunda['extra']), (False, {}))

        # Un monto o una fecha mal escritos no se adivinan: quedan tal cual.
        malos, pago = leer_metadatos_txt(txt(
            linea(uuid, estatus='0', cancelado='ayer', monto='1,160.00'), linea(otro, efecto='P', monto='0')))['filas']
        self.assertEqual((malos['monto'], malos['fecha_cancelacion'], malos['estatus']), (0.0, False, '0'))
        self.assertEqual(malos['extra'], {'Monto': '1,160.00', 'FechaCancelacion': 'ayer'})
        self.assertEqual((pago['monto'], pago['efecto'], pago['extra']), (0.0, 'P', {}))

        # Desde 2022 los terceros llegan en su propio archivo, ligados por folio
        # sin distinguir mayúsculas.
        filas, ilegibles = leer_paquete_de_metadatos([
            ('X_01.txt', txt(linea(uuid), linea(otro))),
            ('X_tercero.txt', txt(f"{uuid.lower()}~AAAA010101AA2~PERSONA DOS",
                                  encabezado='Uuid~RfcACuentaTerceros~NombreACuentaTerceros')),
        ])
        self.assertEqual(ilegibles, [])
        self.assertEqual([(f['uuid'], f.get('rfc_a_cuenta_terceros') or False) for f in filas],
                         [(uuid, 'AAAA010101AA2'), (otro, False)])

    def test_una_linea_rota_se_cuenta_y_no_se_adivina(self):
        buenas = [self._uuid(), self._uuid()]
        lectura = leer_metadatos_txt(txt(
            linea(buenas[0]),
            linea(self._uuid(), nombre="EMPRESA ~ RARA"),       # un «~» dentro del nombre
            linea(self._uuid())[:-30],                           # cortada
            linea('', estatus='1'),                               # sin folio
            linea(self._uuid(), estatus='X'),                     # estatus desconocido
            '',                                                   # en blanco: no cuenta
            linea(buenas[1], estatus='0', cancelado='2026-10-05 09:30:00'),
        ))
        self.assertEqual([f['uuid'] for f in lectura['filas']], buenas)
        self.assertEqual([(i['linea'], i['motivo']) for i in lectura['ilegibles']],
                         [(3, 'campos'), (4, 'campos'), (5, 'sin_folio'), (6, 'estatus')])
        self.assertEqual((lectura['ilegibles'][0]['campos'], lectura['ilegibles'][0]['columnas']), (13, 12))
        self.assertIn("trae 13 campos y el encabezado 12",
                      self.Metadato._describir_ilegible(dict(lectura['ilegibles'][0], archivo='X_01.txt')))

        # Un archivo que no es de metadatos no se lee.
        lectura = leer_metadatos_txt(b"Hola~mundo\r\n1~2\r\n")
        self.assertFalse(lectura['tipo'])
        self.assertEqual((lectura['filas'], len(lectura['ilegibles'])), ([], 1))

    def test_con_fin_de_linea_lf_solo_el_lf_separa_lineas(self):
        """str.splitlines() también corta en caracteres que no son fin de
        línea: el «…» de Windows leído como Latin-1 (\\x85), \\u2028 o un salto
        de página dentro de un nombre partían la línea en dos ilegibles."""
        uuid = self._uuid()
        casos = {
            '«…» de Windows': ("Compañía… Óptima", 'cp1252', "Compañía\x85 Óptima"),
            'separador de línea Unicode': ("Compañía\u2028Óptima", 'utf-8', "Compañía\u2028Óptima"),
            'salto de página': ("Compañía\x0cÓptima", 'utf-8', "Compañía\x0cÓptima"),
        }
        for caso, (nombre, codificacion, leido) in casos.items():
            with self.subTest(caso=caso):
                lectura = leer_metadatos_txt(txt(
                    linea(uuid, nombre=nombre), linea(self._uuid()), fin='\n', codificacion=codificacion))
                self.assertEqual(lectura['ilegibles'], [])
                self.assertEqual(len(lectura['filas']), 2)
                self.assertEqual(lectura['filas'][0]['nombre_emisor'], leido)

    # -- El paquete -------------------------------------------------------------

    def test_el_paquete_de_metadatos_se_adjunta_y_se_guarda(self):
        """Por el camino completo de la descarga, con el SAT simulado: los TXT
        quedan adjuntos a la solicitud, las filas guardadas y la línea rota,
        contada y registrada."""
        vigente, cancelado = self._uuid(), self._uuid()
        solicitud = self._solicitud()
        zip_bytes = paquete(**{
            'AAAAAAAA-0000-0000-0000-000000000001_01': txt(
                linea(vigente), linea(self._uuid(), nombre="ROTA ~ SA"),
                linea(cancelado, estatus='0', cancelado='2026-10-05 09:30:00')),
            'AAAAAAAA-0000-0000-0000-000000000001_tercero': txt(
                f"{vigente}~AAAA010101AA1~PERSONA UNO", encabezado='Uuid~RfcACuentaTerceros~NombreACuentaTerceros'),
        })
        with self.assertLogs(LOG_METADATOS, level='WARNING') as registro:
            descargar(solicitud, zip_bytes)
        self.assertIn("1 líneas de metadatos", registro.output[0])

        self.assertEqual(solicitud.state, 'processed')
        self.assertEqual(solicitud.imported_count, 0, "no trae XML")
        self.assertEqual((solicitud.metadata_count, solicitud.metadata_error_count), (2, 1))
        self.assertIn("línea 3", solicitud.metadata_errors)
        self.assertFalse(solicitud.metadata_pending)
        self.assertEqual(len(solicitud.metadata_attachment_ids), 2)
        self.assertEqual(set(solicitud.metadata_attachment_ids.mapped('res_model')), {'sat.download.request'})
        self.assertEqual(set(solicitud.metadata_attachment_ids.mapped('res_id')), {solicitud.id})

        metadatos = self.Metadato.search([('request_id', '=', solicitud.id)])
        self.assertEqual(len(metadatos), 2)
        primero = self._metadato(vigente)
        self.assertEqual((primero.direction, primero.estatus, primero.rfc_a_cuenta_terceros),
                         ('received', '1', 'AAAA010101AA1'))
        self.assertEqual((self._metadato(cancelado).estatus, self._metadato(cancelado).fecha_cancelacion),
                         ('0', utc(2026, 10, 5, 9, 30, 0)))
        self.assertFalse(self.env['cfdi.document'].search([('uuid', 'in', [vigente, cancelado])]))

    def test_un_paquete_que_se_vuelve_a_descargar_no_se_adjunta_dos_veces(self):
        """Si falla la descarga de un paquete posterior, los TXT de los
        anteriores ya quedaron adjuntos y el reintento los descarga otra vez:
        quedaban dos veces y sus líneas ilegibles se contaban doble. Además
        no van como text/*: Odoo copiaría todo su contenido a su índice."""
        solicitud = self._solicitud()
        zip_bytes = paquete(**{'AAAAAAAA-0000-0000-0000-000000000002_01': txt(
            linea(self._uuid()), linea(self._uuid())[:-30])})
        solicitud._procesar_paquete_zip(zip_bytes)
        solicitud._procesar_paquete_zip(zip_bytes)
        with self.assertLogs(LOG_METADATOS, level='WARNING'):
            solicitud._procesar_metadatos()
        adjunto = solicitud.metadata_attachment_ids
        self.assertEqual(len(adjunto), 1)
        self.assertEqual((solicitud.metadata_count, solicitud.metadata_error_count), (1, 1))
        self.assertFalse(adjunto.mimetype.startswith('text/'))
        # Odoo 18 guarda en el índice el tipo sin subtipo («application»); 19 y
        # 20, nada. Lo que importa es que no copie el contenido.
        self.assertNotIn('Uuid', adjunto.sudo().index_content or '')
        self.assertEqual(adjunto.name, 'AAAAAAAA-0000-0000-0000-000000000002_01.txt')
        self.assertEqual(_bytes_de_adjunto(adjunto)[:4], b'Uuid')

    def test_muchas_filas_se_guardan_por_tandas(self):
        """Más filas que una tanda (LOTE_DE_METADATOS): la segunda lectura
        solo escribe lo que cambió, en cualquier tanda, y mueve «Visto por
        última vez» de todas."""
        uuids = [f"BBBBBBBB-5555-4000-8000-{numero:012d}" for numero in range(2500)]
        solicitud = self._solicitud()
        todos, cambiados = self.Metadato._guardar_filas(
            solicitud, leer_metadatos_txt(txt(*(linea(uuid) for uuid in uuids)))['filas'])
        self.assertEqual((len(todos), len(cambiados)), (2500, 2500))

        otra = self._solicitud()
        nuevos = [f"BBBBBBBB-5555-4000-8000-{numero:012d}" for numero in range(2500, 2600)]
        filas = leer_metadatos_txt(txt(
            *(linea(uuid) for uuid in uuids[:1500]),
            linea(uuids[1500], estatus='0', cancelado='2026-10-05 09:30:00'),
            linea(uuids[2400], nombre="Otro nombre"),
            *(linea(uuid) for uuid in uuids[1501:2400] + uuids[2401:] + nuevos)))['filas']
        todos, cambiados = self.Metadato._guardar_filas(otra, filas)
        self.assertEqual(len(todos), 2600)
        self.assertEqual(set(cambiados.mapped('uuid')), {uuids[1500], uuids[2400], *nuevos})
        self.assertEqual(self._metadato(uuids[1500]).estatus, '0')
        self.assertEqual(self._metadato(uuids[2400]).nombre_emisor, "Otro nombre")
        self.assertEqual(self.Metadato.search_count([('uuid', 'in', uuids + nuevos), ('request_id', '=', otra.id)]),
                         2600)

    def test_la_cancelacion_es_definitiva(self):
        uuid, otro = self._uuid(), self._uuid()
        self._procesar(linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00'), linea(otro))
        # Un archivo posterior lo trae vigente: se queda cancelado, con su fecha.
        self._procesar(linea(uuid), linea(otro, estatus='0', cancelado='2026-10-06 10:00:00'))
        self.assertEqual((self._metadato(uuid).estatus, self._metadato(uuid).fecha_cancelacion),
                         ('0', utc(2026, 10, 5, 9, 30, 0)))
        self.assertEqual((self._metadato(otro).estatus, self._metadato(otro).fecha_cancelacion),
                         ('0', utc(2026, 10, 6, 10, 0, 0)), "de vigente a cancelado sí")
        # En un mismo archivo, si viene dos veces, gana el cancelado.
        tercero = self._uuid()
        self._procesar(linea(tercero, estatus='0', cancelado='2026-10-07 10:00:00'), linea(tercero))
        self.assertEqual(self._metadato(tercero).estatus, '0')
        self.assertEqual(self.Metadato.search_count([('uuid', 'in', [uuid, otro, tercero])]), 3)

    # -- A la Bóveda --------------------------------------------------------------

    def test_los_metadatos_le_dan_su_estatus_al_cfdi(self):
        sin_verificar = self._cfdi()
        por_cancelar = self._cfdi()
        vigente = self._cfdi(sat_status='valid')
        cancelado_por_consulta = self._cfdi(sat_status='valid')
        self._sat_dice(cancelado_por_consulta, 'cancelled')
        self.assertEqual(cancelado_por_consulta.sat_status_source, 'consulta')

        self._procesar(
            linea(sin_verificar.uuid),
            linea(por_cancelar.uuid, estatus='0', cancelado='2026-10-05 09:30:00'),
            linea(vigente.uuid, estatus='0', cancelado='2026-10-06 09:30:00'),
            linea(cancelado_por_consulta.uuid, estatus='0', cancelado='2026-10-07 09:30:00'),
        )
        # Vigente: llena lo que no se sabía, sin contar como verificación.
        self.assertEqual((sin_verificar.sat_status, sin_verificar.sat_status_source), ('valid', 'metadatos'))
        self.assertFalse(sin_verificar.sat_last_check)
        # Cancelado: con su fecha y su origen; sí cuenta como verificación.
        for doc, fecha in ((por_cancelar, utc(2026, 10, 5, 9, 30, 0)), (vigente, utc(2026, 10, 6, 9, 30, 0))):
            self.assertEqual((doc.sat_status, doc.sat_cancel_date, doc.sat_status_source),
                             ('canceled', fecha, 'metadatos'))
            self.assertTrue(doc.sat_last_check)
        # Ya cancelado por la consulta: solo se agrega la fecha.
        self.assertEqual((cancelado_por_consulta.sat_cancel_date, cancelado_por_consulta.sat_status_source),
                         (utc(2026, 10, 7, 9, 30, 0), 'consulta'))
        self.assertEqual(self._metadato(vigente.uuid).cfdi_document_id, vigente)
        # Una cancelación deja historial; llenar un vigente, no.
        self._confirmar_historial()
        self.assertTrue(mensajes_que_registran(vigente, 'sat_status'))
        self.assertFalse(mensajes_que_registran(sin_verificar, 'sat_status'))

        # La consulta pública ya no lo regresa a vigente, y lo deja dicho.
        self._sat_dice(vigente, 'valid')
        self.assertEqual((vigente.sat_status, vigente.sat_status_source), ('canceled', 'metadatos'))
        self.assertIn("es definitiva", str(vigente.message_ids[:1].body))
        # Un archivo vigente posterior tampoco.
        self._procesar(linea(por_cancelar.uuid))
        self.assertEqual(por_cancelar.sat_status, 'canceled')

    def test_el_xml_que_llega_despues_toma_los_metadatos(self):
        uuid = self._uuid()
        self._procesar(linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        metadato = self._metadato(uuid)
        self.assertFalse(metadato.cfdi_document_id)

        doc = self._cfdi(uuid)
        self.assertEqual(metadato.cfdi_document_id, doc)
        self.assertEqual((doc.sat_status, doc.sat_cancel_date, doc.sat_status_source),
                         ('canceled', utc(2026, 10, 5, 9, 30, 0), 'metadatos'))

    def test_la_factura_sin_cfdi_en_la_boveda_toma_su_estado_de_los_metadatos(self):
        uuid = self._uuid()
        venta = self._venta_timbrada(uuid)
        self.assertFalse(venta.dsi_vault_sat_state, "su XML no está en la Bóveda")

        self._procesar(linea(uuid, emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE), direction='issued')
        self.assertEqual(self._metadato(uuid).move_id, venta)
        self.assertEqual(venta.dsi_vault_sat_state, 'valid')
        self.assertFalse(venta.dsi_vault_sat_mismatch)

        with self._try_sat() as try_sat:
            self._procesar(linea(uuid, emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE, estatus='0',
                                 cancelado='2026-10-05 09:30:00'), direction='issued')
        try_sat.assert_not_called()
        self.assertEqual(venta.dsi_vault_sat_state, 'cancelled')
        self.assertTrue(venta.dsi_vault_sat_mismatch)
        self.assertEqual(venta.state, 'posted')
        registro = self._registros(venta)
        self.assertEqual((registro.state, registro.source), ('open', 'metadata'))
        self.assertIn("no tiene el XML de este CFDI", registro.observation)
        self.assertIn("Cancelado el", registro.observation)

    def test_la_revision_diaria_liga_la_factura_que_entra_despues(self):
        """La venta se timbra o se importa por fuera de la Bóveda después de
        que llegaron sus metadatos: la revisión diaria la liga."""
        uuid = self._uuid()
        self._procesar(linea(uuid, emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE, estatus='0',
                             cancelado='2026-10-05 09:30:00'), direction='issued')
        venta = self._venta_timbrada(uuid)
        self.assertFalse(venta.dsi_vault_sat_state)

        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(self._metadato(uuid).move_id, venta)
        self.assertEqual(venta.dsi_vault_sat_state, 'cancelled')
        self.assertEqual(self._registros(venta).state, 'open')

    def test_la_compra_que_entra_despues_con_su_cfdi_sin_ligar_toma_los_metadatos(self):
        """El XML recibido llegó a la Bóveda sin factura y sus metadatos lo
        cancelaron; después el contador registra la compra con el import de
        Odoo, que no pasa por la Bóveda. La revisión diaria solo volvía a ligar
        los metadatos sin CFDI: la compra se quedaba sin Estado bóveda ni
        discrepancia."""
        uuid = self._uuid()
        doc = self._cfdi(uuid, sat_status='valid')
        self._procesar(linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        self.assertEqual(doc.sat_status, 'canceled')
        compra = self._compra_importada_por_odoo(uuid)
        self.assertFalse(compra.dsi_vault_sat_state)

        with self._try_sat() as try_sat:
            self.Inconsistencia._cron_detectar_inconsistencias()
        try_sat.assert_not_called()
        self.assertEqual(self._metadato(uuid).move_id, compra)
        self.assertFalse(doc.move_id, "la revisión diaria no concilia CFDI")
        self.assertEqual(compra.dsi_vault_sat_state, 'cancelled')
        self.assertEqual(self._registros(compra).state, 'open')
        self.assertTrue(self._por_revisar(compra))

    def test_un_choque_al_pasar_los_metadatos_a_un_cfdi_nuevo_no_pierde_el_cfdi(self):
        """Guardar un CFDI le pasa los metadatos que ya estaban de su folio, y
        eso escribe metadatos que otras transacciones también escriben. Sin
        savepoint, un choque abortaba la transacción de quien lo guardaba: la
        descarga entera. Se deshace solo esa parte, queda un aviso y la
        revisión de discrepancias, que lo aplica, se vuelve a programar."""
        uuid = self._uuid()
        self._procesar(linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        Metadato = type(self.Metadato)

        def choque(metadatos, source='metadata', cambiados=None):
            metadatos.env.cr.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'choque simulado' USING ERRCODE = 'serialization_failure'; END $$")

        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        Disparo = self.env['ir.cron.trigger']
        disparos = Disparo.search_count([('cron_id', '=', cron.id)])
        with patch.object(Metadato, '_aplicar', choque), \
                self.assertLogs(LOG_METADATOS, level='WARNING') as log:
            doc = self._cfdi(uuid)
        self.assertEqual([registro.levelname for registro in log.records], ['WARNING'])
        self.assertIn("chocó con otra transacción (SerializationFailure)", log.output[0])
        self.assertEqual(doc.sat_status, 'not_verified', "el CFDI se guarda; lo de los metadatos se deshizo")
        self.assertFalse(self._metadato(uuid).cfdi_document_id)
        self.assertGreater(Disparo.search_count([('cron_id', '=', cron.id)]), disparos)

        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(self._metadato(uuid).cfdi_document_id, doc)
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('canceled', 'metadatos'))

    def test_los_xml_descargados_toman_los_metadatos_una_vez_por_paquete(self):
        """La descarga guarda los XML de uno en uno y sus metadatos suelen
        estar ya guardados (el paquete de metadatos del mismo periodo llega
        antes). Se los pasaba el gancho de cada CFDI: una aplicación completa
        por XML, casi el doble de tiempo por XML dentro de la transacción de
        la descarga. Ahora es una sola al final del paquete."""
        cancelados, vigente = [self._uuid(), self._uuid()], self._uuid()
        self._procesar(*[linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00') for uuid in cancelados],
                       linea(vigente))
        solicitud = self._solicitud()
        solicitud.write({'request_type': 'cfdi', 'estado_comprobante': 'vigente'})
        Metadato = type(self.Metadato)
        with patch.object(Metadato, '_aplicar', autospec=True, side_effect=Metadato._aplicar) as aplicar:
            descargar(solicitud, paquete_zip(*[cfdi_xml(uuid) for uuid in [*cancelados, vigente]]))
        self.assertEqual(aplicar.call_count, 1)
        self.assertEqual(solicitud.imported_count, 3)
        docs = self.env['cfdi.document'].search([('uuid', 'in', [*cancelados, vigente])])
        self.assertEqual(
            {doc.uuid: (doc.sat_status, doc.sat_status_source, doc.sat_cancel_date) for doc in docs},
            {**dict.fromkeys(cancelados, ('canceled', 'metadatos', utc(2026, 10, 5, 9, 30, 0))),
             vigente: ('valid', 'metadatos', False)})
        self.assertEqual(self._metadato(vigente).cfdi_document_id.uuid, vigente)

        # Quien guarda un CFDI suelto (la importación manual) los toma al guardarlo.
        uuid = self._uuid()
        self._procesar(linea(uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        with patch.object(Metadato, '_aplicar', autospec=True, side_effect=Metadato._aplicar) as aplicar:
            doc = self._cfdi(uuid)
        self.assertEqual(aplicar.call_count, 1)
        self.assertEqual(doc.sat_status, 'canceled')

    def test_no_encontrado_por_la_consulta_y_vigente_en_los_metadatos_no_va_y_viene(self):
        """Un CFDI que la consulta pública no encuentra y los metadatos dan
        vigente iba y venía cada día entre los dos, con un mensaje en el CFDI
        y en su factura cada vez. Gana lo más reciente: los metadatos, si
        llegaron después de esa consulta; la consulta, si respondió después
        de que llegaron (volver a leer el mismo vigente no es información
        nueva)."""
        doc, _compra = self._compra()
        with freeze_time('2026-10-20 10:00:00'):
            self._sat_dice(doc, 'not_found')
        with freeze_time('2026-10-21 10:00:00'):
            self._procesar(linea(doc.uuid))
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('valid', 'metadatos'))

        estatus = []
        for dia in (22, 23, 24):
            with freeze_time(f'2026-10-{dia} 10:00:00'):
                self._sat_dice(doc, 'not_found')
                estatus.append(doc.sat_status)
                self.Inconsistencia._cron_detectar_inconsistencias()
                estatus.append(doc.sat_status)
                self._procesar(linea(doc.uuid))
                estatus.append(doc.sat_status)
        self.assertEqual(set(estatus), {'not_found'})

    def test_llenar_vigentes_no_deja_mensajes_en_las_facturas(self):
        """Como en el CFDI: la primera descarga de 90 días dejaba un mensaje
        «Estado bóveda: → Vigente» en cada factura. Una cancelación sí lo
        deja."""
        uuid = self._uuid()
        venta = self._venta_timbrada(uuid)
        doc, compra = self._compra()
        doc.write({'sat_status': 'not_verified'})
        self.assertFalse(compra.dsi_vault_sat_state)
        self._confirmar_historial()

        def seguimiento(factura):
            return len(mensajes_que_registran(factura, 'dsi_vault_sat_state'))

        antes = {factura: seguimiento(factura) for factura in (venta, compra)}
        self._procesar(linea(uuid, emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE), direction='issued')
        self._procesar(linea(doc.uuid))
        self._confirmar_historial()
        self.assertEqual((venta.dsi_vault_sat_state, compra.dsi_vault_sat_state), ('valid', 'valid'))
        self.assertEqual({factura: seguimiento(factura) for factura in (venta, compra)}, antes)

        with self._try_sat():
            self._procesar(linea(uuid, emisor=RFC_RECEPTOR, receptor=RFC_CLIENTE, estatus='0',
                                 cancelado='2026-10-05 09:30:00'), direction='issued')
        self._confirmar_historial()
        self.assertEqual(venta.dsi_vault_sat_state, 'cancelled')
        self.assertEqual(seguimiento(venta), antes[venta] + 1)

    def test_compra_cancelada_por_metadatos_queda_por_revisar_sin_actualizar_sat(self):
        doc, compra = self._compra()
        self.assertFalse(self._por_revisar(compra))
        asiento = compra.line_ids.read(['account_id', 'debit', 'credit', 'tax_ids'])

        with self._try_sat() as try_sat:
            self._procesar(linea(doc.uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        try_sat.assert_not_called()
        self.assertEqual(doc.sat_status, 'canceled')
        self.assertEqual(compra.dsi_vault_sat_state, 'cancelled')
        self.assertTrue(compra.dsi_vault_sat_mismatch)
        self.assertEqual(compra.state, 'posted')
        self.assertEqual(compra.line_ids.read(['account_id', 'debit', 'credit', 'tax_ids']), asiento)
        self.assertTrue(self._por_revisar(compra))
        registro = self._registros(compra)
        self.assertEqual((registro.state, registro.source), ('open', 'metadata'))
        self.assertIn("No se ejecutó «Actualizar SAT»", registro.observation)
        self.assertIn("metadatos de la descarga masiva", registro.observation)

    def test_un_rep_cancelado_por_metadatos_cancela_su_pago_recibido(self):
        doc, compra = self._compra()
        uuid_rep = self._uuid()
        rep = self._guardar(rep_xml(uuid_rep, [(500.0, 'MXN', '1', [docto(doc.uuid, 500.0)])]))
        rep._ligar_reps()
        documento = rep.dsi_edi_document_id
        self.assertEqual((documento.state, documento.sat_state), ('payment_received', 'not_defined'))

        self._procesar(linea(uuid_rep, estatus='0', cancelado='2026-10-05 09:30:00', efecto='P', monto='0'))
        self.assertEqual(rep.sat_status, 'canceled')
        self.assertEqual(documento.sat_state, 'cancelled')
        self.assertEqual(documento.invoice_ids, compra, "nunca se borra")
        self.assertEqual(compra.dsi_vault_sat_state, 'valid', "el REP no es el CFDI de la compra")

    # -- Volver a leer y fallas -----------------------------------------------------

    def test_volver_a_leer_el_adjunto_no_cambia_nada(self):
        doc, compra = self._compra()
        uuid = self._uuid()
        with self.assertLogs(LOG_METADATOS, level='WARNING'):
            solicitud = self._procesar(
                linea(doc.uuid, estatus='0', cancelado='2026-10-05 09:30:00'), linea(uuid), linea(self._uuid())[:-20])
        self._confirmar_historial()

        def foto():
            metadatos = self.Metadato.search([('company_id', '=', self.company.id)])
            return (metadatos.ids, metadatos.mapped('estatus'), len(doc.message_ids), len(compra.message_ids),
                    self.Inconsistencia.search([('move_id', '=', compra.id)]).mapped('state'),
                    solicitud.metadata_count, solicitud.metadata_error_count)
        antes = foto()
        with self.assertLogs(LOG_METADATOS, level='WARNING'):
            solicitud.action_reprocesar_metadatos()
        self._confirmar_historial()
        self.assertEqual(foto(), antes)
        # Ni siquiera se escribe: lo leído ya es lo guardado.
        filas, _ilegibles = leer_paquete_de_metadatos(
            [(adjunto.name, _bytes_de_adjunto(adjunto)) for adjunto in solicitud.metadata_attachment_ids])
        _todos, cambiados = self.Metadato._guardar_filas(solicitud, filas)
        self.assertFalse(cambiados)

    def test_un_choque_al_aplicar_no_pierde_los_metadatos(self):
        """Pasar los metadatos a la Bóveda puede chocar con la revisión diaria
        o con la verificación SAT. Se deshace solo esa parte: los metadatos se
        quedan, queda un aviso y la revisión de discrepancias, que los aplica,
        se vuelve a programar."""
        doc = self._cfdi()
        Metadato = type(self.Metadato)

        def choque(metadatos, source='metadata', cambiados=None):
            metadatos.env.cr.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'choque simulado' USING ERRCODE = 'serialization_failure'; END $$")

        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        Disparo = self.env['ir.cron.trigger']
        disparos = Disparo.search_count([('cron_id', '=', cron.id)])
        with patch.object(Metadato, '_aplicar', choque), \
                self.assertLogs(LOG_SOLICITUD, level='WARNING') as log:
            solicitud = self._procesar(linea(doc.uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        self.assertIn("chocó con otra transacción (SerializationFailure)", log.output[0])
        self.assertEqual(solicitud.metadata_count, 1)
        self.assertEqual(self._metadato(doc.uuid).estatus, '0')
        self.assertEqual(doc.sat_status, 'not_verified', "la aplicación se deshizo")
        self.assertGreater(Disparo.search_count([('cron_id', '=', cron.id)]), disparos)

        # La revisión diaria la aplica.
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('canceled', 'metadatos'))

    def test_un_choque_al_guardar_se_reintenta_cada_hora(self):
        uuid = self._uuid()
        solicitud = self._solicitud()
        solicitud._procesar_paquete_zip(paquete(X_01=txt(linea(uuid))))
        Metadato = type(self.Metadato)

        def choque(_modelo, solicitud, filas):
            raise psycopg2.errors.SerializationFailure("choque simulado")

        with patch.object(Metadato, '_guardar_filas', choque), \
                self.assertLogs(LOG_SOLICITUD, level='WARNING'):
            solicitud._procesar_metadatos()
        self.assertTrue(solicitud.metadata_pending)
        self.assertFalse(self._metadato(uuid))

        solicitud.state = 'processed'
        with patch.object(self.env.cr, 'commit', lambda *args: None):
            self.env['sat.sync.schedule']._cron_avanzar_solicitudes()
        self.assertFalse(solicitud.metadata_pending)
        self.assertEqual(self._metadato(uuid).estatus, '1')

    # -- Refresco semanal -------------------------------------------------------------

    def _programacion(self, company=None, credential=None):
        return self.env['sat.sync.schedule'].create({
            'company_id': (company or self.company).id,
            'credential_id': (credential or self.credential).id,
            'date_start': date(2026, 1, 1),
        })

    def _avances(self):
        return patch.object(self.env.registry['sat.download.request'], '_avanzar_sin_excepciones', autospec=True)

    def test_el_refresco_pide_90_dias_sin_mover_lo_cubierto(self):
        programacion = self._programacion()
        programacion.date_covered_to = date(2026, 10, 20)
        hoy = date(2026, 10, 27)
        with patch.object(fields.Date, 'context_today', return_value=hoy), self._avances() as avance:
            self.env['sat.sync.schedule']._cron_refrescar_metadatos()
            primeras = programacion.request_ids
            # El botón el mismo día: otra ventana, nunca los mismos parámetros.
            programacion.action_refrescar_metadatos()
            segundas = programacion.request_ids - primeras

        self.assertEqual(len(primeras), 2)
        self.assertEqual(set(primeras.mapped('direction')), {'received', 'issued'})
        for solicitud in primeras:
            self.assertEqual((solicitud.request_type, solicitud.estado_comprobante, solicitud.is_refresh),
                             ('metadata', 'todos', True))
            self.assertEqual((solicitud.date_from, solicitud.date_to), (date(2026, 7, 29), date(2026, 10, 26)))
            self.assertEqual((solicitud.date_to - solicitud.date_from).days + 1, 90)
        self.assertEqual(len(segundas), 2)
        self.assertEqual(set(segundas.mapped('date_from')), {date(2026, 7, 28)})
        parametros = [(s.direction, s.request_type, s.estado_comprobante, s.date_from, s.date_to)
                      for s in primeras | segundas]
        self.assertEqual(len(parametros), len(set(parametros)), "ninguna solicitud repite parámetros")
        self.assertEqual(programacion.date_covered_to, date(2026, 10, 20), "no mueve «Cubierto hasta»")
        self.assertTrue(programacion.metadata_refreshed_on)
        self.assertEqual(avance.call_count, 4, "se mandan al SAT de inmediato")

        # Sin metadatos o con 0 días no se pide nada.
        programacion.metadata_refresh_days = 0
        with self._avances():
            self.env['sat.sync.schedule']._cron_refrescar_metadatos()
        self.assertEqual(len(programacion.request_ids), 4)

    def test_sin_licencia_no_hay_refresco(self):
        empresa = self.env['res.company'].create({'name': "Sin licencia"})
        credencial = self.env['sat.credential'].create({
            'company_id': empresa.id, 'rfc': 'AAA010101AAA', 'state': 'validated'})
        programacion = self._programacion(empresa, credencial)
        with self._avances() as avance:
            self.env['sat.sync.schedule']._cron_refrescar_metadatos()
            with self.assertRaises(RedirectWarning):
                programacion.action_refrescar_metadatos()
        avance.assert_not_called()
        self.assertFalse(programacion.request_ids)

    # -- Permisos -----------------------------------------------------------------------

    def test_el_usuario_de_la_boveda_no_escribe_lo_de_la_verificacion_ni_los_metadatos(self):
        doc = self._cfdi()
        self._procesar(linea(doc.uuid, estatus='0', cancelado='2026-10-05 09:30:00'))
        metadato = self._metadato(doc.uuid)
        contexto = {'allowed_company_ids': self.company.ids}
        como_usuario = doc.with_user(self.usuario).with_context(contexto)

        for valores in ({'sat_status': 'valid'}, {'sat_cancel_date': False},
                        {'sat_status_source': 'consulta'}, {'sat_last_check': fields.Datetime.now()}):
            with self.subTest(valores=valores), self.assertRaises(AccessError):
                como_usuario.write(valores)
        with self.assertRaises(AccessError):
            self.env['cfdi.document'].with_user(self.usuario).with_context(contexto).create({
                'company_id': self.company.id, 'direction': 'received', 'uuid': self._uuid(),
                'currency_id': self.company.currency_id.id, 'sat_status_source': 'metadatos',
                'xml_attachment_id': doc.xml_attachment_id.id,
            })
        # Los metadatos los lee, pero no los escribe, crea ni borra.
        Metadato = self.Metadato.with_user(self.usuario).with_context(contexto)
        self.assertEqual(Metadato.search([('uuid', '=', doc.uuid)]), metadato)
        for intento in (lambda: metadato.with_user(self.usuario).write({'estatus': '1'}),
                        lambda: metadato.with_user(self.usuario).unlink(),
                        lambda: Metadato.create({'company_id': self.company.id, 'uuid': self._uuid(),
                                                 'direction': 'received', 'estatus': '1'})):
            with self.assertRaises(AccessError):
                intento()
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('canceled', 'metadatos'))

        # El gestor contable sí lo escribe a mano: queda como corrección manual
        # y, como ya no está cancelado, sin fecha de cancelación.
        gestor = new_test_user(
            self.env, login='gestor-metadatos', groups=GRUPOS_BOVEDA + ',account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        doc.with_user(gestor).with_context(contexto).sat_status = 'valid'
        self.assertEqual((doc.sat_status, doc.sat_status_source, doc.sat_cancel_date), ('valid', 'manual', False))
        # Pero la cancelación es definitiva: mientras los metadatos lo den
        # cancelado, la revisión diaria la vuelve a aplicar.
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual((doc.sat_status, doc.sat_status_source), ('canceled', 'metadatos'))

    def test_cada_empresa_ve_solo_lo_suyo(self):
        """Ni el custodio de la e.firma ve lo de una empresa que no tiene
        abierta: su e.firma, su descarga automática, sus solicitudes, sus
        metadatos ni los CFDI que trajeron. Solo los metadatos tenían regla
        multiempresa; el resto lo veía cualquiera con el permiso."""
        empresa_b = self.env['res.company'].create({'name': "Empresa B de prueba"})
        credencial_b = self.env['sat.credential'].create({
            'company_id': empresa_b.id, 'rfc': RFC_EMISOR, 'state': 'validated',
        })
        doc_a, doc_b = self._cfdi(), self._cfdi()
        doc_b.company_id = empresa_b
        solicitud_b = self._solicitud()
        solicitud_b.write({'company_id': empresa_b.id, 'credential_id': credencial_b.id})
        de_a = {
            'sat.credential': self.credential,
            'sat.sync.schedule': self._programacion(),
            'sat.download.request': self._solicitud(),
            'dsi.cfdi.sat.metadata': self.Metadato.create({
                'company_id': self.company.id, 'uuid': doc_a.uuid, 'direction': 'received', 'estatus': '1'}),
            'cfdi.document': doc_a,
        }
        de_b = {
            'sat.credential': credencial_b,
            'sat.sync.schedule': self._programacion(company=empresa_b, credential=credencial_b),
            'sat.download.request': solicitud_b,
            'dsi.cfdi.sat.metadata': self.Metadato.create({
                'company_id': empresa_b.id, 'uuid': doc_b.uuid, 'direction': 'received', 'estatus': '1'}),
            'cfdi.document': doc_b,
        }
        custodio = new_test_user(
            self.env, login='custodio-a', groups=GRUPOS_BOVEDA + ',dsi_cfdi_sat_sync.group_sat_credential_manager',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])

        for modelo, registro_a in de_a.items():
            with self.subTest(modelo=modelo):
                Modelo = self.env[modelo].with_user(custodio).with_context(allowed_company_ids=self.company.ids)
                self.assertEqual(Modelo.search([('id', 'in', (registro_a | de_b[modelo]).ids)]), registro_a)
                with self.assertRaises(AccessError):
                    Modelo.browse(de_b[modelo].id).read(['company_id'])
