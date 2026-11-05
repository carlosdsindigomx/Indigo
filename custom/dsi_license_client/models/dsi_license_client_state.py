import ast
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
from odoo.tools.safe_eval import safe_eval

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
# Producto de un registro que no dice otra cosa, y de un token que no trae
# product_code (los que emitía el servidor antes de vender otros productos).
DEFAULT_PRODUCT = 'cfdi_vault'
# Solo por compatibilidad: el nombre de cada producto sale del catálogo.
PRODUCT_NAME = "Bóveda CFDI"
# Días antes del fin de la prueba en que se avisa a los usuarios.
TRIAL_REMINDER_DAYS = (3, 1)
# Lo que un producto deja hacer según su licencia (ver _license_mode).
LICENSE_MODES = [
    ('full', "Completa"),
    ('read', "Solo consulta"),
    ('none', "Sin licencia"),
]
# Lo que se le quita a una vista en modo solo consulta (web/static/src/views/view.js).
READ_ONLY_CONTEXT = {
    'create': False,
    'edit': False,
    'delete': False,
    'group_create': False,
    'group_edit': False,
    'group_delete': False,
}

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


def _commit(env):
    """Guarda lo hecho por una tarea programada después de cada registro, para
    que una falla en otro no se lleve lo ya hecho (el servidor de licencias ya
    gastó la prueba o ya renovó el token). Odoo 19 y 20 lo hacen con
    ir.cron._commit_progress, que fuera de una tarea solo guarda; Odoo 18 no
    lo tiene."""
    cron = env['ir.cron']
    if hasattr(cron, '_commit_progress'):
        cron._commit_progress(1)
    else:
        env.cr.commit()


