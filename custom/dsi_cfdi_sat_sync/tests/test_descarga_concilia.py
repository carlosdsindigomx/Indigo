"""La descarga del SAT debe conciliar lo que trae, no solo guardarlo.

Existe por un hueco real: `_procesar_paquete_zip` creaba los comprobantes y
terminaba sin llamar a la cascada de conciliación. La importación manual de
XML sí la llamaba, así que el hueco solo afectaba al camino desatendido —
justo el que el cliente usa de verdad. El efecto medido en la base de
desarrollo fue que 2,397 de 2,404 comprobantes descargados del SAT estaban en
'pendiente' sin que nadie lo hubiera intentado nunca, y los tableros que dicen
"sin vincular" en realidad decían "nunca se intentó".
"""
import base64
import zipfile
from datetime import date
from io import BytesIO
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

RFC_EMISOR = 'PRO010101AA1'
RFC_RECEPTOR = 'EKU9003173C9'
UUID_CON_FACTURA = 'AAAAAAAA-1111-2222-3333-444444444444'
UUID_SIN_FACTURA = 'BBBBBBBB-1111-2222-3333-444444444444'
IMPORTE = 1234.56
FECHA = date(2026, 6, 15)


def cfdi_xml(uuid, total, fecha, rfc_emisor=RFC_EMISOR):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
                  xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
                  Version="4.0" Fecha="{fecha.isoformat()}T12:00:00"
                  Total="{total}" SubTotal="{total}" Moneda="MXN"
                  TipoDeComprobante="I" MetodoPago="PUE" FormaPago="03">
  <cfdi:Emisor Rfc="{rfc_emisor}" Nombre="Proveedor de Prueba"/>
  <cfdi:Receptor Rfc="{RFC_RECEPTOR}" Nombre="Mi Empresa"/>
  <cfdi:Conceptos>
    <cfdi:Concepto Descripcion="Servicio" Cantidad="1" ValorUnitario="{total}" Importe="{total}"/>
  </cfdi:Conceptos>
  <cfdi:Complemento>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/>
  </cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


def paquete_zip(*xmls):
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for i, contenido in enumerate(xmls):
            zf.writestr(f'comprobante_{i}.xml', contenido)
    return buf.getvalue()


