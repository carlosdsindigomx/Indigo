import logging
import secrets
import string
import uuid
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import trial

_logger = logging.getLogger(__name__)

TOKEN_VALIDITY_DAYS = 30
# Días que el cliente sigue funcionando tras vencer el token sin poder
# renovarlo (servidor caído, sin internet...). Va firmado en el token. Las
# pruebas no tienen: terminan el día que terminan.
GRACE_DAYS = 15


class DsiLicense(models.Model):
    _name = 'dsi.license'
    _description = "Licencia DS Indigo"
    _inherit = ['mail.thread']
    _rec_name = 'key'

    key = fields.Char(
        required=True, copy=False, readonly=True, tracking=True,
        default=lambda self: self._generate_key())
    partner_id = fields.Many2one('res.partner', string="Cliente", required=True, tracking=True)
    product_id = fields.Many2one('dsi.license.product', string="Producto", required=True, tracking=True)
    edition = fields.Selection([
        ('basic', "Basic"),
        ('pro', "Pro"),
    ], default='pro', required=True, tracking=True)
    license_type = fields.Selection([
        ('subscription', "Suscripción"),
        ('perpetual', "Perpetua"),
        ('trial', "Prueba"),
    ], default='subscription', required=True, tracking=True,
        help="Las pruebas las crea sola la base del cliente al pedirla (ver Productos > "
             "Permitir prueba): quedan amarradas a la dirección de esa base y sin días de gracia.")
    rfc_authorized = fields.Char(string="RFC autorizado", required=True, tracking=True)
    date_start = fields.Date(default=fields.Date.context_today, required=True)
    date_end = fields.Date(string="Vigente hasta", tracking=True,
                            help="Vacío = licencia perpetua sin fecha de expiración.")
    max_instances = fields.Integer(default=2, required=True,
                                    help="Cupo de activaciones simultáneas (ej. producción + pruebas).")
    odoo_versions = fields.Char(default="19.0", required=True,
                                 help="Versiones de Odoo permitidas para esta licencia, separadas por coma.")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('active', "Activa"),
        ('suspended', "Suspendida"),
        ('expired', "Expirada"),
        ('revoked', "Revocada"),
    ], default='draft', required=True, tracking=True)
    instance_ids = fields.One2many('dsi.license.instance', 'license_id', string="Instancias")
    instance_count = fields.Integer(compute='_compute_instance_count')
    active_instance_count = fields.Integer(compute='_compute_instance_count')
    log_ids = fields.One2many('dsi.license.log', 'license_id', string="Bitácora")
    sale_reference = fields.Char(string="Referencia comercial",
                                  help="Folio de la orden de venta o suscripción de origen (texto libre).")

    _key_uniq = models.Constraint('unique(key)', "Ya existe una licencia con esta clave.")
    _product_rfc_uniq = models.Constraint(
        'unique(product_id, rfc_authorized)',
        "Ya existe una licencia de este producto para este RFC.")

    @api.model
    def _generate_key(self):
        while True:
            groups = ['DSI'] + [
                ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
                for _ in range(3)
            ]
            key = '-'.join(groups)
            if not self.search_count([('key', '=', key)]):
                return key

    @api.depends('instance_ids.state')
    def _compute_instance_count(self):
        for license in self:
            license.instance_count = len(license.instance_ids)
            license.active_instance_count = len(license.instance_ids.filtered(lambda i: i.state == 'active'))

    def action_activate(self):
        self.write({'state': 'active'})

    def action_suspend(self):
        self.write({'state': 'suspended'})

    def action_revoke(self):
        self.write({'state': 'revoked'})
        self.instance_ids.filtered(lambda i: i.state == 'active').write({'state': 'inactive'})

    def action_reset_to_draft(self):
        self.write({'state': 'draft'})

    # -------------------------------------------------------------------
    # Lógica usada por los endpoints (controllers/main.py)
    # -------------------------------------------------------------------

    def _check_usable(self, odoo_version):
        """Lanza UserError con un mensaje claro si la licencia no se puede usar ahora mismo."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_("La licencia no está activa (estado: %s).", dict(self._fields['state'].selection).get(self.state)))
        # Comparación en UTC, no en la tz del usuario que dispara la llamada
        # (los endpoints corren con auth='none', sin usuario real detrás).
        if self.date_end and datetime.utcnow().date() > self.date_end:
            self.state = 'expired'
            raise UserError(_("La licencia expiró el %s.", self.date_end))
        allowed_versions = [v.strip() for v in (self.odoo_versions or '').split(',') if v.strip()]
        # Normaliza sufijos de build como "19.0+e" (enterprise) o "19.0-20260101" antes de comparar.
        normalized_version = (odoo_version or '').split('+')[0].split('-')[0]
        if allowed_versions and normalized_version not in allowed_versions:
            raise UserError(_("Esta licencia no cubre la versión de Odoo %s.", odoo_version))

    def _find_or_create_instance(self, db_uuid, base_url, odoo_version, module_version, is_production):
        self.ensure_one()
        instance = self.instance_ids.filtered(lambda i: i.database_uuid == db_uuid)
        if instance:
            instance.write({
                'base_url': base_url or instance.base_url,
                'odoo_version': odoo_version,
                'module_version': module_version,
                'is_production': is_production,
                'state': 'active',
            })
            return instance
        active_count = len(self.instance_ids.filtered(lambda i: i.state == 'active'))
        if active_count >= self.max_instances:
            raise UserError(_(
                "Esta licencia ya tiene %(count)s instancias activas (cupo: %(max)s). "
                "Libera una instancia desde el portal antes de activar una nueva.",
                count=active_count, max=self.max_instances,
            ))
        return self.env['dsi.license.instance'].create({
            'license_id': self.id,
            'database_uuid': db_uuid,
            'base_url': base_url,
            'odoo_version': odoo_version,
            'module_version': module_version,
            'is_production': is_production,
            'state': 'active',
        })

    def _build_token_payload(self, instance=None):
        self.ensure_one()
        token_expires = datetime.utcnow().date() + timedelta(days=TOKEN_VALIDITY_DAYS)
        if self.date_end:
            # Nunca más allá de la licencia. Antes el token duraba 30 días
            # siempre, así que una licencia (o una prueba) de 7 días funcionaba
            # 30 más los de gracia.
            token_expires = min(token_expires, self.date_end)
        es_prueba = self.license_type == 'trial'
        payload = {
            'license_key': self.key,
            'rfc': self.rfc_authorized,
            'edition': self.edition,
            'license_type': self.license_type,
            'features': ['vault', 'reconcile'] if self.edition == 'pro' else ['vault'],
            'license_end': self.date_end.isoformat() if self.date_end else None,
            'token_expires': token_expires.isoformat(),
            'grace_days': 0 if es_prueba else GRACE_DAYS,
            'nonce': uuid.uuid4().hex,
        }
        if instance:
            # Amarrado a la base; las pruebas, además, a su dirección. Las
            # pagadas no: en Odoo.sh cada build de staging cambia de URL.
            payload['db_uuid'] = instance.database_uuid
            if es_prueba:
                payload['base_url'] = instance.base_url
        return payload

    # -------------------------------------------------------------------
    # Prueba de autoservicio (/dsi/license/v1/trial)
    # -------------------------------------------------------------------

    @api.model
    def _trial_test_mode(self):
        """Solo para probar en local: acepta bases en http y en direcciones
        privadas. Nunca debe quedar activo en producción."""
        cp = self.env['ir.config_parameter'].sudo()
        valor = (cp.get_str('dsi_license_server.trial_test_mode', '')
                 if hasattr(cp, 'get_str') else cp.get_param('dsi_license_server.trial_test_mode'))
        if valor == '1':
            _logger.warning("dsi_license_server.trial_test_mode está activo: la prueba acepta bases "
                            "locales y sin https. Desactívalo fuera de pruebas.")
            return True
        return False

    @api.model
    def _start_trial(self, data):
        """Crea la licencia de prueba que pide una base. Devuelve (licencia,
        instancia). Lanza UserError si no procede: una prueba por RFC, por base
        y por dirección, y solo si la base contesta el reto desde su URL."""
        product = self.env['dsi.license.product'].search([
            ('code', '=', data.get('product_code')), ('allow_trial', '=', True)], limit=1)
        if not product:
            raise UserError(_("La prueba no está disponible para este producto."))
        rfc = str(data.get('rfc') or '').strip().upper()
        if not trial.RFC_RE.match(rfc):
            raise UserError(_("El RFC %s no es válido.", rfc))
        db_uuid = str(data.get('db_uuid') or '').strip()
        base_url = str(data.get('base_url') or '').strip()
        challenge_id = str(data.get('challenge_id') or '')
        challenge_secret = str(data.get('challenge_secret') or '')
        if not (db_uuid and challenge_id and challenge_secret):
            raise UserError(_("La solicitud de prueba está incompleta."))
        modo_prueba = self._trial_test_mode()
        host = trial.validar_direccion(base_url, modo_prueba=modo_prueba)

        if self.search_count([('product_id', '=', product.id), ('rfc_authorized', '=ilike', rfc)]):
            raise UserError(_(
                "El RFC %s ya usó su prueba o ya tiene una licencia de este producto. "
                "Escríbenos para contratarla.", rfc))
        if self.env['dsi.license.instance'].search_count([
                ('license_id.product_id', '=', product.id), ('database_uuid', '=', db_uuid)]):
            raise UserError(_("Esta base de datos ya usó su prueba o ya tiene una licencia."))
        pruebas = self.search([('product_id', '=', product.id), ('license_type', '=', 'trial')])
        if any(trial.host_de(i.base_url) == host for i in pruebas.instance_ids):
            raise UserError(_("La dirección %s ya usó su prueba.", host))

        trial.verificar_reto(base_url, challenge_id, challenge_secret, modo_prueba=modo_prueba)

        hoy = datetime.utcnow().date()
        odoo_version = str(data.get('odoo_version') or '')
        license = self.create({
            'partner_id': self._trial_partner(rfc, data.get('company_name')).id,
            'product_id': product.id,
            'edition': 'pro',
            'license_type': 'trial',
            'rfc_authorized': rfc,
            'date_start': hoy,
            'date_end': hoy + timedelta(days=product.trial_days),
            'max_instances': 1,
            'odoo_versions': odoo_version.split('+')[0].split('-')[0],
            'state': 'active',
            'sale_reference': _("Prueba pedida desde %s", base_url),
        })
        instance = license._find_or_create_instance(
            db_uuid=db_uuid, base_url=base_url, odoo_version=odoo_version,
            module_version=data.get('module_version'), is_production=True)
        license.message_post(body=_(
            "Prueba de %(dias)s días pedida desde %(url)s; la dirección se comprobó con el reto.",
            dias=product.trial_days, url=base_url))
        return license, instance

    @api.model
    def _trial_partner(self, rfc, company_name):
        """El contacto de quien pide la prueba: el que ya tenga ese RFC o uno
        nuevo con el nombre de su empresa."""
        Partner = self.env['res.partner']
        partner = Partner.search([('vat', '=ilike', rfc)], limit=1)
        if partner:
            return partner
        valores = {
            'name': str(company_name or '').strip()[:128] or rfc,
            'is_company': True,
            'country_id': self.env.ref('base.mx').id,
        }
        try:
            with self.env.cr.savepoint():
                return Partner.create({**valores, 'vat': rfc})
        except ValidationError:
            # Si la validación de RFC de Odoo no lo acepta, se crea sin él: el
            # RFC queda de todos modos en la licencia.
            return Partner.create(valores)

    def _check_trial_activation(self, db_uuid, base_url):
        """Una prueba solo se reactiva en la misma base y la misma dirección
        que la pidió; no se puede llevar a otra con /activate."""
        self.ensure_one()
        if self.license_type != 'trial':
            return
        instancia = self.instance_ids.filtered(lambda i: i.database_uuid == db_uuid)
        if not instancia or trial.host_de(instancia.base_url) != trial.host_de(base_url):
            raise UserError(_("Esta licencia de prueba solo se puede usar en la base que la pidió."))

    def action_convert_to_subscription(self):
        """Al contratar: la misma licencia (misma clave) pasa a suscripción. El
        cliente recibe el token nuevo en su siguiente renovación diaria, o al
        momento si pulsa Activar; no tiene que capturar nada."""
        hoy = fields.Date.context_today(self)
        for license in self.filtered(lambda l: l.license_type == 'trial'):
            license.write({
                'license_type': 'subscription',
                'date_end': max(license.date_end or hoy, hoy) + timedelta(days=365),
                'max_instances': 2,
                'state': 'active',
            })
            license.message_post(body=_("Prueba convertida en suscripción."))
