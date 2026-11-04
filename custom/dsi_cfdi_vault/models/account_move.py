import logging

from odoo import api, fields, models
from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

# Estatus SAT del CFDI en la Bóveda -> «Estado bóveda» de la factura. Usa las
# claves de l10n_mx_edi_cfdi_sat_state ('cancelled' con dos eles, no el
# 'canceled' de la Bóveda) para poder compararlos. Sin verificar no dice nada.
ESTADO_BOVEDA_POR_SAT_STATUS = {
    'valid': 'valid',
    'canceled': 'cancelled',
    'not_found': 'not_found',
}

# Solo los CFDI de ingreso y egreso amparan la factura. Uno de Pago ligado a la
# factura acusa un pago y su estatus no es el de la factura.
TIPOS_DE_CFDI_DE_FACTURA = ('I', 'E')

# Las columnas que _auto_init crea y llena por SQL.
CAMPOS_ESTADO_BOVEDA = ('dsi_vault_sat_state', 'dsi_vault_sat_mismatch')


class AccountMove(models.Model):
    _inherit = 'account.move'

    # Solo para la Bóveda: quien no es Usuario de la Bóveda no puede leer los
    # CFDI, y sin el grupo aquí no podía ni abrir una factura (el botón «CFDI»
    # los cuenta al cargar el formulario).
    cfdi_document_ids = fields.One2many(
        'cfdi.document', 'move_id', string="CFDI vinculados",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    cfdi_document_count = fields.Integer(
        string="Número de CFDI vinculados", compute='_compute_cfdi_document_count',
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    # Solo para la Bóveda: quien no es Usuario de la Bóveda no puede leer las
    # discrepancias, y sin el grupo aquí le fallaría la factura al pedirlas.
    dsi_inconsistency_ids = fields.One2many(
        'dsi.cfdi.inconsistency', 'move_id', string="Discrepancias de la Bóveda",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    dsi_inconsistency_count = fields.Integer(
        string="Discrepancias pendientes", compute='_compute_dsi_inconsistency_count',
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    dsi_inconsistency_total = fields.Integer(
        string="Total de discrepancias", compute='_compute_dsi_inconsistency_count',
        groups='dsi_cfdi_vault.group_cfdi_vault_user')

    # Solo para la Bóveda, como los CFDI: los pagos (REP) que abonan esta
    # factura, uno por documento relacionado.
    dsi_rep_line_ids = fields.One2many(
        'dsi.cfdi.payment.line', 'invoice_id', string="Pagos (REP)",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    dsi_rep_count = fields.Integer(
        string="Número de pagos (REP)", compute='_compute_dsi_rep_count',
        groups='dsi_cfdi_vault.group_cfdi_vault_user')
    # En el asiento de un pago o de un movimiento del banco: los documentos
    # relacionados de REP ligados a él (dsi.cfdi.payment.line.payment_move_id).
    dsi_rep_payment_line_ids = fields.One2many(
        'dsi.cfdi.payment.line', 'payment_move_id', string="REP de este pago",
        groups='dsi_cfdi_vault.group_cfdi_vault_user')

    # Sin grupo: son de la factura y no de la Bóveda, y el contador que no usa
    # la Bóveda también debe ver que el SAT ya canceló el CFDI. Se guardan para
    # poder filtrar y agrupar por ellos en la lista de facturas.
    dsi_vault_sat_state = fields.Selection([
        ('valid', "Vigente"),
        ('cancelled', "Cancelado"),
        ('not_found', "No encontrado"),
    ], string="Estado bóveda", compute='_compute_dsi_vault_sat_state', store=True,
        tracking=True, copy=False, index='btree_not_null',
        help="Estatus en el SAT del CFDI de esta factura según la última verificación de la "
             "Bóveda. Vacío si la Bóveda no tiene el CFDI de ingreso o egreso de la factura, o "
             "si todavía no lo verifica.")
    dsi_vault_sat_mismatch = fields.Boolean(
        string="Diferencia SAT / bóveda", compute='_compute_dsi_vault_sat_mismatch', store=True,
        copy=False, index='btree_not_null',
        help="La Bóveda dice que el CFDI está cancelado y en Odoo ni el estado SAT ni la factura "
             "están cancelados, o la Bóveda lo da vigente y Odoo cancelado. Vigente contra «No "
             "definido», error o sin estado SAT no cuenta: Odoo aún no lo consulta.")

    @api.depends('cfdi_document_ids')
    def _compute_cfdi_document_count(self):
        for move in self:
            move.cfdi_document_count = len(move.cfdi_document_ids)

    # Sin los REP cancelados: el que el emisor cancela y vuelve a emitir se
    # contaría dos veces. El asiento de un pago o de un movimiento del banco
    # también muestra los REP ligados a él.
    @api.depends('dsi_rep_line_ids.payment_sat_status', 'dsi_rep_payment_line_ids.payment_sat_status')
    def _compute_dsi_rep_count(self):
        for move in self:
            move.dsi_rep_count = len((move.dsi_rep_line_ids | move.dsi_rep_payment_line_ids).filtered(
                lambda linea: linea.payment_sat_status != 'canceled').payment_doc_id)

    def _dominio_de_inconsistencias(self):
        """Las de la factura y las de sus CFDI: una de riesgo 69-B se registra
        sobre el CFDI, pero también es de la factura que lo contabiliza. En el
        asiento de un pago o de un movimiento del banco, además, las «Compra
        PPD pagada sin REP» de ese pago: se registran sobre la compra, pero lo
        que falta es el REP de este pago."""
        return ['|', '|', ('move_id', 'in', self.ids), ('payment_move_id', 'in', self.ids),
                ('cfdi_document_id', 'in', self.cfdi_document_ids.ids)]

    # Sin depends de payment_move_id: no hay un One2many que lo siga, y no
    # hace falta. El campo no se guarda: el formulario lo vuelve a calcular
    # cada vez que se abre.
    @api.depends('dsi_inconsistency_ids.state', 'cfdi_document_ids.dsi_inconsistency_ids.state')
    def _compute_dsi_inconsistency_count(self):
        registros = self.env['dsi.cfdi.inconsistency'].search(self._dominio_de_inconsistencias())
        for move in self:
            propios = registros.filtered(
                lambda r: r.move_id == move or r.payment_move_id == move
                or r.cfdi_document_id in move.cfdi_document_ids)
            move.dsi_inconsistency_total = len(propios)
            move.dsi_inconsistency_count = len(propios.filtered(lambda r: r.state == 'open'))

    # -------------------------------------------------------------------
    # Estado bóveda
    # -------------------------------------------------------------------

    @api.depends('cfdi_document_ids.sat_status', 'cfdi_document_ids.uuid', 'cfdi_document_ids.move_type',
                 'cfdi_document_ids.active', 'l10n_mx_edi_cfdi_uuid')
    def _compute_dsi_vault_sat_state(self):
        for move in self:
            move.dsi_vault_sat_state = ESTADO_BOVEDA_POR_SAT_STATUS.get(
                move._dsi_cfdi_de_la_boveda().sat_status, False)

    @api.depends('dsi_vault_sat_state', 'l10n_mx_edi_cfdi_sat_state', 'state', 'move_type')
    def _compute_dsi_vault_sat_mismatch(self):
        for move in self:
            move.dsi_vault_sat_mismatch = move._dsi_hay_diferencia_sat()

    def _dsi_cfdi_de_la_boveda(self):
        """El CFDI de la Bóveda del que sale el Estado bóveda: de los de ingreso
        o egreso activos ligados a la factura, el que lleva su folio fiscal.
        Solo si la factura no tiene folio (no se timbró ni se importó su XML
        en Odoo), el primero que llegó.

        Con folio, uno ligado con otro no es el suyo: es el CFDI anterior de
        una venta que se canceló, se regresó a borrador y se volvió a timbrar,
        o uno que se ligó por RFC, total y fecha. Antes también contaba, y la
        venta refacturada quedaba con el Estado bóveda cancelado del CFDI
        anterior y un «Estatus SAT distinto» que aconsejaba «Actualizar SAT».

        La misma regla está en SQL en _dsi_llenar_estado_boveda_por_sql: si
        cambia una, cambia la otra."""
        self.ensure_one()
        folio = (self.l10n_mx_edi_cfdi_uuid or '').upper()
        cfdis = self.cfdi_document_ids.filtered(
            lambda d: d.active and d.move_type in TIPOS_DE_CFDI_DE_FACTURA
            and (not folio or (d.uuid or '').upper() == folio))
        return cfdis.sorted('id')[:1]

    def _dsi_hay_diferencia_sat(self):
        """La regla de «Diferencia SAT / bóveda», solo en facturas y recibos.

        - Bóveda cancelado y Odoo no: ni el estado SAT de Odoo ni la factura
          dicen cancelado. Incluye «No definido» (una solicitud de
          cancelación sin respuesta) y la factura sin estado SAT.
        - Bóveda vigente y el estado SAT de Odoo cancelado.

        Vigente contra «No definido», error o vacío no es diferencia: es que
        Odoo aún no consulta. «No encontrado» en la Bóveda tampoco: no dice
        qué pasó con el CFDI.
        """
        self.ensure_one()
        if not self.is_invoice(include_receipts=True):
            return False
        sat_odoo = self.l10n_mx_edi_cfdi_sat_state
        if self.dsi_vault_sat_state == 'cancelled':
            return sat_odoo != 'cancelled' and self.state != 'cancel'
        return self.dsi_vault_sat_state == 'valid' and sat_odoo == 'cancelled'

    def _dsi_marcar_por_revisar(self):
        """Marca las facturas «Por revisar» en su propia lista, como hace la
        conversión con sus avisos. Odoo 18 y 19 lo hacen con `checked = False`;
        20 sustituyó ese booleano por `review_state`. Solo es esa marca: el
        asiento no se toca. Es el sistema quien la pone, no el usuario.

        En 20, 'anomaly' (el import de Odoo encontró totales o impuestos que
        no cuadran) ya es «Por revisar», y la más grave: no se cambia por
        'todo'."""
        sistema = self.sudo().with_context(skip_is_manually_modified=True)
        if 'review_state' in self._fields:
            sistema.filtered(lambda m: m.review_state not in ('todo', 'anomaly')).write({'review_state': 'todo'})
        elif 'checked' in self._fields:
            sistema.write({'checked': False})

    def _post(self, soft=True):
        """Al publicar, Odoo da la factura por revisada si quien publica puede
        revisar (checked en 18 y 19, review_state en 20). Una compra que se
        marcó «Por revisar» en borrador perdía la marca al publicarla: se le
        vuelve a poner mientras siga pendiente en la Bóveda su «Estatus SAT
        distinto» (cancelada en el SAT) o un «Aviso al convertir». Si después
        el contador la revisa, ya no se le vuelve a marcar: la revisión diaria
        solo marca las discrepancias nuevas o reabiertas. Mismo método en 18,
        19 y 20."""
        publicadas = super()._post(soft=soft)
        compras = publicadas.filtered(lambda f: f.is_purchase_document(include_receipts=True))
        if compras:
            pendientes = self.env['dsi.cfdi.inconsistency'].sudo().search([
                ('kind', 'in', ('sat_distinto', 'aviso_conversion')), ('state', '=', 'open'),
                ('move_id', 'in', compras.ids),
            ])
            pendientes.filtered(
                lambda r: r.kind == 'aviso_conversion' or r.move_id.dsi_vault_sat_state == 'cancelled'
            ).move_id._dsi_marcar_por_revisar()
        return publicadas

    def _auto_init(self):
        """Crea y llena por SQL las columnas del Estado bóveda la primera vez.

        Si la columna de un campo calculado guardado no existe, Odoo la crea y
        calcula el campo en Python para TODAS las facturas de la base, al
        instalar o al actualizar el módulo. Con años de facturas eso tarda.
        Aquí se crean antes y se llenan con dos UPDATE; con la columna ya
        creada, Odoo no recalcula nada. Mismo código en 18, 19 y 20 (tools.sql).
        """
        cr = self.env.cr
        nuevas = []
        if sql.table_exists(cr, self._table):
            for nombre in CAMPOS_ESTADO_BOVEDA:
                if not sql.column_exists(cr, self._table, nombre):
                    campo = self._fields[nombre]
                    sql.create_column(cr, self._table, nombre, campo.column_type[1], campo.string)
                    nuevas.append(nombre)
        if nuevas:
            self._dsi_llenar_estado_boveda_por_sql()
        return super()._auto_init()

    @api.model
    def _dsi_llenar_estado_boveda_por_sql(self):
        """Llena el Estado bóveda y la diferencia con la misma regla que
        _compute_dsi_vault_sat_state y _dsi_hay_diferencia_sat.

        Supone las columnas vacías, recién creadas: solo escribe donde hay
        valor, para no reescribir toda la tabla. Una factura sin CFDI en la
        Bóveda o sin diferencia se queda en NULL, que Odoo lee como vacío y
        False, igual que el cálculo.
        """
        cr = self.env.cr
        if not sql.table_exists(cr, 'cfdi_document'):
            return
        estado = SQL("CASE cd.sat_status %s END", SQL(" ").join(
            SQL("WHEN %s THEN %s", sat_status, valor)
            for sat_status, valor in ESTADO_BOVEDA_POR_SAT_STATUS.items()))
        cr.execute(SQL("""
            UPDATE account_move am
               SET dsi_vault_sat_state = boveda.estado
              FROM (
                    SELECT DISTINCT ON (cd.move_id) cd.move_id, %(estado)s AS estado
                      FROM cfdi_document cd
                      JOIN account_move m ON m.id = cd.move_id
                     WHERE cd.active AND cd.move_type IN %(tipos)s
                       AND (COALESCE(m.l10n_mx_edi_cfdi_uuid, '') = ''
                            OR upper(cd.uuid) = upper(m.l10n_mx_edi_cfdi_uuid))
                  ORDER BY cd.move_id, cd.id
                   ) boveda
             WHERE am.id = boveda.move_id AND boveda.estado IS NOT NULL
        """, estado=estado, tipos=TIPOS_DE_CFDI_DE_FACTURA))
        con_estado = cr.rowcount
        cr.execute(SQL("""
            UPDATE account_move
               SET dsi_vault_sat_mismatch = TRUE
             WHERE move_type IN %(facturas)s
               AND ((dsi_vault_sat_state = 'cancelled'
                     AND l10n_mx_edi_cfdi_sat_state IS DISTINCT FROM 'cancelled'
                     AND state != 'cancel')
                    OR (dsi_vault_sat_state = 'valid' AND l10n_mx_edi_cfdi_sat_state = 'cancelled'))
        """, facturas=tuple(self.get_invoice_types(include_receipts=True))))
        _logger.info(
            "Estado bóveda llenado por SQL: %s facturas con estado, %s con diferencia SAT / bóveda.",
            con_estado, cr.rowcount)

    # -------------------------------------------------------------------
    # Acciones
    # -------------------------------------------------------------------

    def action_view_cfdi_documents(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "CFDI vinculados",
            'res_model': 'cfdi.document',
            'view_mode': 'list,form',
            'domain': [('move_id', '=', self.id)],
        }

    def action_view_rep_lines(self):
        """Botón «REP»: lo que dice cada pago de esta factura (parcialidad,
        pagado, saldo insoluto), un renglón por documento relacionado; en el
        asiento de un pago o de un movimiento del banco, los documentos
        relacionados ligados a él. Abre sin los REP cancelados, para que
        «Pagado» sea lo pagado; quitando el filtro se ven también."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Pagos (REP)",
            'res_model': 'dsi.cfdi.payment.line',
            'view_mode': 'list,form',
            'domain': ['|', ('invoice_id', '=', self.id), ('payment_move_id', '=', self.id)],
            'context': {'create': False, 'search_default_filter_not_canceled': 1},
        }

    def action_view_inconsistencies(self):
        self.ensure_one()
        return self.env['dsi.cfdi.inconsistency']._accion_ver(
            self._dominio_de_inconsistencias(), pendientes=self.dsi_inconsistency_count)
