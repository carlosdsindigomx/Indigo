import calendar
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import _puede_hacer_commit

_logger = logging.getLogger(__name__)

# Refresco semanal de metadatos: cuántos días hacia atrás por omisión y
# cuántas veces se recorre un día el inicio de la ventana para no repetir una
# solicitud ya hecha con los mismos parámetros. El SAT no deja repetir una
# solicitud vigente con los mismos parámetros (CodEstatus 5005) y limita las
# que se repiten (5002).
DIAS_DE_REFRESCO = 90
INTENTOS_SIN_REPETIR = 8


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

    metadata_refresh_days = fields.Integer(
        string="Refrescar metadatos (días)", default=DIAS_DE_REFRESCO,
        help="Una vez por semana se vuelven a pedir los metadatos de emitidos y recibidos de "
             "estos últimos días, para ver las cancelaciones de lo que ya se descargó: cada periodo "
             "se pide una sola vez y un CFDI se puede cancelar después. No mueve «Cubierto hasta». "
             "0 lo apaga. Requiere «Descargar metadatos».")
    metadata_refreshed_on = fields.Datetime(string="Último refresco de metadatos", readonly=True)

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
        # Antes de crear nada: sin licencia, las solicitudes no podrían
        # mandarse al SAT y aun así el periodo quedaría marcado como cubierto.
        self.env['dsi.license.client.state']._check_feature(
            'vault', _("descargar CFDI del SAT"), self.company_id)
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
    # Refresco semanal de metadatos
    # -------------------------------------------------------------------

    def action_refrescar_metadatos(self):
        """«Refrescar metadatos ahora», sin esperar a la tarea semanal."""
        self.ensure_one()
        creadas = self._refrescar_metadatos()
        if not creadas:
            raise UserError(_(
                "No se pidió nada: activa «Descargar metadatos» y pon los días a refrescar, o ya se "
                "pidieron hoy todas las ventanas posibles."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Solicitudes generadas"),
            'res_model': 'sat.download.request',
            'view_mode': 'list,form',
            'domain': [('id', 'in', creadas.ids)],
        }

    def _refrescar_metadatos(self):
        """Pide los metadatos (vigentes y cancelados) de emitidos y recibidos
        de los últimos `metadata_refresh_days` días, hasta ayer. Cada periodo
        se pide una sola vez al descargar, y un CFDI se puede cancelar después:
        sin esto, esa cancelación nunca llegaba por metadatos.

        Son emitidos y recibidos aunque la programación solo descargue unos:
        el estatus le sirve a las facturas de los dos lados. No toca «Cubierto
        hasta»: el avance de la descarga es otra cosa. Nunca repite una
        solicitud con los mismos parámetros (_inicio_sin_repetir).

        Devuelve las solicitudes creadas, ya mandadas al SAT.
        """
        self.ensure_one()
        self.env['dsi.license.client.state']._check_feature(
            'vault', _("descargar CFDI del SAT"), self.company_id)
        Request = self.env['sat.download.request']
        if not self.download_metadata or self.metadata_refresh_days <= 0:
            return Request
        hoy = fields.Date.context_today(self)
        hasta = hoy - timedelta(days=1)
        desde = hoy - timedelta(days=self.metadata_refresh_days)
        creadas = Request
        for direccion in ('received', 'issued'):
            inicio = self._inicio_sin_repetir(direccion, desde, hasta)
            if not inicio:
                _logger.info(
                    "%s: el refresco de metadatos (%s) ya se pidió hoy con todas las ventanas posibles.",
                    self.display_name, direccion)
                continue
            creadas |= Request.create({
                'company_id': self.company_id.id,
                'credential_id': self.credential_id.id,
                'schedule_id': self.id,
                'direction': direccion,
                'request_type': 'metadata',
                'estado_comprobante': 'todos',
                'date_from': inicio,
                'date_to': hasta,
                'is_refresh': True,
            })
        for solicitud in creadas:
            solicitud._avanzar_sin_excepciones()
        self.write({'metadata_refreshed_on': fields.Datetime.now()})
        return creadas

    def _inicio_sin_repetir(self, direccion, desde, hasta):
        """El inicio de la ventana para que la solicitud no repita una ya hecha
        en esta empresa con los mismos parámetros (dirección, tipo, estatus y
        fechas), en cualquier estado: se recorre un día hacia atrás cada vez.
        La tarea semanal ya mueve la ventana siete días; esto cubre el botón
        apretado varias veces. False si ninguna sirve."""
        Request = self.env['sat.download.request']
        for atras in range(INTENTOS_SIN_REPETIR):
            inicio = desde - timedelta(days=atras)
            if not Request.search_count([
                ('company_id', '=', self.company_id.id),
                ('direction', '=', direccion),
                ('request_type', '=', 'metadata'),
                ('estado_comprobante', '=', 'todos'),
                ('date_from', '=', inicio),
                ('date_to', '=', hasta),
            ], limit=1):
                return inicio
        return False

    # -------------------------------------------------------------------
    # Crons
    # -------------------------------------------------------------------

    @api.model
    def _cron_refrescar_metadatos(self):
        """Refresco semanal de metadatos de las programaciones activas con
        licencia. Una sin licencia no se toca (la tarea diaria ya deja dicho
        por qué no avanza). Nunca lanza: una programación rota no frena a las
        demás."""
        Licencia = self.env['dsi.license.client.state']
        for programacion in self.search([('download_metadata', '=', True), ('metadata_refresh_days', '>', 0)]):
            if not Licencia._get_or_create(programacion.company_id).is_feature_enabled('vault'):
                continue
            try:
                programacion._refrescar_metadatos()
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Falló el refresco de metadatos de %s", programacion.display_name)
            if _puede_hacer_commit():
                self.env.cr.commit()

    @api.model
    def _cron_generar_solicitudes(self):
        """Recorre las programaciones activas y pide el siguiente periodo.
        Nunca lanza: una programación rota no debe frenar a las demás."""
        Licencia = self.env['dsi.license.client.state']
        for programacion in self.search([]):
            licencia = Licencia._get_or_create(programacion.company_id)
            if not licencia.is_feature_enabled('vault'):
                # No es una falla técnica: no cuenta para el aviso de fallas
                # seguidas, solo se deja dicho por qué no avanza.
                programacion.write({'last_error': licencia.status_message})
                continue
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
            '|', ('state', 'in', ('draft', 'authenticated', 'requested', 'verifying', 'downloading')),
            '&', ('state', '=', 'processed'), ('metadata_pending', '=', True),
        ])
        Licencia = self.env['dsi.license.client.state']
        con_licencia = pendientes.company_id.filtered(
            lambda empresa: Licencia._get_or_create(empresa).is_feature_enabled('vault'))
        for solicitud in pendientes.filtered(lambda s: s.company_id in con_licencia):
            solicitud._avanzar_sin_excepciones()
            self.env.cr.commit()
