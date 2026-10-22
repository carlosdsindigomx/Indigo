import ipaddress
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from markupsafe import Markup

from odoo import _, api, fields, models, release
from odoo.exceptions import RedirectWarning, UserError
from odoo.tools import format_date

from ..crypto import verify_and_decode_token

_logger = logging.getLogger(__name__)

# Tolerancia tras el vencimiento cuando el token no dice otra cosa. Los tokens
# nuevos traen 'grace_days' firmado por el servidor (0 en las pruebas); los
# emitidos antes de eso no, y para ellos se conserva lo de siempre.
GRACE_DAYS = 15
REQUEST_TIMEOUT = 5
# La solicitud de prueba tarda más: antes de contestar, el servidor de
# licencias llama de vuelta a esta base para comprobar que la URL es suya.
TRIAL_REQUEST_TIMEOUT = (5, 40)
# Cuánto vive el reto de una solicitud de prueba.
TRIAL_CHALLENGE_MINUTES = 15
# Producto que se pide a prueba: el único que se vende con este cliente.
TRIAL_PRODUCT_CODE = 'cfdi_vault'
PRODUCT_NAME = "Bóveda CFDI"
# Días antes del fin de la prueba en que se avisa a los usuarios.
TRIAL_REMINDER_DAYS = (3, 1)

# Dirección del servidor de licencias de DS Indigo. Viene fija de fábrica con
# el módulo — el cliente NUNCA debe verla ni poder editarla desde la interfaz
# (por eso el campo es readonly y no aparece en la vista). Si algún día hay
# que apuntar a otra dirección, se cambia esta constante y se libera una
# actualización del módulo — no es una opción de configuración del cliente.
DEFAULT_ACTIVATION_URL = "https://obi-dsindigo.odoo.com"


def _param(env, clave, por_omision=''):
    """Lee un parámetro de sistema en Odoo 19 y en Odoo 20.

    Odoo 20 sustituyó el `get_param` genérico por accesores tipados
    (`get_str`, `get_int`, `get_bool`, `get_float`). Todos los parámetros que
    usamos son cadenas, así que basta con `get_str` cuando existe.
    """
    cp = env['ir.config_parameter'].sudo()
    if hasattr(cp, 'get_str'):
        return cp.get_str(clave, por_omision)
    return cp.get_param(clave, por_omision)


def _host(url):
    """El host de una URL en minúsculas, con el puerto solo si no es el de
    omisión. Es lo que se compara para amarrar una prueba a la dirección de la
    base: el esquema y la ruta no cuentan."""
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return host


def _is_public_https(url):
    """Revisión rápida, antes de molestar al servidor: https y un nombre que
    no sea local ni una IP privada. La revisión de verdad la hace el servidor
    de licencias, que además resuelve el nombre."""
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or not host:
        return False
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan', '.test')):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return '.' in host


