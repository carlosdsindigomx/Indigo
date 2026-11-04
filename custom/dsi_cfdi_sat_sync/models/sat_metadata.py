"""Metadatos de la descarga masiva del SAT: el índice de lo emitido y lo
recibido, con su estatus (vigente o cancelado) y la fecha de cancelación.

Antes se pedían (estatus «todos», el único tipo de solicitud que trae vigentes
y cancelados juntos) y se tiraban: el paquete trae un TXT y solo se abrían los
XML. Ahora se leen, se guardan uno por empresa y folio fiscal, y alimentan el
estatus SAT de los CFDI de la Bóveda y el «Estado bóveda» de las facturas,
también de las que no tienen su XML en la Bóveda (el XML de un cancelado nunca
se descarga: el SAT solo da el de los vigentes).

El formato no lo publica Odoo ni este repositorio tenía una muestra. Se tomó de
phpcfdi/sat-ws-descarga-masiva, la librería pública que más se usa con este
servicio (CsvReader, MetadataPreprocessor y los filtros de archivo del lector
de paquetes): separador «~» sin comillas, fin de línea CRLF con algún LF suelto
dentro de un nombre, encabezado en la primera línea y, desde septiembre de
2022, un archivo aparte `<id>_tercero.txt` con los datos «a cuenta de
terceros». Por eso el lector va por encabezado y no por posición, y una línea
que no cuadra con él no se adivina: se cuenta y se registra.
"""
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from odoo import api, fields, models, tools
from odoo.tools import SQL, split_every

from odoo.addons.dsi_cfdi_vault.models.account_move import TIPOS_DE_CFDI_DE_FACTURA
from odoo.addons.dsi_cfdi_vault.models.cfdi_document import ERRORES_DE_CONCURRENCIA
from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import MARGEN_TRAS_UN_CHOQUE
from odoo.addons.dsi_cfdi_vault.models.cfdi_payment_line import ZONA_DEL_CFDI

_logger = logging.getLogger(__name__)

# Columna del TXT (normalizada con _clave_de_columna) -> campo del modelo.
COLUMNAS = {
    'uuid': 'uuid',
    'rfcemisor': 'rfc_emisor',
    'nombreemisor': 'nombre_emisor',
    'rfcreceptor': 'rfc_receptor',
    'nombrereceptor': 'nombre_receptor',
    'rfcpac': 'rfc_pac',
    'fechaemision': 'fecha_emision',
    'fechacertificacionsat': 'fecha_certificacion',
    'monto': 'monto',
    'efectocomprobante': 'efecto',
    'estatus': 'estatus',
    'fechacancelacion': 'fecha_cancelacion',
    'rfcacuentaterceros': 'rfc_a_cuenta_terceros',
    'nombreacuentaterceros': 'nombre_a_cuenta_terceros',
}
CAMPOS_DE_FECHA = {'fecha_emision', 'fecha_certificacion', 'fecha_cancelacion'}
CAMPOS_DE_TERCEROS = ('rfc_a_cuenta_terceros', 'nombre_a_cuenta_terceros')

# Estatus del TXT: 1 vigente y 0 cancelado. Se aceptan también las palabras,
# que es como lo muestra el portal; cualquier otro valor hace la línea ilegible.
ESTATUS = {'1': '1', '0': '0', 'vigente': '1', 'cancelado': '0'}

EFECTOS = ('I', 'E', 'T', 'N', 'P')

FORMATOS_DE_FECHA = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%d/%m/%Y %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y')

# Metadatos por tanda al buscar los que ya están guardados y sus ligas.
LOTE_DE_METADATOS = 1000

# Las primeras líneas ilegibles que se guardan en la solicitud para revisarlas;
# las demás solo se cuentan.
LINEAS_ILEGIBLES_A_MOSTRAR = 20


