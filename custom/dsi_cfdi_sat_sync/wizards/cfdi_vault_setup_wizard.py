from cryptography.fernet import Fernet

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_date

from odoo.addons.dsi_cfdi_vault.wizards.cfdi_vault_setup_wizard import _rfc_limpio

from ..crypto import MASTER_KEY_ENV, MASTER_KEY_PARAM, is_valid_master_key, master_key_source


class CfdiVaultSetupWizard(models.TransientModel):
    """Los pasos de la Descarga SAT en la configuración inicial: la llave que
    cifra la e.firma, la e.firma de la empresa y la descarga automática (esta
    última opcional)."""
    _inherit = 'dsi.cfdi.vault.setup.wizard'

    llave_origen = fields.Selection([
        ('conf', "odoo.conf"),
        ('env', "Variable de entorno"),
        ('param', "Parámetro del sistema"),
    ], compute='_compute_llave')
    llave_ok = fields.Boolean(compute='_compute_llave')
    llave_texto = fields.Char(compute='_compute_llave')

    efirma_ok = fields.Boolean(compute='_compute_efirma')
    efirma_texto = fields.Char(compute='_compute_efirma')

    descarga_ok = fields.Boolean(compute='_compute_descarga')
    descarga_texto = fields.Char(compute='_compute_descarga')

    @api.depends('company_id')
    def _compute_llave(self):
        origen, llave = master_key_source(self.env)
        valida = bool(llave) and is_valid_master_key(llave)
        if not origen:
            texto = _("Sin ella no se puede guardar la e.firma. En un servidor propio va en el "
                      "odoo.conf como %(param)s (el instalador la agrega con --generar-llave-sat). En "
                      "Odoo.sh, que no deja editar el odoo.conf, genérala aquí.",
                      param=MASTER_KEY_PARAM)
        elif not valida:
            texto = _("La llave configurada (%s) no es válida: tiene que ser una llave de Fernet, "
                      "44 caracteres en base64. Revísala.",
                      dict(self._fields['llave_origen'].selection)[origen])
        elif origen == 'conf':
            texto = _("En el odoo.conf del servidor.")
        elif origen == 'env':
            texto = _("En la variable de entorno %s del servidor.", MASTER_KEY_ENV)
        else:
            texto = _("Guardada como parámetro del sistema (lo normal en Odoo.sh). Queda en cada "
                      "respaldo de la base junto con la e.firma cifrada: trata los respaldos como "
                      "información sensible.")
        for wiz in self:
            wiz.llave_origen = origen or False
            wiz.llave_ok = valida
            wiz.llave_texto = texto

    @api.depends('company_id')
    def _compute_efirma(self):
        hoy = fields.Date.context_today(self)
        estados = dict(self.env['sat.credential']._fields['state'].selection)
        for wiz in self:
            efirma = self.env['sat.credential'].sudo().search(
                [('company_id', '=', wiz.company_id.id)], limit=1)
            rfc_empresa = _rfc_limpio(wiz.company_id.vat)
            wiz.efirma_ok = False
            if not efirma:
                wiz.efirma_texto = _("No hay e.firma cargada para esta empresa: el .cer, el .key y "
                                     "su contraseña.")
            elif efirma.state != 'validated':
                wiz.efirma_texto = _("La e.firma de %(rfc)s está %(estado)s: carga una vigente.",
                                     rfc=efirma.rfc, estado=estados[efirma.state].lower())
            elif efirma.date_valid_to and efirma.date_valid_to < hoy:
                wiz.efirma_texto = _("La e.firma de %(rfc)s venció el %(fecha)s: carga una vigente.",
                                     rfc=efirma.rfc, fecha=format_date(self.env, efirma.date_valid_to))
            elif rfc_empresa and _rfc_limpio(efirma.rfc) != rfc_empresa:
                wiz.efirma_texto = _("La e.firma es de %(efirma)s y la empresa tiene el RFC "
                                     "%(empresa)s: el SAT solo entrega los CFDI del RFC de la e.firma.",
                                     efirma=efirma.rfc, empresa=rfc_empresa)
            else:
                wiz.efirma_ok = True
                wiz.efirma_texto = _("e.firma de %(rfc)s, vigente hasta el %(fecha)s.",
                                     rfc=efirma.rfc, fecha=format_date(self.env, efirma.date_valid_to))

    @api.depends('company_id')
    def _compute_descarga(self):
        periodicidades = dict(self.env['sat.sync.schedule']._fields['periodicity'].selection)
        for wiz in self:
            programa = self.env['sat.sync.schedule'].sudo().search(
                [('company_id', '=', wiz.company_id.id)], limit=1)
            wiz.descarga_ok = bool(programa)
            if programa:
                wiz.descarga_texto = _("%(periodicidad)s, trayendo desde el %(fecha)s.",
                                       periodicidad=periodicidades[programa.periodicity],
                                       fecha=format_date(self.env, programa.date_start))
            else:
                wiz.descarga_texto = _("Sin programar: los CFDI del SAT solo se descargan cuando "
                                       "los pides.")

    def _pasos_obligatorios(self):
        return super()._pasos_obligatorios() + [self.llave_ok, self.efirma_ok]

    @api.depends('empresa_ok', 'rfc_ok', 'licencia_ok', 'llave_ok', 'efirma_ok')
    def _compute_listo(self):
        return super()._compute_listo()

    # -------------------------------------------------------------------
    # Botones
    # -------------------------------------------------------------------

    def action_generar_llave(self):
        """Para Odoo.sh, donde no hay odoo.conf propio: genera la llave y la
        guarda como parámetro del sistema. Nunca reemplaza una que ya exista:
        la e.firma guardada dejaría de poder descifrarse."""
        self.ensure_one()
        origen, _llave = master_key_source(self.env)
        if origen:
            raise UserError(_("Ya hay una llave configurada; no se reemplaza: la e.firma guardada "
                              "dejaría de poder descifrarse."))
        cp = self.env['ir.config_parameter'].sudo()
        # Odoo 20 sustituyó set_param por accesores tipados.
        guardar = getattr(cp, 'set_str', None) or cp.set_param
        guardar(MASTER_KEY_PARAM, Fernet.generate_key().decode())
        return True

    def action_cargar_efirma(self):
        self.ensure_one()
        accion = self.env['ir.actions.act_window']._for_xml_id(
            'dsi_cfdi_sat_sync.action_sat_credential_import_wizard')
        accion['context'] = {
            'default_company_id': self.company_id.id,
            # Al cargarla, cierra el diálogo y vuelve aquí en vez de abrir la e.firma.
            'dsi_cfdi_vault_setup': True,
        }
        return accion

    def action_programar_descarga(self):
        self.ensure_one()
        efirma = self.env['sat.credential'].search(
            [('company_id', '=', self.company_id.id), ('state', '=', 'validated')], limit=1)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Descarga automática"),
            'res_model': 'sat.sync.schedule',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_company_id': self.company_id.id,
                'default_credential_id': efirma.id,
            },
        }
