"""Cifrado en reposo de la e.firma (.key y contraseña).

La llave maestra vive SOLO en odoo.conf o en la variable de entorno
DSI_SAT_CREDENTIAL_MASTER_KEY — nunca en la base de datos. Sin ella, el
contenido cifrado de sat.credential es indescifrable incluso con acceso
completo a un respaldo de la base de datos.

Generación de una llave nueva (una sola vez, por instalación):

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Luego, en odoo.conf:

    [options]
    dsi_sat_credential_master_key = <la llave generada>

O como variable de entorno:

    export DSI_SAT_CREDENTIAL_MASTER_KEY="<la llave generada>"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

from odoo.tools import config


class DsiSatCredentialKeyError(Exception):
    """La llave maestra de cifrado no está configurada en este servidor."""


def _get_master_key(env=None):
    """La llave maestra que cifra la e.firma.

    Orden de búsqueda: odoo.conf, variable de entorno y —solo si se pasa
    `env`— el parámetro de sistema `dsi_sat_credential_master_key`.

    ⚠️  ADVERTENCIA SOBRE EL TERCER ORIGEN. La e.firma cifrada vive en la
    base de datos. Si la llave que la descifra se guarda TAMBIÉN en la base,
    las dos acaban juntas en cada respaldo y el cifrado deja de proteger
    frente a quien obtenga uno: tendría la identidad legal del contribuyente
    ante el SAT. Úsalo únicamente donde no haya alternativa (Odoo.sh gestiona
    el odoo.conf, reconstruye el contenedor en cada push y no admite
    variables de entorno propias) y preferiblemente solo con e.firmas de
    prueba. Para custodiar la e.firma de un cliente real, el servidor debe
    permitir configurar la llave fuera de la base.
    """
    key = config.get('dsi_sat_credential_master_key') or os.environ.get('DSI_SAT_CREDENTIAL_MASTER_KEY')
    if not key and env is not None:
        cp = env['ir.config_parameter'].sudo()
        # Odoo 20 sustituyó get_param por accesores tipados.
        key = (cp.get_str('dsi_sat_credential_master_key', '')
               if hasattr(cp, 'get_str')
               else cp.get_param('dsi_sat_credential_master_key'))
    if not key:
        raise DsiSatCredentialKeyError(
            "Falta configurar 'dsi_sat_credential_master_key' en odoo.conf, en la variable "
            "de entorno DSI_SAT_CREDENTIAL_MASTER_KEY, o —si estás en Odoo.sh— como "
            "parámetro de sistema, antes de guardar una e.firma.")
    return key.encode() if isinstance(key, str) else key


def encrypt_bytes(plain: bytes, env=None) -> bytes:
    return Fernet(_get_master_key(env)).encrypt(plain)


def decrypt_bytes(token: bytes, env=None) -> bytes:
    try:
        return Fernet(_get_master_key(env)).decrypt(token)
    except InvalidToken as e:
        raise DsiSatCredentialKeyError(
            "No se pudo descifrar la e.firma guardada — la llave maestra de este servidor "
            "no coincide con la que se usó para cifrarla originalmente.") from e
