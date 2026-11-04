"""«Relacionar con póliza»: los CFDI de nómina seleccionados con la póliza
(asiento contable) que los contabiliza.

El asistente se crea ya guardado, con sus candidatas, como el de sugerencias:
así el botón «Elegir» de cada renglón llama al servidor sin depender de un
guardado previo. Hay dos formas de elegir la póliza: de la lista, que el
servidor ordena por evidencia (una cuenta con el neto, el diario de siempre,
la cercanía a la fecha de pago), o buscándola por número o referencia en el
campo «Póliza». Elegir no liga nada: liga «Relacionar», que se escribe sobre
decenas o cientos de CFDI y puede mover los que ya estaban en otra póliza.
"""
from collections import Counter, defaultdict
from datetime import timedelta

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, format_amount, format_date

# Días antes y después de las fechas de pago y de fin de periodo de la
# selección: la póliza de provisión suele fecharse al cierre del periodo y la
# del pago, el día del pago.
HOLGURA_DIAS_POLIZA = 7

# Cada recibo trae su Total (el neto) a dos decimales y el sistema de nómina
# suma los mismos; un peso cubre el redondeo de uno que reparte distinto.
TOLERANCIA_NETO = 1.0

# Pólizas de la ventana que se revisan sin contar el neto: las primeras por
# diario habitual y cercanía a la fecha de pago (_polizas_cercanas). Las que
# tienen el neto se traen aparte (_polizas_con_importe) y no se pierden por
# este límite.
LIMITE_DE_BUSQUEDA = 300

# Renglones que se muestran.
LIMITE_DE_CANDIDATAS = 50


def _neto(nominas, empresa):
    """El neto de los recibos: la suma de sus Totales, en la moneda de la
    empresa (en un CFDI de Nómina el Total ya es lo pagado al empleado). Si
    alguno viene en otra moneda no se puede sumar sin convertir y vale 0: la
    búsqueda sigue por fecha, diario y referencia, sin comparar importes."""
    if not nominas or any(doc.currency_id != empresa.currency_id for doc in nominas):
        return 0.0
    return empresa.currency_id.round(sum(nominas.mapped('amount_total')))


def _fecha_de_pago(doc):
    """La fecha de pago del complemento; sin complemento, la de emisión."""
    return doc.dsi_nomina_fecha_pago or doc.invoice_date


def _poliza_vigente_de(doc):
    """La póliza con la que ya está `doc`, si cuenta como tal: un asiento
    contable de su empresa, no cancelado. Si no, vacío: una liga a una póliza
    cancelada o, de antes de 0.3.0, a una factura o a un asiento de otra
    empresa cuenta como libre. Se lee con sudo: quien relaciona puede no ver
    esa otra empresa, y el asistente no debe fallar por una liga vieja."""
    poliza = doc.sudo().move_id
    if poliza.move_type == 'entry' and poliza.state != 'cancel' and poliza.company_id == doc.company_id:
        return poliza
    return poliza.browse()


