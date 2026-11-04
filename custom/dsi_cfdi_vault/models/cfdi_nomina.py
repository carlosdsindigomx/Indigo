"""Los CFDI de nómina (N) con la póliza del periodo.

Una N no es una cuenta por pagar: la contabiliza la póliza de nómina (un
asiento contable), una para todos los recibos de un periodo. El vínculo es el
mismo move_id de siempre, ahora solo hacia un asiento de la misma empresa
(_check_poliza_de_nomina) y no cancelado (_check_poliza_no_cancelada), y al
ligarla queda una copia de su XML en los adjuntos de la póliza, venga la liga
del asistente «Relacionar con póliza», del formulario o de la conciliación
por UUID.

Del complemento nomina12 se guardan el tipo, la fecha de pago y el periodo: la
Fecha del comprobante es la de emisión, que llega días o semanas después del
pago, y con ella sola el asistente no encuentra la póliza ni se distinguen dos
quincenas timbradas el mismo día.
"""
import logging

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import split_every

from .cfdi_conceptos import _bytes_de_adjunto
from .cfdi_document import _datos_de_nomina
from .cfdi_payment_line import LOTE_DEL_LLENADO

_logger = logging.getLogger(__name__)


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    # Del complemento de nómina (_datos_de_nomina). Los llena el parseo al
    # importar o descargar y, en las ya guardadas, la actualización.
    dsi_nomina_tipo = fields.Selection([
        ('O', "Ordinaria"),
        ('E', "Extraordinaria"),
    ], string="Tipo de nómina", readonly=True)
    dsi_nomina_fecha_pago = fields.Date(string="Fecha de pago", index='btree_not_null', readonly=True)
    dsi_nomina_fecha_inicial = fields.Date(string="Periodo desde", readonly=True)
    dsi_nomina_fecha_final = fields.Date(string="Periodo hasta", readonly=True)
    # Como invoice_attachment_id de las líneas del REP: la copia que la Bóveda
    # dejó en la póliza. Si alguien la borra se vacía y se repone al volver a
    # relacionar.
    dsi_poliza_attachment_id = fields.Many2one(
        'ir.attachment', string="Copia del XML en la póliza", ondelete='set null', copy=False, readonly=True)

    @api.constrains('move_id', 'move_type', 'company_id')
    def _check_poliza_de_nomina(self):
        """Una N solo se liga a un asiento contable de su empresa. El dominio
        del campo solo filtra en el navegador; esto vale también por RPC y
        para el asistente, que copia el XML con sudo. Antes move_id no tenía
        dominio en las N y una se podía ligar a una factura de proveedor."""
        for doc in self.sudo().filtered(lambda d: d.move_type == 'N' and d.move_id):
            if doc.move_id.move_type != 'entry':
                raise ValidationError(_(
                    "El CFDI de nómina %s solo se relaciona con una póliza (asiento contable), no con "
                    "una factura.", doc.uuid))
            if doc.move_id.company_id != doc.company_id:
                raise ValidationError(_(
                    "La póliza %(poliza)s es de otra empresa que el CFDI de nómina %(uuid)s.",
                    poliza=doc.move_id.display_name, uuid=doc.uuid))

    def _check_poliza_no_cancelada(self):
        """Una N no se liga a una póliza cancelada: el dominio del formulario
        solo filtra en el navegador, y por RPC o una importación CSV se ligaba
        y le copiaba el XML. No es una @api.constrains, que correría también
        al escribir la empresa o el tipo: la llaman create y write solo en las
        que estrenan o cambian de póliza. Una liga cuya póliza se canceló
        después sigue valiendo (sale en «CFDI de nómina sin póliza») y el CFDI
        se sigue pudiendo guardar."""
        for doc in self.sudo().filtered(lambda d: d.move_type == 'N' and d.move_id.state == 'cancel'):
            raise ValidationError(self.env._(
                "La póliza %(poliza)s está cancelada: relaciona el CFDI de nómina %(uuid)s con una vigente.",
                poliza=doc.move_id.display_name, uuid=doc.uuid))

    # -------------------------------------------------------------------
    # La copia del XML en la póliza
    # -------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        docs = super().create(vals_list)
        con_poliza = docs.filtered('move_id')
        con_poliza._check_poliza_no_cancelada()
        con_poliza._copiar_xml_de_nomina_a_su_poliza()
        return docs

    def write(self, vals):
        """Al ligar o desligar una N, su copia del XML: se crea en la póliza
        nueva y se suelta la de la anterior. También con `dsi_sin_sync`: es
        copiar un archivo, no una revisión. La restricción corre dentro del
        write, y _check_poliza_no_cancelada antes de copiar, así que una liga
        que no vale nunca llega a copiarse."""
        if 'move_id' not in vals:
            return super().write(vals)
        antes = {doc.id: doc.move_id for doc in self}
        resultado = super().write(vals)
        self.filtered(lambda d: d.move_id != antes[d.id])._check_poliza_no_cancelada()
        self._copiar_xml_de_nomina_a_su_poliza()
        return resultado

    def _copiar_xml_de_nomina_a_su_poliza(self):
        """Una copia del XML de cada N en los adjuntos de su póliza, como la
        del REP en sus facturas (_copiar_rep_a_sus_facturas):

        - sin mensaje: una nómina quincenal son decenas o cientos de recibos,
          y nunca con message_post en la póliza, que importaría los XML como
          facturas;
        - con `no_document`: con Documents, cada XML sería un documento en la
          carpeta contable;
        - si la póliza ya tiene un adjunto con el mismo contenido (una liga
          anterior o el XML que subió el sistema de nómina), se usa ese;
        - solo en un asiento de la misma empresa: una N que antes de 0.3.0 se
          ligó a un asiento de otra empresa se queda así, sin copia, y su XML
          no pasa a la otra empresa.

        La copia de una póliza anterior no se borra (con bitácora de
        auditoría Odoo no deja borrar el XML de un asiento publicado): solo
        se suelta el campo. Con sudo, como todo lo que escribe la Bóveda; el
        usuario que liga queda como autor de la copia."""
        nominas = self.sudo().with_context(active_test=False).filtered(lambda d: d.move_type == 'N')
        sueltas = nominas.filtered(lambda d: d.dsi_poliza_attachment_id and (
            d.dsi_poliza_attachment_id.res_model != 'account.move'
            or d.dsi_poliza_attachment_id.res_id != d.move_id.id))
        if sueltas:
            sueltas.write({'dsi_poliza_attachment_id': False})
        pendientes = nominas.filtered(lambda d: d.move_id.move_type == 'entry'
                                      and d.move_id.company_id == d.company_id and not d.dsi_poliza_attachment_id)
        if not pendientes:
            return
        Adjunto = self.env['ir.attachment'].sudo().with_context(no_document=True)
        for poliza, docs in pendientes.grouped('move_id').items():
            contenido, suma = {}, {}
            for doc in docs:
                datos = _bytes_de_adjunto(doc.xml_attachment_id)
                if datos:
                    contenido[doc] = datos
                    suma[doc] = Adjunto._compute_checksum(datos)
            if not suma:
                continue
            por_suma = {}
            for adjunto in Adjunto.search([
                    ('res_model', '=', 'account.move'), ('res_id', '=', poliza.id),
                    ('checksum', 'in', list(set(suma.values())))], order='id'):
                por_suma.setdefault(adjunto.checksum, adjunto)
            # Una por contenido: dos N con el mismo XML (no debería pasar, el
            # UUID es único por empresa) comparten la copia.
            faltan = {}
            for doc, checksum in suma.items():
                if checksum not in por_suma:
                    faltan.setdefault(checksum, doc)
            copias = Adjunto.create([{
                'name': doc._nombre_de_la_copia_de_nomina(),
                'raw': contenido[doc],
                'mimetype': 'application/xml',
                'res_model': 'account.move',
                'res_id': poliza.id,
                'company_id': poliza.company_id.id,
            } for doc in faltan.values()])
            por_suma.update(zip(faltan, copias))
            for doc, checksum in suma.items():
                doc.write({'dsi_poliza_attachment_id': por_suma[checksum].id})

    def _nombre_de_la_copia_de_nomina(self):
        """Con el RFC del empleado delante, para reconocer el recibo entre los
        adjuntos de la póliza sin abrirlo."""
        self.ensure_one()
        if self.partner_rfc_receiver:
            return f"Nomina-{self.partner_rfc_receiver}-{self.uuid}.xml"
        return f"Nomina-{self.uuid}.xml"

    # -------------------------------------------------------------------
    # «Relacionar con póliza»
    # -------------------------------------------------------------------

    def action_relacionar_con_poliza(self):
        """Acciones › Relacionar con póliza en la lista, y el botón del
        formulario: abre el asistente con los CFDI de nómina de la selección.
        Los demás se dejan fuera y el asistente dice por qué; si no queda
        ninguno, o vienen de varias empresas (la póliza es de una), no abre.

        Pide licencia, como importar o convertir. Ligar a mano en el
        formulario y Desvincular no la piden, como hasta ahora."""
        nominas, descartados = self._clasificar_para_poliza()
        if not nominas:
            raise UserError(self.env._(
                "Ninguno de los CFDI seleccionados se puede relacionar con una póliza: %s.",
                "; ".join(self._detalle_descartados(descartados))))
        if len(nominas.company_id) > 1:
            raise UserError(self.env._(
                "Selecciona CFDI de nómina de una sola empresa: la póliza es de una empresa. Vienen de: %s.",
                ", ".join(nominas.company_id.mapped('name'))))
        self.env['dsi.license.client.state']._check_feature(
            'vault', self.env._("relacionar CFDI de nómina con pólizas"), nominas.company_id)
        return self.env['dsi.cfdi.nomina.poliza.wizard']._abrir_para(nominas, descartados)

    def _clasificar_para_poliza(self):
        """(N que se pueden relacionar, {motivo: CFDI que no}), con el formato
        de _clasificar_para_factura para reutilizar _detalle_descartados.

        Las que ya tienen póliza no se descartan: el asistente las deja donde
        están o, con «Reemplazar», las mueve. Las «Sin verificar» y «No
        encontrado» cuentan como vigentes, como en el resto de la Bóveda."""
        nominas = self.browse()
        descartados = {}
        for doc in self:
            if doc.move_type != 'N':
                motivo = _("no son CFDI de nómina")
            elif not doc.active:
                motivo = _("están archivados")
            elif doc.sat_status == 'canceled':
                motivo = _("están cancelados en el SAT (relaciona el que lo sustituye)")
            else:
                nominas |= doc
                continue
            descartados[motivo] = descartados.get(motivo, self.browse()) | doc
        return nominas, descartados

    # -------------------------------------------------------------------
    # Actualización a 0.3.0 (data/cfdi_nomina.xml)
    # -------------------------------------------------------------------

    @api.model
    def _backfill_datos_de_nomina(self):
        """La fecha de pago, el periodo y el tipo de las N ya guardadas. Solo
        las que no tienen fecha de pago y solo escribe lo que cambia: correrla
        otra vez no hace nada. Una N sin complemento se vuelve a leer en cada
        actualización, que es barato.

        En tandas con flush y sin commit, como _backfill_lineas_de_pago, y
        soltando la caché al final de cada una: una base con años de nómina
        son decenas de miles de XML, y sin eso el contenido de todos se
        quedaba en memoria hasta el final de la actualización. Por eso las
        tandas van por ids y solo se cuentan números entre una y otra. Un XML
        que no se puede leer queda en el log como información: no es un error
        de la actualización, y el asistente usa entonces la Fecha del
        comprobante."""
        Docs = self.sudo().with_context(active_test=False)
        ids_nominas = Docs.search([('move_type', '=', 'N'), ('dsi_nomina_fecha_pago', '=', False)]).ids
        llenadas = 0
        for ids in split_every(LOTE_DEL_LLENADO, ids_nominas):
            for doc in Docs.browse(ids):
                try:
                    datos = _datos_de_nomina(etree.fromstring(_bytes_de_adjunto(doc.xml_attachment_id)))
                except Exception as error:  # pylint: disable=broad-except
                    _logger.info("No se pudo leer el complemento de nómina del CFDI %s: %s", doc.uuid, error)
                    continue
                cambios = {campo: valor for campo, valor in datos.items() if doc[campo] != valor}
                if cambios:
                    doc.with_context(dsi_sin_sync=True).write(cambios)
                    llenadas += 1
            # Guarda la tanda y suelta la caché (invalidate_all hace el flush).
            self.env.invalidate_all()
        if ids_nominas:
            _logger.info("Nómina: %s de %s CFDI sin fecha de pago la tomaron de su complemento.",
                         llenadas, len(ids_nominas))

    @api.model
    def _desligar_nominas_de_facturas(self):
        """Las N que la conciliación por RFC, total y fecha ligó a una factura
        (el RFC era el del empleado: ya no lo hace) se desligan, con una nota
        en su historial. Las que alguien ligó a mano a una factura se dejan;
        la restricción ya no deja volver a guardarlas así."""
        nominas = self.sudo().with_context(active_test=False).search([
            ('move_type', '=', 'N'), ('match_method', '=', 'rfc_amount_date'),
            ('move_id', '!=', False), ('move_id.move_type', '!=', 'entry')])
        for doc in nominas:
            factura = doc.move_id
            doc.write({'move_id': False, 'match_state': 'pending', 'match_method': False})
            doc.message_post(body=self.env._(
                "Estaba vinculado a %s por RFC, total y fecha. Un CFDI de nómina va con su póliza: usa "
                "«Relacionar con póliza».", factura.display_name))
        if nominas:
            _logger.info("Nómina: %s CFDI desligados de la factura a la que se ligaron por importe.", len(nominas))

    @api.model
    def _copiar_nominas_a_sus_polizas(self):
        """La copia del XML de las N que alguien ya había ligado a una póliza
        antes de 0.3.0. Solo las que no la tienen: correrla otra vez no hace
        nada.

        Entonces move_id no revisaba la empresa: una N ligada a un asiento de
        otra empresa se deja como está (no se desliga nada al actualizar),
        pero sin copia, que pasaría su XML a la otra empresa; queda en el log
        como información. En tandas que sueltan la caché, como
        _backfill_datos_de_nomina: cada copia lee el XML completo."""
        Docs = self.sudo().with_context(active_test=False)
        nominas = Docs.search([
            ('move_type', '=', 'N'), ('move_id.move_type', '=', 'entry'), ('dsi_poliza_attachment_id', '=', False)])
        de_otra_empresa = nominas.filtered(lambda d: d.move_id.company_id != d.company_id)
        if de_otra_empresa:
            _logger.info("Nómina: %s CFDI ligados a un asiento de otra empresa se quedan sin copia del XML: %s%s.",
                         len(de_otra_empresa), ", ".join(de_otra_empresa[:20].mapped('uuid')),
                         "…" if len(de_otra_empresa) > 20 else "")
        for ids in split_every(LOTE_DEL_LLENADO, (nominas - de_otra_empresa).ids):
            Docs.browse(ids)._copiar_xml_de_nomina_a_su_poliza()
            self.env.invalidate_all()