class DsiLicenseClientState(models.Model):
    _name = 'dsi.license.client.state'
    _description = "Estado de licenciamiento DS Indigo de esta base"
    _inherit = ['mail.thread']

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _("Licenciamiento — %s", rec.company_id.name)

    activation_url = fields.Char(
        string="Servidor de licencias", required=True, readonly=True,
        default=lambda self: DEFAULT_ACTIVATION_URL,
        help="Dirección fija del servidor de licencias de DS Indigo. No es configurable por el "
             "cliente: viene de fábrica con el módulo y solo cambia mediante una actualización.")
    license_key = fields.Char(string="Clave de licencia", tracking=True)
    token = fields.Text(string="Token firmado")
    # Copias del contenido del token, solo para mostrarlas. Lo que decide qué se
    # puede hacer se lee siempre del token verificado (ver _verified_payload).
    token_expires = fields.Date(string="Token válido hasta")
    license_end = fields.Date(string="Licencia vigente hasta")
    edition = fields.Char()
    features = fields.Char(help="Lista separada por comas de las funciones habilitadas por el token.")
    # No se guarda: depende de la fecha de hoy. Guardado solo se recalculaba al
    # llegar un token nuevo, así que una licencia vencida o revocada (que ya no
    # recibe tokens) se quedaba "Vigente" para siempre.
    state = fields.Selection([
        ('not_activated', "Sin activar"),
        ('trial', "En prueba"),
        ('active', "Vigente"),
        ('grace', "En gracia"),
        ('restricted', "Restringido"),
    ], compute='_compute_state')
    status_message = fields.Char(string="Situación", compute='_compute_state')
    last_check = fields.Datetime(string="Última verificación")
    last_error = fields.Char(string="Último error", tracking=True)

    # Solicitud de prueba
    trial_state = fields.Selection([
        ('none', "Sin solicitar"),
        ('requested', "Verificando"),
        ('failed', "No se pudo"),
    ], string="Prueba", default='none', required=True, readonly=True)
    trial_error = fields.Char(string="Motivo", readonly=True)
    trial_requested_at = fields.Datetime(readonly=True)
    trial_challenge_id = fields.Char(readonly=True, copy=False)
    trial_challenge_secret = fields.Char(readonly=True, copy=False)
    trial_reminders_sent = fields.Char(
        readonly=True, copy=False,
        help="Avisos de fin de prueba ya enviados (días antes, separados por coma).")

    _company_uniq = models.Constraint(
        'unique(company_id)', "Ya existe un registro de licenciamiento para esta empresa.")

    @api.depends('token')
    def _compute_state(self):
        for rec in self:
            rec.state, rec.status_message = rec._evaluate()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @api.model
    def _get_or_create(self, company=None):
        company = company or self.env.company
        rec = self.sudo().search([('company_id', '=', company.id)], limit=1)
        if not rec:
            rec = self.sudo().create({'company_id': company.id})
        return rec

    def _get_db_uuid(self):
        return _param(self.env, 'database.uuid')

    def _get_base_url(self):
        return _param(self.env, 'web.base.url')

    def _get_module_version(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'dsi_license_client')], limit=1)
        return module.installed_version or False

    def _get_proxies(self):
        """Soporte de proxy corporativo: parámetro de sistema opcional
        'dsi_license_client.proxy_url', usado tanto para http como https."""
        proxy_url = _param(self.env, 'dsi_license_client.proxy_url')
        if not proxy_url:
            return None
        return {'http': proxy_url, 'https': proxy_url}

    def _verified_payload(self):
        """El contenido del token si su firma es válida; None si no hay token o
        no es válido. Las fechas y funciones guardadas en el registro son solo
        para mostrarse: cualquiera con acceso técnico podría editarlas, el
        token firmado no."""
        self.ensure_one()
        if not self.token:
            return None
        try:
            return verify_and_decode_token(self.token)
        except ValueError:
            return None

    def _evaluate(self):
        """(estado, mensaje) según el token verificado y la fecha de hoy."""
        self.ensure_one()
        if not self.token:
            return 'not_activated', _("Esta base no tiene una licencia activada.")
        payload = self._verified_payload()
        if payload is None:
            return 'restricted', _("El token de licencia no es válido.")
        # Token amarrado a la base: copiar el token o la base a otro servidor
        # no la hace válida ahí.
        if payload.get('db_uuid') and payload['db_uuid'] != self._get_db_uuid():
            return 'restricted', _("La licencia está registrada para otra base de datos.")
        # Las pruebas, además, a la dirección de la base que las pidió.
        if payload.get('base_url') and _host(payload['base_url']) != _host(self._get_base_url()):
            return 'restricted', _(
                "La licencia está registrada para %s, no para la dirección de esta base.",
                payload['base_url'])

        # Vence lo que ocurra primero: el token o la licencia. El servidor ya
        # no emite tokens más allá de la licencia, pero los anteriores sí.
        fechas = [fields.Date.to_date(payload.get(clave)) for clave in ('token_expires', 'license_end')]
        fechas = [fecha for fecha in fechas if fecha]
        if not fechas:
            return 'restricted', _("El token de licencia no trae fecha de vigencia.")
        fin = min(fechas)
        es_prueba = payload.get('license_type') == 'trial'
        gracia = int(payload.get('grace_days', GRACE_DAYS))
        # Odoo corre en UTC, así que hoy es el día UTC, no el de la tz del
        # usuario: cerca de medianoche eso adelantaría o atrasaría un día.
        hoy = fields.Date.today()
        if hoy <= fin:
            if es_prueba:
                return 'trial', _("En prueba hasta el %s.", format_date(self.env, fin))
            return 'active', _("Licencia vigente hasta el %s.", format_date(self.env, fin))
        if hoy <= fin + timedelta(days=gracia):
            return 'grace', _(
                "La licencia venció el %(fin)s; sigue funcionando hasta el %(limite)s. "
                "Renuévala para no perder el acceso.",
                fin=format_date(self.env, fin), limite=format_date(self.env, fin + timedelta(days=gracia)))
        if es_prueba:
            return 'restricted', _("La prueba terminó el %s.", format_date(self.env, fin))
        return 'restricted', _("La licencia venció el %s.", format_date(self.env, fin))

    def _apply_token(self, token):
        """Verifica localmente el token y actualiza los campos que se muestran."""
        self.ensure_one()
        try:
            payload = verify_and_decode_token(token)
        except ValueError as e:
            raise UserError(str(e)) from e
        self.write({
            'token': token,
            'token_expires': payload.get('token_expires'),
            'license_end': payload.get('license_end'),
            'edition': payload.get('edition'),
            'features': ','.join(payload.get('features') or []),
            'last_check': fields.Datetime.now(),
            'last_error': False,
        })

    def is_feature_enabled(self, feature):
        """Punto de entrada para que otros módulos (ej. dsi_cfdi_vault) pregunten
        si pueden operar. Nunca bloquea el arranque del módulo: solo se llama
        cuando el usuario ejecuta una acción real."""
        self.ensure_one()
        if self.state not in ('trial', 'active', 'grace'):
            return False
        return feature in (self._verified_payload() or {}).get('features', [])

    @api.model
    def _check_feature(self, feature, action, company=None):
        """Lanza UserError si la licencia de la empresa no permite `action`
        (p. ej. "importar CFDI"). Es el único punto de bloqueo: lo usan la
        importación, la descarga del SAT y la conversión a factura. Consultar y
        exportar lo que ya está guardado nunca pasa por aquí."""
        rec = self._get_or_create(company)
        if rec.is_feature_enabled(feature):
            return
        if rec.state in ('trial', 'active', 'grace'):
            raise UserError(_("No se puede %s: tu licencia no incluye esta función.", action))
        if rec.state == 'not_activated':
            siguiente = _("Inicia la prueba de 7 días, o compra o activa tu licencia, para hacerlo.")
        else:
            siguiente = _("Compra o activa tu licencia para seguir haciéndolo.")
        # La ventana lleva un botón al aviso de licencia (comprar, capturar la
        # clave y activar), en lugar de mandar al usuario a buscar en Ajustes.
        error = RedirectWarning(
            _("No se puede %(action)s. %(motivo)s\n\nLo que ya está guardado se puede seguir "
              "consultando y exportando. %(siguiente)s",
              action=action, motivo=rec.status_message, siguiente=siguiente),
            'dsi_license_client.action_license_notice_dialog',
            _("Comprar o activar licencia"))
        # Es un bloqueo esperado, no una falla: sin esto Odoo lo registra como
        # error con traceback, porque RedirectWarning no hereda de UserError.
        error.loglevel = logging.INFO
        raise error

    def _is_trial(self):
        """True si el token vigente (verificado) es de una prueba."""
        self.ensure_one()
        return (self._verified_payload() or {}).get('license_type') == 'trial'

    def _purchase_url(self):
        """El botón «Comprar licencia» lleva al servidor de licencias, que
        redirige a la tienda. La dirección de la tienda se define allá, así
        que puede cambiar sin actualizar este módulo."""
        self.ensure_one()
        query = {'producto': TRIAL_PRODUCT_CODE}
        if self.license_key:
            query['clave'] = self.license_key.strip()
        return f"{self.activation_url.rstrip('/')}/dsi/license/v1/comprar?{urlencode(query)}"

    def action_buy(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self._purchase_url(), 'target': 'new'}

    def _notice_action(self, next_action=None, target='current'):
        """El aviso de licencia (comprar, capturar la clave, activar). Con
        next_action (XML ID de una acción de ventana) ofrece «Seguir solo
        consultando» hacia ella."""
        self.ensure_one()
        # Con el usuario real, no con sudo: cada usuario solo ve los registros
        # temporales que él creó.
        notice = self.env['dsi.license.client.notice'].sudo(False).create({
            'company_id': self.company_id.id,
            'license_key': self.license_key,
            'next_action': next_action,
            'target': target,
        })
        return notice._reopen()

    # -------------------------------------------------------------------
    # Activación manual
    # -------------------------------------------------------------------

    def action_activate(self):
        self.ensure_one()
        if not self.license_key:
            raise UserError(_("Captura una clave de licencia antes de activar."))
        company = self.company_id
        rfc = (company.partner_id.vat or '').strip()
        if not rfc:
            raise UserError(_("La empresa no tiene RFC capturado (Contactos > %s > RFC).", company.name))
        payload = {
            'license_key': self.license_key.strip(),
            'rfc': rfc,
            'db_uuid': self._get_db_uuid(),
            'odoo_version': release.version,
            'module_version': self._get_module_version(),
            'base_url': self._get_base_url(),
            'is_production': True,
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/activate",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
        except requests.RequestException as e:
            self.last_error = str(e)
            raise UserError(_("No se pudo contactar al servidor de licencias: %s", e)) from e

        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get('token'):
            error = data.get('error') or _("Respuesta inesperada del servidor de licencias.")
            self.last_error = error
            raise UserError(error)

        self._apply_token(data['token'])

    def action_deactivate(self):
        """Libera voluntariamente el cupo de esta instancia en el servidor de
        licencias (ej. antes de dar de baja un ambiente de prueba). No borra
        el registro local: solo limpia el token, quedando en 'not_activated'."""
        self.ensure_one()
        if not self.license_key:
            raise UserError(_("Esta base no tiene una licencia activada."))
        payload = {
            'license_key': self.license_key.strip(),
            'db_uuid': self._get_db_uuid(),
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/deactivate",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
        except requests.RequestException as e:
            raise UserError(_("No se pudo contactar al servidor de licencias: %s", e)) from e

        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get('ok'):
            error = data.get('error') or _("Respuesta inesperada del servidor de licencias.")
            raise UserError(error)

        self.write({
            'token': False,
            'token_expires': False,
            'license_end': False,
            'edition': False,
            'features': False,
            'last_check': fields.Datetime.now(),
            'last_error': False,
        })

    # -------------------------------------------------------------------
    # Prueba de 7 días
    #
    # El botón solo deja la solicitud lista; la manda una tarea programada.
    # Antes de contestar, el servidor de licencias llama de vuelta a esta base
    # (/dsi/license/v1/challenge) para comprobar que la URL es suya. Si la
    # solicitud saliera del propio clic, el worker web de esta base estaría
    # ocupado esperando la respuesta y, con un solo worker, no podría atender
    # esa llamada. Desde la tarea programada el worker web queda libre.
    # -------------------------------------------------------------------

    def action_request_trial(self):
        self.ensure_one()
        if self.token:
            raise UserError(_(
                "Esta base ya tiene una licencia. La prueba es solo para bases sin activar."))
        company = self.company_id
        if not (company.partner_id.vat or '').strip():
            raise UserError(_(
                "La empresa no tiene RFC capturado (Contactos > %s > RFC). La prueba queda "
                "registrada a ese RFC.", company.name))
        base_url = self._get_base_url()
        if not _is_public_https(base_url):
            raise UserError(_(
                "La prueba se activa sola únicamente en bases accesibles desde internet con "
                "https, porque el servidor de licencias tiene que poder comprobar la dirección. "
                "Esta base está en %s. Pide tu clave de licencia a DS Indigo.", base_url))
        self.write({
            'trial_state': 'requested',
            'trial_error': False,
            'trial_requested_at': fields.Datetime.now(),
            'trial_challenge_id': secrets.token_urlsafe(16),
            'trial_challenge_secret': secrets.token_urlsafe(32),
        })
        cron = self.env.ref('dsi_license_client.ir_cron_trial_request', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_request_trial(self):
        for rec in self.sudo().search([('trial_state', '=', 'requested')]):
            try:
                rec._do_trial_request()
            except Exception as e:  # pylint: disable=broad-except
                _logger.exception("La solicitud de prueba falló para company_id=%s", rec.company_id.id)
                rec._trial_failed(str(e)[:300])

    def _do_trial_request(self):
        self.ensure_one()
        vence = self.trial_requested_at + timedelta(minutes=TRIAL_CHALLENGE_MINUTES)
        if fields.Datetime.now() > vence:
            self._trial_failed(_("La solicitud venció antes de poder verificarse. Vuelve a intentarlo."))
            return
        company = self.company_id
        payload = {
            'product_code': TRIAL_PRODUCT_CODE,
            'rfc': (company.partner_id.vat or '').strip().upper(),
            'company_name': company.name,
            'db_uuid': self._get_db_uuid(),
            'base_url': self._get_base_url(),
            'odoo_version': release.version,
            'module_version': self._get_module_version(),
            'challenge_id': self.trial_challenge_id,
            'challenge_secret': self.trial_challenge_secret,
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/trial",
                json=payload, timeout=TRIAL_REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as e:
            self._trial_failed(_("No se pudo contactar al servidor de licencias: %s", e))
            return
        if response.status_code != 200 or not data.get('token') or not data.get('license_key'):
            self._trial_failed(data.get('error') or _("Respuesta inesperada del servidor de licencias."))
            return
        self._apply_token(data['token'])
        self.write({
            'license_key': data['license_key'],
            'trial_state': 'none',
            'trial_error': False,
            'trial_challenge_id': False,
            'trial_challenge_secret': False,
        })

    def _trial_failed(self, error):
        self.write({
            'trial_state': 'failed',
            'trial_error': error,
            'trial_challenge_id': False,
            'trial_challenge_secret': False,
        })

    @api.model
    def _trial_challenge_secret(self, challenge_id):
        """Lo que /dsi/license/v1/challenge contesta al servidor de licencias:
        el secreto de una solicitud de prueba en curso, o None."""
        if not challenge_id:
            return None
        rec = self.sudo().search([
            ('trial_state', '=', 'requested'),
            ('trial_challenge_id', '=', challenge_id),
        ], limit=1)
        if not rec:
            return None
        if fields.Datetime.now() > rec.trial_requested_at + timedelta(minutes=TRIAL_CHALLENGE_MINUTES):
            return None
        return rec.trial_challenge_secret

    def action_refresh(self):
        """Solo recarga el formulario, para ver si la prueba ya quedó."""
        return True

    # -------------------------------------------------------------------
    # Heartbeat (cron) — NUNCA bloqueante
    # -------------------------------------------------------------------

    @api.model
    def _cron_heartbeat(self):
        # Antes cada corrida reprogramaba la siguiente ±90 min (jitter). Pero
        # Odoo no deja escribir en una tarea programada mientras se ejecuta:
        # la escritura fallaba y tumbaba la renovación entera, así que el token
        # nunca se renovaba solo. Cada base se instala a una hora distinta, y
        # con eso la carga en el servidor ya queda repartida.
        for rec in self.sudo().search([('token', '!=', False)]):
            try:
                rec._do_heartbeat()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Heartbeat de licenciamiento falló para company_id=%s", rec.company_id.id)
            try:
                rec._send_trial_reminders()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("No se pudo avisar del fin de la prueba a company_id=%s", rec.company_id.id)

    def _reminder_partners(self):
        """A quiénes se avisa que la prueba está por terminar: los
        administradores de la empresa. Los módulos del producto agregan a sus
        usuarios (ver dsi_cfdi_vault)."""
        self.ensure_one()
        usuarios = self.env['res.users'].sudo().search([('share', '=', False)]).filtered(
            lambda u: self.company_id in u.company_ids and u.has_group('base.group_system'))
        return usuarios.partner_id

    def _send_trial_reminders(self):
        """Avisa por notificación de Odoo (bandeja o correo, según prefiera
        cada usuario) cuando a la prueba le quedan 3 días y 1 día. Cada aviso
        sale una sola vez aunque la tarea corra más de una vez al día."""
        self.ensure_one()
        payload = self._verified_payload()
        if not payload or payload.get('license_type') != 'trial' or self.state != 'trial':
            return
        fin = fields.Date.to_date(payload.get('token_expires'))
        dias = (fin - fields.Date.today()).days
        enviados = {d for d in (self.trial_reminders_sent or '').split(',') if d}
        pendientes = [d for d in TRIAL_REMINDER_DAYS if dias <= d and str(d) not in enviados]
        if not pendientes:
            return
        # Si la tarea no corrió unos días, sale solo el aviso más urgente.
        enviados.update(str(d) for d in TRIAL_REMINDER_DAYS if dias <= d)
        self.trial_reminders_sent = ','.join(sorted(enviados))
        partners = self._reminder_partners()
        if not partners:
            return
        cuando = _("mañana") if dias <= 1 else _("en %s días", dias)
        self.env['mail.thread'].sudo().message_notify(
            partner_ids=partners.ids,
            subject=_("Tu prueba de %(producto)s termina %(cuando)s", producto=PRODUCT_NAME, cuando=cuando),
            body=Markup("<p>%s</p><p>%s</p>") % (
                _("La prueba de %(producto)s de %(empresa)s termina el %(fin)s.",
                  producto=PRODUCT_NAME, empresa=self.company_id.name, fin=format_date(self.env, fin)),
                _("Después ya no se podrá importar CFDI, descargarlos del SAT ni convertirlos en "
                  "facturas; lo guardado se podrá seguir consultando. Para no interrumpir nada, "
                  "compra tu licencia desde %s > Licencia.", PRODUCT_NAME)),
        )

    def _do_heartbeat(self):
        self.ensure_one()
        payload = {
            'license_key': self.license_key,
            'db_uuid': self._get_db_uuid(),
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/heartbeat",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
            data = response.json() if response.content else {}
        except requests.RequestException as e:
            self.last_error = str(e)
            return
        if response.status_code == 200 and data.get('token'):
            self._apply_token(data['token'])
        else:
            self.last_error = data.get('error') or _("El servidor de licencias no renovó el token.")
