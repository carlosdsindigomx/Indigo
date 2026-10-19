import logging
from datetime import timedelta

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tolerancia para la conciliación automática de nivel 2 (RFC + total + fecha).
MATCH_AMOUNT_TOLERANCE = 0.05
MATCH_DATE_TOLERANCE_DAYS = 3

# Nivel 3: coincidencia parcial con score (ventana amplia de búsqueda de candidatos,
# el score decide qué tan buena es cada sugerencia — no se vincula solo).
SUGGEST_DATE_WINDOW_DAYS = 30
SUGGEST_AMOUNT_WINDOW_RATIO = 0.30
SUGGEST_MAX_CANDIDATES = 5

# Cron de verificación de estatus SAT: periodicidad decreciente.
SAT_CHECK_RECENT_WINDOW_DAYS = 30   # CFDI de los últimos 30 días
SAT_CHECK_RECENT_INTERVAL_DAYS = 1  # ... se revisan a diario
SAT_CHECK_OLD_INTERVAL_DAYS = 30    # CFDI más antiguos ... se revisan mensualmente


def _local(tag):
    """Strip the XML namespace from a tag, e.g. '{http://...}Comprobante' -> 'Comprobante'."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


class CfdiDocument(models.Model):
    _name = 'cfdi.document'
    _description = "CFDI"
    _inherit = ['mail.thread']
    _rec_name = 'uuid'
    _order = 'invoice_date desc, id desc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, index=True,
        default=lambda self: self.env.company)
    uuid = fields.Char(string="UUID", required=True, tracking=True)
    move_id = fields.Many2one(
        'account.move', string="Factura", ondelete='set null', tracking=True,
        help="Factura de Odoo vinculada a este CFDI. Vacío si el CFDI aún no está contabilizado.")
    direction = fields.Selection([
        ('issued', "Emitido"),
        ('received', "Recibido"),
    ], string="Dirección", required=True)
    move_type = fields.Selection([
        ('I', "Ingreso"),
        ('E', "Egreso"),
        ('T', "Traslado"),
        ('N', "Nómina"),
        ('P', "Pago"),
    ], string="Tipo de comprobante")
    partner_rfc_issuer = fields.Char(string="RFC emisor")
    partner_name_issuer = fields.Char(string="Nombre emisor")
    partner_rfc_receiver = fields.Char(string="RFC receptor")
    partner_name_receiver = fields.Char(string="Nombre receptor")
    invoice_date = fields.Date(string="Fecha del comprobante")
    amount_total = fields.Monetary(string="Total")
    currency_id = fields.Many2one(
        'res.currency', string="Moneda", required=True,
        default=lambda self: self.env.company.currency_id)
    payment_method = fields.Char(string="Método de pago")
    payment_way = fields.Char(string="Forma de pago")
    sat_status = fields.Selection([
        ('not_verified', "Sin verificar"),
        ('valid', "Vigente"),
        ('canceled', "Cancelado"),
        ('not_found', "No encontrado"),
    ], string="Estatus SAT", default='not_verified', tracking=True, required=True)
    sat_last_check = fields.Datetime(string="Última verificación SAT")
    match_state = fields.Selection([
        ('pending', "Pendiente"),
        ('matched', "Vinculado automáticamente"),
        ('manual', "Vinculado manualmente"),
    ], string="Conciliación", default='pending', tracking=True, required=True)
    match_method = fields.Selection([
        ('uuid_exact', "UUID exacto"),
        ('rfc_amount_date', "RFC + total + fecha"),
        ('manual', "Manual"),
    ], string="Método de vinculación", tracking=True,
        help="Cómo se vinculó este CFDI a su factura: UUID exacto (nivel 1, alta confianza), "
             "RFC + total + fecha con tolerancia (nivel 2), o asignación manual del usuario.")
    source = fields.Selection([
        ('manual_upload', "Carga manual"),
    ], string="Origen", default='manual_upload', required=True)
    xml_attachment_id = fields.Many2one(
        'ir.attachment', string="XML", required=True, ondelete='restrict')
    pdf_attachment_id = fields.Many2one(
        'ir.attachment', string="PDF", ondelete='set null')
    parse_state = fields.Selection([
        ('ok', "OK"),
        ('partial', "Parcial"),
        ('error', "Error"),
    ], string="Resultado del parseo", default='ok', required=True)
    parse_error = fields.Text(string="Detalle del error")
    is_discrepancy = fields.Boolean(
        string="Discrepancia crítica", compute='_compute_is_discrepancy', store=True,
        help="CFDI cancelado en el SAT que sigue vinculado a una factura.")
    is_efos_risk = fields.Boolean(
        string="Riesgo 69-B", compute='_compute_efos_risk', store=True,
        help="El RFC emisor o el RFC receptor de este CFDI aparece en la lista 69-B "
             "(Empresas que Facturan Operaciones Simuladas) del SAT.")
    active = fields.Boolean(default=True)

    _uuid_company_uniq = models.Constraint(
        'unique(company_id, uuid)',
        "Ya existe un CFDI con este UUID en esta empresa.",
    )

    @api.depends('sat_status', 'move_id')
    def _compute_is_discrepancy(self):
        for doc in self:
            doc.is_discrepancy = doc.sat_status == 'canceled' and bool(doc.move_id)

    @api.depends('partner_rfc_issuer', 'partner_rfc_receiver')
    def _compute_efos_risk(self):
        rfcs = {rfc for rfc in self.mapped('partner_rfc_issuer') + self.mapped('partner_rfc_receiver') if rfc}
        flagged = set()
        if rfcs:
            flagged = set(self.env['dsi.cfdi.efos.entry'].sudo().search(
                [('rfc', 'in', list(rfcs))]).mapped('rfc'))
        for doc in self:
            doc.is_efos_risk = bool(
                (doc.partner_rfc_issuer and doc.partner_rfc_issuer in flagged)
                or (doc.partner_rfc_receiver and doc.partner_rfc_receiver in flagged)
            )

    def _recompute_efos_risk_all(self):
        """Llamado tras cargar/refrescar la lista 69-B: los CFDI existentes no
        se recalculan solos con @api.depends porque el cambio ocurre en OTRO
        modelo (dsi.cfdi.efos.entry), no en los campos de cfdi.document."""
        self.search([])._compute_efos_risk()

    @api.onchange('move_id')
    def _onchange_move_id(self):
        for doc in self:
            if doc.move_id and doc.match_state == 'pending':
                doc.match_state = 'manual'
                doc.match_method = 'manual'
            elif not doc.move_id:
                doc.match_state = 'pending'
                doc.match_method = False

    def action_unlink_move(self):
        self.write({'move_id': False, 'match_state': 'pending', 'match_method': False})

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    # -------------------------------------------------------------------
    # Parseo de XML
    # -------------------------------------------------------------------

    @api.model
    def _parse_cfdi_xml(self, xml_bytes):
        """Extrae los datos relevantes de un XML de CFDI (3.3 o 4.0).

        :return: dict con los valores para crear/leer un cfdi.document, incluyendo
                 siempre 'parse_state' ('ok' o 'error') y, en caso de error, 'parse_error'.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            return {'parse_state': 'error', 'parse_error': str(e)}

        if _local(root.tag) != 'Comprobante':
            return {
                'parse_state': 'error',
                'parse_error': _("El archivo no es un CFDI válido: no se encontró el nodo Comprobante."),
            }

        attrs = root.attrib
        tfd = next((el for el in root.iter() if _local(el.tag) == 'TimbreFiscalDigital'), None)
        if tfd is None or not tfd.get('UUID'):
            return {
                'parse_state': 'error',
                'parse_error': _("El CFDI no tiene Timbre Fiscal Digital (UUID)."),
            }

        emisor = next((el for el in root if _local(el.tag) == 'Emisor'), None)
        receptor = next((el for el in root if _local(el.tag) == 'Receptor'), None)

        try:
            amount_total = float(attrs.get('Total') or 0.0)
        except ValueError:
            amount_total = 0.0

        currency = self.env['res.currency']
        currency_code = attrs.get('Moneda')
        if currency_code:
            # active_test=False a propósito: en una base de Odoo la mayoría de
            # las monedas vienen desactivadas. Sin esto, un CFDI en USD (con USD
            # inactiva) caía al respaldo de la moneda de la compañía y quedaba
            # etiquetado como MXN — el importe se leería mal. Es preferible
            # referenciar la moneda correcta aunque esté inactiva.
            currency = self.env['res.currency'].with_context(active_test=False).search(
                [('name', '=', currency_code)], limit=1)

        return {
            'parse_state': 'ok',
            'uuid': tfd.get('UUID', '').upper(),
            'move_type': attrs.get('TipoDeComprobante') or False,
            'invoice_date': (attrs.get('Fecha') or '')[:10] or False,
            'amount_total': amount_total,
            'currency_id': currency.id if currency else False,
            'payment_method': attrs.get('MetodoPago') or '',
            'payment_way': attrs.get('FormaPago') or '',
            'partner_rfc_issuer': emisor.get('Rfc') if emisor is not None else False,
            'partner_name_issuer': emisor.get('Nombre') if emisor is not None else False,
            'partner_rfc_receiver': receptor.get('Rfc') if receptor is not None else False,
            'partner_name_receiver': receptor.get('Nombre') if receptor is not None else False,
        }

    # -------------------------------------------------------------------
    # Conciliación automática (REQ-002)
    # -------------------------------------------------------------------

    def action_match_automatically(self):
        """Conciliación automática en cascada:

        1. UUID exacto contra account.move (alta confianza).
        2. RFC de la contraparte + total + fecha con tolerancia, solo cuando
           hay un único candidato sin ambigüedad (nivel 2 del plan de conciliación).

        Los CFDI que no encuentran coincidencia en ninguno de los dos niveles
        quedan 'pending' para asignación manual (nivel 3/4 del plan) — nunca
        se adivina cuando hay más de un candidato posible.
        """
        docs = self.filtered(lambda d: not d.move_id)
        if not docs:
            return
        self._match_by_uuid(docs)
        remaining = docs.filtered(lambda d: not d.move_id)
        if remaining:
            self._match_by_rfc_amount_date(remaining)

    def _match_by_uuid(self, docs):
        moves = self.env['account.move'].search([
            ('l10n_mx_edi_cfdi_uuid', 'in', docs.mapped('uuid')),
            ('company_id', 'in', docs.company_id.ids),
        ])
        move_by_key = {(m.company_id.id, m.l10n_mx_edi_cfdi_uuid): m for m in moves}
        for doc in docs:
            move = move_by_key.get((doc.company_id.id, doc.uuid))
            if move:
                doc.write({'move_id': move.id, 'match_state': 'matched', 'match_method': 'uuid_exact'})

    def _match_by_rfc_amount_date(self, docs):
        # Excluir facturas que ya están vinculadas a OTRO cfdi.document (no solo
        # a los que se están procesando en este batch), para no robarle la
        # factura a un CFDI ya conciliado.
        already_linked = self.env['cfdi.document'].search([('move_id', '!=', False)])
        used_move_ids = set(already_linked.mapped('move_id').ids)
        for doc in docs:
            counterparty_rfc = (
                doc.partner_rfc_receiver if doc.direction == 'issued' else doc.partner_rfc_issuer
            )
            if not counterparty_rfc or not doc.invoice_date:
                continue
            date_from = doc.invoice_date - timedelta(days=MATCH_DATE_TOLERANCE_DAYS)
            date_to = doc.invoice_date + timedelta(days=MATCH_DATE_TOLERANCE_DAYS)
            candidates = self.env['account.move'].search([
                ('company_id', '=', doc.company_id.id),
                ('state', '=', 'posted'),
                ('partner_id.vat', '=ilike', counterparty_rfc),
                ('invoice_date', '>=', date_from),
                ('invoice_date', '<=', date_to),
                ('amount_total', '>=', doc.amount_total - MATCH_AMOUNT_TOLERANCE),
                ('amount_total', '<=', doc.amount_total + MATCH_AMOUNT_TOLERANCE),
                ('id', 'not in', list(used_move_ids)),
            ])
            # Solo se auto-vincula si hay exactamente un candidato: con más de
            # uno, la elección automática sería una adivinanza contable.
            if len(candidates) == 1:
                move = candidates
                used_move_ids.add(move.id)
                doc.write({'move_id': move.id, 'match_state': 'matched', 'match_method': 'rfc_amount_date'})

    # -------------------------------------------------------------------
    # Nivel 3: sugerencias con score (coincidencia parcial, requiere
    # confirmación humana — nunca se vincula sola).
    # -------------------------------------------------------------------

    def action_open_match_suggestions(self):
        self.ensure_one()
        candidates = self._find_match_candidates()
        wizard = self.env['cfdi.match.suggestion.wizard'].create({
            'document_id': self.id,
            'line_ids': [(0, 0, {
                'move_id': c['move'].id,
                'score': c['score'],
                'rfc_match': c['rfc_match'],
                'amount_diff': c['amount_diff'],
                'date_diff_days': c['date_diff_days'],
            }) for c in candidates],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Sugerencias de conciliación"),
            'res_model': 'cfdi.match.suggestion.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    def _find_match_candidates(self, limit=SUGGEST_MAX_CANDIDATES):
        """Busca facturas candidatas en una ventana amplia y devuelve una lista
        de dicts (move, score, rfc_match, amount_diff, date_diff_days)
        ordenada de mayor a menor score. No vincula nada — es responsabilidad
        del asistente (nivel 4) confirmar."""
        self.ensure_one()
        already_linked = self.env['cfdi.document'].search([('move_id', '!=', False)])
        used_move_ids = set(already_linked.mapped('move_id').ids)
        counterparty_rfc = (
            self.partner_rfc_receiver if self.direction == 'issued' else self.partner_rfc_issuer
        )
        if not self.invoice_date:
            return []

        date_from = self.invoice_date - timedelta(days=SUGGEST_DATE_WINDOW_DAYS)
        date_to = self.invoice_date + timedelta(days=SUGGEST_DATE_WINDOW_DAYS)
        amount_low = self.amount_total * (1 - SUGGEST_AMOUNT_WINDOW_RATIO)
        amount_high = self.amount_total * (1 + SUGGEST_AMOUNT_WINDOW_RATIO)
        candidates = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('amount_total', '>=', amount_low),
            ('amount_total', '<=', amount_high),
            ('id', 'not in', list(used_move_ids)),
        ])

        results = []
        for move in candidates:
            rfc_match = bool(counterparty_rfc) and (move.partner_id.vat or '').strip().upper() == counterparty_rfc.strip().upper()
            amount_diff = abs(move.amount_total - self.amount_total)
            amount_ratio = amount_diff / self.amount_total if self.amount_total else 1.0
            date_diff_days = abs((move.invoice_date - self.invoice_date).days) if move.invoice_date else SUGGEST_DATE_WINDOW_DAYS

            rfc_score = 50 if rfc_match else 0
            amount_score = max(0, 30 * (1 - amount_ratio / SUGGEST_AMOUNT_WINDOW_RATIO)) if self.amount_total else 0
            date_score = max(0, 20 * (1 - date_diff_days / SUGGEST_DATE_WINDOW_DAYS))
            score = round(rfc_score + amount_score + date_score, 1)

            results.append({
                'move': move,
                'score': score,
                'rfc_match': rfc_match,
                'amount_diff': amount_diff,
                'date_diff_days': date_diff_days,
            })

        results.sort(key=lambda r: r['score'], reverse=True)
        return results[:limit]

    def action_confirm_match(self, move_id):
        """Confirma una sugerencia de nivel 3 elegida por el usuario (nunca
        automático — siempre viene de una acción explícita del asistente)."""
        self.ensure_one()
        self.write({'move_id': move_id, 'match_state': 'manual', 'match_method': 'manual'})

    # -------------------------------------------------------------------
    # Verificación de estatus SAT bajo demanda (REQ-004)
    # -------------------------------------------------------------------

    def action_verify_sat_status(self):
        errors = []
        sat_doc_model = self.env['l10n_mx_edi.document']
        for doc in self:
            result = sat_doc_model._fetch_sat_status(
                doc.partner_rfc_issuer, doc.partner_rfc_receiver, doc.amount_total, doc.uuid,
            )
            value = result.get('value')
            if value == 'valid':
                doc.write({'sat_status': 'valid', 'sat_last_check': fields.Datetime.now()})
            elif value == 'cancelled':
                doc.write({'sat_status': 'canceled', 'sat_last_check': fields.Datetime.now()})
            elif value == 'not_found':
                doc.write({'sat_status': 'not_found', 'sat_last_check': fields.Datetime.now()})
            else:
                errors.append("%s: %s" % (doc.uuid, result.get('error') or _("estatus no definido")))
        if errors:
            raise UserError(_(
                "No se pudo verificar el estatus SAT de %(count)s CFDI. El estatus anterior no se modificó.\n\n%(errors)s",
                count=len(errors), errors="\n".join(errors),
            ))

    # -------------------------------------------------------------------
    # Verificación de estatus SAT automática (cron, periodicidad decreciente)
    # -------------------------------------------------------------------

    @api.model
    def _cron_verify_sat_status(self, batch_size=200):
        """Revisa estatus SAT con periodicidad decreciente: a diario para CFDI
        de los últimos 30 días, mensual para los más antiguos. Nunca lanza
        excepción — un CFDI que falla no debe tumbar el resto del batch."""
        today = fields.Date.today()
        recent_date_cutoff = today - timedelta(days=SAT_CHECK_RECENT_WINDOW_DAYS)
        recent_check_cutoff = fields.Datetime.now() - timedelta(days=SAT_CHECK_RECENT_INTERVAL_DAYS)
        old_check_cutoff = fields.Datetime.now() - timedelta(days=SAT_CHECK_OLD_INTERVAL_DAYS)

        recent_docs = self.search([
            ('sat_status', '!=', 'canceled'),
            ('invoice_date', '>=', recent_date_cutoff),
            '|', ('sat_last_check', '=', False), ('sat_last_check', '<', recent_check_cutoff),
        ], limit=batch_size)

        remaining = batch_size - len(recent_docs)
        old_docs = self.env['cfdi.document']
        if remaining > 0:
            old_docs = self.search([
                ('sat_status', '!=', 'canceled'),
                ('invoice_date', '<', recent_date_cutoff),
                '|', ('sat_last_check', '=', False), ('sat_last_check', '<', old_check_cutoff),
            ], limit=remaining)

        for doc in recent_docs | old_docs:
            try:
                doc.action_verify_sat_status()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Verificación SAT automática falló para cfdi.document id=%s", doc.id)