@tagged('post_install', '-at_install')
class TestDescargaConcilia(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Se usa la compañía de la base, no una nueva: una compañía recién
        # creada no tiene plan contable ni diarios, y sin diario de compras no
        # se puede crear la factura con la que debe conciliar.
        cls.company = cls.env.company
        cls.company.partner_id.vat = RFC_RECEPTOR
        if not cls.env['account.journal'].search(
                [('type', '=', 'purchase'), ('company_id', '=', cls.company.id)], limit=1):
            raise cls.skipTest(
                cls, "Requiere una compañía con plan contable y diario de compras.")
        credencial = cls.env['sat.credential'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.credential = credencial or cls.env['sat.credential'].create({
            'company_id': cls.company.id, 'rfc': RFC_RECEPTOR, 'state': 'validated',
        })

    def _solicitud(self):
        return self.env['sat.download.request'].create({
            'company_id': self.company.id,
            'credential_id': self.credential.id,
            'direction': 'received',
            'request_type': 'cfdi',
            'date_from': FECHA,
            'date_to': FECHA,
            'state': 'downloading',
            'package_ids_json': '["PAQUETE_DE_PRUEBA_01"]',
        })

    def _factura_del_contador(self, total=IMPORTE, fecha=FECHA):
        """La factura que el contador ya capturó a mano antes de la descarga."""
        proveedor = self.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1,
        })
        diario = self.env['account.journal'].search(
            [('type', '=', 'purchase'), ('company_id', '=', self.company.id)], limit=1)
        cuenta = self.env['account.account'].search(
            [('account_type', '=', 'expense'), ('company_ids', 'in', self.company.ids)], limit=1)
        factura = self.env['account.move'].with_company(self.company).create({
            'move_type': 'in_invoice', 'partner_id': proveedor.id,
            'journal_id': diario.id, 'invoice_date': fecha, 'date': fecha,
            'invoice_line_ids': [(0, 0, {
                'name': "Servicio", 'quantity': 1, 'price_unit': total,
                'account_id': cuenta.id, 'tax_ids': [(5, 0, 0)],
            })],
        })
        factura.action_post()
        return factura

    def _descargar(self, solicitud, zip_bytes):
        """Corre action_descargar_y_procesar con el SAT simulado.

        Se simula únicamente el transporte (firma, token y respuesta SOAP).
        Todo lo que viene después del ZIP —descomprimir, parsear, dar de alta
        y conciliar— es el código real, que es lo que esta prueba verifica.
        """
        respuesta_soap = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<RespuestaDescargaMasivaTercerosSalida'
            ' xmlns="http://DescargaMasivaTerceros.sat.gob.mx">'
            f'<Paquete>{base64.b64encode(zip_bytes).decode()}</Paquete>'
            '</RespuestaDescargaMasivaTercerosSalida>'
            '</s:Body></s:Envelope>'
        )

        class RespuestaFalsa:
            text = respuesta_soap
            content = respuesta_soap.encode()
            status_code = 200

        Cred = type(self.credential)
        Req = type(solicitud)
        SOAP = 'odoo.addons.dsi_cfdi_sat_sync.models.sat_soap_client'
        with patch.object(Cred, '_get_private_key', return_value=None), \
             patch.object(Cred, '_get_cer_bytes', return_value=b''), \
             patch(f'{SOAP}.sign_enveloped', return_value=None), \
             patch(f'{SOAP}._soap_call', return_value=RespuestaFalsa()), \
             patch('cryptography.x509.load_der_x509_certificate', return_value=None), \
             patch.object(Req, '_ensure_token', return_value='token-de-prueba'):
            solicitud.action_descargar_y_procesar()

    # -- la prueba de regresión -----------------------------------------

    def test_lo_descargado_se_concilia_solo(self):
        """Un comprobante que corresponde a una factura ya capturada debe
        quedar vinculado en cuanto se descarga, sin tocar ningún botón."""
        factura = self._factura_del_contador()
        solicitud = self._solicitud()

        self._descargar(solicitud, paquete_zip(cfdi_xml(UUID_CON_FACTURA, IMPORTE, FECHA)))

        doc = self.env['cfdi.document'].search([
            ('company_id', '=', self.company.id), ('uuid', '=', UUID_CON_FACTURA)])
        self.assertEqual(len(doc), 1, "el comprobante debe quedar dado de alta")
        self.assertEqual(
            doc.move_id, factura,
            "debió vincularse a la factura del contador durante la descarga, "
            "sin que nadie pulse «Conciliar automáticamente»")
        self.assertEqual(doc.match_state, 'matched')
        self.assertEqual(doc.match_method, 'rfc_amount_date')
        self.assertEqual(solicitud.state, 'processed')
        self.assertEqual(solicitud.imported_count, 1)

    def test_sin_factura_queda_pendiente_no_se_inventa_vinculo(self):
        """Sin contraparte contable el comprobante se guarda y queda pendiente."""
        solicitud = self._solicitud()

        self._descargar(solicitud, paquete_zip(cfdi_xml(UUID_SIN_FACTURA, 999.99, FECHA)))

        doc = self.env['cfdi.document'].search([
            ('company_id', '=', self.company.id), ('uuid', '=', UUID_SIN_FACTURA)])
        self.assertEqual(len(doc), 1, "se guarda aunque no concilie: el XML es lo valioso")
        self.assertFalse(doc.move_id, "no debe inventar un vínculo")
        self.assertEqual(doc.match_state, 'pending')

    def test_un_fallo_al_conciliar_no_pierde_la_descarga(self):
        """Si la conciliación truena, los comprobantes YA están guardados.

        El paquete del SAT caduca y no se puede volver a pedir; la
        conciliación, en cambio, se repite con el botón cuando se quiera.
        """
        solicitud = self._solicitud()
        Doc = type(self.env['cfdi.document'])

        with patch.object(Doc, 'action_match_automatically',
                          side_effect=ValueError("fallo simulado al conciliar")):
            self._descargar(solicitud, paquete_zip(cfdi_xml(UUID_CON_FACTURA, IMPORTE, FECHA)))

        doc = self.env['cfdi.document'].search([
            ('company_id', '=', self.company.id), ('uuid', '=', UUID_CON_FACTURA)])
        self.assertEqual(len(doc), 1, "la descarga no se pierde por un fallo de conciliación")
        self.assertEqual(solicitud.state, 'processed')
        self.assertEqual(doc.match_state, 'pending')
