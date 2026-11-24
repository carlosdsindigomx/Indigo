import calendar
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError
from odoo.tools import format_date

from odoo.addons.dsi_cfdi_vault.models.cfdi_document import ERRORES_DE_CONCURRENCIA
from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import _puede_hacer_commit

_logger = logging.getLogger(__name__)

# Refresco semanal de metadatos: cuántos días hacia atrás por omisión y
# cuántas veces se recorre un día el inicio de la ventana para no repetir una
# solicitud ya hecha con los mismos parámetros. El SAT no deja repetir una
# solicitud vigente con los mismos parámetros (CodEstatus 5005) y limita las
# que se repiten (5002).
DIAS_DE_REFRESCO = 90
INTENTOS_SIN_REPETIR = 8

# «Verificar y descargar» trabaja en tandas. Al ponerse al día pueden ser
# cientos de solicitudes, y Odoo corta la tarea programada que pasa de su
# tiempo límite (120 s por omisión con workers): cada tanda deja de empezar
# solicitudes a los SEGUNDOS_POR_TANDA y, si quedan, la siguiente arranca
# MINUTOS_ENTRE_TANDAS después. Una solicitud que el SAT todavía prepara no se
# vuelve a consultar antes de MINUTOS_ENTRE_CONSULTAS: así la siguiente tanda
# sigue con las demás en vez de repetir las mismas.
SEGUNDOS_POR_TANDA = 45
MINUTOS_ENTRE_TANDAS = 2
MINUTOS_ENTRE_CONSULTAS = 30
POR_MANDAR = ('draft', 'authenticated')

TAREA_DE_GENERAR = 'dsi_cfdi_sat_sync.ir_cron_generar_solicitudes'
TAREA_DE_AVANZAR = 'dsi_cfdi_sat_sync.ir_cron_avanzar_solicitudes'

