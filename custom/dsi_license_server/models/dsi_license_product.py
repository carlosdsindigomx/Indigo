import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

CFDI_VAULT_CODE = 'cfdi_vault'
# Códigos con que puede estar dada de alta la Bóveda en este servidor. En
# producción se dio de alta a mano como 'I-BCDFI', antes de que el código del
# producto importara. Los clientes la piden y la reciben siempre como
# 'cfdi_vault' (ver client_code): es el único código que conocen.
CFDI_VAULT_CODES = (CFDI_VAULT_CODE, 'I-BCDFI')
# Funciones de la Bóveda, fijas en código. Los clientes ya instalados solo
# conocen estas y abren la Bóveda con 'vault': si cambian, se rompe el
# contrato con ellos (lo vigila la prueba del payload exacto).
CFDI_VAULT_FEATURES = {
    'basic': ('vault',),
    'pro': ('vault', 'reconcile'),
}
# Solo la Bóveda puede emitirlas. Si otro producto las llevara, su clave
# abriría la Bóveda en cualquier cliente.
RESERVED_FEATURES = frozenset(f for features in CFDI_VAULT_FEATURES.values() for f in features)
FEATURE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

MFG_PRODUCT_CODE = 'mrp_manufactura'
# Productos que el módulo da de alta si no existen (ver
# _ensure_default_products). Nunca se escriben sobre uno existente.
DEFAULT_PRODUCTS = [
    {
        'code': MFG_PRODUCT_CODE,
        'name': "Manufactura · Kiosco y Control de Producción",
        'supported_versions': '19.0,20.0',
        # Apagada: se enciende a mano cuando salgan los módulos y la Bóveda
        # haya renovado sin problemas con el servidor nuevo.
        'allow_trial': False,
        'trial_days': 7,
        'trial_edition': 'pro',
        'features_basic': MFG_PRODUCT_CODE,
        'features_pro': MFG_PRODUCT_CODE,
    },
]


def normalize_version(version):
    """La versión de Odoo sin sufijos de build: '19.0+e' (enterprise) y
    '19.0-20260101' quedan en '19.0'."""
    return str(version or '').strip().split('+')[0].split('-')[0].strip()


def split_csv(value):
    """'a, b,,c' → ['a', 'b', 'c']."""
    return [v.strip() for v in str(value or '').split(',') if v.strip()]


def client_code(code):
    """El código de producto como lo conocen los clientes: cualquier código
    de la Bóveda es 'cfdi_vault'; los demás, tal cual."""
    code = str(code or '').strip()
    return CFDI_VAULT_CODE if code in CFDI_VAULT_CODES else code


