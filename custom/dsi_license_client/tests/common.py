"""Licencias firmadas para las pruebas.

La llave privada de producción vive solo en el servidor de licencias, así que
aquí se genera un par temporal y, mientras corre la prueba, el cliente
verifica contra esa llave pública en lugar de la de producción. Lo usan
también las pruebas de dsi_cfdi_vault y dsi_cfdi_sat_sync que necesitan una
licencia vigente para importar, descargar del SAT o convertir a factura.
"""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odoo import fields

from odoo.addons.dsi_license_client import crypto

_LLAVE_PRIVADA = Ed25519PrivateKey.generate()
LLAVE_PUBLICA_B64 = base64.b64encode(_LLAVE_PRIVADA.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
CLAVE_DE_PRUEBA = 'DSI-TEST-TEST-TEST'


def _b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def firmar(payload):
    """Firma igual que dsi_license_server/crypto.py:sign_token."""
    payload_b64 = _b64u(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode())
    return f"{payload_b64}.{_b64u(_LLAVE_PRIVADA.sign(payload_b64.encode()))}"


def token_de_prueba(env, dias=30, **extra):
    """Un token como los que emite el servidor, vigente `dias` días desde hoy
    (negativo para uno ya vencido) y amarrado a esta base."""
    payload = {
        'license_key': CLAVE_DE_PRUEBA,
        'rfc': 'EKU9003173C9',
        'edition': 'pro',
        'features': ['vault', 'reconcile'],
        'license_end': None,
        'token_expires': (fields.Date.today() + timedelta(days=dias)).isoformat(),
        'db_uuid': env['dsi.license.client.state']._get_db_uuid(),
        'nonce': 'prueba',
    }
    payload.update(extra)
    return firmar(payload)


def usar_llave_de_prueba(cls):
    """En setUpClass: el cliente verifica con la llave temporal."""
    cls.startClassPatcher(patch.object(crypto, 'DSI_LICENSE_PUBLIC_KEY_B64', LLAVE_PUBLICA_B64))


def activar_licencia(cls, company=None, **extra):
    """En setUpClass: deja una licencia vigente para la empresa."""
    usar_llave_de_prueba(cls)
    estado = cls.env['dsi.license.client.state']._get_or_create(company)
    estado.write({'license_key': CLAVE_DE_PRUEBA, 'token': token_de_prueba(cls.env, **extra)})
    return estado
