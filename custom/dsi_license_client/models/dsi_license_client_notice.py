import logging

from odoo import _, api, fields, models
from odoo.tools import format_date

from .dsi_license_client_state import DEFAULT_PRODUCT, LICENSE_MODES, _normalize_key

_logger = logging.getLogger(__name__)


class DsiLicenseClientNotice(models.TransientModel):
    """Aviso de licencia para cualquier usuario, no solo el administrador:
    se muestra al entrar a un producto sin licencia vigente, al intentar algo
    bloqueado y desde el menú Licencia de cada producto. Ahí mismo se compra
    (botón hacia la tienda) y se activa (capturando la clave). Activar es
    seguro para cualquiera: el servidor solo acepta una clave comprada para el
    RFC de la empresa. Desactivar sigue siendo solo del administrador, en
    Ajustes.

    El producto y la empresa llegan por el contexto con claves propias
    ('dsi_license_product' y 'dsi_license_company'), nunca como
    default_product_code: así no se cuelan a otros modelos."""
    _name = 'dsi.license.client.notice'
    _description = "Aviso de licencia DS Indigo"

    product_code = fields.Char(required=True, default=lambda self: self._default_product_code())
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self._default_company())
    license_key = fields.Char(string="Clave de licencia", default=lambda self: self._default_license_key())
    next_action = fields.Char(help="XML ID de la acción a la que llevan «Seguir solo consultando» y «Entrar».")
    target = fields.Char(default='current')
    allow_continue = fields.Boolean(
        default=True, help="Ofrece «Seguir solo consultando» cuando no hay licencia vigente.")

    state = fields.Selection(
        selection=lambda self: self.env['dsi.license.client.state']._fields['state'].selection,
        compute='_compute_license')
    status_message = fields.Char(compute='_compute_license')
    title = fields.Char(compute='_compute_license')
    is_trial = fields.Boolean(compute='_compute_license')
    trial_state = fields.Selection(
        selection=lambda self: self.env['dsi.license.client.state']._fields['trial_state'].selection,
        compute='_compute_license')
    trial_error = fields.Char(compute='_compute_license')
    license_mode = fields.Selection(LICENSE_MODES, compute='_compute_license')
    can_enter = fields.Boolean(compute='_compute_license')
    product_name = fields.Char(compute='_compute_license')
    notice_text = fields.Char(compute='_compute_license')
    trial_text = fields.Char(compute='_compute_license')
    read_text = fields.Char(compute='_compute_license')

    @api.model
    def _default_product_code(self):
        return self.env.context.get('dsi_license_product') or DEFAULT_PRODUCT

    @api.model
    def _default_company(self):
        company_id = self.env.context.get('dsi_license_company')
        if company_id:
            return self.env['res.company'].browse(company_id)
        return self.env.company

    @api.model
    def _default_license_key(self):
        # La de la prueba: si DS Indigo la convierte en suscripción al comprar,
        # basta con pulsar Activar, sin capturar nada.
        contexto = self.env.context
        company_id = contexto.get('dsi_license_company') or contexto.get('default_company_id')
        company = self.env['res.company'].browse(company_id) if company_id else self.env.company
        licencia = self.env['dsi.license.client.state']._get_product_license(
            self._default_product_code(), company)
        return licencia.license_key

    @api.depends('product_code', 'company_id')
    def _compute_display_name(self):
        State = self.env['dsi.license.client.state']
        for notice in self:
            notice.display_name = _("Licencia de %s", State._product_info(notice.product_code)['name'])

    def _license(self, create=False):
        """El registro de licencia del producto. Sin create no escribe nada:
        el aviso se lee también en transacciones de solo lectura."""
        self.ensure_one()
        return self.env['dsi.license.client.state']._get_product_license(
            self.product_code or DEFAULT_PRODUCT, self.company_id, create=create)

    @api.depends('company_id', 'product_code', 'next_action', 'target')
    def _compute_license(self):
        State = self.env['dsi.license.client.state']
        for notice in self:
            info = State._product_info(notice.product_code)
            producto = info['name']
            licencia = notice._license()
            if licencia:
                detalle = licencia._evaluate_detail()
                modo, _mensaje = State._status_from_detail(detalle, info)
                estado = detalle['state']
                es_prueba = (detalle['payload'] or {}).get('license_type') == 'trial'
                notice.status_message = detalle['message']
                notice.trial_state = licencia.trial_state
                notice.trial_error = licencia.trial_error
            else:
                detalle = {'kind': None, 'limite': None}
                modo, estado, es_prueba = 'none', 'not_activated', False
                notice.status_message = _("Esta base no tiene una licencia activada.")
                notice.trial_state = 'none'
                notice.trial_error = False
            notice.state = estado
            notice.is_trial = es_prueba
            notice.license_mode = modo
            notice.product_name = producto
            notice.can_enter = bool(notice.next_action) and notice.target == 'main' and modo != 'none'
            notice.notice_text = info['notice_text'] if State._shows_block(estado, modo, info) else False
            notice.trial_text = info['trial_text'] if estado == 'trial' else False
            notice.read_text = info['read_text'] if modo == 'read' else False
            if estado == 'not_activated':
                titulo = _("%s no tiene una licencia activada", producto)
            elif estado == 'restricted' and es_prueba:
                titulo = _("Tu prueba de %s terminó", producto)
            elif estado == 'restricted':
                titulo = _("Tu licencia de %s no está vigente", producto)
            elif estado == 'grace' and modo == 'read':
                titulo = _("Tu licencia de %(producto)s venció: solo consulta hasta el %(limite)s",
                           producto=producto, limite=format_date(self.env, detalle['limite']))
            elif estado == 'grace' and detalle['kind'] == 'renewal' and notice.product_code != DEFAULT_PRODUCT:
                # La Bóveda conserva su título de siempre.
                titulo = _("No se ha podido renovar tu licencia de %s", producto)
            elif estado == 'grace':
                titulo = _("Tu licencia de %s venció", producto)
            elif estado == 'trial':
                titulo = _("Estás usando la prueba de %s", producto)
            else:
                titulo = _("Tu licencia de %s está vigente", producto)
            notice.title = titulo

    def _form_action(self):
        """Este aviso, con su id."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Licencia de %s", self.env['dsi.license.client.state']._product_info(
                self.product_code)['name']),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': self.target or 'current',
        }

    def _reopen(self):
        """El aviso otra vez, después de un botón. En pantalla completa (la
        entrada de un producto) se abre como registro nuevo con sus valores en
        el contexto, igual que la entrada (_entry_notice_action): con su id en
        la URL, una tableta que se queda en el aviso daría «registro
        inexistente» al recargar después de que Odoo borre los temporales.
        En una ventana emergente o dentro de la ruta, con su id, como
        siempre."""
        self.ensure_one()
        if self.target == 'main':
            return self.env['dsi.license.client.state']._entry_notice_action(
                self.product_code, self.next_action, self.company_id,
                allow_continue=self.allow_continue)
        return self._form_action()

    def _next(self):
        """A dónde seguir: la acción indicada o, si es una ventana emergente,
        cerrarla. Sirve cualquier tipo de acción (de ventana, cliente o de
        servidor): con una acción cliente, la activación ya no se revierte."""
        self.ensure_one()
        if not self.next_action:
            return {'type': 'ir.actions.act_window_close'}
        try:
            accion = self.env['ir.actions.actions']._for_xml_id(self.next_action)
        except (ValueError, AssertionError):
            _logger.warning("El aviso de licencia no encontró la acción %s", self.next_action)
            return {'type': 'ir.actions.act_window_close'}
        if accion.get('type') == 'ir.actions.server':
            # La entrada de un producto (ver _entry_action): con esto devuelve
            # su destino sin dejar el aviso en la ruta de navegación.
            accion['context'] = {'dsi_license_from_notice': True}
        elif self.target == 'main' and accion.get('target') != 'fullscreen':
            # Desde la pantalla al entrar: el destino sin el aviso en la ruta.
            accion['target'] = 'main'
        return accion

    def action_buy(self):
        self.ensure_one()
        licencia = self._license(create=True)
        if _normalize_key(self.license_key) and not licencia.license_key:
            licencia.license_key = _normalize_key(self.license_key)
        return {'type': 'ir.actions.act_url', 'url': licencia._purchase_url(), 'target': 'new'}

    def action_activate(self):
        self.ensure_one()
        licencia = self._license(create=True)
        if _normalize_key(self.license_key):
            licencia.license_key = _normalize_key(self.license_key)
        licencia.action_activate()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Licencia activada"),
                'message': licencia.status_message,
                'type': 'success',
                'sticky': False,
                'next': self._next(),
            },
        }

    def action_start_trial(self):
        self.ensure_one()
        self._license(create=True).action_request_trial()
        return self._reopen()

    def action_refresh(self):
        self.ensure_one()
        return self._reopen()

    def action_continue(self):
        """«Seguir solo consultando» y «Entrar». Sin licencia, un producto que
        no deja seguir consultando vuelve al aviso aunque el botón se llame a
        mano."""
        self.ensure_one()
        if not self.allow_continue and self.license_mode == 'none':
            return self._reopen()
        return self._next()
