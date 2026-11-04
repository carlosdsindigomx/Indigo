"""«Elegir pago»: el contador liga a mano un documento relacionado de un REP
con uno de los pagos aplicados a su factura, cuando la Bóveda no pudo
decidir sola (varios cuadran, el que cuadra está fuera de fecha o ya lo
tiene otro REP, o el importe no cuadra con ninguno).

El Usuario de la Bóveda solo lee las líneas de los REP: el asistente vuelve a
validar en el servidor (que el pago siga aplicado a la factura) y escribe con
sudo. La liga a mano se respeta mientras ese pago siga aplicado a la factura;
si deja de estarlo, vuelve a automática con un mensaje en el REP.
"""
from odoo import Command, api, fields, models
from odoo.exceptions import UserError


class RepPagoWizard(models.TransientModel):
    _name = 'dsi.cfdi.rep.pago.wizard'
    _description = "Elegir el pago de Odoo de un REP"

    # Se crea ya guardado con sus opciones (_abrir_para), como el de
    # sugerencias: así el botón de cada opción llama al servidor sin depender
    # de guardar antes el formulario.
    line_id = fields.Many2one(
        'dsi.cfdi.payment.line', string="Documento relacionado", required=True, readonly=True,
        ondelete='cascade')
    payment_doc_id = fields.Many2one(related='line_id.payment_doc_id', string="REP")
    invoice_id = fields.Many2one(related='line_id.invoice_id')
    fecha_pago = fields.Date(related='line_id.fecha_pago')
    num_parcialidad = fields.Integer(related='line_id.num_parcialidad')
    imp_pagado = fields.Monetary(related='line_id.imp_pagado')
    currency_id = fields.Many2one(related='line_id.currency_id')
    # Lo aplicado en Odoo va en la moneda de la factura en Odoo, que puede no
    # ser la MonedaDR del REP.
    invoice_currency_id = fields.Many2one(related='line_id.invoice_currency_id')
    payment_link_state = fields.Selection(related='line_id.payment_link_state')
    payment_link_note = fields.Char(related='line_id.payment_link_note')
    apply_to_node = fields.Boolean(
        string="Aplicar a las demás facturas del mismo pago del REP", default=True,
        help="Liga también a ese pago los demás documentos relacionados del mismo nodo Pago del REP "
             "(salvo los que ya están ligados a mano), si el pago está aplicado a su factura.")
    option_ids = fields.One2many('dsi.cfdi.rep.pago.wizard.line', 'wizard_id', string="Pagos de la factura")

    @api.model
    def _abrir_para(self, linea):
        """El asistente de `linea` con una opción por cada pago aplicado a su
        factura (de las empresas que el usuario tiene abiertas): primero los
        que cuadran, después por cercanía de fecha."""
        linea = linea.sudo()
        factura = linea.invoice_id
        empresas = self.env.companies
        todas = factura._dsi_pagos_aplicados()[factura]
        unidades = [e for e in todas if (e['pago'] or e['asiento']).company_id in empresas]
        # «Cuadra» como al ligar solo: por las reglas de respaldo, si ningún
        # pago de la factura de su ventana cuadra por las de siempre.
        ventana = linea._ventana_de_dias()
        respaldo = not any(linea._importe_cuadra(e) and linea._en_su_ventana(e, ventana) for e in todas)
        actual = linea._clave_de_la_liga()
        opciones = []
        for e in unidades:
            otros = (e['lineas_rep'] - linea).payment_doc_id
            opciones.append({
                'key': e['clave'],
                'account_payment_id': e['pago'].id or False,
                'payment_move_id': e['asiento'].id or False,
                'journal_id': e['diario'].id or False,
                'date': e['fecha'],
                'amount': e['importe'] or 0.0,
                'amount_known': e['importe'] is not None,
                'payment_amount': e['monto'],
                'payment_currency_id': e['moneda_monto'].id or False,
                'days': linea._dias_a(e),
                'matches': linea._importe_cuadra(e, respaldo=respaldo),
                'is_current': e['clave'] == actual,
                'taken_by': ", ".join(otros.mapped('display_name')) or False,
            })
        # sort es estable: los empates quedan en el orden de
        # _dsi_pagos_aplicados (fecha, y el asiento antes que el pago, por id
        # como número; la clave como texto ponía 'pago:1000' antes que
        # 'pago:999').
        opciones.sort(key=lambda o: (not o['matches'], o['days']))
        asistente = self.create({
            'line_id': linea.id,
            'option_ids': [Command.create(dict(opcion, sequence=n)) for n, opcion in enumerate(opciones)],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': "Elegir pago",
            'res_model': self._name,
            'res_id': asistente.id,
            'view_mode': 'form',
            'target': 'new',
        }


class RepPagoWizardLine(models.TransientModel):
    _name = 'dsi.cfdi.rep.pago.wizard.line'
    _description = "Pago de Odoo que se puede elegir para un REP"
    _order = 'sequence, id'

    wizard_id = fields.Many2one('dsi.cfdi.rep.pago.wizard', required=True, ondelete='cascade')
    sequence = fields.Integer()
    # La clave del pago aplicado (account.move._dsi_pagos_aplicados): con ella
    # se vuelve a buscar al elegir, por si cambió desde que se abrió.
    key = fields.Char(string="Clave", readonly=True)
    account_payment_id = fields.Many2one('account.payment', string="Pago", readonly=True)
    payment_move_id = fields.Many2one('account.move', string="Asiento", readonly=True)
    journal_id = fields.Many2one('account.journal', string="Diario", readonly=True)
    date = fields.Date(string="Fecha", readonly=True)
    currency_id = fields.Many2one(related='wizard_id.invoice_currency_id')
    amount = fields.Monetary(string="Aplicado a la factura", readonly=True)
    amount_known = fields.Boolean(
        string="Importe conocido", readonly=True,
        help="Odoo no dice cuánto aplicó a cada factura un pago sin asiento de varias facturas.")
    payment_currency_id = fields.Many2one('res.currency', string="Moneda del pago", readonly=True)
    payment_amount = fields.Monetary(
        string="Total del pago", currency_field='payment_currency_id', readonly=True)
    days = fields.Integer(string="Días de diferencia", readonly=True)
    matches = fields.Boolean(string="Cuadra", readonly=True)
    is_current = fields.Boolean(string="Liga actual", readonly=True)
    taken_by = fields.Char(string="Ya ligado a", readonly=True)

    def action_elegir(self):
        """Liga a mano el documento relacionado (y, con «Aplicar a las demás
        facturas», los del mismo nodo cuya factura tiene ese pago) y vuelve a
        evaluar las demás líneas de esas facturas: la liga automática que
        tenía ese pago queda por elegir o toma otro. Un pago puede quedar
        ligado a mano a varias líneas."""
        self.ensure_one()
        linea = self.wizard_id.line_id.sudo()
        linea.with_user(self.env.user).check_access('read')
        self.env['dsi.license.client.state']._check_feature(
            'vault', self.env._("relacionar un REP con su pago en Odoo"), linea.company_id)
        factura = linea.invoice_id
        unidad = next((e for e in factura._dsi_pagos_aplicados(con_reps=False)[factura] if e['clave'] == self.key),
                      None)
        if unidad is None:
            raise UserError(self.env._(
                "Ese pago ya no está aplicado a la factura %s; vuelve a abrir «Elegir pago».", factura.display_name))
        elegidas = {linea: unidad}
        if self.wizard_id.apply_to_node:
            del_nodo = linea.payment_doc_id.dsi_payment_line_ids.filtered(lambda otra: (
                otra != linea and otra.pago_idx == linea.pago_idx and otra.invoice_id
                and otra.payment_link_state != 'manual'))
            if del_nodo:
                pagos_de = del_nodo.invoice_id._dsi_pagos_aplicados(con_reps=False)
                for otra in del_nodo:
                    suya = next((e for e in pagos_de[otra.invoice_id] if e['clave'] == self.key), None)
                    if suya:
                        elegidas[otra] = suya
        nota = self.env._("Ligado a mano por %s.", self.env.user.name)
        for otra, e in elegidas.items():
            otra.write({
                'account_payment_id': e['pago'].id or False,
                'payment_move_id': e['asiento'].id or False,
                'payment_link_state': 'manual',
                'payment_amount_applied': e['importe'] or 0.0,
                'payment_link_note': nota,
                # Para nombrarlo si se borra (_ligar_con_sus_pagos).
                'manual_payment_name': (e['pago'] or e['asiento']).display_name,
            })
        lineas = linea.browse([otra.id for otra in elegidas])
        self.env['dsi.cfdi.inconsistency']._ligar_reps_con_pagos(lineas, 'rep')
        linea.payment_doc_id.message_post(body=self.env._(
            "Documento %(folio)s (factura %(factura)s) ligado a mano al pago %(pago)s.",
            folio=linea.display_name, factura=factura.display_name,
            pago=(unidad['pago'] or unidad['asiento']).display_name))
        return {'type': 'ir.actions.act_window_close'}