def _clave_de_columna(nombre):
    """El nombre de una columna sin acentos, espacios, guiones bajos ni
    mayúsculas: «Uuid», « UUID », «Fecha_Cancelación» y «FECHACANCELACION»
    son la misma columna."""
    sin_acentos = unicodedata.normalize('NFKD', nombre or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[\s_\-]', '', sin_acentos).lower()


def _texto_del_archivo(contenido):
    """Los bytes del TXT como texto: UTF-8 (con o sin BOM) y, si no lo es,
    Latin-1, que acepta cualquier byte."""
    if contenido.startswith(b'\xef\xbb\xbf'):
        contenido = contenido[3:]
    try:
        return contenido.decode('utf-8')
    except UnicodeDecodeError:
        return contenido.decode('latin-1')


def _registros(texto):
    """Las líneas del TXT. El SAT termina cada una con CRLF y a veces trae un
    LF suelto dentro de un nombre: si la primera línea termina en CRLF, solo
    CRLF separa registros y el LF suelto es un espacio. Si no, separa el LF
    (con o sin CR). No str.splitlines(): también corta en caracteres que no
    son fin de línea y pueden venir en un nombre, como \\x85 (el «…» de
    Windows leído como Latin-1) o \\u2028, y partía la línea en dos
    ilegibles."""
    salto = texto.find('\n')
    if salto > 0 and texto[salto - 1] == '\r':
        return [registro.replace('\n', ' ').replace('\r', ' ') for registro in texto.split('\r\n')]
    return [registro.removesuffix('\r') for registro in texto.split('\n')]


def _fecha_hora(valor):
    """Una fecha del TXT, en la hora del centro de México y sin zona, en UTC
    como Odoo guarda las suyas. False si viene vacía o no se entiende."""
    valor = (valor or '').strip()
    if not valor:
        return False
    valor = valor.split('.', 1)[0]
    for formato in FORMATOS_DE_FECHA:
        try:
            fecha = datetime.strptime(valor, formato)
        except ValueError:
            continue
        return fecha.replace(tzinfo=ZONA_DEL_CFDI).astimezone(timezone.utc).replace(tzinfo=None)
    return False


def _monto(valor):
    """El monto del TXT, o None si no es un número."""
    try:
        return float(valor or 0.0)
    except ValueError:
        return None


def leer_metadatos_txt(contenido, nombre=''):
    """Lee un TXT de metadatos del SAT. Función pura: no toca la base.

    :param contenido: los bytes del archivo.
    :param nombre: el nombre del archivo, solo para los mensajes.
    :return: dict con
        - 'tipo': 'metadatos' (trae Uuid y Estatus), 'terceros' (el archivo
          `_tercero.txt`: Uuid y RfcACuentaTerceros) o False si el encabezado
          no es de ninguno de los dos;
        - 'filas': una lista de dicts con los campos del modelo (UUID en
          mayúsculas, fechas en UTC) y 'extra' con las columnas que no se
          conocen;
        - 'ilegibles': una lista de dicts (archivo, línea, motivo, texto y los
          datos del motivo) de las líneas que no se pudieron leer. No se
          adivinan: una línea con más o menos campos que el encabezado (un «~»
          dentro de un nombre, por ejemplo), sin folio fiscal o sin un estatus
          conocido se cuenta aquí y se sigue con la siguiente. El motivo es
          una clave: el texto lo pone quien lo muestra, con su idioma
          (_describir_ilegible); aquí no hay de dónde sacarlo.
    Las líneas en blanco no cuentan.
    """
    resultado = {'tipo': False, 'filas': [], 'ilegibles': []}

    def ilegible(linea, motivo, texto, **datos):
        resultado['ilegibles'].append(dict(datos, archivo=nombre, linea=linea, motivo=motivo, texto=texto))

    registros = _registros(_texto_del_archivo(contenido or b''))
    encabezado = None
    for numero, registro in enumerate(registros, 1):
        if not registro.strip():
            continue
        valores = [valor.strip() for valor in registro.split('~')]
        if encabezado is None:
            encabezado = [_clave_de_columna(valor) for valor in valores]
            if 'uuid' in encabezado and 'estatus' in encabezado:
                resultado['tipo'] = 'metadatos'
            elif 'uuid' in encabezado and 'rfcacuentaterceros' in encabezado:
                resultado['tipo'] = 'terceros'
            else:
                ilegible(numero, 'encabezado', registro)
                return resultado
            originales = valores
            continue
        if len(valores) != len(encabezado):
            ilegible(numero, 'campos', registro, campos=len(valores), columnas=len(encabezado))
            continue
        fila, extra = {}, {}
        for clave, original, valor in zip(encabezado, originales, valores):
            campo = COLUMNAS.get(clave)
            if not campo:
                if valor:
                    extra[original] = valor
            elif campo in CAMPOS_DE_FECHA or campo == 'monto':
                # Una fecha o un monto mal escritos no hacen ilegible la
                # línea (no deciden nada), pero no se adivinan: el valor queda
                # tal cual en las columnas adicionales.
                leido = _monto(valor) if campo == 'monto' else _fecha_hora(valor)
                if valor and (leido is None or leido is False):
                    extra[original] = valor
                fila[campo] = leido or (0.0 if campo == 'monto' else False)
            else:
                fila[campo] = valor or False
        fila['uuid'] = (fila.get('uuid') or '').upper()
        if not fila['uuid']:
            ilegible(numero, 'sin_folio', registro)
            continue
        if resultado['tipo'] == 'metadatos':
            estatus = ESTATUS.get((fila.get('estatus') or '').lower())
            if not estatus:
                ilegible(numero, 'estatus', registro, valor=fila.get('estatus') or '')
                continue
            fila['estatus'] = estatus
            efecto = (fila.get('efecto') or '').upper()
            if efecto not in EFECTOS:
                if efecto:
                    extra['EfectoComprobante'] = fila.get('efecto')
                efecto = False
            fila['efecto'] = efecto
        fila['extra'] = extra
        resultado['filas'].append(fila)
    if encabezado is None:
        ilegible(0, 'vacio', '')
    return resultado


def leer_paquete_de_metadatos(archivos):
    """Lee los TXT de un paquete (o de todos los paquetes de una solicitud) y
    une a cada fila sus datos «a cuenta de terceros», que desde 2022 llegan en
    su propio archivo y se ligan por folio fiscal.

    :param archivos: lista de (nombre, bytes).
    :return: (filas, ilegibles), como los de leer_metadatos_txt.
    """
    filas, terceros, ilegibles = [], {}, []
    for nombre, contenido in archivos:
        lectura = leer_metadatos_txt(contenido, nombre)
        ilegibles += lectura['ilegibles']
        if lectura['tipo'] == 'terceros':
            for fila in lectura['filas']:
                terceros[fila['uuid']] = {campo: fila.get(campo) or False for campo in CAMPOS_DE_TERCEROS}
        elif lectura['tipo'] == 'metadatos':
            filas += lectura['filas']
    for fila in filas:
        for campo, valor in terceros.get(fila['uuid'], {}).items():
            if valor and not fila.get(campo):
                fila[campo] = valor
    return filas, ilegibles


class CfdiSatMetadata(models.Model):
    _name = 'dsi.cfdi.sat.metadata'
    _description = "Metadatos del SAT de un CFDI"
    _rec_name = 'uuid'
    _order = 'fecha_emision desc, id desc'

    company_id = fields.Many2one(
        'res.company', string="Empresa", required=True, index=True, readonly=True)
    uuid = fields.Char(string="UUID", required=True, index=True, readonly=True)
    direction = fields.Selection([
        ('issued', "Emitido"),
        ('received', "Recibido"),
    ], string="Dirección", required=True, readonly=True)
    rfc_emisor = fields.Char(string="RFC emisor", readonly=True)
    nombre_emisor = fields.Char(string="Nombre emisor", readonly=True)
    rfc_receptor = fields.Char(string="RFC receptor", readonly=True)
    nombre_receptor = fields.Char(string="Nombre receptor", readonly=True)
    rfc_pac = fields.Char(string="RFC del PAC", readonly=True)
    rfc_a_cuenta_terceros = fields.Char(string="RFC a cuenta de terceros", readonly=True)
    nombre_a_cuenta_terceros = fields.Char(string="Nombre a cuenta de terceros", readonly=True)
    fecha_emision = fields.Datetime(string="Emitido el", readonly=True)
    fecha_certificacion = fields.Datetime(string="Certificado por el SAT el", readonly=True)
    monto = fields.Float(string="Monto", readonly=True)
    efecto = fields.Selection([
        ('I', "Ingreso"),
        ('E', "Egreso"),
        ('T', "Traslado"),
        ('N', "Nómina"),
        ('P', "Pago"),
    ], string="Efecto", readonly=True)
    estatus = fields.Selection([
        ('1', "Vigente"),
        ('0', "Cancelado"),
    ], string="Estatus", required=True, index=True, readonly=True,
        help="Lo que dijo el SAT en los metadatos más recientes. Una cancelación es definitiva: si "
             "un archivo posterior lo trae vigente, se queda cancelado.")
    fecha_cancelacion = fields.Datetime(string="Cancelado el", readonly=True)
    extra_json = fields.Json(string="Columnas adicionales", readonly=True)
    # En texto para el formulario: Odoo 18 no tiene con qué mostrar un Json.
    extra_text = fields.Text(string="Columnas adicionales del TXT", compute='_compute_extra_text')

    request_id = fields.Many2one(
        'sat.download.request', string="Última solicitud", ondelete='set null', readonly=True,
        groups='dsi_cfdi_sat_sync.group_sat_credential_manager')
    first_seen = fields.Datetime(string="Visto por primera vez", readonly=True)
    last_seen = fields.Datetime(string="Visto por última vez", readonly=True)

    # Ligas por folio fiscal, dentro de la misma empresa: el CFDI de la Bóveda
    # (al que se le pasa el estatus) y la factura de Odoo que lo lleva (la que
    # no tiene su CFDI en la Bóveda toma de aquí su Estado bóveda).
    cfdi_document_id = fields.Many2one(
        'cfdi.document', string="CFDI en la Bóveda", ondelete='set null', index='btree_not_null',
        readonly=True)
    move_id = fields.Many2one(
        'account.move', string="Factura", ondelete='set null', index='btree_not_null', readonly=True)

    _uuid_company_uniq = models.Constraint(
        'unique(company_id, uuid)',
        "Ya hay metadatos del SAT de este folio fiscal en la empresa.",
    )

    @api.depends('extra_json')
    def _compute_extra_text(self):
        for metadato in self:
            metadato.extra_text = "\n".join(
                f"{columna}: {valor}" for columna, valor in sorted((metadato.extra_json or {}).items())) or False

    # -------------------------------------------------------------------
    # Guardar lo leído
    # -------------------------------------------------------------------

    @api.model
    def _guardar_filas(self, solicitud, filas):
        """Guarda las filas leídas de los TXT de `solicitud`, una por empresa
        y folio fiscal: crea las nuevas y actualiza las que ya estaban.

        La cancelación es definitiva: si ya estaba cancelado y una fila lo
        trae vigente (un archivo más viejo, o el SAT todavía no lo refleja en
        todos sus servicios), se queda cancelado con su fecha. Dentro de un
        mismo archivo, si el folio viene dos veces, gana el cancelado.

        Devuelve (todos, cambiados): los registros de estas filas y, de ellos,
        los nuevos o los que cambiaron de estatus, de fecha de cancelación o
        de algún otro dato. Solo se escribe lo que cambió.

        Por tandas de LOTE_DE_METADATOS: cada una se escribe y sale del caché
        antes de la siguiente, y los registros se juntan por id. Un refresco
        trae decenas de miles de filas y el SAT permite hasta un millón por
        solicitud: juntarlos con «|», que copia el conjunto cada vez, tardaba
        minutos, y crearlos todos de una vez llenaba la memoria del proceso.
        """
        Metadato = self.sudo()
        empresa = solicitud.company_id
        ahora = fields.Datetime.now()
        por_uuid = {}
        for fila in filas:
            anterior = por_uuid.get(fila['uuid'])
            if anterior and anterior['estatus'] == '0' and fila['estatus'] == '1':
                continue
            por_uuid[fila['uuid']] = fila

        todos, cambiados = [], []
        for uuids in split_every(LOTE_DE_METADATOS, list(por_uuid)):
            existentes = {m.uuid: m for m in Metadato.search([
                ('company_id', '=', empresa.id), ('uuid', 'in', list(uuids))])}
            nuevos = []
            for uuid in uuids:
                valores = self._valores_de_la_fila(por_uuid[uuid])
                existente = existentes.get(uuid)
                if not existente:
                    nuevos.append(dict(
                        valores, company_id=empresa.id, direction=solicitud.direction,
                        first_seen=ahora, last_seen=ahora, request_id=solicitud.id))
                    continue
                if existente.estatus == '0':
                    entrante = valores.pop('estatus')
                    if entrante == '1' or existente.fecha_cancelacion:
                        valores.pop('fecha_cancelacion')
                cambios = {campo: valor for campo, valor in valores.items() if existente[campo] != valor}
                if cambios:
                    existente.write(cambios)
                    cambiados.append(existente.id)
            vistos = [metadato.id for metadato in existentes.values()]
            if vistos:
                Metadato.browse(vistos).write({'last_seen': ahora, 'request_id': solicitud.id})
            creados = Metadato.create(nuevos).ids if nuevos else []
            todos += vistos + creados
            cambiados += creados
            self.env.invalidate_all()  # escribe lo pendiente y suelta el caché de la tanda
        return Metadato.browse(todos), Metadato.browse(cambiados)

    @api.model
    def _valores_de_la_fila(self, fila):
        valores = {campo: fila.get(campo) or False for campo in COLUMNAS.values()}
        valores['monto'] = fila.get('monto') or 0.0
        valores['extra_json'] = fila.get('extra') or False
        # Vigente no tiene fecha de cancelación, aunque la columna traiga algo.
        if valores['estatus'] == '1':
            valores['fecha_cancelacion'] = False
        return valores

    # -------------------------------------------------------------------
    # Ligas y aplicación a la Bóveda y a las facturas
    # -------------------------------------------------------------------

    def _resolver_ligas(self):
        """Liga cada metadato con el CFDI de la Bóveda y la factura de Odoo
        que llevan su folio fiscal en la misma empresa. Solo escribe lo que
        cambió.

        El CFDI se busca archivado o no. La factura, en mayúsculas y
        minúsculas (Odoo guarda el folio tal como viene en el XML) y, si hay
        varias, gana la que no está cancelada, como al conciliar.

        Devuelve (cambiaron, facturas): los metadatos que cambiaron de liga y
        las facturas de esas ligas, las de antes y las de ahora: a las dos les
        puede cambiar el Estado bóveda."""
        Documento = self.env['cfdi.document'].sudo().with_context(active_test=False)
        Factura = self.env['account.move'].sudo()
        cambiaron, facturas_de_las_ligas = [], set()
        for empresa, metadatos in self.sudo().grouped('company_id').items():
            for ids in split_every(LOTE_DE_METADATOS, metadatos.ids):
                lote = metadatos.browse(ids)
                uuids = set(lote.mapped('uuid'))
                variantes = list(uuids | {uuid.lower() for uuid in uuids})
                cfdis = {d.uuid.upper(): d for d in Documento.search([
                    ('company_id', '=', empresa.id), ('uuid', 'in', variantes),
                ])}
                facturas = {}
                for factura in Factura.search([
                    ('company_id', '=', empresa.id), ('l10n_mx_edi_cfdi_uuid', 'in', variantes),
                ]).sorted(lambda f: f.state != 'cancel'):
                    facturas[factura.l10n_mx_edi_cfdi_uuid.upper()] = factura
                for metadato in lote:
                    cfdi = cfdis.get(metadato.uuid, Documento)
                    factura = facturas.get(metadato.uuid, Factura)
                    cambios = {}
                    if metadato.cfdi_document_id != cfdi:
                        cambios['cfdi_document_id'] = cfdi.id
                    if metadato.move_id != factura:
                        cambios['move_id'] = factura.id
                        facturas_de_las_ligas.update((metadato.move_id | factura).ids)
                    if cambios:
                        metadato.write(cambios)
                        cambiaron.append(metadato.id)
                self._soltar_la_tanda()
        return self.sudo().browse(cambiaron), Factura.browse(facturas_de_las_ligas)

    def _soltar_la_tanda(self):
        """Entre tandas de muchos metadatos (más de LOTE_DE_METADATOS), escribe
        lo pendiente y saca del caché lo leído: con cientos de miles, el caché
        llenaba la memoria del proceso. Con pocos (un CFDI que se guarda, por
        ejemplo) no hace nada: quien llama conserva su caché."""
        if len(self) > LOTE_DE_METADATOS:
            self.env.invalidate_all()

    def _valores_para_el_cfdi(self, cfdi, ahora):
        """Lo que este metadato le cambia al CFDI de la Bóveda, o {}.

        - Cancelado: el CFDI queda cancelado, con su fecha y «metadatos» como
          origen; cuenta como verificación (`ahora`). Si ya estaba cancelado
          (lo dijo la consulta) solo se le agrega la fecha, que la consulta no
          da.
        - Vigente: solo llena lo que la Bóveda no sabe (sin verificar o «No
          encontrado»). Nunca deshace una cancelación y no cuenta como
          verificación: la consulta pública lo vuelve a revisar en su turno.
          Un «No encontrado» lo llena solo si es anterior a que llegaran
          estos metadatos (_es_posterior_a_la_consulta): si la consulta lo
          dijo después, es lo más reciente y se queda.
        """
        self.ensure_one()
        valores = {}
        if self.estatus == '0':
            if cfdi.sat_status != 'canceled':
                valores.update({'sat_status': 'canceled', 'sat_status_source': 'metadatos', 'sat_last_check': ahora})
            if self.fecha_cancelacion and cfdi.sat_cancel_date != self.fecha_cancelacion:
                valores['sat_cancel_date'] = self.fecha_cancelacion
        elif cfdi.sat_status == 'not_verified' or (
                cfdi.sat_status == 'not_found' and self._es_posterior_a_la_consulta(cfdi)):
            valores.update({'sat_status': 'valid', 'sat_status_source': 'metadatos'})
        return valores

    def _es_posterior_a_la_consulta(self, cfdi):
        """Si este metadato llegó por primera vez después de la última
        consulta pública del CFDI. Se compara con la primera vez y no con la
        última: volver a leer el mismo vigente cada semana no es información
        nueva. Sin esto, un CFDI que la consulta no encuentra (y los metadatos
        sí) iba y venía cada día entre «No encontrado» y «Vigente», con un
        mensaje en el CFDI y en su factura cada vez."""
        self.ensure_one()
        return not cfdi.sat_last_check or (self.first_seen or self.create_date) > cfdi.sat_last_check

    def _aplicar(self, source='metadata', cambiados=None):
        """Pasa estos metadatos a la Bóveda y a las facturas:

        1. Los liga por folio fiscal (_resolver_ligas).
        2. Le pasa el estatus a cada CFDI de la Bóveda (_valores_para_el_cfdi),
           con sudo: solo la verificación y los metadatos escriben el estatus.
           Al escribirlo, el documento «Pago recibido» de un REP lo sigue (lo
           hace el write del CFDI). Llenar un vigente que nadie había
           verificado no deja mensaje en el historial de cada CFDI (la primera
           descarga serían miles); una cancelación sí lo deja.
        3. Revisa una sola vez las discrepancias de estatus de las facturas
           cuyo Estado bóveda pudo cambiar: las de los CFDI que cambiaron y
           las que lo toman de los metadatos (no tienen su CFDI ligado en la
           Bóveda) si el metadato es nuevo, cambió (`cambiados`, por omisión
           todos) o cambió de liga; de una liga que cambió, también la factura
           de antes. A una compra nunca se le ejecuta «Actualizar SAT» (ver
           _sincronizar_estado_sat). Una venta con solicitud de cancelación
           queda «Cancelación por actualizar» y se pide la tarea que le
           ejecuta «Actualizar SAT» (dsi.cfdi.inconsistency.
           _sincronizar_cancelaciones_por_actualizar). Que una factura pase
           de vacío a «Vigente» no deja mensaje en su historial, como en el
           CFDI (account.move._dsi_sin_historial_al_llenar_vigentes).

        Idempotente: lo que ya está como dicen los metadatos no se escribe.
        Los CFDI se juntan por id, no con «|»: la primera descarga de un
        cliente con su histórico en la Bóveda pasa decenas de miles.
        Devuelve los CFDI de la Bóveda que cambiaron.
        """
        metadatos = self.sudo().exists()
        Documento = self.env['cfdi.document'].sudo()
        if not metadatos:
            return Documento
        cambiados = metadatos if cambiados is None else cambiados.sudo().exists()
        religados, facturas_de_las_ligas = metadatos._resolver_ligas()
        cambiados |= religados
        ahora = fields.Datetime.now()
        grupos = defaultdict(list)
        for ids in split_every(LOTE_DE_METADATOS, metadatos.ids):
            for metadato in metadatos.browse(ids).filtered('cfdi_document_id'):
                valores = metadato._valores_para_el_cfdi(metadato.cfdi_document_id, ahora)
                if valores:
                    grupos[tuple(sorted(valores.items()))].append(metadato.cfdi_document_id.id)
            metadatos._soltar_la_tanda()
        cambiaron = []
        for clave, todos in grupos.items():
            valores = dict(clave)
            sin_historial = valores.get('sat_status') == 'valid'
            for ids in split_every(LOTE_DE_METADATOS, todos):
                Documento.browse(ids).with_context(dsi_sin_sync=True, mail_notrack=sin_historial).write(valores)
                metadatos._soltar_la_tanda()
            cambiaron += todos
        cfdis_cambiados = Documento.browse(cambiaron)
        facturas = cfdis_cambiados.move_id | (cambiados.move_id | facturas_de_las_ligas).filtered(
            lambda factura: not factura._dsi_cfdi_de_la_boveda())
        if facturas:
            facturas._dsi_sin_historial_al_llenar_vigentes()
            self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
                '_sincronizar_estado_sat', facturas, source)
        # Un REP cancelado ya no cuenta como pago de una factura fuera de Odoo.
        cfdis_cambiados._revisar_reps_sin_factura(source)
        return cfdis_cambiados

    @api.model
    def _aplicar_a_cfdi_nuevos(self, cfdis):
        """Al guardar CFDI en la Bóveda, los metadatos que ya estaban de esos
        folios: el XML de un CFDI puede llegar después que sus metadatos (en
        otra solicitud, o importado a mano). Lo llama el create de cada CFDI
        y la descarga, una sola vez por paquete (METADATOS_AL_FINAL).

        En su propio savepoint, como la conciliación de la descarga: escribe
        metadatos y CFDI que otras transacciones también escriben (la revisión
        diaria, otra lectura de metadatos). Sin él, un choque abortaba la
        transacción de quien guardaba el CFDI y se perdía la descarga entera.
        Si choca, se deshace solo esta parte, queda un aviso y la revisión de
        discrepancias, que aplica lo pendiente (_pendientes_de_aplicar), se
        vuelve a programar. Si falla por otra cosa, queda en el log y también
        la aplica la revisión diaria: el CFDI se guarda igual."""
        uuids = [uuid.upper() for uuid in cfdis.mapped('uuid') if uuid]
        if not uuids:
            return
        metadatos = self.sudo().search([('company_id', 'in', cfdis.company_id.ids), ('uuid', 'in', uuids)])
        if not metadatos:
            return
        # Lo pendiente de quien guarda el CFDI se escribe fuera del savepoint:
        # si eso choca, el error es suyo.
        self.env.cr.flush()
        try:
            with self.env.cr.savepoint(), tools.mute_logger('odoo.sql_db'):
                metadatos._aplicar(cambiados=self.browse())
        except ERRORES_DE_CONCURRENCIA as error:
            _logger.warning(
                "Pasar los metadatos del SAT a %s CFDI recién guardados chocó con otra transacción (%s) y se "
                "deshizo; la revisión de discrepancias lo vuelve a intentar.", len(cfdis), type(error).__name__)
            self.env['dsi.cfdi.inconsistency']._programar_revision(MARGEN_TRAS_UN_CHOQUE)
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "Falló pasar los metadatos del SAT a %s CFDI recién guardados; quedan guardados y la "
                "revisión diaria de discrepancias lo vuelve a intentar.", len(cfdis))

    def _de_factura(self):
        """De estos metadatos, los que pueden ser de una factura: los de
        ingreso o egreso, como el CFDI del que sale el Estado bóveda."""
        return self.filtered(lambda m: m.efecto in TIPOS_DE_CFDI_DE_FACTURA)

    # -------------------------------------------------------------------
    # Revisión diaria
    # -------------------------------------------------------------------

    @api.model
    def _pendientes_de_aplicar(self):
        """Los metadatos que la Bóveda o las facturas todavía no reflejan:

        - sin liga con un CFDI de la Bóveda que sí lleva su folio (el CFDI se
          guardó y el gancho que se los pasa chocó, por ejemplo), o ligados a
          uno de otra empresa;
        - sin liga con la factura que lleva su folio (entró después por fuera
          de la descarga: el import de Odoo), ligados a una que ya no lo lleva,
          o a una cancelada cuando hay otra que no lo está. Pasa aunque su
          CFDI esté en la Bóveda: si no está ligado a la factura, ella toma su
          Estado bóveda de los metadatos;
        - los de un CFDI cuyo estatus no es el que dicen (una aplicación que se
          deshizo por un choque con otra transacción), con las mismas reglas
          de _valores_para_el_cfdi.

        En SQL: son todos los metadatos guardados (decenas o cientos de miles
        con el tiempo) y la revisión diaria solo necesita los que cambian. El
        folio se compara en mayúsculas y en minúsculas, como al ligarlos, para
        usar los índices."""
        for modelo, campos in (
            ('dsi.cfdi.sat.metadata', ['company_id', 'uuid', 'estatus', 'first_seen', 'create_date',
                                       'cfdi_document_id', 'move_id']),
            ('cfdi.document', ['company_id', 'uuid', 'sat_status', 'sat_last_check']),
            ('account.move', ['company_id', 'state', 'l10n_mx_edi_cfdi_uuid']),
        ):
            self.env[modelo].flush_model(campos)
        self.env.cr.execute(SQL("""
            SELECT m.id
              FROM dsi_cfdi_sat_metadata m
         LEFT JOIN cfdi_document cfdi ON cfdi.id = m.cfdi_document_id
         LEFT JOIN account_move ligada ON ligada.id = m.move_id
             WHERE (m.cfdi_document_id IS NULL AND EXISTS (
                        SELECT FROM cfdi_document d
                         WHERE d.company_id = m.company_id AND d.uuid IN (m.uuid, lower(m.uuid))))
                OR cfdi.company_id != m.company_id
                OR (m.move_id IS NULL AND EXISTS (
                        SELECT FROM account_move f
                         WHERE f.company_id = m.company_id
                           AND f.l10n_mx_edi_cfdi_uuid IN (m.uuid, lower(m.uuid))))
                OR (m.move_id IS NOT NULL AND upper(ligada.l10n_mx_edi_cfdi_uuid) IS DISTINCT FROM m.uuid)
                OR ligada.company_id != m.company_id
                OR (ligada.state = 'cancel' AND EXISTS (
                        SELECT FROM account_move f
                         WHERE f.company_id = m.company_id AND f.state != 'cancel'
                           AND f.l10n_mx_edi_cfdi_uuid IN (m.uuid, lower(m.uuid))))
                OR (m.estatus = '0' AND cfdi.sat_status != 'canceled')
                OR (m.estatus = '1' AND (
                        cfdi.sat_status = 'not_verified'
                        OR (cfdi.sat_status = 'not_found' AND (
                            cfdi.sat_last_check IS NULL
                            OR COALESCE(m.first_seen, m.create_date) > cfdi.sat_last_check))))
        """))
        return self.sudo().browse([fila[0] for fila in self.env.cr.fetchall()])

    @api.model
    def _registrar_ilegibles(self, solicitud, ilegibles):
        """Las líneas que no se pudieron leer: cuántas, en el log y las
        primeras en la solicitud, para revisarlas contra el TXT adjunto."""
        if not ilegibles:
            return ''
        detalle = "\n".join(self._describir_ilegible(ilegible) for ilegible in ilegibles[:LINEAS_ILEGIBLES_A_MOSTRAR])
        _logger.warning(
            "Solicitud %s: %s líneas de metadatos del SAT no se pudieron leer y no se guardaron. "
            "Primera: %s", solicitud.id, len(ilegibles), detalle.split("\n", 1)[0])
        return detalle

    @api.model
    def _describir_ilegible(self, ilegible):
        """Una línea ilegible en texto, en el idioma de quien la ve."""
        motivos = {
            'encabezado': self.env._("el encabezado no es de metadatos del SAT"),
            'campos': self.env._(
                "trae %(campos)s campos y el encabezado %(columnas)s",
                campos=ilegible.get('campos'), columnas=ilegible.get('columnas')),
            'sin_folio': self.env._("no trae folio fiscal"),
            'estatus': self.env._("estatus desconocido «%s»", ilegible.get('valor')),
            'vacio': self.env._("el archivo está vacío"),
        }
        return self.env._(
            "%(archivo)s, línea %(linea)s: %(motivo)s · %(texto)s",
            archivo=ilegible['archivo'], linea=ilegible['linea'],
            motivo=motivos.get(ilegible['motivo'], ilegible['motivo']), texto=(ilegible['texto'] or '')[:200])

    def action_open_cfdi(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cfdi.document',
            'view_mode': 'form',
            'res_id': self.cfdi_document_id.id,
        }

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

