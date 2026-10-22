import base64
import logging
import uuid
import zipfile
from datetime import timedelta
from io import BytesIO

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import RedirectWarning, UserError

from . import sat_soap_client as soap
from .sat_soap_client import NS

_logger = logging.getLogger(__name__)

# Cuánto se espera a que el SAT reconozca una solicitud antes de darla por
# perdida. Observado en vivo: una solicitud llevaba 7 días respondiendo
# "No se encontro la informacion" — sin este corte, el cron la reintentaría
# indefinidamente y el usuario esperaría algo que nunca va a llegar.
MAX_ESPERA_HORAS = 24


class SatDownloadRequest(models.Model):
    _name = 'sat.download.request'
    _description = "Solicitud de Descarga Masiva de CFDI ante el SAT"
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, default=lambda self: self.env.company)
    credential_id = fields.Many2one(
        'sat.credential', string="e.firma", required=True, domain="[('company_id', '=', company_id), ('state', '=', 'validated')]")
    schedule_id = fields.Many2one(
        'sat.sync.schedule', string="Programación", readonly=True, ondelete='set null',
        help="Programación que generó esta solicitud. Vacío si se creó a mano.")
    request_type = fields.Selection([
        ('metadata', "Metadata (índice, rápido)"),
        ('cfdi', "CFDI (paquete de XML completos)"),
    ], string="Tipo de solicitud", default='metadata', required=True)
    direction = fields.Selection([
        ('received', "Recibidos"),
        ('issued', "Emitidos"),
    ], string="Dirección", default='received', required=True)
    date_from = fields.Date(string="Fecha inicial", required=True)
    date_to = fields.Date(string="Fecha final", required=True)
    estado_comprobante = fields.Selection([
        ('vigente', "Solo vigentes"),
        ('cancelado', "Solo cancelados"),
        ('todos', "Todos (vigentes y cancelados)"),
    ], string="Estatus del comprobante", default='vigente', required=True,
        help="El SAT NO permite descargar el XML completo (tipo de solicitud "
             "'CFDI') de comprobantes cancelados junto con vigentes en la misma "
             "solicitud — hay que pedirlos por separado. Con 'Metadata' sí se "
             "pueden traer ambos juntos.")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('authenticated', "Autenticado"),
        ('requested', "Solicitado"),
        ('verifying', "Verificando"),
        ('downloading', "Descargando"),
        ('processed', "Procesado"),
        ('rejected', "Rechazado"),
        ('expired', "Expirado"),
    ], default='draft', required=True, tracking=True)
    sat_request_id = fields.Char(string="ID de solicitud SAT", readonly=True)
    auth_token = fields.Text(string="Token de autenticación", readonly=True)
    auth_token_date = fields.Datetime(string="Token emitido el", readonly=True)
    package_ids_json = fields.Text(string="IDs de paquete (JSON)", readonly=True)
    imported_count = fields.Integer(string="CFDI importados", readonly=True)
    error_message = fields.Text(string="Último error", readonly=True)

    # -------------------------------------------------------------------
    # Máquina de estados: borrador -> autenticado -> solicitado ->
    #                      verificando -> descargando -> procesado
    #                                  \-> rechazado / expirado
    # -------------------------------------------------------------------

    def action_reintentar(self):
        """Regresa una solicitud rechazada a borrador para corregir sus datos
        (fechas, tipo, estatus del comprobante) y volver a intentarla, sin
        tener que crear un registro nuevo cada vez."""
        self.ensure_one()
        self.write({
            'state': 'draft', 'error_message': False, 'auth_token': False,
            'sat_request_id': False, 'package_ids_json': False,
        })

    def _check_license(self):
        """Descargar del SAT requiere licencia vigente. Va al inicio de cada
        paso que habla con el SAT, antes de tocar el estado de la solicitud."""
        self.env['dsi.license.client.state']._check_feature(
            'vault', _("descargar CFDI del SAT"), self.company_id)

    def _avanzar_sin_excepciones(self):
        """Empuja la solicitud al siguiente paso que le toque, tragándose los
        errores. Lo usan los crons: un fallo en una solicitud no debe frenar
        el resto ni tumbar la tarea programada. El detalle del problema queda
        en 'Último error' del registro, como cuando se opera a mano."""
        self.ensure_one()
        try:
            if self.state == 'draft':
                self.action_autenticar()
                self.action_solicitar()
            elif self.state == 'authenticated':
                self.action_solicitar()
            elif self.state in ('requested', 'verifying'):
                self.action_verificar()
                # Si quedó lista en esta misma pasada, se aprovecha el viaje.
                if self.state == 'downloading':
                    self.action_descargar_y_procesar()
            elif self.state == 'downloading':
                self.action_descargar_y_procesar()
        except (UserError, RedirectWarning) as e:
            # Errores de negocio esperables (el SAT aún no la registra, rechazo
            # con motivo, etc.). Ya quedaron escritos en error_message.
            _logger.info("Solicitud %s no avanzó: %s", self.id, e)
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Error inesperado avanzando la solicitud %s", self.id)

    def _ensure_token(self):
        """Devuelve un token vigente, re-autenticando si hace falta.

        El token del SAT dura 5 minutos. Como verificar y descargar ocurren
        mucho después de solicitar (el SAT tarda de minutos a horas en preparar
        el paquete), el token SIEMPRE está vencido para cuando se usan. Sin
        esto, el SAT responde EstadoSolicitud=0 "Token invalido" y el usuario
        cree que su solicitud sigue en proceso cuando en realidad ni siquiera
        se está consultando."""
        self.ensure_one()
        margen = timedelta(minutes=4)  # el token vive 5 min; renovamos antes
        vencido = (
            not self.auth_token
            or not self.auth_token_date
            or fields.Datetime.now() - self.auth_token_date > margen
        )
        if vencido:
            self._autenticar()
        return self.auth_token

    def action_autenticar(self):
        self.ensure_one()
        self._check_license()
        self._autenticar()
        if self.state == 'draft':
            self.state = 'authenticated'

    def _autenticar(self):
        self.ensure_one()
        credential = self.credential_id
        private_key = credential._get_private_key()
        cer_bytes = credential._get_cer_bytes()

        security = soap.build_signed_timestamp_security_header(private_key, cer_bytes)

        envelope = etree.Element(f'{{{NS["soapenv"]}}}Envelope', nsmap={
            'soapenv': NS['soapenv'], 'wsse': NS['wsse'], 'xd': NS['xd'],
        })
        header = etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Header')
        header.append(security)
        body = etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Body')
        etree.SubElement(body, f'{{{NS["xd"]}}}Autentica')

        xml_str = etree.tostring(envelope, xml_declaration=True, encoding='UTF-8').decode()
        response = soap._soap_call(
            soap.URL_AUTENTICA,
            'http://DescargaMasivaTerceros.gob.mx/IAutenticacion/Autentica',
            xml_str,
        )
        try:
            root = etree.fromstring(response.content)
        except etree.XMLSyntaxError as e:
            self.write({'state': 'rejected', 'error_message': f"Respuesta no es XML válido: {response.text[:500]}"})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT respondió algo inesperado al autenticar. Revisa el detalle del error en la solicitud.")) from e

        fault = root.find('.//{http://schemas.xmlsoap.org/soap/envelope/}Fault')
        if fault is not None:
            fault_text = etree.tostring(fault, pretty_print=True).decode()
            self.write({'state': 'rejected', 'error_message': fault_text})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_(
                "El SAT rechazó la autenticación (revisa que la e.firma sea vigente y real — "
                "ver el mensaje completo en el campo 'Último error' de esta solicitud)."))

        token_el = root.find('.//{http://DescargaMasivaTerceros.gob.mx}AutenticaResult')
        if token_el is None or not token_el.text:
            self.write({'state': 'rejected', 'error_message': response.text[:2000]})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT no devolvió un token de autenticación. Ver detalle del error."))

        self.write({
            'auth_token': token_el.text,
            'auth_token_date': fields.Datetime.now(),
            'error_message': False,
        })

    def action_solicitar(self):
        """Construye la petición según el contrato REAL del SAT, confirmado
        contra el WSDL/XSD publicado en vivo por
        cfdidescargamasivasolicitud.clouda.sat.gob.mx/SolicitaDescargaService.svc
        (verificado en esta sesión — el nombre del servicio es
        "SolicitaDescargaService", NO "SolicitudDescargaService" como sugieren
        varios tutoriales de terceros). Hay una operación distinta para
        Emitidos vs. Recibidos, cada una con su propio elemento raíz y su
        propio tipo de "solicitud" anidada — no es un único mensaje genérico."""
        self.ensure_one()
        self._check_license()
        if self.state != 'authenticated':
            raise UserError(_("Primero hay que autenticar esta solicitud."))
        if self.request_type == 'cfdi' and self.estado_comprobante == 'todos':
            # Confirmado en vivo contra el SAT real: rechaza con "No se permite
            # la descarga de xml que se encuentren cancelados" cuando el tipo
            # de solicitud es CFDI (XML completo) y no se filtra el estatus.
            raise UserError(_(
                "El SAT no permite traer el XML completo de comprobantes vigentes y "
                "cancelados en la misma solicitud. Elige 'Solo vigentes' o 'Solo "
                "cancelados' en Estatus del comprobante, o usa el tipo 'Metadata' si "
                "quieres traer ambos juntos."))
        credential = self.credential_id
        private_key = credential._get_private_key()
        cer_bytes = credential._get_cer_bytes()
        from cryptography import x509 as _x509
        cert_obj = _x509.load_der_x509_certificate(cer_bytes)

        des = NS['des']
        tipo_solicitud = 'CFDI' if self.request_type == 'cfdi' else 'Metadata'
        op_name = 'SolicitaDescargaEmitidos' if self.direction == 'issued' else 'SolicitaDescargaRecibidos'

        wrapper = etree.Element(f'{{{des}}}{op_name}', nsmap={'des': des})
        # El elemento <solicitud> hijo también debe ir calificado en el
        # namespace "des" — el XSD del SAT declara elementFormDefault="qualified",
        # así que un <solicitud> sin namespace (lo que produce
        # etree.SubElement(wrapper, 'solicitud') sin más) no es reconocido por
        # el servidor: responde "se recibió una solicitud vacía" en vez de un
        # error de negocio real, porque no encuentra el contenido esperado.
        solicitud = etree.SubElement(wrapper, f'{{{des}}}solicitud')
        solicitud.set('FechaInicial', f"{self.date_from.isoformat()}T00:00:00")
        solicitud.set('FechaFinal', f"{self.date_to.isoformat()}T23:59:59")
        solicitud.set('RfcSolicitante', credential.rfc)
        solicitud.set('TipoSolicitud', tipo_solicitud)
        if self.estado_comprobante != 'todos':
            # ⚠️ Probado en vivo: "1"/"0" NO funciona — el SAT lo rechaza con el
            # mismo mensaje que si no se mandara el filtro en absoluto ("No se
            # permite la descarga de xml que se encuentren cancelados"), lo que
            # sugiere que no reconoce ese valor como un filtro válido. El resto
            # de la API del SAT usa palabras legibles (TipoSolicitud="CFDI"/
            # "Metadata", no códigos), así que se prueba ahora con el mismo
            # patrón. AÚN NO CONFIRMADO tampoco — si esto también falla, hay
            # que revisar el WSDL real en vivo (ver sección 9 del HANDOFF).
            solicitud.set('EstadoComprobante', 'Vigente' if self.estado_comprobante == 'vigente' else 'Cancelado')
        if self.direction == 'received':
            # Confirmado en vivo contra el servicio real: a pesar de que el
            # XSD marca RfcReceptor como opcional, el SAT lo exige en la
            # práctica para "Recibidos" — devuelve "La solicitud de descarga
            # de facturas recibidas requiere el RfcReceptor" si se omite.
            # Es el RFC propio del solicitante (tú, recibiendo).
            solicitud.set('RfcReceptor', credential.rfc)
        else:
            # Confirmado en vivo (mismo patrón que RfcReceptor arriba): el SAT
            # exige "RfcEmisor" para "Emitidos" — "La solicitud de descarga de
            # facturas emitidas requiere el RfcEmisor" si se omite. Es el RFC
            # propio del solicitante (tú, emitiendo). RfcReceptores (arreglo,
            # para acotar a un receptor específico) existe en el XSD pero es
            # opcional — no se manda si no se necesita filtrar por receptor.
            solicitud.set('RfcEmisor', credential.rfc)

        soap.sign_enveloped(solicitud, private_key, cert_obj, cer_bytes)

        envelope = etree.Element(f'{{{NS["soapenv"]}}}Envelope', nsmap={
            'soapenv': NS['soapenv'], 'des': des,
        })
        etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Header')
        body = etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Body')
        body.append(wrapper)

        xml_str = etree.tostring(envelope, xml_declaration=True, encoding='UTF-8').decode()
        response = soap._soap_call(
            soap.URL_SOLICITA,
            f'http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/{op_name}',
            xml_str,
            auth_token=self._ensure_token(),
        )
        if not response.content:
            self.write({'state': 'rejected', 'error_message': f"HTTP {response.status_code}, respuesta vacía"})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT respondió vacío (HTTP %s) al solicitar la descarga.", response.status_code))
        root = etree.fromstring(response.content)
        fault = root.find('.//{http://schemas.xmlsoap.org/soap/envelope/}Fault')
        if fault is not None:
            self.write({'state': 'rejected', 'error_message': etree.tostring(fault, pretty_print=True).decode()})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT rechazó la solicitud de descarga. Ver detalle del error."))

        result = root.find(f'.//{{http://DescargaMasivaTerceros.sat.gob.mx}}{op_name}Result')
        request_id = result.get('IdSolicitud') if result is not None else None
        if not request_id:
            mensaje = result.get('Mensaje') if result is not None else response.text[:2000]
            self.write({'state': 'rejected', 'error_message': mensaje})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT no devolvió un ID de solicitud: %s", mensaje))

        self.write({'state': 'requested', 'sat_request_id': request_id, 'error_message': False})

    def action_verificar(self):
        self.ensure_one()
        self._check_license()
        if self.state not in ('requested', 'verifying'):
            raise UserError(_("Esta solicitud todavía no se ha enviado al SAT."))
        credential = self.credential_id
        private_key = credential._get_private_key()
        cer_bytes = credential._get_cer_bytes()
        from cryptography import x509 as _x509
        cert_obj = _x509.load_der_x509_certificate(cer_bytes)

        des = NS['des']
        wrapper = etree.Element(f'{{{des}}}VerificaSolicitudDescarga', nsmap={'des': des})
        solicitud = etree.SubElement(wrapper, f'{{{des}}}solicitud')
        solicitud.set('IdSolicitud', self.sat_request_id)
        solicitud.set('RfcSolicitante', credential.rfc)
        soap.sign_enveloped(solicitud, private_key, cert_obj, cer_bytes)

        envelope = etree.Element(f'{{{NS["soapenv"]}}}Envelope', nsmap={
            'soapenv': NS['soapenv'], 'des': des,
        })
        etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Header')
        body = etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Body')
        body.append(wrapper)

        xml_str = etree.tostring(envelope, xml_declaration=True, encoding='UTF-8').decode()
        response = soap._soap_call(
            soap.URL_VERIFICA,
            'http://DescargaMasivaTerceros.sat.gob.mx/IVerificaSolicitudDescargaService/VerificaSolicitudDescarga',
            xml_str,
            auth_token=self._ensure_token(),
        )
        if not response.content:
            self.write({'state': 'rejected', 'error_message': f"HTTP {response.status_code}, respuesta vacía"})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT respondió vacío (HTTP %s) al verificar la solicitud.", response.status_code))
        root = etree.fromstring(response.content)
        result = root.find('.//{http://DescargaMasivaTerceros.sat.gob.mx}VerificaSolicitudDescargaResult')
        if result is None:
            self.write({'state': 'rejected', 'error_message': response.text[:2000]})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_("El SAT no devolvió estatus de verificación. Ver detalle del error."))

        # Catálogo de EstadoSolicitud del SAT:
        #   0 Token inválido · 1 Aceptada · 2 En proceso · 3 Terminada
        #   4 Error · 5 Rechazada · 6 Vencida
        estado_solicitud = result.get('EstadoSolicitud')
        mensaje_sat = result.get('Mensaje') or ''
        cod_estatus = result.get('CodEstatus') or ''
        numero_cfdis = result.get('NumeroCFDIs')

        if estado_solicitud == '0':
            # OJO: EstadoSolicitud="0" NO significa siempre "token inválido".
            # Es un genérico; quien dice qué pasó es CodEstatus. Confirmado en
            # vivo contra el SAT real:
            #   300  -> "Token invalido"          (problema de autenticación)
            #   5004 -> "No se encontro la informacion" (el SAT aún no registra
            #           la solicitud recién creada, o el folio no existe)
            if cod_estatus == '300':
                self.write({'error_message': f"CodEstatus 300 · {mensaje_sat}"})
                self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
                raise UserError(_(
                    "El SAT rechazó el token de autenticación. Vuelve a intentar; si persiste, "
                    "revisa que la e.firma siga vigente."))

            if cod_estatus == '5004':
                # Normal en los primeros minutos: el servicio de consulta del
                # SAT tarda en enterarse de la solicitud recién creada.
                # PERO si lleva días así, ya no es propagación — el SAT nunca la
                # registró o la purgó. Sin este corte, el cron la reintentaría
                # para siempre y el usuario esperaría algo que no va a llegar.
                antiguedad = fields.Datetime.now() - self.create_date
                if antiguedad > timedelta(hours=MAX_ESPERA_HORAS):
                    self.write({'state': 'expired', 'error_message': (
                        f"El SAT sigue sin reconocer esta solicitud después de "
                        f"{antiguedad.days} día(s) (CodEstatus 5004 · {mensaje_sat}). "
                        f"Se da por perdida; crea una nueva para el mismo periodo.")})
                    self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
                    raise UserError(_(
                        "El SAT nunca registró esta solicitud (lleva %s días sin reconocerla). "
                        "Crea una solicitud nueva para el mismo periodo.", antiguedad.days))

                self.write({'state': 'verifying', 'error_message': (
                    f"El SAT aún no registra esta solicitud (CodEstatus 5004 · {mensaje_sat}). "
                    f"Es normal en los primeros minutos; vuelve a verificar más tarde. "
                    f"Última consulta {fields.Datetime.now()} UTC.")})
                return

            # Cualquier otro CodEstatus con estado 0: se muestra literal lo que
            # dijo el SAT, en vez de inventar una interpretación.
            self.write({'error_message': f"CodEstatus {cod_estatus} · {mensaje_sat}"})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_(
                "El SAT respondió: %(msg)s (código %(cod)s). Ver el detalle en la solicitud.",
                msg=mensaje_sat or "sin mensaje", cod=cod_estatus or "sin código"))

        if estado_solicitud == '6':  # Vencida
            self.write({
                'state': 'expired',
                'error_message': f"El SAT marcó la solicitud como vencida: {mensaje_sat}",
            })
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
            raise UserError(_(
                "Esta solicitud venció en el SAT: el paquete dejó de estar disponible antes "
                "de descargarse. Crea una solicitud nueva para el mismo periodo."))

        if estado_solicitud == '3':  # Terminada
            # <IdsPaquetes> es un elemento de texto simple (no un atributo
            # IdPaquete), confirmado contra el XSD real del SAT.
            package_ids = [
                p.text for p in root.findall('.//{http://DescargaMasivaTerceros.sat.gob.mx}IdsPaquetes')
                if p.text
            ]
            import json
            self.write({'state': 'downloading', 'package_ids_json': json.dumps(package_ids), 'error_message': False})
        elif estado_solicitud in ('4', '5'):  # Error / Rechazada
            self.write({'state': 'rejected', 'error_message': f"EstadoSolicitud={estado_solicitud}: {response.text[:1000]}"})
            self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
        else:
            # 1 Aceptada / 2 En proceso — sigue esperando. Se deja constancia
            # de la última respuesta real del SAT para que "Verificando" no sea
            # una caja negra: el usuario ve si ya sabe cuántos CFDI trae.
            leyenda = {'1': "Aceptada, en cola", '2': "En proceso"}.get(estado_solicitud, f"Estado {estado_solicitud}")
            detalle = f"{leyenda} · última consulta {fields.Datetime.now()} UTC"
            if numero_cfdis:
                detalle += f" · {numero_cfdis} CFDI reportados"
            self.write({'state': 'verifying', 'error_message': detalle})

    def action_descargar_y_procesar(self):
        self.ensure_one()
        self._check_license()
        if self.state != 'downloading':
            raise UserError(_("Esta solicitud todavía no tiene paquetes listos para descargar."))
        import json
        package_ids = json.loads(self.package_ids_json or '[]')
        if not package_ids:
            raise UserError(_("No hay paquetes que descargar."))

        credential = self.credential_id
        private_key = credential._get_private_key()
        cer_bytes = credential._get_cer_bytes()
        from cryptography import x509 as _x509
        cert_obj = _x509.load_der_x509_certificate(cer_bytes)

        importados = self.env['cfdi.document']
        for package_id in package_ids:
            # Este servicio NO publica WSDL (responde HTTP 400 a ?wsdl), así que
            # la estructura se dedujo del mensaje de respuesta real del SAT:
            # devuelve <RespuestaDescargaMasivaTercerosSalida><Paquete/>, lo que
            # confirma el par Entrada/Salida — la petición va envuelta en
            # <PeticionDescargaMasivaTercerosEntrada> con un hijo
            # <peticionDescarga> (no <solicitud>, que es el nombre que usan
            # Solicita/Verifica). Enviar <peticionDescarga> como raíz da
            # CodEstatus=301 "XML Mal Formado".
            des = NS['des']
            wrapper = etree.Element(f'{{{des}}}PeticionDescargaMasivaTercerosEntrada', nsmap={'des': des})
            peticion = etree.SubElement(wrapper, f'{{{des}}}peticionDescarga')
            peticion.set('IdPaquete', package_id)
            peticion.set('RfcSolicitante', credential.rfc)
            soap.sign_enveloped(peticion, private_key, cert_obj, cer_bytes)

            envelope = etree.Element(f'{{{NS["soapenv"]}}}Envelope', nsmap={
                'soapenv': NS['soapenv'], 'des': des,
            })
            etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Header')
            body = etree.SubElement(envelope, f'{{{NS["soapenv"]}}}Body')
            body.append(wrapper)

            xml_str = etree.tostring(envelope, xml_declaration=True, encoding='UTF-8').decode()
            response = soap._soap_call(
                soap.URL_DESCARGA,
                'http://DescargaMasivaTerceros.sat.gob.mx/IDescargaMasivaTercerosService/Descargar',
                xml_str,
                auth_token=self._ensure_token(),
            )
            if not response.content:
                self.write({'error_message': f"HTTP {response.status_code}, respuesta vacía al descargar {package_id}"})
                self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
                raise UserError(_("El SAT respondió vacío (HTTP %s) al descargar el paquete %s.", response.status_code, package_id))
            root = etree.fromstring(response.content)
            # A diferencia de Solicita/Verifica, este servicio devuelve el
            # estatus en el HEADER (<h:respuesta CodEstatus=".." Mensaje="..">),
            # no en el body — hay que leerlo de ahí para dar un error legible.
            respuesta_el = root.find('.//{http://DescargaMasivaTerceros.sat.gob.mx}respuesta')
            cod_estatus = respuesta_el.get('CodEstatus') if respuesta_el is not None else None
            mensaje_sat = respuesta_el.get('Mensaje') if respuesta_el is not None else None
            # "Paquete" va con mayúscula aquí (en Solicita/Verifica los nombres
            # equivalentes van en minúscula) — confirmado en la respuesta real.
            paquete_el = root.find('.//{http://DescargaMasivaTerceros.sat.gob.mx}Paquete')
            if paquete_el is None or not paquete_el.text:
                detalle = f"CodEstatus={cod_estatus} Mensaje={mensaje_sat}\n\n{response.text[:2000]}"
                self.write({'error_message': detalle})
                self.env.cr.commit()  # persistir el error ANTES del raise: si no, el rollback automático de la excepción se lo lleva
                raise UserError(_(
                    "El SAT no devolvió el paquete %(pkg)s (CodEstatus=%(cod)s: %(msg)s). "
                    "Ver el detalle completo en el campo 'Último error'.",
                    pkg=package_id, cod=cod_estatus or '?', msg=mensaje_sat or '?'))

            zip_bytes = base64.b64decode(paquete_el.text)
            importados |= self._procesar_paquete_zip(zip_bytes)

        self.write({
            'state': 'processed', 'imported_count': len(importados), 'error_message': False,
        })
        # Conciliar lo recién descargado, igual que hace la importación manual
        # (cfdi_import_wizard). Sin esto los comprobantes que llegan por el
        # camino desatendido —el que el cliente usa de verdad— se quedan todos
        # en 'pending' y los tableros dicen "sin vincular" cuando en realidad
        # nadie lo intentó nunca.
        self._conciliar_importados(importados)

    # Tamaño de lote para conciliar. La cascada hace búsquedas sobre
    # account.move por UUID y por RFC+importe+fecha; con paquetes de miles de
    # comprobantes conviene trocear para no armar dominios gigantes.
    CONCILIACION_LOTE = 200

    def _conciliar_importados(self, documentos):
        """Pasa los comprobantes recién descargados por la cascada de conciliación.

        Deliberadamente a prueba de fallos: la descarga ya terminó y los
        comprobantes YA están guardados, que es lo valioso e irrepetible (el
        paquete del SAT caduca). La conciliación, en cambio, es idempotente y
        se puede repetir desde el botón «Conciliar automáticamente». Por eso un
        error aquí se registra y se sigue, en vez de tumbar la transacción y
        perder la importación completa.
        """
        if not documentos:
            return
        conciliados = 0
        for inicio in range(0, len(documentos), self.CONCILIACION_LOTE):
            lote = documentos[inicio:inicio + self.CONCILIACION_LOTE]
            try:
                lote.action_match_automatically()
                conciliados += len(lote.filtered('move_id'))
            except Exception:
                _logger.exception(
                    "Solicitud %s: falló la conciliación automática de un lote de %s "
                    "comprobantes. Quedan importados y en 'pendiente'; se pueden "
                    "conciliar a mano con el botón «Conciliar automáticamente».",
                    self.id, len(lote))
        _logger.info(
            "Solicitud %s: %s comprobantes importados, %s vinculados automáticamente.",
            self.id, len(documentos), conciliados)

    def _procesar_paquete_zip(self, zip_bytes):
        """Descomprime el paquete y da de alta cada XML en la Bóveda CFDI,
        reutilizando exactamente el mismo parser/dedup que la carga manual.

        Devuelve el recordset de los cfdi.document creados (no un contador):
        quien llama necesita los registros para conciliarlos, igual que hace
        la importación manual."""
        CfdiDocument = self.env['cfdi.document']
        company = self.company_id
        creados = CfdiDocument
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            for member in zf.namelist():
                if not member.lower().endswith('.xml'):
                    continue
                xml_bytes = zf.read(member)
                vals = CfdiDocument._parse_cfdi_xml(xml_bytes)
                if vals.get('parse_state') == 'error':
                    _logger.warning("CFDI del paquete SAT no se pudo parsear (%s): %s", member, vals.get('parse_error'))
                    continue
                existing = CfdiDocument.search([('company_id', '=', company.id), ('uuid', '=', vals['uuid'])], limit=1)
                if existing:
                    continue
                xml_attachment = self.env['ir.attachment'].create({
                    'name': member, 'raw': xml_bytes, 'mimetype': 'text/xml',
                    'res_model': 'cfdi.document', 'res_id': 0,
                })
                company_rfc = (company.partner_id.vat or '').upper()
                direction = 'issued' if vals.get('partner_rfc_issuer', '').upper() == company_rfc else 'received'
                doc_vals = {
                    'company_id': company.id,
                    'direction': direction,
                    'xml_attachment_id': xml_attachment.id,
                    'source': 'sat_download',
                    **{k: v for k, v in vals.items() if k != 'parse_state'},
                }
                # Mismo respaldo que la importación manual: currency_id es
                # obligatorio, y hay CFDI (nómina, traslado) sin atributo Moneda
                # o con un código que no existe en esta base.
                if not doc_vals.get('currency_id'):
                    doc_vals['currency_id'] = company.currency_id.id
                doc = CfdiDocument.create(doc_vals)
                xml_attachment.write({'res_id': doc.id})
                creados |= doc
        return creados
