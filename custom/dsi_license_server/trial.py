"""Comprobaciones de la prueba de autoservicio: que la base que la pide esté
en internet y que la solicitud venga de verdad de la dirección que declara.

El servidor hace una petición saliente a una dirección que le dio un
desconocido, así que se acota todo lo posible: solo https al puerto 443, solo
direcciones públicas (nunca la red interna de este servidor), sin seguir
redirecciones y leyendo a lo más unos cientos de bytes.
"""
import hmac
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import requests

from odoo import _
from odoo.exceptions import UserError

RFC_RE = re.compile(r'^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$')
CHALLENGE_PATH = '/dsi/license/v1/challenge'
CHALLENGE_TIMEOUT = (5, 10)
MAX_RESPUESTA = 512


def host_de(url):
    """El host de una URL en minúsculas, con el puerto solo si no es el de
    omisión. Debe coincidir con dsi_license_client (_host)."""
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return host


def _direcciones(host):
    """Las IP a las que resuelve un nombre."""
    return {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}


def validar_direccion(base_url, modo_prueba=False):
    """Devuelve el host si la base está en https://<nombre público>; si no,
    UserError. En modo de prueba (solo para probar en local) acepta http y
    direcciones locales."""
    parts = urlsplit((base_url or '').strip())
    host = (parts.hostname or '').lower()
    if not host or parts.scheme not in ('http', 'https'):
        raise UserError(_("La dirección de la base no es válida: %s", base_url))
    if modo_prueba:
        return host_de(base_url)
    if parts.scheme != 'https' or parts.port not in (None, 443):
        raise UserError(_(
            "La prueba solo se da a bases accesibles desde internet con https "
            "(puerto 443). La dirección recibida es %s.", base_url))
    try:
        direcciones = _direcciones(host)
    except (socket.gaierror, UnicodeError):
        raise UserError(_("No se encontró la dirección %s en internet.", host)) from None
    for direccion in direcciones:
        if not ipaddress.ip_address(direccion.split('%')[0]).is_global:
            raise UserError(_("La dirección %s no es accesible desde internet.", host))
    return host_de(base_url)


def verificar_reto(base_url, challenge_id, challenge_secret, modo_prueba=False):
    """Pide a la base el reto de la solicitud y comprueba que contesta el
    secreto que viajó en ella. Solo quien controla esa dirección puede hacer
    que conteste ese secreto: quien escriba a mano la URL de otro, no."""
    parts = urlsplit(base_url.strip())
    origen = f"{parts.scheme}://{parts.netloc}"
    try:
        with requests.get(f"{origen}{CHALLENGE_PATH}", params={'id': challenge_id},
                          timeout=CHALLENGE_TIMEOUT, allow_redirects=False, stream=True) as respuesta:
            contenido = respuesta.raw.read(MAX_RESPUESTA + 1, decode_content=True) or b''
            estatus = respuesta.status_code
    except requests.RequestException as e:
        raise UserError(_(
            "No se pudo contactar a %(origen)s para comprobar la solicitud: %(error)s",
            origen=origen, error=e)) from e
    texto = contenido.decode('utf-8', 'replace').strip()
    if estatus != 200 or len(contenido) > MAX_RESPUESTA or not hmac.compare_digest(texto.encode(), challenge_secret.encode()):
        raise UserError(_(
            "No se pudo comprobar que la solicitud viene de %s. La prueba solo se da a la "
            "base que la pide, desde su propia dirección.", origen))
