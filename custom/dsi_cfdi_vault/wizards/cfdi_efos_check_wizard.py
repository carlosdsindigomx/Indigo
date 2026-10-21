"""Revisión de las compras de un periodo contra la lista 69-B del SAT.

El cruce automático solo cubre los CFDI que están en la bóveda. Este asistente
revisa además las facturas de proveedor registradas en Odoo, tengan o no CFDI
en la bóveda (por ejemplo, las capturadas a mano), que hoy nadie revisa.
"""
from dateutil.relativedelta import relativedelta

from ..models.cfdi_efos_entry import EFOS_CLEARED_SITUATIONS
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Solo compras, a propósito: lo que el 69-B pone en riesgo es la deducción y el
# IVA acreditable de lo que se le compró a un EFOS.
TIPOS_DE_COMPRA = ('in_invoice', 'in_refund')

# Orden de los proveedores en el resultado: primero los definitivos, que son
# los que obligan a corregir la contabilidad.
ORDEN_SITUACION = {'definitivo': 0, 'presunto': 1, 'other': 2}


def _rfc_normalizado(vat):
    """El RFC como viene en la lista del SAT: mayúsculas, sin espacios ni
    guiones y sin el prefijo de país que algunos capturan ('MX...')."""
    rfc = (vat or '').upper().replace(' ', '').replace('-', '')
    if rfc.startswith('MX') and len(rfc) > 13:
        rfc = rfc[2:]
    return rfc


