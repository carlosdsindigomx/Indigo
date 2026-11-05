from odoo import _, fields, models


class DsiLicenseInstance(models.Model):
    _name = 'dsi.license.instance'
    _description = "Instancia activada de una licencia"
    _order = 'date_last_heartbeat desc'

    license_id = fields.Many2one('dsi.license', required=True, ondelete='cascade', index=True)
    database_uuid = fields.Char(string="UUID de la base", required=True)
    base_url = fields.Char()
    odoo_version = fields.Char()
    module_version = fields.Char()
    ip_address = fields.Char()
    date_first_activation = fields.Datetime(default=fields.Datetime.now, required=True)
    date_last_heartbeat = fields.Datetime()
    is_production = fields.Boolean(default=True)
    state = fields.Selection([
        ('active', "Activa"),
        ('inactive', "Inactiva"),
    ], default='active', required=True)
    released_by = fields.Selection([
        ('client', "La propia base"),
        ('operator', "Operador"),
    ], string="Liberada por", readonly=True,
        help="Operador: la base deja de renovar hasta que se reactive aquí o desde su ventana de "
             "licencia. La propia base (botón «Desactivar»): esa base ya no renueva; si otra "
             "empresa de la misma base sigue usando la licencia, vuelve a ocupar el lugar "
             "mientras quepa en el cupo.")

    _license_uuid_uniq = models.Constraint(
        'unique(license_id, database_uuid)',
        "Esta base ya está registrada como instancia de esta licencia.")

    def action_release(self):
        """Botón «Liberar» del operador: la base deja de renovar y su lugar
        queda libre para otra."""
        self._release('operator')

    def _release(self, released_by):
        # Solo las activas: una que ya estaba liberada conserva quién lo hizo
        # (si la base pulsa «Desactivar» después de que el operador la
        # liberó, sigue sin renovar).
        activas = self.filtered(lambda i: i.state == 'active')
        activas.write({'state': 'inactive', 'released_by': released_by})
        if released_by == 'operator':
            for instance in activas:
                instance.license_id.message_post(body=_(
                    "Base %(uuid)s (%(url)s) liberada: deja de renovar.",
                    uuid=instance.database_uuid, url=instance.base_url or '-'))

    def action_reactivate(self):
        """Deshace «Liberar» desde el servidor, si cabe en el cupo. La base
        vuelve a renovar en su siguiente heartbeat."""
        for instance in self.filtered(lambda i: i.state != 'active'):
            instance.license_id._check_cupo()
            instance.write({'state': 'active', 'released_by': False})
            instance.license_id.message_post(body=_(
                "Base %(uuid)s (%(url)s) reactivada desde el servidor.",
                uuid=instance.database_uuid, url=instance.base_url or '-'))