class CfdiNominaPolizaWizard(models.TransientModel):
    _name = 'dsi.cfdi.nomina.poliza.wizard'
    _description = "Relacionar CFDI de nómina con su póliza"

    company_id = fields.Many2one('res.company', string="Empresa", required=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    cfdi_ids = fields.Many2many(
        'cfdi.document', 'dsi_nomina_poliza_wizard_cfdi_rel', 'wizard_id', 'cfdi_id',
        string="CFDI de nómina", readonly=True)
    cfdi_count = fields.Integer(string="CFDI", compute='_compute_resumen')
    net_total = fields.Monetary(string="Neto", compute='_compute_resumen', currency_field='currency_id')
    payment_date_from = fields.Date(string="Primera fecha de pago", compute='_compute_resumen')
    payment_date_to = fields.Date(string="Última fecha de pago", compute='_compute_resumen')
    payment_date_count = fields.Integer(string="Fechas de pago distintas", compute='_compute_resumen')
    skipped_note = fields.Text(string="No se incluyeron", readonly=True)

    # Búsqueda.
    date_from = fields.Date(string="Desde", required=True)
    date_to = fields.Date(string="Hasta", required=True)
    journal_id = fields.Many2one(
        'account.journal', string="Diario", domain="[('company_id', 'parent_of', company_id)]")
    reference = fields.Char(string="Referencia o etiqueta")
    amount = fields.Monetary(string="Importe a buscar", currency_field='currency_id')
    only_matching_amount = fields.Boolean(string="Solo las que tienen ese importe")
    include_draft = fields.Boolean(string="Incluir borradores")
    line_ids = fields.One2many('dsi.cfdi.nomina.poliza.wizard.line', 'wizard_id', string="Pólizas candidatas")

    # Elección.
    move_id = fields.Many2one('account.move', string="Póliza")
    other_move_count = fields.Integer(string="Con otra póliza", compute='_compute_other_move_count')
    replace_existing = fields.Boolean(string="Reemplazar la póliza de los que ya tienen otra")

    @api.depends('cfdi_ids', 'company_id')
    def _compute_resumen(self):
        for asistente in self:
            fechas = [fecha for fecha in map(_fecha_de_pago, asistente.cfdi_ids) if fecha]
            asistente.cfdi_count = len(asistente.cfdi_ids)
            asistente.net_total = _neto(asistente.cfdi_ids, asistente.company_id)
            asistente.payment_date_from = min(fechas, default=False)
            asistente.payment_date_to = max(fechas, default=False)
            asistente.payment_date_count = len(set(fechas))

    @api.depends('cfdi_ids.move_id', 'cfdi_ids.move_id.state', 'cfdi_ids.move_id.move_type', 'move_id')
    def _compute_other_move_count(self):
        for asistente in self:
            asistente.other_move_count = len(asistente._con_otra_poliza(asistente.cfdi_ids, asistente.move_id))

    @api.model
    def _con_otra_poliza(self, nominas, poliza):
        """Las de `nominas` que ya están en otra póliza vigente: sin
        «Reemplazar» se quedan donde están. Las ligadas a una póliza cancelada
        o, de antes de 0.3.0, a una factura o a un asiento de otra empresa
        cuentan como libres (_poliza_vigente_de): no hay nada que reemplazar
        ni una póliza anterior que avisar."""
        return nominas.filtered(lambda d: d.move_id and d.move_id != poliza and _poliza_vigente_de(d))

    # -------------------------------------------------------------------
    # Abrir y buscar
    # -------------------------------------------------------------------

    @api.model
    def _abrir_para(self, nominas, descartados):
        """El asistente para `nominas` (ya clasificadas, de una sola empresa),
        guardado y con sus candidatas.

        La ventana va de la primera a la última fecha de pago, más el fin de
        periodo (la provisión se fecha al cierre), con HOLGURA_DIAS_POLIZA de
        cada lado. Si todas ya están en la misma póliza vigente viene elegida:
        volver a relacionar repara las copias que falten."""
        empresa = nominas.company_id
        fechas = [fecha for fecha in map(_fecha_de_pago, nominas) if fecha]
        fechas += [fecha for fecha in nominas.mapped('dsi_nomina_fecha_final') if fecha]
        hoy = fields.Date.context_today(self)
        holgura = timedelta(days=HOLGURA_DIAS_POLIZA)
        valores = {
            'company_id': empresa.id,
            'cfdi_ids': [Command.set(nominas.ids)],
            'date_from': min(fechas, default=hoy) - holgura,
            'date_to': max(fechas, default=hoy) + holgura,
            'amount': _neto(nominas, empresa),
        }
        if descartados:
            valores['skipped_note'] = self.env._(
                "No se incluyeron: %s.", "; ".join(nominas._detalle_descartados(descartados)))
        polizas = {_poliza_vigente_de(doc).id for doc in nominas}
        if len(polizas) == 1 and False not in polizas:
            valores['move_id'] = polizas.pop()
        asistente = self.create(valores)
        asistente._buscar_candidatas()
        return asistente._reabrir()

    def _reabrir(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Relacionar con póliza",
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_buscar(self):
        """Botón «Buscar»: el navegador guarda los filtros antes de llamarlo."""
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_("La fecha «Desde» es posterior a «Hasta»."))
        self._buscar_candidatas()
        return self._reabrir()

    def _dominio_de_busqueda(self, habitual):
        """Asientos contables de la empresa en la ventana, publicados (y en
        borrador si se pide), con el diario y la referencia opcionales. La
        referencia se busca en el número, la referencia y las etiquetas de
        las líneas: el sistema de nómina la pone en cualquiera de las tres.

        Se dejan fuera los que Odoo crea solo al conciliar y nunca son una
        póliza de nómina, aunque traigan el neto: los de impuestos en base de
        efectivo (CABA) y los del diario de diferencias cambiarias. En una
        base real son decenas al día. Los pagos y los movimientos del banco
        se quedan: una nómina se puede contabilizar desde el extracto. El
        diario de diferencias cambiarias sí se busca si es el que se pidió o
        el `habitual` de la nómina: hay empresas que lo apuntan al de
        operaciones varias."""
        self.ensure_one()
        Poliza = self.env['account.move']
        dominio = [
            ('company_id', '=', self.company_id.id),
            ('move_type', '=', 'entry'),
            ('state', 'in', ['posted', 'draft'] if self.include_draft else ['posted']),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ]
        # Odoo marca un asiento CABA con los dos campos y revisa los dos:
        # tax_cash_basis_rec_id se vacía si se deshace la conciliación.
        dominio += [(campo, '=', False) for campo in ('tax_cash_basis_rec_id', 'tax_cash_basis_origin_move_id')
                    if campo in Poliza._fields]
        cambiario = self.company_id.currency_exchange_journal_id
        if cambiario and cambiario != self.journal_id and cambiario != habitual:
            dominio.append(('journal_id', '!=', cambiario.id))
        if self.journal_id:
            dominio.append(('journal_id', '=', self.journal_id.id))
        if self.reference:
            dominio += ['|', '|', ('ref', 'ilike', self.reference), ('name', 'ilike', self.reference),
                        ('line_ids.name', 'ilike', self.reference)]
        return dominio

    def _buscar_candidatas(self):
        """Llena la lista de pólizas candidatas, ordenadas por: una cuenta con
        el neto, el diario de la última N relacionada en la empresa, la
        cercanía a la fecha de pago más frecuente y, al final, la más nueva.

        Se lee como el usuario, sin sudo: ve las pólizas que Odoo le deja ver.
        «Tiene el neto» solo ordena; nunca se liga por importe: el total de
        una póliza de nómina incluye percepciones y cuotas patronales, y el
        neto puede ir repartido en varias cuentas o líneas de banco."""
        self.ensure_one()
        Poliza = self.env['account.move']
        habitual = self._diario_habitual()
        central = self._fecha_central()
        dominio = self._dominio_de_busqueda(habitual)
        # Sin importe, «Solo las que tienen ese importe» no tiene con qué
        # comparar: se ignora en vez de dejar la lista vacía.
        solo_con_importe = self.only_matching_amount and self.amount
        con_importe = self._polizas_con_importe(dominio) if self.amount else Poliza
        cercanas = Poliza if solo_con_importe else self._polizas_cercanas(dominio, habitual, central)
        polizas = con_importe | cercanas
        importes, cargos = self._importes_por_cuenta(polizas)
        ligados = dict(self.env['cfdi.document']._read_group(
            [('move_id', 'in', polizas.ids)], ['move_id'], ['__count']))

        filas = []
        for poliza in polizas:
            cercano = 0.0
            if self.amount and importes.get(poliza.id):
                cercano = min(importes[poliza.id], key=lambda monto: abs(monto - self.amount))
            coincide = bool(self.amount) and abs(cercano - self.amount) <= TOLERANCIA_NETO
            if solo_con_importe and not coincide:
                continue
            filas.append((poliza, cercano, coincide))
        filas.sort(key=lambda fila: (
            not fila[2], fila[0].journal_id != habitual, abs((fila[0].date - central).days), -fila[0].id))

        self.line_ids.unlink()
        self.write({'line_ids': [Command.create({
            'sequence': orden,
            'move_id': poliza.id,
            'total_debit': cargos.get(poliza.id, 0.0),
            'closest_amount': cercano,
            'difference': abs(cercano - self.amount) if self.amount else 0.0,
            'amount_matches': coincide,
            'linked_cfdi_count': ligados.get(poliza, 0),
        }) for orden, (poliza, cercano, coincide) in enumerate(filas[:LIMITE_DE_CANDIDATAS])]})

    def _polizas_cercanas(self, dominio, habitual, central):
        """Las LIMITE_DE_BUSQUEDA pólizas de `dominio` que van primero sin
        contar el neto: las del diario `habitual`, luego las más cercanas a la
        fecha `central` y, a la misma distancia, la más nueva. Es el orden de
        la lista quitando «Tiene el neto».

        El orden va en la consulta, antes del límite. Antes se cortaba a las
        más recientes y se ordenaba después: los pagos, los extractos y los
        demás asientos de los días siguientes al pago llenaban el límite, y la
        póliza correcta solo llegaba si tenía el neto en una cuenta (no con
        una selección parcial, el neto repartido o un finiquito).

        El dominio lo resuelve _search como el usuario, con sus reglas de
        registro; la consulta de fuera solo ordena y corta."""
        Poliza = self.env['account.move']
        Poliza.flush_model(['journal_id', 'date'])
        filas = self.env.execute_query(SQL(
            """
            SELECT m.id
              FROM account_move m
             WHERE m.id IN %(polizas)s
          ORDER BY m.journal_id IS DISTINCT FROM %(habitual)s, ABS(m.date - %(central)s::date), m.id DESC
             LIMIT %(limite)s
            """,
            polizas=Poliza._search(dominio).subselect(),
            habitual=habitual.id or None,
            central=central,
            limite=LIMITE_DE_BUSQUEDA,
        ))
        return Poliza.browse([fila[0] for fila in filas])

    def _polizas_con_importe(self, dominio):
        """Las pólizas de `dominio` con alguna cuenta cuya suma de cargos o de
        abonos es el neto (± TOLERANCIA_NETO): la de nómina por pagar en la
        provisión, o el banco en el pago. Por SQL agrupado: con cientos de
        asientos en la ventana no se pierden por LIMITE_DE_BUSQUEDA."""
        self.ensure_one()
        Linea = self.env['account.move.line']
        polizas = self.env['account.move']
        bajo, alto = self.amount - TOLERANCIA_NETO, self.amount + TOLERANCIA_NETO
        for campo in ('debit', 'credit'):
            for poliza, _cuenta in Linea._read_group(
                    [('move_id', 'any', dominio)], ['move_id', 'account_id'],
                    having=[(f'{campo}:sum', '>=', bajo), (f'{campo}:sum', '<=', alto)]):
                polizas |= poliza
        return polizas

    def _importes_por_cuenta(self, polizas):
        """({póliza: [sumas de cargos y de abonos por cuenta]}, {póliza: total
        de cargos}), en la moneda de la empresa."""
        importes, cargos = defaultdict(list), defaultdict(float)
        if polizas:
            for poliza, _cuenta, debe, haber in self.env['account.move.line']._read_group(
                    [('move_id', 'in', polizas.ids)], ['move_id', 'account_id'], ['debit:sum', 'credit:sum']):
                importes[poliza.id].extend(monto for monto in (debe, haber) if monto)
                cargos[poliza.id] += debe
        return importes, cargos

    def _diario_habitual(self):
        """El diario de la póliza de la última N relacionada en la empresa,
        con una póliza de la empresa (no una liga de antes de 0.3.0 a otra).
        Ordena y, si es también el de diferencias cambiarias, hace que ese
        diario no se excluya; no filtra."""
        self.ensure_one()
        return self.env['cfdi.document'].search([
            ('company_id', '=', self.company_id.id), ('move_type', '=', 'N'),
            ('move_id.move_type', '=', 'entry'), ('move_id.company_id', '=', self.company_id.id),
        ], order='write_date desc, id desc', limit=1).move_id.journal_id

    def _fecha_central(self):
        """La fecha de pago más frecuente de la selección; sin ninguna, la
        mitad de la ventana."""
        self.ensure_one()
        fechas = [fecha for fecha in map(_fecha_de_pago, self.cfdi_ids) if fecha]
        if fechas:
            return Counter(fechas).most_common(1)[0][0]
        return self.date_from + (self.date_to - self.date_from) / 2

    # -------------------------------------------------------------------
    # Relacionar
    # -------------------------------------------------------------------

    def _poliza_valida(self):
        """La póliza elegida, revisada en el servidor: el dominio del campo
        solo filtra en el navegador y la copia del XML se hace con sudo."""
        self.ensure_one()
        poliza = self.move_id.exists()
        if not poliza:
            raise UserError(_("Elige la póliza: de la lista o búscala en «Póliza»."))
        # Quien relaciona podría adjuntarle el XML él mismo.
        poliza.check_access('write')
        if poliza.move_type != 'entry':
            raise UserError(_("%s no es una póliza: es una factura.", poliza.display_name))
        if poliza.company_id != self.company_id:
            raise UserError(_("La póliza %s es de otra empresa.", poliza.display_name))
        if poliza.state == 'cancel':
            raise UserError(_("La póliza %s está cancelada.", poliza.display_name))
        return poliza

    def action_relacionar(self):
        """Liga los CFDI con la póliza elegida.

        Se vuelven a clasificar: el asistente pudo quedarse abierto mientras
        alguien cancelaba, archivaba o ligaba alguno. Los libres se ligan; los
        que ya estaban en esta póliza solo reparan su copia; los que están en
        otra se mueven solo con «Reemplazar», y la anterior conserva su copia
        y recibe una nota. El gancho de write copia el XML.

        En la póliza queda una nota por ejecución, sin adjuntos: con ellos,
        Odoo trataría de importar cada XML como factura."""
        self.ensure_one()
        self.env['dsi.license.client.state']._check_feature(
            'vault', self.env._("relacionar CFDI de nómina con pólizas"), self.company_id)
        poliza = self._poliza_valida()
        nominas, _descartados = self.cfdi_ids.exists()._clasificar_para_poliza()
        nominas = nominas.filtered(lambda d: d.company_id == self.company_id)
        if not nominas:
            raise UserError(_(
                "Ninguno de los CFDI del asistente se puede relacionar ya: se cancelaron, se archivaron o "
                "cambiaron de empresa."))
        ya_estaban = nominas.filtered(lambda d: d.move_id == poliza)
        con_otra = self._con_otra_poliza(nominas, poliza)
        libres = nominas - ya_estaban - con_otra
        reemplazos = con_otra if self.replace_existing else nominas.browse()
        anteriores = reemplazos.grouped('move_id')
        a_ligar = libres | reemplazos
        if a_ligar:
            a_ligar.write({'move_id': poliza.id, 'match_state': 'manual', 'match_method': 'manual'})
        ya_estaban._copiar_xml_de_nomina_a_su_poliza()

        sin_importar = {'disable_attachment_import': True, 'no_new_invoice': True}
        if a_ligar:
            fechas = sorted({fecha for fecha in map(_fecha_de_pago, a_ligar) if fecha})
            poliza.with_context(**sin_importar).message_post(
                subtype_xmlid='mail.mt_note',
                body=self.env._(
                    "Se relacionaron %(n)s CFDI de nómina desde la Bóveda (fecha de pago %(fechas)s, neto "
                    "%(neto)s). Una copia de cada XML está en los adjuntos.",
                    n=len(a_ligar),
                    fechas=", ".join(format_date(self.env, fecha) for fecha in fechas) or "-",
                    neto=format_amount(self.env, _neto(a_ligar, self.company_id), self.currency_id)))
        for anterior, docs in anteriores.items():
            anterior.with_context(**sin_importar).message_post(
                subtype_xmlid='mail.mt_note',
                body=self.env._(
                    "%(n)s CFDI de nómina se relacionaron ahora con %(poliza)s. Sus copias del XML se quedan "
                    "aquí.", n=len(docs), poliza=poliza.display_name))

        partes = [self.env._("Se relacionaron %(n)s CFDI de nómina con %(poliza)s.",
                             n=len(a_ligar), poliza=poliza.display_name)]
        if ya_estaban:
            partes.append(self.env._("%s ya estaban en ella.", len(ya_estaban)))
        quedaron = con_otra - reemplazos
        if quedaron:
            partes.append(self.env._(
                "%s se quedaron con su póliza anterior: marca «Reemplazar» para moverlos.", len(quedaron)))
        tipo = 'warning' if quedaron else 'success'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Relacionar con póliza"),
                'message': " ".join(partes),
                'type': tipo,
                'sticky': tipo != 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class CfdiNominaPolizaWizardLine(models.TransientModel):
    _name = 'dsi.cfdi.nomina.poliza.wizard.line'
    _description = "Póliza candidata para CFDI de nómina"
    _order = 'sequence, id'

    wizard_id = fields.Many2one('dsi.cfdi.nomina.poliza.wizard', required=True, ondelete='cascade')
    sequence = fields.Integer()
    move_id = fields.Many2one('account.move', string="Póliza", required=True, readonly=True, ondelete='cascade')
    date = fields.Date(related='move_id.date')
    ref = fields.Char(related='move_id.ref')
    journal_id = fields.Many2one(related='move_id.journal_id')
    state = fields.Selection(related='move_id.state')
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    total_debit = fields.Monetary(string="Total de la póliza", currency_field='currency_id', readonly=True)
    closest_amount = fields.Monetary(string="Importe más cercano", currency_field='currency_id', readonly=True)
    difference = fields.Monetary(string="Diferencia con el neto", currency_field='currency_id', readonly=True)
    amount_matches = fields.Boolean(string="Tiene el neto", readonly=True)
    linked_cfdi_count = fields.Integer(string="CFDI ya relacionados", readonly=True)

    def action_elegir(self):
        """Elige la póliza del renglón; liga «Relacionar»."""
        self.ensure_one()
        self.wizard_id.move_id = self.move_id
        return self.wizard_id._reabrir()
