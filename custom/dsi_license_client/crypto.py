"""Verificación local de tokens de licencia firmados con Ed25519.

La llave pública está embebida aquí a propósito: verificar la firma de un
token NUNCA requiere red. Debe coincidir con la llave privada configurada
en el servidor (dsi_license_server/crypto.py).

Esta es la llave pública de PRODUCCIÓN de DS Indigo. Su contraparte privada
vive únicamente en el servidor de licencias y nunca sale de ahí. Publicar la
pública no supone riesgo: solo sirve para VERIFICAR firmas, no para crearlas.
"""
import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Llave pública de producción. Debe corresponder a la privada configurada en
# el servidor de licencias; si no, todo token se rechaza por firma inválida.
DSI_LICENSE_PUBLIC_KEY_B64 = "47kQrQGbhYM9EZ7Ldgfus39b63s+fW5EVC2IsveArBM="


def _b64u_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def verify_and_decode_token(token: str) -> dict:
    """Verifica la firma del token contra la llave pública embebida.

    :return: el payload (dict) si la firma es válida.
    :raises ValueError: si el token está mal formado o la firma no coincide.
    """
    if not token or '.' not in token:
        raise ValueError("Token de licencia mal formado.")
    payload_b64, _, sig_b64 = token.partition('.')
    try:
        signature = _b64u_decode(sig_b64)
        pub_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(DSI_LICENSE_PUBLIC_KEY_B64))
        pub_key.verify(signature, payload_b64.encode())
        return json.loads(_b64u_decode(payload_b64))
    except InvalidSignature as e:
        raise ValueError("La firma del token de licencia no es válida.") from e
    except Exception as e:
        raise ValueError(f"No se pudo leer el token de licencia: {e}") from e
