import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_date

from odoo.addons.dsi_license_client.models.dsi_license_client_state import DEFAULT_PRODUCT

# Forma de un RFC: 3 letras (moral) o 4 (física), fecha AAMMDD y homoclave.
RFC_RE = re.compile(r'^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$')
# Estados de la licencia con los que la Bóveda trabaja completa.
LICENCIA_VIGENTE = ('trial', 'active', 'grace')


def _rfc_limpio(rfc):
    """El RFC como se compara: sin espacios ni guiones y en mayúsculas. Quita
    el prefijo de país que traen algunas bases (MXEKU9003173C9)."""
    rfc = re.sub(r'[\s-]', '', rfc or '').upper()
    if len(rfc) in (14, 15) and rfc.startswith('MX'):
        rfc = rfc[2:]
    return rfc


class CfdiVaultSetupWizard(models.TransientModel):
    """Configuración inicial de la Bóveda: lo que hace falta para trabajar en
    una empresa, paso por paso, cada uno con su estado («Listo» o «Falta») y
    el botón que lo resuelve. Se abre solo al instalar desde Aplicaciones
    (ir.actions.todo de data/cfdi_vault_setup_todo.xml) y siempre está en
    Bóveda CFDI > Configuración inicial, para administradores.

    No guarda nada propio salvo el RFC que se está capturando: los estados son
    campos calculados que vuelven a mirar la empresa, la licencia y la lista
    69-B cada vez que el formulario se abre o se recarga (Odoo lo recarga al
    cerrar el diálogo de cualquier paso). dsi_cfdi_sat_sync le agrega la llave
    de la e.firma, la e.firma y la descarga automática."""
    _name = 'dsi.cfdi.vault.setup.wizard'
    _description = "Configuración inicial de la Bóveda CFDI"

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, default=lambda self: self.env.company)

    empresa_ok = fields.Boolean(compute='_compute_empresa')
    empresa_texto = fields.Char(compute='_compute_empresa')

    rfc = fields.Char(string="RFC", compute='_compute_rfc', store=True, readonly=False)
    rfc_ok = fields.Boolean(compute='_compute_rfc_estado')
    rfc_texto = fields.Char(compute='_compute_rfc_estado')
    rfc_cambiado = fields.Boolean(compute='_compute_rfc_estado')

    licencia_ok = fields.Boolean(compute='_compute_licencia')
    licencia_texto = fields.Char(compute='_compute_licencia')

    efos_ok = fields.Boolean(compute='_compute_efos')
    efos_texto = fields.Char(compute='_compute_efos')

    listo = fields.Boolean(compute='_compute_listo')

    def _compute_display_name(self):
        for wiz in self:
            wiz.display_name = _("Configuración inicial")

    @api.model
    def default_get(self, fields_list):
        """Abrirlo a mano cuenta como haberlo visto: si se instaló por línea de
        comandos, el aviso pendiente (ir.actions.todo) ya no salta la próxima
        vez que alguien instale cualquier otro módulo desde Aplicaciones."""
        todo = self.env.ref('dsi_cfdi_vault.todo_cfdi_vault_setup', raise_if_not_found=False)
        if todo and todo.state == 'open':
            todo.sudo().state = 'done'
        return super().default_get(fields_list)

    # -------------------------------------------------------------------
    # Estados
    # -------------------------------------------------------------------

    def _empresas(self):
        """La empresa y sus sucursales: todas tienen que ser de México en MXN."""
        self.ensure_one()
        return self.env['res.company'].sudo().search([('id', 'child_of', self.company_id.id)])

    @api.depends('company_id')
    def _compute_empresa(self):
        mexico = self.env.ref('base.mx')
        pesos = self.env.ref('base.MXN')
        for wiz in self:
            faltas = []
            for empresa in wiz._empresas() if wiz.company_id else []:
                if empresa.country_id != mexico:
                    faltas.append(_("%s no tiene país México", empresa.name))
                if empresa.currency_id != pesos:
                    faltas.append(_("%s no tiene moneda MXN", empresa.name))
            plantilla = wiz.company_id.chart_template or ''
            if wiz.company_id and not plantilla.startswith('mx'):
                faltas.append(_("el catálogo de cuentas no es el mexicano (Contabilidad > Ajustes > "
                                "Localización fiscal)"))
            wiz.empresa_ok = bool(wiz.company_id) and not faltas
            if wiz.empresa_ok:
                wiz.empresa_texto = _("País México, moneda MXN y catálogo de cuentas mexicano.")
            else:
                texto = "; ".join(faltas)
                wiz.empresa_texto = texto[:1].upper() + texto[1:] + "."

    @api.depends('company_id')
    def _compute_rfc(self):
        for wiz in self:
            wiz.rfc = wiz.company_id.vat

    @api.depends('company_id', 'rfc')
    def _compute_rfc_estado(self):
        for wiz in self:
            guardado = _rfc_limpio(wiz.company_id.vat)
            wiz.rfc_cambiado = _rfc_limpio(wiz.rfc) != guardado
            wiz.rfc_ok = bool(RFC_RE.match(guardado))
            if not guardado:
                wiz.rfc_texto = _("La empresa no tiene RFC. La licencia se activa con él: captúralo "
                                  "aquí y guárdalo.")
            elif not wiz.rfc_ok:
                wiz.rfc_texto = _("«%s» no tiene la forma de un RFC (12 o 13 caracteres).", guardado)
            else:
                wiz.rfc_texto = _("La licencia queda atada a este RFC: tiene que ser el mismo que "
                                  "autorizó DS Indigo.")

    @api.depends('company_id')
    def _compute_licencia(self):
        Licencias = self.env['dsi.license.client.state']
        for wiz in self:
            licencia = Licencias._get_product_license(DEFAULT_PRODUCT, wiz.company_id)
            if not licencia:
                wiz.licencia_ok = False
                wiz.licencia_texto = _("Sin activar: captura la clave de DS Indigo o pide la prueba "
                                       "de 7 días.")
                continue
            wiz.licencia_ok = licencia.state in LICENCIA_VIGENTE
            if not wiz.licencia_ok and licencia.trial_state == 'requested':
                wiz.licencia_texto = _("Se pidió la prueba y el servidor de licencias la está "
                                       "verificando. Vuelve a revisar en unos minutos.")
            else:
                wiz.licencia_texto = licencia.status_message or dict(
                    licencia._fields['state'].selection).get(licencia.state)

    @api.depends('company_id')
    def _compute_efos(self):
        Efos = self.env['dsi.cfdi.efos.entry'].sudo()
        total = Efos.search_count([])
        ultima = Efos.search([], order='write_date desc', limit=1).write_date
        for wiz in self:
            wiz.efos_ok = bool(total)
            if total:
                wiz.efos_texto = _("%(total)s contribuyentes, cargada el %(fecha)s. Se intenta "
                                   "actualizar sola cada semana.",
                                   total=total, fecha=format_date(self.env, ultima))
            else:
                wiz.efos_texto = _("Vacía: sin ella la Bóveda no avisa de proveedores en la lista "
                                   "69-B del SAT.")

    def _pasos_obligatorios(self):
        """Los pasos sin los que la Bóveda no trabaja. dsi_cfdi_sat_sync suma
        los suyos. La lista 69-B es recomendada, no obligatoria."""
        self.ensure_one()
        return [self.empresa_ok, self.rfc_ok, self.licencia_ok]

    @api.depends('empresa_ok', 'rfc_ok', 'licencia_ok')
    def _compute_listo(self):
        for wiz in self:
            wiz.listo = all(wiz._pasos_obligatorios())

    # -------------------------------------------------------------------
    # Botones
    # -------------------------------------------------------------------

    def action_abrir_empresa(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.company_id.name,
            'res_model': 'res.company',
            'res_id': self.company_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
        }

    def action_guardar_rfc(self):
        self.ensure_one()
        rfc = _rfc_limpio(self.rfc)
        if not RFC_RE.match(rfc):
            raise UserError(_("«%s» no tiene la forma de un RFC: 3 o 4 letras, la fecha (AAMMDD) y "
                              "la homoclave, 12 o 13 caracteres en total.", self.rfc or ''))
        self.company_id.vat = rfc
        self.rfc = rfc
        return True

    def action_licencia(self):
        """El aviso de licencia de siempre (comprar, capturar la clave, pedir
        la prueba), en un diálogo: al activar se cierra y este formulario se
        recarga con el estado nuevo."""
        self.ensure_one()
        licencia = self.env['dsi.license.client.state']._get_or_create(self.company_id)
        return licencia._notice_action(target='new', allow_continue=False)

    def action_descargar_69b(self):
        self.ensure_one()
        return self.env['dsi.cfdi.efos.entry'].sudo().action_actualizar_del_sat(como_cargar=_("Cargar CSV"))

    def action_cargar_69b(self):
        return self.env['ir.actions.act_window']._for_xml_id('dsi_cfdi_vault.action_cfdi_efos_import_wizard')

    def action_ir_a_la_boveda(self):
        self.ensure_one()
        # Con la licencia de la empresa del asistente, no la de la empresa activa.
        return self.env['cfdi.document'].with_company(self.company_id)._vault_entry_action()
