"""Cliente SOAP para el Servicio de Descarga Masiva de CFDI del SAT.

Implementa la firma WS-Security (XMLDSig, RSA-SHA1, canonicalización C14N
exclusiva) a mano con lxml + cryptography, sin depender de `signxml` — esa
librería requiere cryptography>=45, que rompe pyopenssl (usado por el resto
de Odoo) en este entorno. Ver `../crypto.py` para el porqué.

⚠️ No se ha podido probar de punta a punta contra el servicio real: se
necesita una e.firma (FIEL) auténtica, emitida por el SAT, que no existe en
este entorno de desarrollo (se probó con un certificado autofirmado propio,
no emitido por el SAT). Lo que SÍ se confirmó en vivo durante esta sesión:

    - AUTENTICACIÓN: FUNCIONA COMPLETO. Con el certificado de prueba se
      obtuvo un JWT real y válido emitido por el SAT (issuer
      "LoadSolicitudDecargaMasivaTerceros"), con el número de serie del
      certificado de prueba embebido en el claim "actort". Esto confirma que
      la firma WS-Security del Timestamp (XMLDSig, RSA-SHA1, C14N exclusiva)
      está bien construida — si estuviera mal formada, el SAT habría
      devuelto un SOAP Fault de firma inválida, no un token.
    - SOLICITUD y VERIFICACIÓN: el nombre real del servicio es
      "SolicitaDescargaService" (NO "SolicitudDescargaService", como sugieren
      varios tutoriales de terceros) y su contrato exacto (nombres de
      elementos, atributos, namespaces) se descargó y confirmó contra el
      WSDL/XSD publicado en vivo por el propio SAT. Hay 3 operaciones
      distintas según el caso (Emitidos / Recibidos / Folio), cada una con
      su propio elemento raíz — no es un solo mensaje genérico.
    - DESCARGA (paso 4): NO se pudo confirmar. El servicio parece vivir en
      un host distinto (`cfdidescargamasiva.clouda.sat.gob.mx`, sin
      "solicitud") que respondió 400 sin cuerpo — posiblemente requiere algo
      de transporte (mTLS u otro) distinto a los otros 3 servicios. Ver la
      nota en `URL_DESCARGA` más abajo.

Antes de usarse con un cliente real, alguien con una e.firma vigente debe
validar el flujo completo (autenticar → solicitar → verificar → descargar),
y en particular confirmar la URL/nombre real del servicio de descarga.
"""
import base64
import uuid
from datetime import datetime, timedelta

import requests
from lxml import etree

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from odoo.exceptions import UserError

NS = {
    'soapenv': 'http://schemas.xmlsoap.org/soap/envelope/',
    'wsse': 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd',
    'wsu': 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd',
    'ds': 'http://www.w3.org/2000/09/xmldsig#',
    'des': 'http://DescargaMasivaTerceros.sat.gob.mx',
    'xd': 'http://DescargaMasivaTerceros.gob.mx',
}

BASE_URL = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx"
URL_AUTENTICA = f"{BASE_URL}/Autenticacion/Autenticacion.svc"
# OJO: el servicio se llama "SolicitaDescargaService", NO "SolicitudDescargaService"
# (con "d" extra) como sugieren varios tutoriales de terceros — confirmado en vivo
# contra el WSDL real de este host durante el desarrollo de este módulo.
URL_SOLICITA = f"{BASE_URL}/SolicitaDescargaService.svc"
URL_VERIFICA = f"{BASE_URL}/VerificaSolicitudDescargaService.svc"
# ⚠️ NO CONFIRMADO. El servicio de descarga del paquete parece vivir en un host
# DISTINTO ("cfdidescargamasiva.clouda.sat.gob.mx", sin "solicitud"), pero no se
# pudo obtener su WSDL desde este entorno (responde 400 sin cuerpo, posiblemente
# por un requisito de transporte — mTLS u otro — distinto al de los otros 3
# servicios). Antes de usar este módulo en producción, confirmar la URL y el
# nombre exacto del servicio contra la documentación vigente del SAT.
URL_DESCARGA = "https://cfdidescargamasiva.clouda.sat.gob.mx/DescargaMasivaService.svc"
REQUEST_TIMEOUT = 30

