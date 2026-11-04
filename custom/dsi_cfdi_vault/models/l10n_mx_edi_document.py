from odoo import api, fields, models


class L10nMxEdiDocument(models.Model):
    _inherit = 'l10n_mx_edi.document'

    # Un REP recibido en la pestaña CFDI de las compras que paga (ver
    # cfdi_payment_line.py). Estado propio y no uno de Odoo: «Recibido» le
    # robaría el folio fiscal a la factura, y «Pago enviado» trae Cancelar con
    # el PAC de la empresa, Imprimir y la cancelación automática del pago. Este
    # no está en ningún mapa de botones de Odoo ni en sus consultas al SAT, y
    # no cuenta para el folio ni el estado CFDI de la factura. Al desinstalar,
    # sus documentos se borran.
    state = fields.Selection(
        selection_add=[('payment_received', "Pago recibido")],
        ondelete={'payment_received': 'cascade'},
    )

    def _get_action_l10n_mx_edi_document_domain(self):
        """Solo Odoo 20: el dominio de Contabilidad › Auditoría › México ›
        Documentos CFDI, la lista de lo que Odoo timbró, importó o descargó.
        Un «Pago recibido» es la copia de un REP de la Bóveda en la pestaña
        CFDI de las compras que paga: ahí no es de Odoo y solo hace ruido. En
        18 y 19 ni el menú ni este método existen en Odoo."""
        de_odoo = getattr(super(), '_get_action_l10n_mx_edi_document_domain', None)
        return [*(de_odoo() if de_odoo else []), ('state', '!=', 'payment_received')]

    @api.model
    def _quitar_pagos_recibidos_del_menu_de_odoo(self):
        """Pone el dominio de arriba en la acción de ese menú. Odoo lo evalúa
        una sola vez, al cargar l10n_mx_edi (eval="obj()._get_action_..."), y
        lo guarda en la acción, antes de que exista este módulo: sobrescribir
        el método no basta. Corre al instalar o actualizar la Bóveda (data/
        cfdi_payment_lines.xml); si se actualiza l10n_mx_edi, Odoo actualiza
        también la Bóveda, que depende de él, y lo vuelve a poner. En 18 y 19 la
        acción no existe y no hace nada."""
        accion = self.env.ref('l10n_mx_edi.action_l10n_mx_edi_document', raise_if_not_found=False)
        if not accion:
            return
        dominio = str(self._get_action_l10n_mx_edi_document_domain())
        if accion.domain != dominio:
            accion.sudo().domain = dominio
