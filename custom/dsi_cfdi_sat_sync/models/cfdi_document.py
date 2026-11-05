from odoo import api, fields, models
from odoo.tools import sql

# Lo que, además del estatus y la última verificación, solo escriben la
# verificación SAT o los metadatos (con sudo) o el gestor contable: de dónde
# salió el estatus y cuándo se canceló. Ver _solo_la_verificacion.
CAMPOS_DE_LOS_METADATOS = {'sat_cancel_date', 'sat_status_source'}

# Contexto con el que quien guarda muchos CFDI le pasa después los metadatos
# a todos juntos (_aplicar_a_cfdi_nuevos) en lugar de a cada uno al crearlo.
METADATOS_AL_FINAL = 'dsi_metadatos_al_final'


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    source = fields.Selection(
        selection_add=[('sat_download', "Descarga Masiva SAT")],
        ondelete={'sat_download': 'set default'},
    )
    sat_cancel_date = fields.Datetime(
        string="Cancelado en el SAT el", readonly=True, copy=False,
        help="Fecha de cancelación según los metadatos de la descarga masiva del SAT. La "
             "consulta pública del SAT no la da: si el CFDI se canceló y no hay metadatos, "
             "queda vacía.")
    sat_status_source = fields.Selection([
        ('consulta', "Consulta pública del SAT"),
        ('metadatos', "Metadatos de la descarga masiva"),
        ('manual', "Corrección manual"),
    ], string="Origen del estatus SAT", readonly=True, copy=False,
        help="De dónde salió el estatus SAT actual: la consulta pública (el botón «Verificar "
             "estatus SAT» o su tarea diaria), los metadatos de la descarga masiva, o el gestor "
             "contable que lo corrigió a mano. Una cancelación del SAT es definitiva: si los "
             "metadatos lo dan cancelado, la revisión diaria vuelve a aplicar la cancelación sobre "
             "una corrección a mano.")
    dsi_sat_metadata_ids = fields.One2many(
        'dsi.cfdi.sat.metadata', 'cfdi_document_id', string="Metadatos del SAT")

    @api.model_create_multi
    def create(self, vals_list):
        if any(CAMPOS_DE_LOS_METADATOS & set(vals) for vals in vals_list):
            self._solo_la_verificacion()
        docs = super().create(vals_list)
        # El XML puede llegar después que sus metadatos: los que ya estaban de
        # estos folios se les pasan al guardarlos. La descarga, que guarda los
        # XML de uno en uno, se los pasa una sola vez al final del paquete
        # (sat.download.request._procesar_paquete_zip).
        if not self.env.context.get(METADATOS_AL_FINAL):
            self.env['dsi.cfdi.sat.metadata']._aplicar_a_cfdi_nuevos(docs)
        return docs

    def write(self, vals):
        """El origen y la fecha de cancelación los escribe quien escribe el
        estatus: la verificación (_valores_de_la_consulta) o los metadatos.
        Un estatus que llega sin origen es una corrección del gestor contable.
        Un CFDI que deja de estar cancelado ya no tiene fecha de cancelación.

        Un CFDI emitido que pasa a cancelado (la consulta pública diaria, los
        metadatos o el gestor contable) puede ser el de una venta con
        solicitud de cancelación: se pide que corra la tarea de «Actualizar
        SAT», que lo revisa (account.move._cron_actualizar_cancelaciones_emitidas)."""
        if CAMPOS_DE_LOS_METADATOS & set(vals):
            self._solo_la_verificacion()
        if 'sat_status' in vals:
            vals = dict(vals)
            vals.setdefault('sat_status_source', 'manual')
            if vals['sat_status'] != 'canceled':
                vals.setdefault('sat_cancel_date', False)
        resultado = super().write(vals)
        if vals.get('sat_status') == 'canceled' and any(doc.direction == 'issued' for doc in self.sudo()):
            self.env['account.move']._dsi_programar_actualizar_sat()
        return resultado

    def _valores_de_la_consulta(self, status):
        valores = super()._valores_de_la_consulta(status)
        if 'sat_status' in valores:
            valores['sat_status_source'] = 'consulta'
        return valores

    def _auto_init(self):
        """Los estatus ya guardados al instalar o actualizar salieron de la
        consulta pública: era lo único que los escribía (o el gestor contable,
        que no se distingue)."""
        nueva = not sql.column_exists(self.env.cr, self._table, 'sat_status_source')
        resultado = super()._auto_init()
        if nueva:
            self.env.cr.execute("""
                UPDATE cfdi_document SET sat_status_source = 'consulta'
                 WHERE sat_status IS NOT NULL AND sat_status != 'not_verified'
            """)
        return resultado