C14N_EXCLUSIVE = 'http://www.w3.org/2001/10/xml-exc-c14n#'
SIG_RSA_SHA1 = 'http://www.w3.org/2000/09/xmldsig#rsa-sha1'
DIGEST_SHA1 = 'http://www.w3.org/2000/09/xmldsig#sha1'


def _c14n(element):
    return etree.tostring(element, method='c14n', exclusive=True, with_comments=False)


def _sha1_digest_b64(data: bytes) -> str:
    from cryptography.hazmat.primitives import hashes as _hashes
    digest = _hashes.Hash(_hashes.SHA1())
    digest.update(data)
    return base64.b64encode(digest.finalize()).decode()


def _sign_rsa_sha1(private_key, data: bytes) -> str:
    signature = private_key.sign(data, padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(signature).decode()


def build_signed_timestamp_security_header(private_key, cer_bytes: bytes):
    """Construye el <wsse:Security> con Timestamp firmado (XMLDSig), tal como
    lo exige el paso de Autenticación del SAT. Devuelve el elemento lxml
    listo para insertarse en el <soapenv:Header>."""
    ts_id = f"_TS-{uuid.uuid4()}"
    now = datetime.utcnow()
    created = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    expires = (now + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    wsu = NS['wsu']
    timestamp = etree.Element(f'{{{wsu}}}Timestamp', nsmap={'wsu': wsu})
    timestamp.set(f'{{{wsu}}}Id', ts_id)
    created_el = etree.SubElement(timestamp, f'{{{wsu}}}Created')
    created_el.text = created
    expires_el = etree.SubElement(timestamp, f'{{{wsu}}}Expires')
    expires_el.text = expires

    digest_b64 = _sha1_digest_b64(_c14n(timestamp))

    ds = NS['ds']
    signed_info = etree.Element(f'{{{ds}}}SignedInfo', nsmap={'ds': ds})
    etree.SubElement(signed_info, f'{{{ds}}}CanonicalizationMethod', Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(signed_info, f'{{{ds}}}SignatureMethod', Algorithm=SIG_RSA_SHA1)
    reference = etree.SubElement(signed_info, f'{{{ds}}}Reference', URI=f"#{ts_id}")
    transforms = etree.SubElement(reference, f'{{{ds}}}Transforms')
    etree.SubElement(transforms, f'{{{ds}}}Transform', Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(reference, f'{{{ds}}}DigestMethod', Algorithm=DIGEST_SHA1)
    digest_value = etree.SubElement(reference, f'{{{ds}}}DigestValue')
    digest_value.text = digest_b64

    signature_b64 = _sign_rsa_sha1(private_key, _c14n(signed_info))

    signature = etree.Element(f'{{{ds}}}Signature', nsmap={'ds': ds})
    signature.append(signed_info)
    sig_value = etree.SubElement(signature, f'{{{ds}}}SignatureValue')
    sig_value.text = signature_b64

    bst_id = f"_BST-{uuid.uuid4()}"
    key_info = etree.SubElement(signature, f'{{{ds}}}KeyInfo')
    wsse = NS['wsse']
    str_el = etree.SubElement(key_info, f'{{{wsse}}}SecurityTokenReference', nsmap={'wsse': wsse})
    etree.SubElement(str_el, f'{{{wsse}}}Reference', URI=f"#{bst_id}",
                      ValueType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3")

    security = etree.Element(f'{{{wsse}}}Security', nsmap={'wsse': wsse})
    security.set(f'{{{NS["soapenv"]}}}mustUnderstand', '1')
    bst = etree.SubElement(security, f'{{{wsse}}}BinarySecurityToken', nsmap={'wsu': wsu})
    bst.set('EncodingType', 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary')
    bst.set('ValueType', 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3')
    bst.set(f'{{{wsu}}}Id', bst_id)
    bst.text = base64.b64encode(cer_bytes).decode()
    security.append(timestamp)
    security.append(signature)
    return security


def sign_enveloped(element, private_key, cert, cer_bytes: bytes):
    """Firma XMLDSig "envuelta" (enveloped-signature): el <Signature> se
    inserta COMO HIJO del propio elemento que firma. Lo exige el SAT para el
    nodo <des:solicitud> de SolicitaDescarga (no solo el Timestamp del sobre
    SOAP). El digest se calcula sobre el elemento SIN el <Signature> (todavía
    no insertado), canonicalizado."""
    digest_b64 = _sha1_digest_b64(_c14n(element))

    ds = NS['ds']
    signed_info = etree.Element(f'{{{ds}}}SignedInfo', nsmap={'ds': ds})
    etree.SubElement(signed_info, f'{{{ds}}}CanonicalizationMethod', Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(signed_info, f'{{{ds}}}SignatureMethod', Algorithm=SIG_RSA_SHA1)
    reference = etree.SubElement(signed_info, f'{{{ds}}}Reference', URI="")
    transforms = etree.SubElement(reference, f'{{{ds}}}Transforms')
    etree.SubElement(transforms, f'{{{ds}}}Transform',
                      Algorithm='http://www.w3.org/2000/09/xmldsig#enveloped-signature')
    etree.SubElement(transforms, f'{{{ds}}}Transform', Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(reference, f'{{{ds}}}DigestMethod', Algorithm=DIGEST_SHA1)
    digest_value = etree.SubElement(reference, f'{{{ds}}}DigestValue')
    digest_value.text = digest_b64

    signature_b64 = _sign_rsa_sha1(private_key, _c14n(signed_info))

    signature = etree.SubElement(element, f'{{{ds}}}Signature', nsmap={'ds': ds} if element.nsmap.get(None) is None else None)
    signature.append(signed_info)
    sig_value = etree.SubElement(signature, f'{{{ds}}}SignatureValue')
    sig_value.text = signature_b64

    key_info = etree.SubElement(signature, f'{{{ds}}}KeyInfo')
    x509_data = etree.SubElement(key_info, f'{{{ds}}}X509Data')
    issuer_serial = etree.SubElement(x509_data, f'{{{ds}}}X509IssuerSerial')
    issuer_name = etree.SubElement(issuer_serial, f'{{{ds}}}X509IssuerName')
    issuer_name.text = cert.issuer.rfc4514_string()
    serial_number = etree.SubElement(issuer_serial, f'{{{ds}}}X509SerialNumber')
    serial_number.text = str(cert.serial_number)
    x509_cert = etree.SubElement(x509_data, f'{{{ds}}}X509Certificate')
    x509_cert.text = base64.b64encode(cer_bytes).decode()
    return element


def _soap_call(url, soap_action, body_xml_str, headers_extra=None, auth_token=None):
    """auth_token: si se da, se manda como 'Authorization: WRAP access_token=...'
    (protocolo WRAP/SWT de Azure ACS, NO un Bearer/JWT estándar). Es el
    formato real que usa el SAT en Solicita/Verifica/Descarga — el token que
    entrega Autenticación es un SWT (contiene un "&wrap_subject=..." al
    final), y el propio SOAP Fault "MustUnderstand" del servicio confirma
    que NO espera el token dentro de <wsse:Security> como BinarySecurityToken."""
    headers = {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': soap_action,
    }
    if auth_token:
        headers['Authorization'] = f'WRAP access_token="{auth_token}"'
    if headers_extra:
        headers.update(headers_extra)
    try:
        response = requests.post(url, data=body_xml_str.encode('utf-8'), headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise UserError(f"No se pudo contactar al SAT ({url}): {e}") from e
    return response