# Lo que cambia qué le toca pedir: al guardarlo se lanza la tarea que genera
# las solicitudes, sin esperar a su corrida diaria.
CAMPOS_QUE_LANZAN_LA_DESCARGA = {'date_start', 'periodicity', 'active'}


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
        help="Al guardar, la descarga se pone al día: pide al SAT todo lo que falte desde esta "
             "fecha hasta hoy, en varias solicitudes, sea cual sea la periodicidad. Moverla a una "
             "fecha anterior pide lo que falta desde ahí; lo ya pedido no se repite.")
    periodicity = fields.Selection([
        ('daily', "Diaria"),
        ('weekly', "Semanal"),
        ('biweekly', "Quincenal"),
    ], string="Periodicidad", default='daily', required=True,
        help="Cada cuánto vuelve al SAT ya que está al día, y cada vez trae lo que falta hasta ese "
             "día. Diaria: cada día. Semanal: a los siete días de la vez anterior. Quincenal: al "
             "empezar cada quincena natural (el 1 y el 16), con la quincena que terminó. Es también "
             "el tamaño de cada solicitud: al ponerse al día se pide un periodo tras otro.")
    lookback_days = fields.Integer(
        string="Días de recuperación", default=3,
        help="Al volver al SAT, la primera solicitud empieza N días antes de lo ya cubierto, para "
             "atrapar comprobantes que el proveedor timbró tarde. El último día ya pedido se repite "
             "siempre, aunque sea 0: se pidió antes de que terminara. Los duplicados se descartan "
             "solos por folio fiscal.")

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

    date_covered_from = fields.Date(
        string="Cubierto desde", readonly=True, copy=False,
        help="Primer día ya pedido al SAT. Si «Traer desde» se mueve antes, se pide lo que falta "
             "hasta aquí.")
    date_covered_to = fields.Date(
        string="Cubierto hasta", readonly=True,
        help="Último día ya pedido al SAT. El día de hoy se pide como vaya: lo que se timbre "
             "después llega en la siguiente ida al SAT.")
    date_next_visit = fields.Date(
        string="Siguiente ida al SAT", compute='_compute_date_next_visit',
        help="Cuándo vuelve a pedir al SAT por su cuenta, según la periodicidad. «Ponerse al día "
             "ahora» no espera.")
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

    @api.depends('date_start', 'date_covered_from', 'date_covered_to', 'periodicity', 'active')
    def _compute_date_next_visit(self):
        hoy = fields.Date.context_today(self)
        for rec in self:
            if not (rec.active and rec.date_start):
                rec.date_next_visit = False
            elif rec._falta_antes_de_lo_cubierto():
                rec.date_next_visit = hoy
            else:
                rec.date_next_visit = max(hoy, rec._siguiente_ida())

    @api.model_create_multi
    def create(self, vals_list):
        programaciones = super().create(vals_list)
        programaciones._lanzar_la_descarga()
        return programaciones

    def write(self, vals):
        if vals.get('date_start'):
            # Las que se pusieron al día antes de «Cubierto desde» empezaron en su
            # «Traer desde»: se anota antes de cambiarlo, para que al moverlo
            # antes se pida lo que falta.
            for rec in self.filtered(lambda rec: rec.date_covered_to and not rec.date_covered_from):
                super(SatSyncSchedule, rec).write({'date_covered_from': rec.date_start})
        resultado = super().write(vals)
        if CAMPOS_QUE_LANZAN_LA_DESCARGA & vals.keys():
            self._lanzar_la_descarga()
        return resultado

    def _lanzar_la_descarga(self):
        """Al configurar o cambiar lo que toca pedir, la tarea que genera las
        solicitudes corre ya, sin esperar a su corrida diaria."""
        if self.filtered('active'):
            self._lanzar_tarea(TAREA_DE_GENERAR)

    @api.model
    def _lanzar_tarea(self, tarea, minutos=0):
        """Lanza una tarea programada de la descarga (ya, o en `minutos`) sin
        esperar a su siguiente corrida."""
        cron = self.env.ref(tarea, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(fields.Datetime.now() + timedelta(minutes=minutos))

    # -------------------------------------------------------------------
    # Qué pedir y cuándo
    # -------------------------------------------------------------------

    def _avance(self):
        """El primer día que todavía no se pide."""
        self.ensure_one()
        if not self.date_covered_to:
            return self.date_start
        return max(self.date_covered_to + timedelta(days=1), self.date_start)

    def _fin_del_periodo(self, inicio):
        """El último día del periodo que empieza en `inicio`: ese mismo día, siete
        días o la quincena natural (del 1 al 15 y del 16 a fin de mes)."""
        self.ensure_one()
        if self.periodicity == 'daily':
            return inicio
        if self.periodicity == 'weekly':
            return inicio + timedelta(days=6)
        if inicio.day <= 15:
            return inicio.replace(day=15)
        return inicio.replace(day=calendar.monthrange(inicio.year, inicio.month)[1])

    def _cubierto_desde(self):
        """El primer día ya pedido. Las que se pusieron al día antes de que
        existiera «Cubierto desde» empezaron en su «Traer desde»."""
        self.ensure_one()
        return self.date_covered_from or self.date_start

    def _falta_antes_de_lo_cubierto(self):
        """«Traer desde» se movió antes de lo ya pedido: falta ese tramo."""
        self.ensure_one()
        return bool(self.date_covered_to) and self.date_start < self._cubierto_desde()

    def _siguiente_ida(self):
        """El día en que le toca volver al SAT por su cuenta (hoy, o antes si va
        atrasada). La primera vez, en «Traer desde». Después, al día siguiente
        de lo cubierto, a los siete días, o al empezar la quincena siguiente
        (el 16, o el 1 del mes que sigue)."""
        self.ensure_one()
        cubierto = self.date_covered_to
        if not cubierto:
            return self.date_start
        if self.periodicity == 'daily':
            return cubierto + timedelta(days=1)
        if self.periodicity == 'weekly':
            return cubierto + timedelta(days=7)
        if cubierto.day < 16:
            return cubierto.replace(day=16)
        return (cubierto.replace(day=1) + timedelta(days=32)).replace(day=1)

    def _partir(self, desde, hasta):
        """[desde, hasta] en periodos del tamaño de la periodicidad, del más
        antiguo al más reciente. El último puede quedar corto."""
        self.ensure_one()
        periodos = []
        while desde <= hasta:
            fin = min(self._fin_del_periodo(desde), hasta)
            periodos.append((desde, fin))
            desde = fin + timedelta(days=1)
        return periodos

    def _inicio_de_la_recuperacion(self, avance):
        """Desde dónde empieza la primera solicitud de una ida al SAT: los días
        de recuperación antes de lo ya cubierto, y al menos el último día, que
        se pidió antes de que terminara. Nunca antes de «Traer desde»."""
        self.ensure_one()
        return max(self.date_start, avance - timedelta(days=max(self.lookback_days, 1)))

    def _periodos_pendientes(self, forzar=False):
        """Los periodos (desde, hasta) que hay que pedir ahora, del más antiguo
        al más reciente.

        Primero lo que falte antes de lo ya cubierto, si «Traer desde» se movió
        antes. Después, si le toca ir al SAT (o con `forzar`, el botón), todo lo
        que falta hasta hoy, un periodo tras otro: así se pone al día aunque
        vaya meses atrás, sea cual sea la periodicidad. El día de hoy se pide
        como vaya; lo que se timbre después llega en la siguiente ida, que lo
        vuelve a pedir.

        Los días de recuperación solo ensanchan el primer periodo de la ida,
        hacia atrás sobre lo ya cubierto: los demás son días que se piden por
        primera vez. Si ya estaba pedido hasta hoy, el botón vuelve a pedir esos
        últimos días sin repetir las fechas de una solicitud anterior, porque
        el SAT no lo deja.
        """
        self.ensure_one()
        hoy = fields.Date.context_today(self)
        periodos = []
        if self._falta_antes_de_lo_cubierto():
            periodos += self._partir(self.date_start, self._cubierto_desde() - timedelta(days=1))
        if self.date_start > hoy or not (forzar or self._siguiente_ida() <= hoy):
            return periodos
        avance = self._avance()
        if avance <= hoy:
            adelante = self._partir(avance, hoy)
            if self.date_covered_to:
                adelante[0] = (self._inicio_de_la_recuperacion(avance), adelante[0][1])
            periodos += adelante
        elif forzar:
            inicio = self._inicio_sin_repetir(
                self._inicio_de_la_recuperacion(avance), hoy, self._combinaciones())
            if inicio:
                periodos.append((inicio, hoy))
        return periodos

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
        """«Ponerse al día ahora»: pide lo que falte hasta hoy sin esperar a que
        le toque ir al SAT; si ya estaba pedido hasta hoy, vuelve a pedir los
        últimos días para traer lo que se timbró después. Las solicitudes se
        crean al momento y la tarea «verificar y descargar» las manda
        enseguida."""
        self.ensure_one()
        creadas = self._generar_solicitudes(forzar=True)
        if not creadas:
            if not self._combinaciones():
                raise UserError(_(
                    "No hay nada que pedir: activa «Recibidos» o «Emitidos», y «Descargar XML» o "
                    "«Descargar metadatos»."))
            if self.date_start > fields.Date.context_today(self):
                raise UserError(_(
                    "Todavía no hay nada que pedir: «Traer desde» es el %s.",
                    format_date(self.env, self.date_start)))
            raise UserError(_(
                "Lo de hoy ya se volvió a pedir varias veces, y el SAT no deja repetir una solicitud "
                "con las mismas fechas. Mañana se puede otra vez."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Solicitudes generadas"),
            'res_model': 'sat.download.request',
            'view_mode': 'list,form',
            'domain': [('id', 'in', creadas.ids)],
        }

    def _generar_solicitudes(self, forzar=False):
        """Crea las solicitudes de los periodos pendientes (_periodos_pendientes),
        una por combinación de cada periodo, y da por cubierto de «Traer desde»
        al último.

        No habla con el SAT, así que es rápido aunque sean meses: las manda la
        tarea «verificar y descargar», que se lanza enseguida y trabaja en tandas.
        Sin nada que pedir (ni recibidos ni emitidos, o ni XML ni metadatos) no
        mueve «Cubierto hasta»: al activarlo se pide desde ahí.

        Devuelve las solicitudes creadas, de la más antigua a la más reciente.
        """
        self.ensure_one()
        # Antes de crear nada: sin licencia, las solicitudes no podrían mandarse
        # al SAT y aun así el periodo quedaría marcado como cubierto.
        self.env['dsi.license.client.state']._check_feature(
            'vault', _("descargar CFDI del SAT"), self.company_id)
        Request = self.env['sat.download.request']
        combinaciones = self._combinaciones()
        periodos = self._periodos_pendientes(forzar=forzar) if combinaciones else []
        if not periodos:
            return Request
        creadas = Request.create([{
            'company_id': self.company_id.id,
            'credential_id': self.credential_id.id,
            'schedule_id': self.id,
            'direction': direccion,
            'request_type': tipo,
            'estado_comprobante': estatus,
            'date_from': desde,
            'date_to': hasta,
        } for desde, hasta in periodos for direccion, tipo, estatus in combinaciones])
        hasta = max(fin for _inicio, fin in periodos)
        self.write({
            'date_covered_from': min(self._cubierto_desde(), self.date_start),
            'date_covered_to': max(hasta, self.date_covered_to) if self.date_covered_to else hasta,
            'last_run': fields.Datetime.now(),
        })
        self._lanzar_tarea(TAREA_DE_AVANZAR)
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
            inicio = self._inicio_sin_repetir(desde, hasta, [(direccion, 'metadata', 'todos')])
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

    def _inicio_sin_repetir(self, desde, hasta, combinaciones):
        """El inicio de la ventana para que ninguna de sus solicitudes (una por
        combinación de dirección, tipo y estatus) repita una ya hecha en esta
        empresa con los mismos parámetros, en cualquier estado: se recorre un
        día hacia atrás cada vez. Cubre los botones apretados varias veces (la
        tarea semanal de metadatos ya mueve la ventana siete días). False si
        ninguna sirve."""
        Request = self.env['sat.download.request']
        for atras in range(INTENTOS_SIN_REPETIR):
            inicio = desde - timedelta(days=atras)
            if not any(Request.search_count([
                ('company_id', '=', self.company_id.id),
                ('direction', '=', direccion),
                ('request_type', '=', tipo),
                ('estado_comprobante', '=', estatus),
                ('date_from', '=', inicio),
                ('date_to', '=', hasta),
            ], limit=1) for direccion, tipo, estatus in combinaciones):
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
        """Crea en cada programación activa lo que le toque: ponerse al día, o su
        periodo de cada día, semana o quincena. Corre a diario y al guardar una
        programación. Nunca lanza: una programación rota no debe frenar a las
        demás."""
        Licencia = self.env['dsi.license.client.state']
        for programacion in self.search([]):
            licencia = Licencia._get_or_create(programacion.company_id)
            if not licencia.is_feature_enabled('vault'):
                # No es una falla técnica: no cuenta para el aviso de fallas
                # seguidas, solo se deja dicho por qué no avanza.
                programacion.write({'last_error': licencia.status_message})
                continue
            try:
                with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                    creadas = programacion._generar_solicitudes()
                if creadas:
                    programacion.write({'last_error': False, 'consecutive_failures': 0})
            except ERRORES_DE_CONCURRENCIA as error:
                # El botón la ponía al día a la vez: ya lo hizo él.
                _logger.info("%s: otra transacción generaba sus solicitudes a la vez (%s); se deja.",
                             programacion.display_name, type(error).__name__)
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
            if _puede_hacer_commit():
                self.env.cr.commit()

    @api.model
    def _cron_avanzar_solicitudes(self):
        """Empuja las solicitudes en vuelo: manda al SAT las nuevas, verifica las
        pendientes y descarga las que ya estén listas. Es el que hace que el
        usuario no tenga que apretar botones.

        Va en tandas (ver SEGUNDOS_POR_TANDA): primero las que faltan de mandar,
        de la más antigua a la más reciente, y después las que llevan más tiempo
        sin consultarse. Si el SAT no contesta o no acepta la e.firma, las demás
        de esa e.firma se dejan para la siguiente corrida: una e.firma vencida no
        deja cientos de solicitudes rechazadas."""
        consultadas_antes_de = fields.Datetime.now() - timedelta(minutes=MINUTOS_ENTRE_CONSULTAS)
        pendientes = self.env['sat.download.request'].search([
            '|', '|',
            ('state', 'in', POR_MANDAR),
            '&', ('state', 'in', ('requested', 'verifying', 'downloading')),
            ('write_date', '<', consultadas_antes_de),
            '&', ('state', '=', 'processed'), ('metadata_pending', '=', True),
        ], order='write_date, id')
        Licencia = self.env['dsi.license.client.state']
        con_licencia = pendientes.company_id.filtered(
            lambda empresa: Licencia._get_or_create(empresa).is_feature_enabled('vault'))
        pendientes = pendientes.filtered(lambda s: s.company_id in con_licencia).sorted(
            lambda s: s.state not in POR_MANDAR)
        fin_de_la_tanda = time.monotonic() + SEGUNDOS_POR_TANDA
        sin_respuesta = self.env['sat.credential']
        avanzadas = 0
        for solicitud in pendientes:
            if solicitud.credential_id in sin_respuesta:
                continue
            if avanzadas and time.monotonic() > fin_de_la_tanda:
                self._lanzar_tarea(TAREA_DE_AVANZAR, MINUTOS_ENTRE_TANDAS)
                break
            avanzadas += 1
            if not solicitud._avanzar_sin_excepciones():
                sin_respuesta |= solicitud.credential_id
            if _puede_hacer_commit():
                self.env.cr.commit()
