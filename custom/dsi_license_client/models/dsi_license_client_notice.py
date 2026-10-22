from odoo import _, api, fields, models

from .dsi_license_client_state import PRODUCT_NAME


class DsiLicenseClientNotice(models.TransientModel):
    """Aviso de licencia para cualquier usuario, no solo el administrador:
    se muestra al entrar a la Bóveda sin licencia vigente, al intentar algo
    bloqueado y desde el menú Licencia. Ahí mismo se compra (botón hacia la
    tienda) y se activa (capturando la clave). Activar es seguro para
    cualquiera: el servidor solo acepta una clave comprada para el RFC de esta
    empresa. Desactivar sigue siendo solo del administrador, en Ajustes."""
    _name = 'dsi.license.client.notice'
    _description = "Aviso de licencia DS Indigo"

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    license_key = fields.Char(string="Clave de licencia", default=lambda self: self._default_license_key())
    next_action = fields.Char(help="XML ID de la acción a la que lleva «Seguir solo consultando».")
    target = fields.Char(default='current')

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

    def _compute_display_name(self):
        for notice in self:
            notice.display_name = _("Licencia de %s", PRODUCT_NAME)

    def _license(self):
        self.ensure_one()
        return self.env['dsi.license.client.state']._get_or_create(self.company_id)

    @api.model
    def _default_license_key(self):
        # La de la prueba: si DS Indigo la convierte en suscripción al comprar,
        # basta con pulsar Activar, sin capturar nada.
        company = self.env['res.company'].browse(self.env.context.get('default_company_id')) or self.env.company
        return self.env['dsi.license.client.state']._get_or_create(company).license_key

    @api.depends('company_id')
    def _compute_license(self):
        for notice in self:
            licencia = notice._license()
            es_prueba = licencia._is_trial()
            notice.state = licencia.state
            notice.status_message = licencia.status_message
            notice.is_trial = es_prueba
            notice.trial_state = licencia.trial_state
            notice.trial_error = licencia.trial_error
            if licencia.state == 'not_activated':
                titulo = _("%s no tiene una licencia activada", PRODUCT_NAME)
            elif licencia.state == 'restricted' and es_prueba:
                titulo = _("Tu prueba de %s terminó", PRODUCT_NAME)
            elif licencia.state == 'restricted':
                titulo = _("Tu licencia de %s no está vigente", PRODUCT_NAME)
            elif licencia.state == 'grace':
                titulo = _("Tu licencia de %s venció", PRODUCT_NAME)
            elif licencia.state == 'trial':
                titulo = _("Estás usando la prueba de %s", PRODUCT_NAME)
            else:
                titulo = _("Tu licencia de %s está vigente", PRODUCT_NAME)
            notice.title = titulo

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Licencia de %s", PRODUCT_NAME),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': self.target or 'current',
        }

    def _next(self):
        """A dónde seguir: la acción indicada o, si es una ventana emergente,
        cerrarla."""
        if self.next_action:
            accion = self.env['ir.actions.act_window']._for_xml_id(self.next_action)
            if self.target == 'main':
                # Desde la pantalla al entrar: la lista sin el aviso en la ruta.
                accion['target'] = 'main'
            return accion
        return {'type': 'ir.actions.act_window_close'}

    def action_buy(self):
        self.ensure_one()
        licencia = self._license()
        if self.license_key and not licencia.license_key:
            licencia.license_key = self.license_key.strip()
        return {'type': 'ir.actions.act_url', 'url': licencia._purchase_url(), 'target': 'new'}

    def action_activate(self):
        self.ensure_one()
        licencia = self._license()
        if self.license_key:
            licencia.license_key = self.license_key.strip().upper()
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
        self._license().action_request_trial()
        return self._reopen()

    def action_refresh(self):
        self.ensure_one()
        return self._reopen()

    def action_continue(self):
        self.ensure_one()
        return self._next()
