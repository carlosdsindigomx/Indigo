"""Firma de tokens de licencia con Ed25519.

La llave privada se busca, en este orden:

  1. `odoo.conf`, parámetro `dsi_license_private_key`
  2. variable de entorno `DSI_LICENSE_PRIVATE_KEY`
  3. parámetro de sistema `dsi_license_private_key` (ir.config_parameter)

Siempre en base64 (32 bytes crudos de la llave).

Las dos primeras son las preferibles porque dejan la llave FUERA de la base
de datos y por tanto fuera de los respaldos. La tercera existe por Odoo.sh,
donde ninguna de las dos es viable: la plataforma gestiona el `odoo.conf`,
el contenedor se reconstruye en cada push y no hay forma de definir
variables de entorno propias. Ahí la llave tiene que vivir en la base.

Si usas la opción 3, ten presente que la llave queda en cada respaldo de la
base. Quien tenga administrador en este Odoo ya podía emitir licencias desde
la interfaz, así que el riesgo adicional es sobre todo el de los respaldos
que se copian fuera del servidor: trátalos como material sensible.

Genera un par de llaves nuevo con:

    python3 -c "
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    import base64
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub_raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print('private (odoo.conf / env):', base64.b64encode(priv_raw).decode())
    print('public (embebida en dsi_license_client):', base64.b64encode(pub_raw).decode())
    "
"""
import base64
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odoo.tools import config


class DsiLicenseKeyError(Exception):
    pass


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _b64u_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def get_private_key(env=None) -> Ed25519PrivateKey:
    """Carga la llave privada de firma. Ver el orden de búsqueda arriba.

    :param env: entorno de Odoo. Hace falta para el tercer origen
        (ir.config_parameter); sin él solo se consultan odoo.conf y el
        entorno del proceso.
    """
    key_b64 = config.get('dsi_license_private_key') or os.environ.get('DSI_LICENSE_PRIVATE_KEY')
    if not key_b64 and env is not None:
        # sudo(): los endpoints de licenciamiento corren con auth='none', sin
        # usuario con permisos para leer parámetros de sistema.
        cp = env['ir.config_parameter'].sudo()
        # Odoo 20 sustituyó get_param por accesores tipados (get_str).
        key_b64 = (cp.get_str('dsi_license_private_key', '')
                   if hasattr(cp, 'get_str')
                   else cp.get_param('dsi_license_private_key'))
    if not key_b64:
        raise DsiLicenseKeyError(
            "Falta la llave privada de licenciamiento. Defínela en odoo.conf "
            "(dsi_license_private_key = <base64>), en la variable de entorno "
            "DSI_LICENSE_PRIVATE_KEY, o —si estás en Odoo.sh— como parámetro "
            "de sistema dsi_license_private_key."
        )
    try:
        raw = base64.b64decode(key_b64)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as e:
        raise DsiLicenseKeyError(f"Llave privada de licenciamiento inválida: {e}") from e


def sign_token(payload: dict, env=None) -> str:
    """Firma un payload de licencia. Retorna '<payload_b64url>.<firma_b64url>'.

    :param env: se reenvía a get_private_key para poder leer la llave del
        parámetro de sistema cuando no está en odoo.conf ni en el entorno
        (caso Odoo.sh).
    """
    priv = get_private_key(env=env)
    payload_bytes = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    payload_b64 = _b64u_encode(payload_bytes)
    signature = priv.sign(payload_b64.encode())
    return f"{payload_b64}.{_b64u_encode(signature)}"


def decode_token_unsafe(token: str) -> dict:
    """Decodifica el payload de un token SIN verificar la firma (solo para inspección/UI)."""
    payload_b64, _, _sig = token.partition('.')
    return json.loads(_b64u_decode(payload_b64))