class DsiLicenseProduct(models.Model):
    _name = 'dsi.license.product'
    _description = "Producto licenciable DS Indigo"

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        help="Identificador técnico estable, ej. 'cfdi_vault'. Los clientes lo mandan en cada "
             "llamada y viaja en el token: no se puede cambiar si el producto ya tiene licencias. "
             "La Bóveda CFDI puede llamarse cfdi_vault o I-BCDFI; a los clientes siempre les "
             "llega como cfdi_vault.")
    is_cfdi_vault = fields.Boolean(
        string="Es la Bóveda CFDI", compute='_compute_is_cfdi_vault', search='_search_is_cfdi_vault')
    supported_versions = fields.Char(
        default="19.0", required=True,
        help="Versiones de Odoo soportadas, separadas por coma. Ej: 17.0,18.0,19.0. Se revisan "
             "al dar una prueba (salvo en la Bóveda CFDI); en las licencias pagadas manda el "
             "campo «Versiones de Odoo» de cada licencia.")
    active = fields.Boolean(default=True)
    allow_trial = fields.Boolean(
        string="Permitir prueba",
        help="La base del cliente puede pedir sola una prueba desde Ajustes > Licenciamiento "
             "DS Indigo, sin pasar por aquí. Desmárcalo para dejar de darlas.")
    trial_days = fields.Integer(string="Días de prueba", default=7)
    trial_edition = fields.Selection([
        ('basic', "Basic"),
        ('pro', "Pro"),
    ], string="Edición de la prueba", default='pro', required=True,
        help="Edición con la que se crean las pruebas de autoservicio de este producto.")
    features_basic = fields.Char(
        string="Funciones (Basic)",
        help="Funciones que abre el token en la edición Basic, separadas por coma "
             "(minúsculas, números y guion bajo). La Bóveda CFDI las tiene fijas en código.")
    features_pro = fields.Char(
        string="Funciones (Pro)",
        help="Funciones que abre el token en la edición Pro, separadas por coma "
             "(minúsculas, números y guion bajo). La Bóveda CFDI las tiene fijas en código.")
    features_summary = fields.Char(string="Funciones", compute='_compute_features_summary')
    license_count = fields.Integer(string="Licencias", compute='_compute_license_count')
    purchase_url = fields.Char(
        string="URL de compra",
        help="A dónde lleva el botón «Comprar licencia» de los clientes. Pasa por "
             "/dsi/license/v1/comprar, así que se puede definir o cambiar aquí sin actualizar "
             "el módulo en cada cliente. Se le agrega ?clave=<clave de la licencia>. Vacía, "
             "el cliente ve una página que dice que la compra en línea aún no está disponible.")

    _code_uniq = models.Constraint('unique(code)', "Ya existe un producto con este código.")

    @api.depends('code')
    def _compute_is_cfdi_vault(self):
        for product in self:
            product.is_cfdi_vault = product.code in CFDI_VAULT_CODES

    def _search_is_cfdi_vault(self, operator, value):
        if operator != 'in':
            return NotImplemented
        return [('code', 'in', list(CFDI_VAULT_CODES))]

    def _client_code(self):
        """El código con que los clientes piden este producto y que viaja en
        su token."""
        self.ensure_one()
        # Con la misma regla que is_cfdi_vault (código exacto): client_code()
        # quita espacios, y un ' I-BCDFI' que no es la Bóveda saldría como tal.
        return CFDI_VAULT_CODE if self.is_cfdi_vault else self.code

    @api.model
    def _search_client_code(self, code, domain=None):
        """El producto que pide un cliente con `code`. 'cfdi_vault' encuentra
        la Bóveda aunque esté dada de alta con otro de sus códigos."""
        code = client_code(code)
        if not code:
            return self.browse()
        codes = list(CFDI_VAULT_CODES) if code == CFDI_VAULT_CODE else [code]
        return self.search([('code', 'in', codes)] + list(domain or []), limit=1)

    @api.depends('code', 'features_basic', 'features_pro')
    def _compute_features_summary(self):
        for product in self:
            product.features_summary = "Basic: %s · Pro: %s" % (
                ', '.join(product._features_for('basic')) or '—',
                ', '.join(product._features_for('pro')) or '—',
            )

    def _compute_license_count(self):
        counts = dict(self.env['dsi.license']._read_group(
            [('product_id', 'in', self.ids)], ['product_id'], ['__count']))
        for product in self:
            product.license_count = counts.get(product._origin, 0)

    @api.constrains('purchase_url')
    def _check_purchase_url(self):
        for product in self:
            if product.purchase_url and not product.purchase_url.startswith(('https://', 'http://')):
                raise ValidationError(_("La URL de compra debe empezar con https://"))

    @api.constrains('code')
    def _check_single_cfdi_vault(self):
        # Con dos productos de la Bóveda (cfdi_vault e I-BCDFI) no se sabría
        # cuál dar en la prueba ni en «Comprar».
        Product = self.with_context(active_test=False)
        if any(product.is_cfdi_vault for product in self) and \
                Product.search_count([('code', 'in', list(CFDI_VAULT_CODES))]) > 1:
            raise ValidationError(_(
                "Ya existe el producto de la Bóveda CFDI: solo puede haber uno (con código "
                "%s).", " o ".join(CFDI_VAULT_CODES)))

    @api.constrains('code', 'features_basic', 'features_pro')
    def _check_features(self):
        for product in self:
            if product.is_cfdi_vault:
                if product.features_basic or product.features_pro:
                    raise ValidationError(_(
                        "Las funciones de la Bóveda CFDI están fijas en código (Basic: vault · "
                        "Pro: vault, reconcile). Deja vacíos los campos de funciones."))
                continue
            for label, csv in (("Basic", product.features_basic), ("Pro", product.features_pro)):
                features = split_csv(csv)
                if not features:
                    raise ValidationError(_(
                        "El producto %(producto)s necesita al menos una función en la edición "
                        "%(edicion)s.", producto=product.name, edicion=label))
                mal_formadas = [f for f in features if not FEATURE_RE.match(f)]
                if mal_formadas:
                    raise ValidationError(_(
                        "Funciones no válidas en %(producto)s: %(funciones)s. Usa minúsculas, "
                        "números y guion bajo, separadas por coma.",
                        producto=product.name, funciones=', '.join(mal_formadas)))
                reservadas = [f for f in features if f in RESERVED_FEATURES]
                if reservadas:
                    raise ValidationError(_(
                        "Las funciones %(funciones)s son exclusivas de la Bóveda CFDI: si otro "
                        "producto las emitiera, su clave abriría la Bóveda.",
                        funciones=', '.join(reservadas)))

    def write(self, vals):
        if 'code' in vals:
            License = self.env['dsi.license'].sudo()
            for product in self:
                if product.code != vals['code'] and License.search_count([('product_id', '=', product.id)]):
                    raise UserError(_(
                        "No se puede cambiar el código de %s: ya tiene licencias y los clientes "
                        "lo usan para pedir y validar su token.", product.name))
        return super().write(vals)

    # -------------------------------------------------------------------
    # Funciones del token y versiones
    # -------------------------------------------------------------------

    def _features_for(self, edition):
        """Las funciones que abre el token de una licencia de este producto en
        esa edición. La Bóveda, fijas en código; los demás productos, las de
        su configuración y nunca las de la Bóveda (aunque alguien las haya
        metido sin pasar por la validación)."""
        self.ensure_one()
        if self.is_cfdi_vault:
            return list(CFDI_VAULT_FEATURES['pro' if edition == 'pro' else 'basic'])
        csv = self.features_pro if edition == 'pro' else self.features_basic
        return [f for f in split_csv(csv) if f not in RESERVED_FEATURES]

    def _supported_versions(self):
        self.ensure_one()
        return [normalize_version(v) for v in split_csv(self.supported_versions)]

    def _supports_version(self, odoo_version):
        self.ensure_one()
        versiones = self._supported_versions()
        return not versiones or normalize_version(odoo_version) in versiones

    # -------------------------------------------------------------------
    # Alta de productos (data/dsi_license_product_data.xml)
    # -------------------------------------------------------------------

    @api.model
    def _ensure_default_products(self):
        """Crea los productos de DEFAULT_PRODUCTS que no existan. Corre en cada
        instalación y en cada -u. Nunca escribe sobre uno existente (no pisa la
        URL de compra ni «Permitir prueba» editados aquí) ni revive uno
        archivado. Un <record noupdate> chocaría con unique(code) si alguien lo
        hubiera creado antes a mano."""
        Product = self.sudo().with_context(active_test=False)
        for values in DEFAULT_PRODUCTS:
            if not Product.search_count([('code', '=', values['code'])]):
                Product.create(dict(values))
                _logger.info("Producto licenciable %s dado de alta.", values['code'])
        return True