class CfdiEfosCheckWizard(models.TransientModel):
    _name = 'dsi.cfdi.efos.check.wizard'
    _description = "Revisar un periodo contra la lista 69-B"

    def _default_date_from(self):
        # El mes anterior completo: el periodo que se revisa al cerrar el mes.
        return fields.Date.context_today(self).replace(day=1) - relativedelta(months=1)

    def _default_date_to(self):
        return fields.Date.context_today(self).replace(day=1) - relativedelta(days=1)

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True,
        default=lambda self: self.env.company)
    date_from = fields.Date(string="Desde", required=True, default=_default_date_from)
    date_to = fields.Date(string="Hasta", required=True, default=_default_date_to)
    refresh_list = fields.Boolean(
        string="Actualizar la lista del SAT antes de revisar", default=True,
        help="Descarga la versión más reciente del listado completo 69-B. Si el SAT "
             "no responde, se revisa con la lista que ya está cargada y se avisa.")
    state = fields.Selection([
        ('draft', "Borrador"),
        ('done', "Terminado"),
    ], string="Estado", default='draft')

    # Resultado
    cfdi_ids = fields.Many2many('cfdi.document', string="CFDI de proveedores en la lista")
    move_ids = fields.Many2many('account.move', string="Facturas de proveedores en la lista")
    line_ids = fields.One2many(
        'dsi.cfdi.efos.check.wizard.line', 'wizard_id', string="Proveedores en la lista")
    cfdi_checked_count = fields.Integer(string="CFDI revisados", readonly=True)
    move_checked_count = fields.Integer(string="Facturas revisadas", readonly=True)
    cfdi_count = fields.Integer(string="CFDI en riesgo", compute='_compute_counts')
    move_count = fields.Integer(string="Facturas en riesgo", compute='_compute_counts')
    list_date = fields.Datetime(string="Lista cargada el", readonly=True)
    list_risk_count = fields.Integer(string="Contribuyentes en riesgo en la lista", readonly=True)
    refresh_error = fields.Char(string="Aviso", readonly=True)

    @api.depends('cfdi_ids', 'move_ids')
    def _compute_counts(self):
        for wizard in self:
            wizard.cfdi_count = len(wizard.cfdi_ids)
            wizard.move_count = len(wizard.move_ids)

    def action_check(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_("La fecha inicial es posterior a la final."))

        # sudo: es la lista oficial del SAT (o la URL que configuró el
        # administrador), la misma que baja el cron semanal. Lo que sigue
        # reservado a Asesor es subir un CSV a mano.
        efos = self.env['dsi.cfdi.efos.entry'].sudo()
        refresh_error = False
        if self.refresh_list:
            result = efos._refresh_from_url()
            if not result.get('ok'):
                refresh_error = _(
                    "No se pudo actualizar la lista del SAT, así que se revisó con la que ya "
                    "estaba cargada. Detalle: %s", result.get('error'))
        if not efos.search_count([]):
            raise UserError(_(
                "No hay ninguna lista 69-B cargada y no se pudo descargar del SAT. "
                "Cárgala desde «Cargar lista 69-B» o vuelve a intentar más tarde."))
        en_riesgo = {
            entry.rfc: entry
            for entry in efos.search([('situation', 'not in', EFOS_CLEARED_SITUATIONS)])
        }

        cfdis = self.env['cfdi.document'].search([
            ('company_id', '=', self.company_id.id),
            ('direction', '=', 'received'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])
        cfdis_riesgo = cfdis.filtered(lambda d: _rfc_normalizado(d.partner_rfc_issuer) in en_riesgo)

        facturas = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', TIPOS_DE_COMPRA),
            ('state', '!=', 'cancel'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])
        facturas_riesgo = facturas.filtered(
            lambda m: _rfc_normalizado(m.commercial_partner_id.vat) in en_riesgo)

        por_proveedor = {}
        for doc in cfdis_riesgo:
            rfc = _rfc_normalizado(doc.partner_rfc_issuer)
            por_proveedor.setdefault(rfc, {'cfdi_count': 0, 'move_count': 0, 'partner_id': False})
            por_proveedor[rfc]['cfdi_count'] += 1
        for move in facturas_riesgo:
            rfc = _rfc_normalizado(move.commercial_partner_id.vat)
            por_proveedor.setdefault(rfc, {'cfdi_count': 0, 'move_count': 0, 'partner_id': False})
            por_proveedor[rfc]['move_count'] += 1
            por_proveedor[rfc]['partner_id'] = move.commercial_partner_id.id
        lineas = sorted(
            (
                {
                    'rfc': rfc,
                    'name': en_riesgo[rfc].name,
                    'situation': en_riesgo[rfc].situation,
                    **valores,
                }
                for rfc, valores in por_proveedor.items()
            ),
            key=lambda v: (ORDEN_SITUACION.get(v['situation'], 9), -(v['cfdi_count'] + v['move_count'])),
        )

        self.line_ids.unlink()
        self.write({
            'state': 'done',
            'cfdi_ids': [(6, 0, cfdis_riesgo.ids)],
            'move_ids': [(6, 0, facturas_riesgo.ids)],
            'line_ids': [(0, 0, valores) for valores in lineas],
            'cfdi_checked_count': len(cfdis),
            'move_checked_count': len(facturas),
            'list_date': efos.search([], order='create_date desc', limit=1).create_date,
            'list_risk_count': len(en_riesgo),
            'refresh_error': refresh_error,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Revisar periodo contra lista 69-B"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_view_cfdi(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("CFDI de proveedores en la lista 69-B"),
            'res_model': 'cfdi.document',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.cfdi_ids.ids)],
            'context': {'create': False},
        }

    def action_view_moves(self):
        self.ensure_one()
        # Las vistas normales de Facturas de proveedor, pero solo con las halladas.
        # Sin id ni path: con ellos la URL apunta a la acción estándar y, al
        # recargar la página, se abrirían todas las facturas, no solo estas.
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_in_invoice_type')
        for clave in ('id', 'path', 'xml_id'):
            action.pop(clave, None)
        nombre = _("Facturas de proveedores en la lista 69-B")
        action.update({
            'name': nombre,
            'display_name': nombre,
            'domain': [('id', 'in', self.move_ids.ids)],
            'context': {'create': False},
        })
        return action


class CfdiEfosCheckWizardLine(models.TransientModel):
    _name = 'dsi.cfdi.efos.check.wizard.line'
    _description = "Proveedor del periodo que está en la lista 69-B"

    wizard_id = fields.Many2one('dsi.cfdi.efos.check.wizard', required=True, ondelete='cascade')
    rfc = fields.Char(string="RFC")
    name = fields.Char(string="Razón social (lista 69-B)")
    situation = fields.Selection(
        selection=lambda self: self.env['dsi.cfdi.efos.entry']._fields['situation'].selection,
        string="Situación")
    partner_id = fields.Many2one('res.partner', string="Contacto en Odoo")
    cfdi_count = fields.Integer(string="CFDI")
    move_count = fields.Integer(string="Facturas")
