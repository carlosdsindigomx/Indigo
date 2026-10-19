import base64
import logging
import re

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..crypto import decrypt_bytes, encrypt_bytes

_logger = logging.getLogger(__name__)

_RFC_RE = re.compile(r'\b([A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3})\b')


class SatCredential(models.Model):
    _name = 'sat.credential'
    _description = "e.firma (FIEL) del contribuyente, custodiada de forma cifrada"
    _inherit = ['mail.thread']
    _rec_name = 'rfc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, default=lambda self: self.env.company)
    rfc = fields.Char(string="RFC del certificado", readonly=True, tracking=True)
    serial_number = fields.Char(string="Número de serie", readonly=True)
    cer_file = fields.Binary(string="Certificado (.cer)", readonly=True, attachment=True)
    cer_filename = fields.Char(readonly=True)
    date_valid_from = fields.Date(string="Válido desde", readonly=True)
    date_valid_to = fields.Date(string="Válido hasta", readonly=True, tracking=True)
    # El .key y la contraseña se guardan SIEMPRE cifrados (Fernet, llave maestra
    # fuera de la base de datos — ver crypto.py). No hay ningún campo de texto
    # plano para la contraseña en este modelo.
    key_file_encrypted = fields.Binary(string="Llave privada (cifrada)", readonly=True, groups="dsi_cfdi_sat_sync.group_sat_credential_manager")
    password_encrypted = fields.Binary(string="Contraseña (cifrada)", readonly=True, groups="dsi_cfdi_sat_sync.group_sat_credential_manager")
    state = fields.Selection([
        ('validated', "Validada"),
        ('expired', "Vencida"),
        ('revoked', "Revocada"),
    ], default='validated', required=True, tracking=True)

    _company_uniq = models.Constraint(
        'unique(company_id)', "Ya existe una e.firma registrada para esta empresa. Revócala antes de cargar otra.")

    @api.model
    def _parse_and_validate(self, cer_bytes, key_bytes, password, company):
        """Valida que el .cer y el .key hagan pareja, que la contraseña abra la
        llave, y que el RFC del certificado coincida con el de la compañía.
        Devuelve un dict con los valores ya listos para crear/actualizar el
        registro. No persiste nada — eso lo hace el wizard que llama a esto."""
        try:
            cert = x509.load_der_x509_certificate(cer_bytes)
        except ValueError:
            try:
                cert = x509.load_pem_x509_certificate(cer_bytes)
            except ValueError as e:
                raise UserError(_(
                    "El archivo .cer no es un certificado X.509 válido (se probó DER y PEM).")) from e

        try:
            private_key = serialization.load_der_private_key(key_bytes, password=password.encode())
        except ValueError:
            try:
                private_key = serialization.load_pem_private_key(key_bytes, password=password.encode())
            except ValueError as e:
                raise UserError(_(
                    "No se pudo abrir el archivo .key: la contraseña es incorrecta o el archivo "
                    "no corresponde a una llave privada válida.")) from e

        # Pareja cer/key: deben compartir la misma llave pública.
        cert_public_numbers = cert.public_key().public_numbers()
        key_public_numbers = private_key.public_key().public_numbers()
        if cert_public_numbers != key_public_numbers:
            raise UserError(_(
                "El certificado (.cer) y la llave privada (.key) no corresponden al mismo par — "
                "verifica que subiste los dos archivos de la misma e.firma."))

        subject_str = cert.subject.rfc4514_string()
        rfc_match = _RFC_RE.search(subject_str.upper())
        cert_rfc = rfc_match.group(1) if rfc_match else False

        company_rfc = (company.partner_id.vat or '').strip().upper()
        if cert_rfc and company_rfc and cert_rfc != company_rfc:
            raise UserError(_(
                "El RFC del certificado (%(cert_rfc)s) no coincide con el RFC de la compañía "
                "(%(company_rfc)s). No se puede registrar una e.firma de otro contribuyente.",
                cert_rfc=cert_rfc, company_rfc=company_rfc,
            ))
        if not cert_rfc:
            _logger.warning(
                "No se pudo extraer el RFC del subject del certificado (%s); se guarda sin "
                "verificar contra el RFC de la compañía.", subject_str)

        not_valid_before = cert.not_valid_before_utc.date()
        not_valid_after = cert.not_valid_after_utc.date()
        today = fields.Date.today()
        if today > not_valid_after:
            raise UserError(_(
                "Este certificado venció el %s — el SAT no aceptará solicitudes firmadas con él. "
                "Descarga tu e.firma vigente desde el portal del SAT.", not_valid_after))

        return {
            'rfc': cert_rfc or company_rfc,
            'serial_number': format(cert.serial_number, 'x').upper(),
            'date_valid_from': not_valid_before,
            'date_valid_to': not_valid_after,
            'cer_file': base64.b64encode(cer_bytes),
            'key_file_encrypted': base64.b64encode(encrypt_bytes(key_bytes, self.env)),
            'password_encrypted': base64.b64encode(encrypt_bytes(password.encode(), self.env)),
            'state': 'validated',
        }

    def _get_private_key(self):
        """Descifra y carga la llave privada en memoria — solo para uso interno
        al firmar una petición SOAP. Nunca se expone fuera de este método."""
        self.ensure_one()
        key_bytes = decrypt_bytes(base64.b64decode(self.key_file_encrypted), self.env)
        password = decrypt_bytes(base64.b64decode(self.password_encrypted), self.env)
        return serialization.load_der_private_key(key_bytes, password=password)

    def _get_cer_bytes(self):
        self.ensure_one()
        return base64.b64decode(self.cer_file)

    def action_revoke(self):
        self.write({
            'state': 'revoked',
            'key_file_encrypted': False,
            'password_encrypted': False,
        })

    @api.model
    def _cron_check_expiry(self):
        today = fields.Date.today()
        expiring = self.search([('state', '=', 'validated'), ('date_valid_to', '<', today)])
        expiring.write({'state': 'expired'})