def _normalize_key(key):
    """La clave como la conoce el servidor de licencias: sin espacios y en
    mayúsculas. El servidor las genera así y las busca tal cual (distingue
    mayúsculas), así que todo lo que se le manda pasa por aquí."""
    return (key or '').strip().upper()


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
    """Un registro por producto: por empresa (la Bóveda) o uno solo para toda
    la base (Manufactura). Qué productos existen y cómo se comporta cada uno
    lo dice _product_catalog, que extiende el módulo de cada producto."""
    _name = 'dsi.license.client.state'
    _description = "Estado de licenciamiento DS Indigo de esta base"
    _inherit = ['mail.thread']

    # En alcance 'database' es la empresa TITULAR: la del RFC con que se
    # activó o se pidió la prueba. La licencia la usa cualquier empresa.
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    product_code = fields.Char(
        string="Código de producto", required=True, default=DEFAULT_PRODUCT, index=True, readonly=True)
    license_scope = fields.Selection([
        ('company', "Por empresa"),
        ('database', "Por base"),
    ], string="Alcance", required=True, default='company', readonly=True,
        help="Lo fija el catálogo del producto al crear el registro y ya no cambia.")
    product_name = fields.Char(string="Producto", compute='_compute_product_info')
    product_installed = fields.Boolean(
        string="Producto instalado", compute='_compute_product_info', search='_search_product_installed')

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
    license_mode = fields.Selection(LICENSE_MODES, string="Uso permitido", compute='_compute_state')
    restriction_text = fields.Char(compute='_compute_state')
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

    # Mismo nombre que la restricción anterior, unique(company_id): al
    # actualizar, Odoo ve que la definición cambió y la recrea.
    _company_uniq = models.Constraint(
        'unique(company_id, product_code)',
        "Ya existe un registro de licencia de este producto para esta empresa.")
    # Un producto por base tiene un solo registro, sea cual sea la empresa.
    _product_database_uniq = models.UniqueIndex(
        "(product_code) WHERE (license_scope = 'database')",
        "Esta base ya tiene un registro de licencia de este producto.")

    # -------------------------------------------------------------------
    # Catálogo de productos
    # -------------------------------------------------------------------

    @api.model
    def _product_catalog(self):
        """Los productos que conoce esta base. Cada módulo de producto lo
        extiende (super() y agrega su código); la Bóveda vive aquí porque es
        el producto de siempre. Claves de cada producto (las que falten las
        pone _product_info):

        - name: nombre que ve el usuario.
        - module: módulo técnico del producto (para ocultarlo en Ajustes si
          no está instalado).
        - scope: 'company' (un registro por empresa) o 'database' (uno para
          toda la base, activado con el RFC de la empresa titular).
        - feature: la función que el token debe traer para usar el producto.
        - read_only_grace: con la licencia ya vencida, la gracia es solo de
          consulta (_license_mode 'read') en lugar de completa.
        - allow_continue: el aviso al entrar ofrece «Seguir solo consultando».
        - trial_days: días de la prueba de autoservicio.
        - license_menu: ruta del menú «Licencia» del producto.
        - notice_text, trial_text, restriction_text, read_text: textos del
          aviso y de Ajustes (sin licencia, en prueba, bloqueado, solo
          consulta).
        - reminder_text: cuerpo del aviso de fin de prueba.
        - grace_expired_message, grace_renewal_message: situación en la
          gracia con la licencia vencida o con la renovación fallida; llevan
          %(fin)s y %(limite)s.
        """
        gracia_boveda = _(
            "La licencia venció el %(fin)s; sigue funcionando hasta el %(limite)s. "
            "Renuévala para no perder el acceso.")
        return {
            DEFAULT_PRODUCT: {
                'name': _("Bóveda CFDI"),
                'module': 'dsi_cfdi_vault',
                'scope': 'company',
                'feature': 'vault',
                'read_only_grace': False,
                'allow_continue': True,
                'trial_days': 7,
                'license_menu': _("Bóveda CFDI > Licencia"),
                'notice_text': _(
                    "Tus CFDI siguen aquí: puedes consultarlos y exportarlos. Para importar, "
                    "descargar del SAT y convertir a factura necesitas una licencia vigente."),
                'trial_text': _(
                    "Compra tu licencia antes de que termine la prueba para no interrumpir la "
                    "importación, la descarga del SAT ni la conversión a factura."),
                'restriction_text': _(
                    "Mientras tanto no se puede importar, descargar del SAT ni convertir a factura; "
                    "lo ya guardado se puede consultar y exportar."),
                'reminder_text': _(
                    "Después ya no se podrá importar CFDI, descargarlos del SAT ni convertirlos en "
                    "facturas; lo guardado se podrá seguir consultando. Para no interrumpir nada, "
                    "compra tu licencia desde Bóveda CFDI > Licencia."),
                # La Bóveda conserva su texto de siempre en los dos tipos de gracia.
                'grace_expired_message': gracia_boveda,
                'grace_renewal_message': gracia_boveda,
            },
        }

    @api.model
    def _product_info(self, product_code):
        """La entrada del catálogo con todas sus claves. Un código que el
        catálogo no conoce (módulo desinstalado) no tiene función, así que su
        modo es siempre 'none'."""
        code = product_code or DEFAULT_PRODUCT
        catalogo = self._product_catalog()
        conocido = code in catalogo
        info = dict(catalogo.get(code) or {})
        if not conocido:
            # Pasa con el registro de un producto cuyo módulo se desinstaló.
            _logger.debug("Producto de licencia desconocido en el catálogo: %s", code)
        nombre = info.get('name') or code
        info['code'] = code
        info['name'] = nombre
        info.setdefault('module', False)
        if info.get('scope') not in ('company', 'database'):
            info['scope'] = 'company'
        info.setdefault('feature', code if conocido else None)
        info['read_only_grace'] = bool(info.get('read_only_grace', False))
        info['allow_continue'] = bool(info.get('allow_continue', True))
        info['trial_days'] = int(info.get('trial_days') or 7)
        info.setdefault('license_menu', _("%s > Licencia", nombre))
        info.setdefault('notice_text', _(
            "Para usar %s necesitas una licencia vigente: inicia la prueba, compra tu licencia o "
            "activa la clave que ya tienes.", nombre))
        info.setdefault('trial_text', _(
            "Compra tu licencia antes de que termine la prueba para no interrumpir tu trabajo."))
        info.setdefault('restriction_text', _(
            "Mientras tanto no se puede usar %s.", nombre))
        info.setdefault('read_text', _(
            "La licencia venció: hasta que la renueves solo se puede consultar; no se puede "
            "crear, editar ni borrar."))
        info.setdefault('reminder_text', _(
            "Después ya no se podrá usar %(producto)s. Para no interrumpir nada, compra tu "
            "licencia desde %(menu)s.", producto=nombre, menu=info['license_menu']))
        info.setdefault('grace_renewal_message', _(
            "No se ha podido renovar la licencia desde el %(fin)s; todo sigue funcionando "
            "hasta el %(limite)s. Revisa la conexión con el servidor de licencias."))
        if info['read_only_grace']:
            info.setdefault('grace_expired_message', _(
                "La licencia venció el %(fin)s. Hasta el %(limite)s solo se puede consultar: no "
                "se puede crear, editar ni borrar. Renuévala para volver a trabajar."))
        else:
            info.setdefault('grace_expired_message', _(
                "La licencia venció el %(fin)s; sigue funcionando hasta el %(limite)s. "
                "Renuévala para no perder el acceso."))
        return info

    @api.model
    def _installed_product_codes(self):
        """Códigos del catálogo cuyo módulo está instalado (o que no dicen
        módulo)."""
        catalogo = self._product_catalog()
        modulos = {info.get('module') for info in catalogo.values() if info.get('module')}
        instalados = set()
        if modulos:
            instalados = set(self.env['ir.module.module'].sudo().search([
                ('name', 'in', list(modulos)),
                ('state', 'in', ('installed', 'to upgrade')),
            ]).mapped('name'))
        return {
            code for code, info in catalogo.items()
            if not info.get('module') or info['module'] in instalados
        }

    @api.depends('product_code')
    def _compute_product_info(self):
        instalados = self._installed_product_codes()
        for rec in self:
            rec.product_name = rec._product_info(rec.product_code)['name']
            rec.product_installed = rec.product_code in instalados

    def _search_product_installed(self, operator, value):
        # Odoo 19 y 20 mandan 'in'/'not in'; Odoo 18, '='/'!='.
        if operator in ('in', 'not in'):
            valores = {value} if value is None or isinstance(value, (bool, int, str)) else set(value)
        elif operator in ('=', '!='):
            valores = {value}
        else:
            raise UserError(_("Operación no soportada para «Producto instalado»: %s", operator))
        buscados = {bool(v) for v in valores}
        if operator in ('not in', '!='):
            buscados = {True, False} - buscados
        codigos = list(self._installed_product_codes())
        if buscados == {True}:
            return [('product_code', 'in', codigos)]
        if buscados == {False}:
            return [('product_code', 'not in', codigos)]
        if buscados:
            return [('id', '!=', False)]
        return [('id', '=', False)]

    @api.depends('company_id', 'product_code', 'license_scope')
    def _compute_display_name(self):
        for rec in self:
            producto = rec._product_info(rec.product_code)['name']
            if rec.license_scope == 'database':
                rec.display_name = _("%s — toda la base", producto)
            else:
                rec.display_name = _("%(producto)s — %(empresa)s",
                                     producto=producto, empresa=rec.company_id.name)

    @api.depends('token', 'product_code')
    def _compute_state(self):
        for rec in self:
            info = rec._product_info(rec.product_code)
            detalle = rec._evaluate_detail()
            modo, _mensaje = rec._status_from_detail(detalle, info)
            rec.state = detalle['state']
            rec.status_message = detalle['message']
            rec.license_mode = modo
            rec.restriction_text = rec._block_text(detalle['state'], modo, info)

    @api.model
    def _shows_block(self, state, mode, info):
        """Si se explica lo que no se puede hacer: sin licencia y, en los
        productos con gracia completa (la Bóveda), también en la gracia, como
        siempre."""
        return state != 'trial' and (mode == 'none' or (state == 'grace' and not info['read_only_grace']))

    @api.model
    def _block_text(self, state, mode, info):
        """El texto de bloqueo de Ajustes: solo consulta en modo 'read' y el
        de restricción cuando corresponde (_shows_block)."""
        if mode == 'read':
            return info['read_text']
        if self._shows_block(state, mode, info):
            return info['restriction_text']
        return False

    # -------------------------------------------------------------------
    # Alta y búsqueda de registros
    # -------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Explícito: un default_product_code del contexto no debe colarse.
            code = vals.get('product_code') or DEFAULT_PRODUCT
            vals['product_code'] = code
            # El alcance lo dicta siempre el catálogo, venga lo que venga.
            vals['license_scope'] = self._product_info(code)['scope']
        return super().create(vals_list)

    def write(self, vals):
        for campo in ('product_code', 'license_scope'):
            if campo in vals and any(rec[campo] != vals[campo] for rec in self):
                raise UserError(_(
                    "El producto y el alcance de un registro de licencia no se pueden cambiar."))
        return super().write(vals)

    @api.model
    def _get_product_license(self, product_code, company=None, create=False):
        """El registro de licencia del producto (con sudo). En alcance
        'company' es el de la empresa (la del usuario si no se indica); en
        'database', el único de la base, sin importar la empresa. Con
        create=False nunca escribe: se puede llamar desde lecturas."""
        code = product_code or DEFAULT_PRODUCT
        info = self._product_info(code)
        if isinstance(company, int):
            company = self.env['res.company'].browse(company)
        company = company or self.env.company
        licencias = self.sudo()
        dominio = [('product_code', '=', code)]
        if info['scope'] != 'database':
            dominio.append(('company_id', '=', company.id))
        rec = licencias.search(dominio, limit=1, order='id')
        if not rec and create:
            titular = company if info['scope'] != 'database' else self._rfc_company(company)
            rec = licencias.create({'product_code': code, 'company_id': titular.id})
        return rec

    @api.model
    def _get_or_create(self, company=None, product_code=DEFAULT_PRODUCT):
        return self._get_product_license(product_code, company, create=True)

    @api.model
    def _ensure_product_licenses(self):
        """Crea los registros de los productos por base que falten. Lo llaman
        los módulos de producto al instalarse (<function> en sus datos), para
        que no se cree en la primera entrada, donde dos usuarios a la vez
        chocarían con el índice único."""
        for code in self._product_catalog():
            if self._product_info(code)['scope'] == 'database':
                self._get_product_license(code, create=True)
        return True

    @api.model
    def _rfc_company(self, company=None):
        """La empresa cuyo RFC registra una licencia por base: la del usuario
        si tiene RFC; si no, su empresa raíz; si no, la principal. Si ninguna
        lo tiene, la del usuario, para que el error diga dónde capturarlo."""
        company = (company or self.env.company).sudo()
        candidatas = [company]
        if 'root_id' in company._fields:
            candidatas.append(company.root_id)
        principal = self.env.ref('base.main_company', raise_if_not_found=False)
        if not principal:
            principal = self.env['res.company'].sudo().search([], order='id', limit=1)
        candidatas.append(principal.sudo())
        for candidata in candidatas:
            if candidata and (candidata.partner_id.vat or '').strip():
                return candidata
        return company

    def _license_rfc(self):
        """El RFC con que se activa o se pide la prueba: el de la empresa
        titular del registro."""
        self.ensure_one()
        return (self.company_id.sudo().partner_id.vat or '').strip().upper()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

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

    @api.model
    def _payload_product(self, payload):
        """El producto de un token. Los emitidos antes de que el servidor
        vendiera otros productos no lo traen: son de la Bóveda."""
        return (payload or {}).get('product_code') or DEFAULT_PRODUCT

    def _evaluate_detail(self):
        """La situación de la licencia según el token verificado y la fecha de
        hoy: {state, message, payload, kind, fin, limite}. kind solo en la
        gracia: 'expired' si la licencia ya venció (hoy > license_end),
        'renewal' si solo falló la renovación (licencia perpetua o todavía
        vigente)."""
        self.ensure_one()
        detalle = {'state': 'not_activated', 'message': '', 'payload': None,
                   'kind': None, 'fin': None, 'limite': None}
        if not self.token:
            detalle['message'] = _("Esta base no tiene una licencia activada.")
            return detalle
        payload = self._verified_payload()
        if payload is None:
            detalle.update(state='restricted', message=_("El token de licencia no es válido."))
            return detalle
        detalle['payload'] = payload
        info = self._product_info(self.product_code)
        producto_token = self._payload_product(payload)
        if producto_token != (self.product_code or DEFAULT_PRODUCT):
            detalle.update(state='restricted', message=_(
                "La clave activada es de otro producto (%(otro)s), no de %(producto)s.",
                otro=self._product_info(producto_token)['name'], producto=info['name']))
            return detalle
        # Token amarrado a la base: copiar el token o la base a otro servidor
        # no la hace válida ahí.
        if payload.get('db_uuid') and payload['db_uuid'] != self._get_db_uuid():
            detalle.update(state='restricted', message=_(
                "La licencia está registrada para otra base de datos."))
            return detalle
        # Las pruebas, además, a la dirección de la base que las pidió.
        if payload.get('base_url') and _host(payload['base_url']) != _host(self._get_base_url()):
            detalle.update(state='restricted', message=_(
                "La licencia está registrada para %s, no para la dirección de esta base.",
                payload['base_url']))
            return detalle

        # Vence lo que ocurra primero: el token o la licencia. El servidor ya
        # no emite tokens más allá de la licencia, pero los anteriores sí.
        fechas = [fields.Date.to_date(payload.get(clave)) for clave in ('token_expires', 'license_end')]
        fechas = [fecha for fecha in fechas if fecha]
        if not fechas:
            detalle.update(state='restricted', message=_(
                "El token de licencia no trae fecha de vigencia."))
            return detalle
        fin = min(fechas)
        es_prueba = payload.get('license_type') == 'trial'
        gracia = int(payload.get('grace_days', GRACE_DAYS))
        limite = fin + timedelta(days=gracia)
        detalle.update(fin=fin, limite=limite)
        # Odoo corre en UTC, así que hoy es el día UTC, no el de la tz del
        # usuario: cerca de medianoche eso adelantaría o atrasaría un día.
        hoy = fields.Date.today()
        if hoy <= fin:
            if es_prueba:
                detalle.update(state='trial', message=_(
                    "En prueba hasta el %s.", format_date(self.env, fin)))
            else:
                detalle.update(state='active', message=_(
                    "Licencia vigente hasta el %s.", format_date(self.env, fin)))
            return detalle
        if hoy <= limite:
            license_end = fields.Date.to_date(payload.get('license_end'))
            kind = 'expired' if license_end and hoy > license_end else 'renewal'
            plantilla = info['grace_expired_message'] if kind == 'expired' else info['grace_renewal_message']
            detalle.update(state='grace', kind=kind, message=plantilla % {
                'fin': format_date(self.env, fin), 'limite': format_date(self.env, limite)})
            return detalle
        if es_prueba:
            detalle.update(state='restricted', message=_(
                "La prueba terminó el %s.", format_date(self.env, fin)))
        else:
            detalle.update(state='restricted', message=_(
                "La licencia venció el %s.", format_date(self.env, fin)))
        return detalle

    def _evaluate(self):
        """(estado, mensaje) según el token verificado y la fecha de hoy."""
        detalle = self._evaluate_detail()
        return detalle['state'], detalle['message']

    @api.model
    def _status_from_detail(self, detalle, info):
        """(modo, mensaje) a partir de _evaluate_detail:

        - prueba vigente → full; prueba vencida → none, aunque el token
          traiga días de gracia (las pruebas no tienen gracia);
        - vigente → full;
        - gracia por falla de renovación → full;
        - gracia con la licencia vencida → read si el producto es de
          read_only_grace, si no full;
        - cualquier otro caso → none.

        Si el token no trae la función del producto, none."""
        payload = detalle['payload'] or {}
        estado = detalle['state']
        mensaje = detalle['message']
        if payload.get('license_type') == 'trial':
            modo = 'full' if estado == 'trial' else 'none'
        elif estado == 'active':
            modo = 'full'
        elif estado == 'grace':
            modo = 'read' if detalle['kind'] == 'expired' and info['read_only_grace'] else 'full'
        else:
            modo = 'none'
        if modo != 'none' and info['feature'] not in (payload.get('features') or []):
            modo = 'none'
            mensaje = _("Tu licencia no incluye %s.", info['name'])
        return modo, mensaje

    @api.model
    def _license_status(self, product_code, company=None):
        """(modo, mensaje) de la licencia del producto: 'full', 'read' o
        'none' y la situación que se le explica al usuario. Nunca escribe en
        la base (se llama desde lecturas y vistas)."""
        code = product_code or DEFAULT_PRODUCT
        info = self._product_info(code)
        rec = self._get_product_license(code, company)
        if not rec:
            return 'none', _("Esta base no tiene una licencia activada.")
        return rec._status_from_detail(rec._evaluate_detail(), info)

    @api.model
    def _license_mode(self, product_code, company=None):
        """'full' (se trabaja normal), 'read' (solo consulta) o 'none' (sin
        licencia). Ver _status_from_detail. Nunca escribe en la base."""
        return self._license_status(product_code, company)[0]

    @api.model
    def _license_error(self, product_code, action, company=None):
        """La excepción que bloquea `action` (p. ej. «registrar la
        producción») si la licencia no es de uso completo, o None. Es un
        RedirectWarning hacia el aviso del producto, que cualquier usuario
        puede usar para comprar o activar. No exenta a nadie (ni a env.su):
        eso lo decide quien llama."""
        code = product_code or DEFAULT_PRODUCT
        info = self._product_info(code)
        rec = self._get_product_license(code, company)
        if rec:
            detalle = rec._evaluate_detail()
            modo, motivo = rec._status_from_detail(detalle, info)
            estado = detalle['state']
        else:
            modo, motivo, estado = 'none', _("Esta base no tiene una licencia activada."), 'not_activated'
        if modo == 'full':
            return None
        if modo == 'read':
            mensaje = _("No se puede %(action)s: %(motivo)s", action=action, motivo=motivo)
        else:
            if estado == 'not_activated':
                siguiente = _("Inicia la prueba de %s días, o compra o activa tu licencia, para hacerlo.",
                              info['trial_days'])
            else:
                siguiente = _("Compra o activa tu licencia para seguir haciéndolo.")
            mensaje = _("No se puede %(action)s. %(motivo)s %(siguiente)s",
                        action=action, motivo=motivo, siguiente=siguiente)
        empresa = rec.company_id if rec else (company or self.env.company)
        if isinstance(empresa, int):
            empresa = self.env['res.company'].browse(empresa)
        return self._redirect_to_notice(mensaje, code, empresa)

    @api.model
    def _redirect_to_notice(self, mensaje, product_code, company):
        # La ventana lleva un botón al aviso de licencia del producto (comprar,
        # capturar la clave y activar), en lugar de mandar al usuario a Ajustes.
        error = RedirectWarning(
            mensaje,
            'dsi_license_client.action_license_notice_dialog',
            _("Comprar o activar licencia"),
            {'dsi_license_product': product_code, 'dsi_license_company': company.id},
        )
        # Es un bloqueo esperado, no una falla: sin esto Odoo lo registra como
        # error con traceback, porque RedirectWarning no hereda de UserError.
        error.loglevel = logging.INFO
        return error

    @api.model
    def _check_license_write(self, product_code, action, company=None):
        """Lanza el error de _license_error si la licencia no es de uso
        completo. No exenta env.su: lo hace quien llama."""
        error = self._license_error(product_code, action, company)
        if error:
            raise error

    def _apply_token(self, token):
        """Verifica localmente el token y actualiza los campos que se muestran.
        Un token de otro producto no se guarda."""
        self.ensure_one()
        try:
            payload = verify_and_decode_token(token)
        except ValueError as e:
            raise UserError(str(e)) from e
        producto_token = self._payload_product(payload)
        if producto_token != (self.product_code or DEFAULT_PRODUCT):
            raise UserError(_(
                "Esta clave es de otro producto (%(otro)s), no de %(producto)s.",
                otro=self._product_info(producto_token)['name'],
                producto=self._product_info(self.product_code)['name']))
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
    def _check_feature(self, feature, action, company=None, product_code=DEFAULT_PRODUCT):
        """Lanza UserError si la licencia de la empresa no permite `action`
        (p. ej. "importar CFDI"). Es el único punto de bloqueo de la Bóveda:
        lo usan la importación, la descarga del SAT y la conversión a factura.
        Consultar y exportar lo que ya está guardado nunca pasa por aquí."""
        rec = self._get_or_create(company, product_code)
        if rec.is_feature_enabled(feature):
            return
        if rec.state in ('trial', 'active', 'grace'):
            raise UserError(_("No se puede %s: tu licencia no incluye esta función.", action))
        if rec.state == 'not_activated':
            siguiente = _("Inicia la prueba de %s días, o compra o activa tu licencia, para hacerlo.",
                          rec._product_info(rec.product_code)['trial_days'])
        else:
            siguiente = _("Compra o activa tu licencia para seguir haciéndolo.")
        raise self._redirect_to_notice(
            _("No se puede %(action)s. %(motivo)s\n\nLo que ya está guardado se puede seguir "
              "consultando y exportando. %(siguiente)s",
              action=action, motivo=rec.status_message, siguiente=siguiente),
            rec.product_code, rec.company_id)

    def _is_trial(self):
        """True si el token vigente (verificado) es de una prueba."""
        self.ensure_one()
        return (self._verified_payload() or {}).get('license_type') == 'trial'

    def _purchase_url(self):
        """El botón «Comprar licencia» lleva al servidor de licencias, que
        redirige a la tienda del producto. La dirección de la tienda se define
        allá, así que puede cambiar sin actualizar este módulo."""
        self.ensure_one()
        query = {'producto': self.product_code or DEFAULT_PRODUCT}
        if _normalize_key(self.license_key):
            query['clave'] = _normalize_key(self.license_key)
        return f"{self.activation_url.rstrip('/')}/dsi/license/v1/comprar?{urlencode(query)}"

    def action_buy(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self._purchase_url(), 'target': 'new'}

    def _notice_action(self, next_action=None, target='current', allow_continue=None):
        """El aviso de licencia de este producto (comprar, capturar la clave,
        activar). Con next_action (XML ID de una acción) ofrece «Seguir solo
        consultando» hacia ella si allow_continue lo permite (None: lo que
        diga el catálogo). Llámalo siempre con argumentos por nombre."""
        self.ensure_one()
        if allow_continue is None:
            allow_continue = self._product_info(self.product_code)['allow_continue']
        # Con el usuario real, no con sudo: cada usuario solo ve los registros
        # temporales que él creó.
        notice = self.env['dsi.license.client.notice'].sudo(False).create({
            'product_code': self.product_code,
            'company_id': self.company_id.id,
            'license_key': self.license_key,
            'next_action': next_action,
            'target': target,
            'allow_continue': allow_continue,
        })
        # Con su id, como siempre (la Bóveda y sus pruebas lo usan). La
        # entrada sin licencia de un producto usa _entry_notice_action.
        return notice._form_action()

    # -------------------------------------------------------------------
    # Entrada a un producto
    # -------------------------------------------------------------------

    @api.model
    def _entry_action(self, product_code, target_xmlid, entry_xmlid, company=None):
        """Lo que abre el menú de un producto con licencia:

        - 'none': el aviso de licencia en pantalla completa, con «Seguir solo
          consultando» solo si el catálogo lo permite y, al activar o pulsar
          «Entrar», de vuelta a `entry_xmlid` (la propia entrada).
        - 'full': la acción `target_xmlid` tal cual (sin envolverla, para que
          conserve el path de la entrada).
        - 'read': la misma acción con las vistas sin crear, editar ni borrar
          y 'dsi_license_mode': 'read' en el contexto.

        Si viene del aviso (contexto dsi_license_from_notice), la acción
        reemplaza la ruta de navegación salvo que sea de pantalla completa."""
        code = product_code or DEFAULT_PRODUCT
        modo = self._license_mode(code, company)
        if modo == 'none':
            return self._entry_notice_action(code, entry_xmlid, company)
        accion = self.env['ir.actions.actions']._for_xml_id(target_xmlid)
        if modo == 'read':
            contexto = self._action_context(accion.get('context'))
            contexto.update(READ_ONLY_CONTEXT)
            contexto['dsi_license_mode'] = 'read'
            accion['context'] = contexto
        if self.env.context.get('dsi_license_from_notice') and accion.get('target') != 'fullscreen':
            accion['target'] = 'main'
        return accion

    @api.model
    def _entry_notice_action(self, product_code, entry_xmlid, company=None, allow_continue=None):
        """El aviso que muestra la entrada sin licencia. Se abre como registro
        nuevo, con sus valores en el contexto y sin res_id: es un registro
        temporal y, si Odoo ya lo hubiera borrado, recargar la pestaña horas
        después daría «registro inexistente». Lo usa también el aviso en
        pantalla completa para volver a mostrarse después de un botón
        (dsi.license.client.notice._reopen). allow_continue None: lo que diga
        el catálogo."""
        info = self._product_info(product_code)
        if isinstance(company, int):
            company = self.env['res.company'].browse(company)
        company = company or self.env.company
        if allow_continue is None:
            allow_continue = info['allow_continue']
        return {
            'type': 'ir.actions.act_window',
            'name': _("Licencia de %s", info['name']),
            'res_model': 'dsi.license.client.notice',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'main',
            'context': {
                'dsi_license_product': info['code'],
                'dsi_license_company': company.id,
                'default_next_action': entry_xmlid or False,
                'default_target': 'main',
                'default_allow_continue': bool(allow_continue),
            },
        }

    @api.model
    def _action_context(self, raw):
        """El contexto de una acción como dict. En las acciones de ventana es
        una cadena: se lee como literal y, si usa variables (uid,
        allowed_company_ids…), se evalúa con las del usuario. Si no se puede,
        {}."""
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        # Como las arma el cliente web: la empresa actual primero. Sin
        # allowed_company_ids en el contexto (shell, cron, RPC externo),
        # env.companies sale en el orden de res.company y allowed_company_ids[0]
        # sería otra empresa.
        empresas = [self.env.company.id] + (self.env.companies - self.env.company).ids
        try:
            valor = ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError):
            try:
                valor = safe_eval(raw, {
                    'uid': self.env.uid,
                    'context': dict(self.env.context),
                    'allowed_company_ids': empresas,
                    'current_company_id': self.env.company.id,
                    'active_id': False,
                    'active_ids': [],
                    'active_model': False,
                })
            except Exception:  # pylint: disable=broad-except
                _logger.warning("No se pudo leer el contexto de la acción: %s", raw)
                return {}
        return dict(valor) if isinstance(valor, dict) else {}

    # -------------------------------------------------------------------
    # Activación manual
    # -------------------------------------------------------------------

    def _key_is_locked(self, key):
        """En los productos por base, una clave vigente no se reemplaza por
        otra desde el aviso: cualquier usuario puede activar, y una clave
        ajena dejaría a toda la base con la licencia de otro. Se permite con
        la licencia vencida (solo consulta o restringida) o sin activar. La
        Bóveda (por empresa) no cambia."""
        self.ensure_one()
        if self.license_scope != 'database':
            return False
        actual = _normalize_key((self._verified_payload() or {}).get('license_key'))
        if not actual or actual == key:
            return False
        detalle = self._evaluate_detail()
        return detalle['state'] in ('trial', 'active') or (
            detalle['state'] == 'grace' and detalle['kind'] == 'renewal')

    def action_activate(self):
        self.ensure_one()
        key = _normalize_key(self.license_key)
        if not key:
            raise UserError(_("Captura una clave de licencia antes de activar."))
        info = self._product_info(self.product_code)
        if self._key_is_locked(key):
            raise UserError(_(
                "Esta base ya tiene una licencia vigente de %(producto)s (clave %(clave)s). Para "
                "cambiarla, un administrador debe desactivarla en Ajustes > Licenciamiento DS Indigo.",
                producto=info['name'], clave=(self._verified_payload() or {}).get('license_key')))
        # La empresa titular: la del registro en la Bóveda y al reactivar la
        # misma clave; con una clave nueva por base, la de la cadena de RFC del
        # usuario que la activa.
        clave_actual = _normalize_key((self._verified_payload() or {}).get('license_key'))
        if self.license_scope == 'database' and clave_actual != key:
            company = self._rfc_company(self.env.company)
        else:
            company = self.company_id
        rfc = (company.sudo().partner_id.vat or '').strip()
        if not rfc:
            raise UserError(_("La empresa no tiene RFC capturado (Contactos > %s > RFC).", company.name))
        payload = {
            'license_key': key,
            'rfc': rfc,
            'db_uuid': self._get_db_uuid(),
            'odoo_version': release.version,
            'module_version': self._get_module_version(),
            'base_url': self._get_base_url(),
            'is_production': True,
            'product_code': self.product_code,
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
        valores = {}
        # La clave queda como la conoce el servidor: la renovación, la
        # desactivación y la compra la mandan desde aquí, y con la que se
        # capturó (' dsi-…') el servidor no la encontraría.
        if self.license_key != key:
            valores['license_key'] = key
        if company != self.company_id:
            valores['company_id'] = company.id
        if valores:
            self.write(valores)

    def action_deactivate(self):
        """Libera voluntariamente el cupo de esta instancia en el servidor de
        licencias (ej. antes de dar de baja un ambiente de prueba). No borra
        el registro local: solo limpia el token, quedando en 'not_activated'."""
        self.ensure_one()
        if not _normalize_key(self.license_key):
            raise UserError(_("Esta base no tiene una licencia activada."))
        payload = {
            'license_key': _normalize_key(self.license_key),
            'db_uuid': self._get_db_uuid(),
            'product_code': self.product_code,
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
    # Prueba de autoservicio
    #
    # El botón solo deja la solicitud lista; la manda una tarea programada.
    # Antes de contestar, el servidor de licencias llama de vuelta a esta base
    # (/dsi/license/v1/challenge) para comprobar que la URL es suya. Si la
    # solicitud saliera del propio clic, el worker web de esta base estaría
    # ocupado esperando la respuesta y, con un solo worker, no podría atender
    # esa llamada. Desde la tarea programada el worker web queda libre.
    # -------------------------------------------------------------------

    def _trial_in_progress(self):
        self.ensure_one()
        return bool(
            self.trial_state == 'requested' and self.trial_requested_at
            and fields.Datetime.now() <= self.trial_requested_at + timedelta(minutes=TRIAL_CHALLENGE_MINUTES))

    def action_request_trial(self):
        self.ensure_one()
        if self.token:
            raise UserError(_(
                "Esta base ya tiene una licencia. La prueba es solo para bases sin activar."))
        if self._trial_in_progress():
            # Un segundo clic no cambia el reto: el servidor de licencias ya
            # podría estar verificando el de la solicitud en curso.
            return
        company = self._rfc_company(self.env.company) if self.license_scope == 'database' else self.company_id
        if not (company.sudo().partner_id.vat or '').strip():
            raise UserError(_(
                "La empresa no tiene RFC capturado (Contactos > %s > RFC). La prueba queda "
                "registrada a ese RFC.", company.name))
        base_url = self._get_base_url()
        if not _is_public_https(base_url):
            raise UserError(_(
                "La prueba se activa sola únicamente en bases accesibles desde internet con "
                "https, porque el servidor de licencias tiene que poder comprobar la dirección. "
                "Esta base está en %s. Pide tu clave de licencia a DS Indigo.", base_url))
        valores = {
            'trial_state': 'requested',
            'trial_error': False,
            'trial_requested_at': fields.Datetime.now(),
            'trial_challenge_id': secrets.token_urlsafe(16),
            'trial_challenge_secret': secrets.token_urlsafe(32),
        }
        if company != self.company_id:
            valores['company_id'] = company.id
        self.write(valores)
        cron = self.env.ref('dsi_license_client.ir_cron_trial_request', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_request_trial(self):
        for rec in self.sudo().search([('trial_state', '=', 'requested')]):
            try:
                with self.env.cr.savepoint():
                    rec._do_trial_request()
            except Exception as e:  # pylint: disable=broad-except
                _logger.exception("La solicitud de prueba falló para %s (company_id=%s)",
                                  rec.product_code, rec.company_id.id)
                rec._trial_failed(str(e)[:300])
            # El servidor ya gastó la prueba (una por RFC, base y URL): si otro
            # registro tumbara la transacción, se perderían la clave y el token.
            _commit(self.env)

    def _do_trial_request(self):
        self.ensure_one()
        vence = self.trial_requested_at + timedelta(minutes=TRIAL_CHALLENGE_MINUTES)
        if fields.Datetime.now() > vence:
            self._trial_failed(_("La solicitud venció antes de poder verificarse. Vuelve a intentarlo."))
            return
        payload = {
            'product_code': self.product_code,
            'rfc': self._license_rfc(),
            'company_name': self.company_id.name,
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
                with self.env.cr.savepoint():
                    rec._do_heartbeat()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Heartbeat de licenciamiento falló para %s (company_id=%s)",
                                  rec.product_code, rec.company_id.id)
            try:
                with self.env.cr.savepoint():
                    rec._send_trial_reminders()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("No se pudo avisar del fin de la prueba de %s (company_id=%s)",
                                  rec.product_code, rec.company_id.id)
            _commit(self.env)

    def _reminder_partners(self):
        """A quiénes se avisa que la prueba está por terminar: los
        administradores de la empresa, o de cualquier empresa si la licencia
        es por base. Los módulos del producto agregan a sus usuarios (ver
        dsi_cfdi_vault)."""
        self.ensure_one()
        usuarios = self.env['res.users'].sudo().search([('share', '=', False)])
        if self.license_scope != 'database':
            usuarios = usuarios.filtered(lambda u: self.company_id in u.company_ids)
        return usuarios.filtered(lambda u: u.has_group('base.group_system')).partner_id

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
        info = self._product_info(self.product_code)
        cuando = _("mañana") if dias <= 1 else _("en %s días", dias)
        de_quien = _("esta base") if self.license_scope == 'database' else self.company_id.name
        self.env['mail.thread'].sudo().message_notify(
            partner_ids=partners.ids,
            subject=_("Tu prueba de %(producto)s termina %(cuando)s", producto=info['name'], cuando=cuando),
            body=Markup("<p>%s</p><p>%s</p>") % (
                _("La prueba de %(producto)s de %(empresa)s termina el %(fin)s.",
                  producto=info['name'], empresa=de_quien, fin=format_date(self.env, fin)),
                info['reminder_text']),
        )

    def _do_heartbeat(self):
        self.ensure_one()
        payload = {
            'license_key': _normalize_key(self.license_key),
            'db_uuid': self._get_db_uuid(),
            'product_code': self.product_code,
            'odoo_version': release.version,
            'module_version': self._get_module_version(),
        }
        try:
            response = requests.post(
                f"{self.activation_url.rstrip('/')}/dsi/license/v1/heartbeat",
                json=payload, timeout=REQUEST_TIMEOUT, proxies=self._get_proxies(),
            )
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as e:
            self.last_error = str(e)
            return
        if response.status_code == 200 and data.get('token'):
            try:
                self._apply_token(data['token'])
            except UserError as e:
                self.last_error = str(e)
        else:
            self.last_error = data.get('error') or _("El servidor de licencias no renovó el token.")
