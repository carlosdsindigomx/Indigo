import calendar
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SatSyncSchedule(models.Model):
    _name = 'sat.sync.schedule'
    _description = "Programación de descarga automática del SAT"
    _inherit = ['mail.thread']
    _rec_name = 'display_name'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, default=lambda self: self.env.company)
    credential_id = fields.Many2one(
        'sat.credential', string="e.firma", required=True,
        domain="[('company_id', '=', company_id), ('state', '=', 'validated')]")
    active = fields.Boolean(default=True)

    date_start = fields.Date(
        string="Traer desde", required=True,
        help="Fecha a partir de la cual se descargan los comprobantes. La primera "
             "corrida arranca aquí y avanza sola periodo por periodo hasta ponerse al día.")
    periodicity = fields.Selection([
        ('daily', "Diaria"),
        ('weekly', "Semanal"),
        ('biweekly', "Quincenal"),
    ], string="Periodicidad", default='daily', required=True,
        help="Tamaño del periodo que se pide en cada solicitud. Quincenal usa las "
             "quincenas naturales (del 1 al 15 y del 16 a fin de mes).")
    lookback_days = fields.Integer(
        string="Días de recuperación", default=3,
        help="Vuelve a pedir los últimos N días de cada periodo ya cubierto, para "
             "atrapar comprobantes que el proveedor timbró tarde. Los duplicados se "
             "descartan solos por folio fiscal.")

    sync_received = fields.Boolean(string="Recibidos", default=True)
    sync_issued = fields.Boolean(string="Emitidos", default=True)
    download_xml = fields.Boolean(
        string="Descargar XML", default=True,
        help="El comprobante completo, que es lo que se guarda en la bóveda.")
    download_metadata = fields.Boolean(
        string="Descargar metadatos", default=True,
        help="El índice ligero. Sirve para detectar cancelaciones, porque incluye "
             "vigentes y cancelados en la misma solicitud — cosa que el XML completo "
             "no permite.")

    date_covered_to = fields.Date(
        string="Cubierto hasta", readonly=True,
        help="Última fecha ya solicitada. La siguiente corrida continúa desde aquí.")
    last_run = fields.Datetime(string="Última corrida", readonly=True)
    last_error = fields.Char(string="Último problema", readonly=True)
    consecutive_failures = fields.Integer(readonly=True, default=0)

    request_ids = fields.One2many('sat.download.request', 'schedule_id', string="Solicitudes")
    request_count = fields.Integer(compute='_compute_request_count')

    @api.depends('request_ids')
    def _compute_request_count(self):
        for rec in self:
            rec.request_count = len(rec.request_ids)

    @api.depends('company_id', 'periodicity')
    def _compute_display_name(self):
        etiquetas = dict(self._fields['periodicity'].selection)
        for rec in self:
            rec.display_name = _("Descarga %(periodicidad)s · %(empresa)s",
                                  periodicidad=etiquetas.get(rec.periodicity, ''),
                                  empresa=rec.company_id.name or '')

    # -------------------------------------------------------------------
    # Cálculo del siguiente periodo
    # -------------------------------------------------------------------

    def _next_period(self):
        """Devuelve (desde, hasta) del siguiente periodo a solicitar, o False si
        ya está al día. Aplica la ventana de recuperación sobre lo ya cubierto."""
        self.ensure_one()
        hoy = fields.Date.context_today(self)

        # `avance` es la frontera real: el primer día que todavía no se cubre.
        # Es lo único que determina el periodo y lo único que hace progresar la
        # programación. El lookback NO se le aplica: si se le aplicara, cada
        # corrida retrocedería la frontera `lookback_days - 1` días y la
        # programación se quedaría clavada para siempre en el primer periodo,
        # remandando el mismo rango al SAT todos los días.
        if not self.date_covered_to:
            avance = self.date_start
        else:
            avance = self.date_covered_to + timedelta(days=1)

        if avance > hoy:
            return False

        if self.periodicity == 'daily':
            hasta = avance
        elif self.periodicity == 'weekly':
            hasta = avance + timedelta(days=6)
        else:  # quincenal natural: 1-15 y 16-fin de mes
            if avance.day <= 15:
                hasta = avance.replace(day=15)
            else:
                ultimo = calendar.monthrange(avance.year, avance.month)[1]
                hasta = avance.replace(day=ultimo)

        # El lookback solo ensancha la ventana de consulta hacia atrás, para
        # recapturar comprobantes que el proveedor timbró tarde sobre días ya
        # cubiertos. Los duplicados se descartan por folio fiscal.
        desde = avance
        if self.lookback_days:
            desde = max(self.date_start, avance - timedelta(days=self.lookback_days))

        return desde, min(hasta, hoy)

    # -------------------------------------------------------------------
    # Generación de solicitudes
    # -------------------------------------------------------------------

    def _combinaciones(self):
        """Qué solicitudes hay que crear por periodo.

        Los metadatos se piden con estatus 'todos' a propósito: es la única
        forma de ver vigentes y cancelados juntos, y es justo para lo que
        sirven. El XML completo solo admite 'vigente' — el SAT rechaza la
        mezcla."""
        self.ensure_one()
        direcciones = []
        if self.sync_received:
            direcciones.append('received')
        if self.sync_issued:
            direcciones.append('issued')

        combos = []
        for direccion in direcciones:
            if self.download_metadata:
                combos.append((direccion, 'metadata', 'todos'))
            if self.download_xml:
                combos.append((direccion, 'cfdi', 'vigente'))
        return combos

    def action_ver_solicitudes(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Solicitudes de esta programación"),
            'res_model': 'sat.download.request',
            'view_mode': 'list,form',
            'domain': [('schedule_id', '=', self.id)],
        }

    def action_generar_ahora(self):
        """Genera el siguiente periodo a mano, sin esperar al cron."""
        self.ensure_one()
        creadas = self._generar_periodo()
        if not creadas:
            raise UserError(_("No hay periodo pendiente: esta programación ya está al día."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Solicitudes generadas"),
            'res_model': 'sat.download.request',
            'view_mode': 'list,form',
            'domain': [('id', 'in', creadas.ids)],
        }

    def _generar_periodo(self):
        """Crea las solicitudes del siguiente periodo y las manda al SAT.
        Devuelve el recordset creado (vacío si ya estaba al día)."""
        self.ensure_one()
        Request = self.env['sat.download.request']
        periodo = self._next_period()
        if not periodo:
            return Request
        desde, hasta = periodo

        creadas = Request
        for direccion, tipo, estatus in self._combinaciones():
            creadas |= Request.create({
                'company_id': self.company_id.id,
                'credential_id': self.credential_id.id,
                'schedule_id': self.id,
                'direction': direccion,
                'request_type': tipo,
                'estado_comprobante': estatus,
                'date_from': desde,
                'date_to': hasta,
            })

        # Se avanzan de inmediato hasta dejarlas solicitadas ante el SAT; el
        # resto del ciclo (verificar y descargar) lo hace el otro cron, porque
        # el SAT tarda de minutos a horas en preparar los paquetes.
        for solicitud in creadas:
            solicitud._avanzar_sin_excepciones()

        self.write({
            'date_covered_to': hasta,
            'last_run': fields.Datetime.now(),
        })
        return creadas

    # -------------------------------------------------------------------
    # Crons
    # -------------------------------------------------------------------

    @api.model
    def _cron_generar_solicitudes(self):
        """Recorre las programaciones activas y pide el siguiente periodo.
        Nunca lanza: una programación rota no debe frenar a las demás."""
        for programacion in self.search([]):
            try:
                creadas = programacion._generar_periodo()
                if creadas:
                    programacion.write({'last_error': False, 'consecutive_failures': 0})
            except Exception as e:  # pylint: disable=broad-except
                _logger.exception("Falló la generación de solicitudes para %s", programacion.display_name)
                fallos = programacion.consecutive_failures + 1
                programacion.write({
                    'last_error': str(e)[:500],
                    'consecutive_failures': fallos,
                    'last_run': fields.Datetime.now(),
                })
                if fallos == 2:
                    programacion.message_post(body=_(
                        "La descarga automática del SAT falló dos veces seguidas. "
                        "Último problema: %s", str(e)[:300]))
            self.env.cr.commit()

    @api.model
    def _cron_avanzar_solicitudes(self):
        """Empuja las solicitudes en vuelo: verifica las pendientes y descarga
        las que ya estén listas. Es el que hace que el usuario no tenga que
        apretar botones."""
        pendientes = self.env['sat.download.request'].search([
            ('state', 'in', ('draft', 'authenticated', 'requested', 'verifying', 'downloading')),
        ])
        for solicitud in pendientes:
            solicitud._avanzar_sin_excepciones()
            self.env.cr.commit()
