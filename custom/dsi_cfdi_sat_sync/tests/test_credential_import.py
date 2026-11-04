"""Prueba de la carga de la e.firma por el wizard, con un .cer y una .key como
los que entrega el SAT.

Existe porque la carga tronó en Odoo.sh con «'Certificate' object has no
attribute 'not_valid_before_utc'»: Odoo.sh corre con cryptography 41, y esas
fechas solo existen desde la 42. Ninguna prueba subía una e.firma de verdad, y
en local, con la 42, todo pasaba.
"""
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from odoo.tests.common import TransactionCase, tagged

RFC = 'AAA010101AAA'
PASSWORD = '12345678a'
CARGA_DEL_CER = 'odoo.addons.dsi_cfdi_sat_sync.models.sat_credential.x509.load_der_x509_certificate'


class CertificadoSinUtc:
    """Un certificado como los de cryptography anterior a 42: sin
    not_valid_*_utc, solo con las fechas sin zona horaria (en UTC)."""

    def __init__(self, cert):
        self._cert = cert

    def __getattr__(self, nombre):
        if nombre.endswith('_utc'):
            raise AttributeError(nombre)
        if nombre in ('not_valid_before', 'not_valid_after'):
            return getattr(self._cert, f'{nombre}_utc').replace(tzinfo=None)
        return getattr(self._cert, nombre)


@tagged('post_install', '-at_install')
class TestCredentialImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].create({'name': "Prueba e.firma", 'vat': RFC})
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "PRUEBA SA DE CV"),
            x509.NameAttribute(NameOID.X500_UNIQUE_IDENTIFIER, RFC),
        ])
        now = datetime.now(timezone.utc)
        cls.desde = (now - timedelta(days=1)).date()
        cls.hasta = (now + timedelta(days=365)).date()
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=365))
                .sign(key, hashes.SHA256()))
        # Mismo formato que entrega el SAT: .cer en DER y .key PKCS#8 cifrada en DER.
        cls.cer_bytes = cert.public_bytes(serialization.Encoding.DER)
        cls.key_bytes = key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSWORD.encode()))

    def _wizard(self, **kwargs):
        # El cliente web manda los archivos como base64 en texto.
        values = {
            'company_id': self.company.id,
            'cer_file': base64.b64encode(self.cer_bytes).decode(),
            'cer_filename': 'prueba.cer',
            'key_file': base64.b64encode(self.key_bytes).decode(),
            'key_filename': 'prueba.key',
            'password': PASSWORD,
        }
        values.update(kwargs)
        return self.env['sat.credential.import.wizard'].create(values)

    def _cargar(self):
        with patch.dict('os.environ', {'DSI_SAT_CREDENTIAL_MASTER_KEY': Fernet.generate_key().decode()}):
            action = self._wizard().action_import()
            return self.env['sat.credential'].browse(action['res_id'])

    def test_carga_y_lectura(self):
        with patch.dict('os.environ', {'DSI_SAT_CREDENTIAL_MASTER_KEY': Fernet.generate_key().decode()}):
            action = self._wizard().action_import()
            credential = self.env['sat.credential'].browse(action['res_id'])
            self.assertEqual(credential.rfc, RFC)
            self.assertEqual((credential.date_valid_from, credential.date_valid_to), (self.desde, self.hasta))
            self.assertEqual(credential._get_cer_bytes(), self.cer_bytes)
            self.assertEqual(
                credential._get_private_key().public_key().public_numbers(),
                x509.load_der_x509_certificate(self.cer_bytes).public_key().public_numbers())

    def test_carga_con_cryptography_anterior_a_42(self):
        # Como en Odoo.sh: el certificado no trae not_valid_before_utc.
        original = x509.load_der_x509_certificate
        with patch(CARGA_DEL_CER, side_effect=lambda datos: CertificadoSinUtc(original(datos))):
            credential = self._cargar()
        self.assertEqual(credential.state, 'validated')
        self.assertEqual((credential.date_valid_from, credential.date_valid_to), (self.desde, self.hasta))
