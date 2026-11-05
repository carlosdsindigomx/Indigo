"""Bloque 02 del plan: del comprobante a la cuenta por pagar."""
import logging

from markupsafe import Markup

from odoo import Command, _, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from .cfdi_conceptos import _bytes_de_adjunto
from .cfdi_document import _empresa_con_rfc, _empresas_del_mismo_rfc, _rfc, _rfc_receptor_de
from .cfdi_inconsistency import TOLERANCIA_TOTAL, _clave_del_hallazgo, _huella_de_texto

_logger = logging.getLogger(__name__)

# Estatus SAT de la Bóveda -> estatus SAT del documento CFDI de Odoo. Los
# cancelados nunca llegan aquí: no se convierten en factura.
SAT_STATE_DOCUMENTO = {
    'valid': 'valid',
    'not_found': 'not_found',
}

# El decoder de CFDI de Odoo (l10n_mx_edi). Se llama directo, sin
# `_extend_with_attachments`: en Odoo 19 y 20 ese hace commit antes y después
# del decoder y se traga las excepciones, así que en una conversión por lote
# dejaría facturas a medias confirmadas en la base.
DECODER_CFDI = '_l10n_mx_edi_import_cfdi_invoice'


def _opciones(modelo, campo):
    """Valores válidos de un campo Selection, o vacío si el campo no existe en
    esta versión de Odoo (l10n_mx_edi_tax_object no existe en 18)."""
    definicion = modelo._fields.get(campo)
    return set(definicion.get_values(modelo.env)) if definicion else set()


def _odoo_revisa_rfc_receptor(env):
    """True si el import de CFDI de Odoo revisa por su cuenta el RFC receptor.
    Solo Odoo 20 lo hace; 18 y 19 importan el XML sin mirarlo."""
    return hasattr(type(env['account.move']), '_l10n_mx_edi_import_cfdi_check_can_import')

# Qué tipo de comprobante se convierte en qué tipo de factura.
# Solo I (ingreso) y E (egreso) son cuentas por pagar. Los demás NO se
# convierten: un CFDI de Pago (P) solo acusa un pago de facturas que ya
# existen, uno de Nómina (N) corresponde a la póliza de nómina y uno de
# Traslado (T) no ampara una operación de compra. Antes esto no se filtraba
# y un CFDI de Pago acababa creando una factura de total 0 sin que nada
# avisara, porque sus conceptos vienen en cero.
TIPO_A_FACTURA = {
    'I': 'in_invoice',
    'E': 'in_refund',   # el egreso es una nota de crédito del proveedor
}

MOTIVO_NO_CONVERTIBLE = {
    'P': "Es un CFDI de Pago (complemento de pago): acusa el pago de facturas "
         "que ya deberían existir, no es una cuenta por pagar nueva.",
    'N': "Es un CFDI de Nómina: le corresponde la póliza de nómina, no una "
         "factura de proveedor.",
    'T': "Es un CFDI de Traslado: ampara el movimiento de mercancía, no una "
         "operación de compra.",
}


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    def _buscar_proveedor(self):
        """Busca el contacto que corresponde al RFC emisor. False si no existe."""
        self.ensure_one()
        rfc = (self.partner_rfc_issuer or '').strip().upper()
        if not rfc:
            return self.env['res.partner']
        return self.env['res.partner'].search([
            ('vat', '=ilike', rfc),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ], limit=1)

    def _crear_proveedor_desde_cfdi(self):
        """Da de alta el contacto con lo que trae el comprobante."""
        self.ensure_one()
        return self.env['res.partner'].create({
            'name': self.partner_name_issuer or self.partner_rfc_issuer,
            'vat': (self.partner_rfc_issuer or '').strip().upper(),
            # Odoo 20 quitó company_type (era un Selection calculado); el campo
            # real siempre fue is_company, que sigue existiendo en ambas versiones.
            'is_company': True,
            'supplier_rank': 1,
            'country_id': self.env.ref('base.mx').id,
        })

    def _diario_de_compras(self):
        """El diario de compras con el que se registra la factura.

        Se resuelve solo, como hace Odoo al subir un XML: preguntarlo en un
        diálogo era pedirle al usuario una decisión que casi siempre tiene una
        sola respuesta. Si hay varios, gana el de secuencia más baja, que es
        el que Odoo propone por omisión; el usuario puede cambiarlo en la
        factura, que nace en borrador.
        """
        self.ensure_one()
        diario = self.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', self.company_id.id),
        ], order='sequence, id', limit=1)
        if not diario:
            raise UserError(_(
                "La empresa %s no tiene ningún diario de compras configurado.",
                self.company_id.name))
        return diario

    def action_crear_factura_proveedor(self):
        """Crea la factura de proveedor directamente desde el XML, sin diálogo.

        El flujo imita al de Odoo cuando subes un XML: se toma todo lo que el
        comprobante ya dice —proveedor, fecha, moneda, partidas, impuestos— y
        se deja un BORRADOR para que el usuario complete lo que falte
        (producto, cuenta, centro de costo) sobre la factura misma, que es
        donde tiene el contexto a la vista.

        Antes esto abría un asistente que preguntaba diario, proveedor,
        producto y cuenta por adelantado. Eran decisiones pedidas a ciegas,
        antes de ver la factura, y casi siempre con una sola respuesta posible.
        """
        for empresa in self.company_id:
            self.env['dsi.license.client.state']._check_feature(
                'vault', _("convertir CFDI en facturas de proveedor"), empresa)
        convertibles, descartados = self._clasificar_para_factura()
        # Los que ya tienen factura en Odoo con su folio fiscal (subida a mano
        # o importada por Odoo) se vinculan a ella en lugar de crear otra: es
        # la misma conciliación por UUID exacto que corre al importar.
        ya_facturados = descartados.pop(self._motivo_ya_facturado(), self.env['cfdi.document'])
        if ya_facturados:
            self._match_by_uuid(ya_facturados)
        if not convertibles and not ya_facturados:
            raise UserError(_(
                "Ninguno de los comprobantes seleccionados se puede convertir en "
                "factura de proveedor:\n\n%s",
                "\n".join("- " + linea for linea in self._detalle_descartados(descartados))))

        creadas = self.env['account.move']
        for documento in convertibles:
            creadas |= documento._crear_factura_proveedor(
                journal=documento._diario_de_compras(),
                crear_proveedor=True,
            )

        accion = self._accion_ver_facturas(creadas | ya_facturados.move_id)
        if not descartados:
            return accion
        # Se avisa de lo omitido, pero sin bloquear: lo que sí se podía
        # convertir ya está creado y es lo que el usuario quería. Antes el
        # aviso solo iba al log, y justo los omitidos por RFC son los que piden
        # que el usuario haga algo (capturar el RFC, revisar en qué empresa se
        # importó el CFDI).
        _logger.info(
            "Conversión a factura: %s creadas, %s vinculadas a una factura existente, %s omitidas.",
            len(creadas), len(ya_facturados), sum(len(d) for d in descartados.values()))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Algunos comprobantes no se convirtieron"),
                # La notificación no respeta saltos de línea.
                'message': "; ".join(self._detalle_descartados(descartados)) + ".",
                'type': 'warning',
                'sticky': True,
                'next': accion,
            },
        }

    def _detalle_descartados(self, descartados):
        """Una línea por motivo del clasificador: cuántos y por qué."""
        # env._ y no _(): dentro de un generador _() no encuentra el idioma.
        return [
            self.env._("%(n)s porque %(motivo)s", n=len(docs), motivo=motivo)
            for motivo, docs in descartados.items()
        ]

    def _accion_ver_facturas(self, facturas):
        """Deja al usuario en lo que acaba de crear: el formulario si es una,
        la lista si son varias."""
        accion = {
            'type': 'ir.actions.act_window',
            'name': _("Facturas de proveedor"),
            'res_model': 'account.move',
            'context': {'create': False},
        }
        if len(facturas) == 1:
            accion.update({'view_mode': 'form', 'res_id': facturas.id})
        else:
            accion.update({
                'view_mode': 'list,form',
                'domain': [('id', 'in', facturas.ids)],
            })
        return accion

    def _clasificar_para_factura(self):
        """Separa este conjunto en convertibles y no convertibles.

        Devuelve (recordset convertible, dict {motivo: recordset}). Lo usan el
        asistente —para avisar ANTES de crear nada— y la acción del botón.
        Comparte exactamente las reglas de `_validar_convertible` para que lo
        que el asistente anuncia y lo que la conversión acepta no se separen.
        """
        convertibles = self.env['cfdi.document']
        descartados = {}
        facturas = self._facturas_por_folio_fiscal()

        def descartar(doc, motivo):
            descartados.setdefault(motivo, self.env['cfdi.document'])
            descartados[motivo] |= doc

        for doc in self:
            problema_rfc = doc._problema_rfc_receptor()
            factura = doc._factura_con_su_folio(facturas)
            if doc.move_id:
                descartar(doc, _("ya están vinculados a una factura"))
            elif doc.direction != 'received':
                descartar(doc, _("los emitiste tú, no los recibiste"))
            elif doc.move_type in MOTIVO_NO_CONVERTIBLE:
                etiqueta = dict(self._fields['move_type'].selection).get(doc.move_type, doc.move_type)
                descartar(doc, _("son comprobantes de %s y no son cuentas por pagar", etiqueta))
            elif doc.move_type not in TIPO_A_FACTURA:
                descartar(doc, _("no tienen un tipo de comprobante reconocible"))
            elif doc.sat_status == 'canceled':
                descartar(doc, _("están cancelados en el SAT"))
            elif problema_rfc == 'sin_rfc':
                descartar(doc, _("su empresa no tiene RFC capturado"))
            elif problema_rfc == 'otro_rfc':
                descartar(doc, _("se emitieron a otro RFC, no al de la empresa"))
            elif problema_rfc == 'rfc_mal_capturado':
                descartar(doc, _("Odoo no acepta el RFC de su empresa tal como está capturado"))
            elif factura.company_id == doc.company_id:
                descartar(doc, self._motivo_ya_facturado())
            elif factura:
                descartar(doc, _("ya hay una factura con su folio fiscal en otra empresa con el mismo RFC"))
            elif doc._por_importar_en_otra_empresa():
                descartar(doc, _("Odoo ya los descargó del SAT en otra empresa con el mismo RFC"))
            else:
                convertibles |= doc
        return convertibles, descartados

    def _factura_con_su_folio(self, facturas):
        """La factura de Odoo que ya registra este CFDI: la de su empresa o, si
        no hay, la de otra empresa que recibe con el mismo RFC. `facturas` es
        lo que devuelve `_facturas_por_folio_fiscal`.

        Las canceladas no cuentan, igual que en la revisión de referencias
        duplicadas de Odoo: si el usuario canceló la factura que tenía este
        folio, el CFDI se puede volver a registrar.
        """
        self.ensure_one()
        uuid = (self.uuid or '').upper()
        propia = facturas.get((self.company_id.id, uuid))
        if propia and propia.state != 'cancel':
            return propia
        rfc = _rfc_receptor_de(self.company_id)
        return next((
            factura for (empresa, folio), factura in facturas.items()
            if rfc and folio == uuid and empresa != self.company_id.id and factura.state != 'cancel'
            and _rfc_receptor_de(factura.company_id) == rfc
        ), self.env['account.move'])

    def _motivo_ya_facturado(self):
        """Motivo del clasificador para el comprobante cuyo folio fiscal ya está
        en una factura de Odoo. La acción del botón lo busca por este texto para
        vincular esos comprobantes en lugar de omitirlos."""
        return _("ya hay una factura en Odoo con su folio fiscal")

    def _rfc_de_la_empresa(self):
        """RFC al que deben venir emitidos los CFDI de esta empresa.

        Es la regla del import de Odoo 20: la empresa más cercana con RFC, ella
        misma o su matriz. Una sucursal sin RFC propio recibe con el de su
        matriz.
        """
        self.ensure_one()
        return _rfc_receptor_de(self.company_id)

    def _problema_rfc_receptor(self):
        """False si el comprobante se emitió a la empresa; si no, 'sin_rfc'
        (la empresa no tiene RFC con qué comparar), 'otro_rfc' o
        'rfc_mal_capturado' (es el mismo RFC, pero Odoo 20 no lo reconoce así).

        Antes no se revisaba: un CFDI importado en la empresa equivocada se
        convertía en una factura de proveedor de una compra que no era suya.
        Odoo 20 además rechaza ese XML en su propio import, así que sin esta
        revisión la conversión fallaría ahí con la factura ya armada.
        """
        self.ensure_one()
        rfc_empresa = self._rfc_de_la_empresa()
        if not rfc_empresa:
            return 'sin_rfc'
        if _rfc(self.partner_rfc_receiver) != rfc_empresa:
            return 'otro_rfc'
        if _odoo_revisa_rfc_receptor(self.env):
            # Odoo 20 compacta el RFC del comprobante y lo compara con el de la
            # empresa tal como está guardado, sin quitarle el prefijo MX que
            # Odoo 18 aceptaba (MXEKU9003173C9). Se le pregunta lo mismo para
            # que el clasificador no anuncie como convertible un CFDI que su
            # import va a rechazar a mitad del lote.
            raiz = _empresa_con_rfc(self.company_id)
            rfc, _pais = raiz.partner_id._run_vat_checks(
                raiz.country_id, self.partner_rfc_receiver, validation='setnull')
            if rfc != raiz.partner_id.vat:
                return 'rfc_mal_capturado'
        return False

    def _validar_convertible(self):
        """Comprueba que este comprobante puede convertirse en factura.

        Se valida ANTES de crear nada: una vez creada la factura, deshacerla
        implica borrar el asiento y desvincular el comprobante, y si el error
        salta a mitad de un lote la limpieza es peor que la prevención.
        """
        self.ensure_one()
        if self.move_id:
            raise UserError(_("Este comprobante ya está vinculado a la factura %s.", self.move_id.name))
        if self.direction != 'received':
            raise UserError(_(
                "Este comprobante lo emitiste tú, no lo recibiste: no puede ser una "
                "factura de proveedor."))
        motivo = MOTIVO_NO_CONVERTIBLE.get(self.move_type)
        if motivo:
            raise UserError(_(
                "Este comprobante no se puede convertir en factura de proveedor.\n\n%s", motivo))
        if self.move_type not in TIPO_A_FACTURA:
            raise UserError(_(
                "No se reconoce el tipo de comprobante (%s), así que no se puede decidir "
                "qué clase de factura crear.", self.move_type or _("vacío")))
        if self.sat_status == 'canceled':
            raise UserError(_(
                "Este comprobante está CANCELADO en el SAT. Registrarlo crearía una "
                "discrepancia crítica: una factura respaldada por un CFDI que ya no existe."))
        problema_rfc = self._problema_rfc_receptor()
        if problema_rfc == 'sin_rfc':
            raise UserError(_(
                "La empresa %s no tiene RFC capturado, así que no se puede comprobar que "
                "este comprobante se le haya emitido a ella. Captúralo en los datos de la "
                "empresa antes de convertir.", self.company_id.name))
        if problema_rfc == 'otro_rfc':
            raise UserError(_(
                "Este comprobante se emitió al RFC %(receptor)s y la empresa %(empresa)s "
                "tiene el RFC %(rfc)s. Solo se registra como factura de proveedor lo que se "
                "le facturó a la propia empresa: revisa en qué empresa se importó.",
                receptor=self.partner_rfc_receiver or _("vacío"),
                empresa=self.company_id.name, rfc=self._rfc_de_la_empresa()))
        if problema_rfc == 'rfc_mal_capturado':
            raiz = _empresa_con_rfc(self.company_id)
            raise UserError(_(
                "La empresa %(empresa)s tiene capturado el RFC «%(rfc)s» y Odoo lo compara tal "
                "cual con el del comprobante (%(receptor)s), así que su import rechazaría el "
                "XML. Captura el RFC sin prefijo de país ni espacios antes de convertir.",
                empresa=raiz.name, rfc=raiz.partner_id.vat, receptor=self.partner_rfc_receiver))
        factura = self._factura_con_su_folio(self._facturas_por_folio_fiscal())
        if factura.company_id == self.company_id:
            raise UserError(_(
                "Ya existe la factura %(factura)s con el folio fiscal %(uuid)s. Vincula el "
                "comprobante a ella en lugar de crear otra.",
                factura=factura.display_name, uuid=self.uuid))
        if factura:
            raise UserError(_(
                "Ya existe la factura %(factura)s con el folio fiscal %(uuid)s en la empresa "
                "%(empresa)s, que recibe con el mismo RFC. Un CFDI se registra una sola vez: "
                "no se crea otra factura en %(esta)s.",
                factura=factura.display_name, uuid=self.uuid,
                empresa=factura.company_id.name, esta=self.company_id.name))
        por_importar = self._por_importar_en_otra_empresa()
        if por_importar:
            raise UserError(_(
                "Odoo ya descargó del SAT el CFDI %(uuid)s en la empresa %(empresa)s, que recibe "
                "con el mismo RFC, y su importación automática va a crear la factura ahí. "
                "Conviértelo en esa empresa para no registrarlo dos veces.",
                uuid=self.uuid, empresa=por_importar.company_id.name))

    def _crear_factura_proveedor(self, journal, partner=None, crear_proveedor=False,
                                  product=None, account=None, recordar=True):
        """Crea la factura de proveedor EN BORRADOR a partir de este comprobante."""
        self.ensure_one()
        self._validar_convertible()

        proveedor = partner or self._buscar_proveedor()
        if not proveedor:
            if not crear_proveedor:
                raise UserError(_(
                    "No existe un contacto con el RFC %s. Créalo antes de convertir.",
                    self.partner_rfc_issuer))
            proveedor = self._crear_proveedor_desde_cfdi()

        # Mapeo recordado del proveedor. Antes lo precargaba el asistente; sin
        # diálogo se aplica solo, que es lo que el usuario espera de algo
        # descrito como "se recuerda de la última conversión".
        if product is None:
            product = proveedor.cfdi_product_id or None
        if account is None:
            account = proveedor.cfdi_account_id or None

        conceptos = self._parse_conceptos()
        if not conceptos:
            raise UserError(_(
                "El XML de este comprobante no trae conceptos, así que no hay partidas "
                "que registrar."))

        objetos_de_impuesto = _opciones(self.env['account.move.line'], 'l10n_mx_edi_tax_object')
        lineas, avisos = [], []
        for concepto in conceptos:
            impuestos, avisos_concepto = self._impuestos_odoo_de_concepto(concepto, self.company_id)
            avisos.extend(avisos_concepto)

            valores = {
                'name': concepto['descripcion'],
                'quantity': concepto['cantidad'],
                'price_unit': concepto['valor_unitario'],
                'tax_ids': [(6, 0, impuestos.ids)],
                # Igual que el import de Odoo: la partida sale del comprobante,
                # así que elegir después un producto ya no le pisa el precio ni
                # los impuestos con los del catálogo.
                'is_imported': True,
            }
            # Objeto de impuesto (ObjetoImp), que Odoo 19 y 20 guardan por
            # partida; en 18 el campo no existe.
            if concepto['objeto_imp'] in objetos_de_impuesto:
                valores['l10n_mx_edi_tax_object'] = concepto['objeto_imp']
            if product:
                valores['product_id'] = product.id
            if account:
                valores['account_id'] = account.id
            # Sobre el Importe del concepto, como Odoo: es la base a la que el
            # emisor le aplicó el descuento. Cantidad × ValorUnitario puede
            # diferir del Importe por el redondeo del emisor.
            if concepto['descuento'] and concepto['importe']:
                valores['discount'] = min(100.0, concepto['descuento'] / concepto['importe'] * 100.0)
            lineas.append((0, 0, valores))

        Factura = self.env['account.move']
        # Como el import de Odoo: cantidad y descuento con los decimales del
        # comprobante (hasta 10 y 13) y no con los de la base (2). Sin esto,
        # 38.456 litros de combustible quedaban en 38.46 y un descuento de
        # 10.0072 % en 10.01 %, y la factura dejaba de cuadrar con el CFDI.
        with Factura._disable_discount_precision():
            factura = Factura.create({
                'move_type': TIPO_A_FACTURA[self.move_type],
                'company_id': self.company_id.id,
                'journal_id': journal.id,
                'partner_id': proveedor.id,
                # La fecha de emisión, como hace Odoo 20; 18 y 19 toman la de timbrado.
                'invoice_date': self.invoice_date,
                'currency_id': self.currency_id.id,
                'ref': f"{self.uuid}",
                'invoice_line_ids': lineas,
                **self._valores_cfdi_de_factura(),
            })

        # Lo que sigue lo escribe la conversión, no el usuario. Odoo importa
        # con esta misma marca, y sin ella la factura quedaba como «modificada
        # a mano» (al poner el PDF principal o marcarla por revisar; en 18,
        # siempre, porque su decoder reescribe el tipo de factura), y la
        # sugerencia de contabilizar solas las facturas del proveedor la
        # descartaba.
        sistema = factura.with_context(skip_is_manually_modified=True)
        adjuntos = self._registrar_cfdi_como_odoo(sistema)
        adjuntos |= self._copiar_pdf_a_factura(sistema)

        # Se vincula de una vez: la conciliación ya quedó hecha por construcción.
        self.write({
            'move_id': factura.id,
            'match_state': 'manual',
            'match_method': 'manual',
        })

        if recordar and (product or account):
            valores_mapeo = {}
            if product and proveedor.cfdi_product_id != product:
                valores_mapeo['cfdi_product_id'] = product.id
            if account and proveedor.cfdi_account_id != account:
                valores_mapeo['cfdi_account_id'] = account.id
            if valores_mapeo:
                proveedor.write(valores_mapeo)

        # Markup: message_post escapa un str, y el contador veía las etiquetas
        # (<br/>, <b>, <ul>) como texto. Los avisos sí se escapan.
        cuerpo = Markup("%s") % _("Creada desde el CFDI %s de la Bóveda.", self.uuid)
        avisos_de_impuesto = list(avisos)
        aviso_de_total = False
        diferencia = abs(factura.amount_total - self.amount_total)
        if diferencia > TOLERANCIA_TOTAL:
            # No se corrige sola: si los totales no cuadran es porque falta un
            # impuesto configurado o el comprobante trae algo fuera de lo común.
            # Es mejor que el contador lo vea antes de confirmar.
            aviso_de_total = _(
                "El total de la factura (%(factura)s) no coincide con el del comprobante "
                "(%(cfdi)s). Revisa los impuestos antes de confirmar.",
                factura=factura.amount_total, cfdi=self.amount_total)
            avisos.append(aviso_de_total)
        self._registrar_avisos_de_conversion(factura, avisos_de_impuesto, aviso_de_total)
        if avisos:
            cuerpo += Markup("<br/><br/><b>%s</b><ul>%s</ul>") % (
                _("Revisa esto antes de confirmar:"),
                Markup().join(Markup("<li>%s</li>") % aviso for aviso in avisos),
            )
            # Un aviso en el chatter es fácil de no leer, y con cientos de
            # facturas al mes nadie los abre uno por uno. `checked = False`
            # marca la factura como pendiente de revisión en la propia lista,
            # que es donde el contador sí la va a ver. Es el mismo mecanismo
            # que usa el import nativo de Odoo cuando un impuesto le resulta
            # ambiguo. Odoo 18 y 19 lo marcan con `checked = False`; Odoo 20
            # sustituyó ese booleano por `review_state` (ver
            # _dsi_marcar_por_revisar). Al publicarla, Odoo la da por
            # revisada; account.move._post le vuelve a poner la marca
            # mientras el aviso siga pendiente en la Bóveda.
            factura._dsi_marcar_por_revisar()
        # Con estos contextos el mensaje no se toma como un XML subido a la
        # factura: Odoo 18 lo decodificaría otra vez y publicaría «la factura ya
        # tiene partidas»; 19 y 20 ya lo omiten para mensajes de sistema. El
        # PDF, si viene, queda como adjunto principal (la vista previa); el XML
        # nunca lo es.
        sistema.with_context(no_new_invoice=True, disable_attachment_import=True).message_post(
            body=cuerpo, attachment_ids=adjuntos.ids)
        return factura

    def _registrar_avisos_de_conversion(self, factura, avisos_de_impuesto, aviso_de_total):
        """Los avisos de la conversión, además de en el chatter, como
        discrepancias «Aviso al convertir»: una por aviso distinto, ligadas al
        CFDI y a la factura (si la factura se borra, se van con ella).

        El de total lleva los dos totales en la huella: si después el total
        cambia y sigue sin cuadrar es otro hallazgo, y si cuadra, la revisión
        diaria lo resuelve sola. Los de impuesto no se pueden volver a revisar
        (nada en la factura dice si ya se capturó a mano lo que faltaba), así
        que esperan a que alguien los marque como revisados.
        """
        self.ensure_one()
        comunes = {
            'company_id': self.company_id.id,
            'dedupe_key': _clave_del_hallazgo('aviso_conversion', cfdi=self, factura=factura),
            'cfdi_document_id': self.id,
            'move_id': factura.id,
            'partner_id': factura.commercial_partner_id.id,
            'uuid': self.uuid,
        }
        hallazgos = [
            dict(comunes, fingerprint=_huella_de_texto('impuesto', aviso), observation=aviso)
            for aviso in avisos_de_impuesto
        ]
        if aviso_de_total:
            total_cfdi = self.currency_id.round(self.amount_total)
            total_factura = factura.currency_id.round(factura.amount_total)
            hallazgos.append(dict(
                comunes,
                fingerprint=f"total:{total_cfdi:.2f}:{total_factura:.2f}",
                vault_value=_("Total del CFDI: %s", f"{total_cfdi:,.2f}"),
                odoo_value=_("Total de la factura: %s", f"{total_factura:,.2f}"),
                observation=aviso_de_total,
            ))
        if not hallazgos:
            return
        self.env['dsi.cfdi.inconsistency']._sincronizar_desde_gancho(
            '_sincronizar', 'aviso_conversion', [('move_id', '=', factura.id)], hallazgos, 'conversion')

    def _valores_cfdi_de_factura(self):
        """Los datos del comprobante que el import de Odoo pone en la factura y
        que la conversión no ponía: forma de pago, uso CFDI, método de pago,
        tipo de cambio y modo de impuestos. Cada uno solo si esta versión de
        Odoo lo guarda y el valor del XML es uno que Odoo acepta.
        """
        self.ensure_one()
        Factura = self.env['account.move']
        cabecera = self._datos_cabecera()
        valores = {}
        if 'l10n_mx_edi_payment_method_id' in Factura._fields:
            # Siempre, aunque el código no exista, como el import de Odoo: la
            # 99 «Por definir» de todo CFDI en PPD está archivada. Sin la
            # clave, Odoo le ponía la forma de pago del proveedor o del diario,
            # que el comprobante no dice.
            valores['l10n_mx_edi_payment_method_id'] = self.env['l10n_mx_edi.payment.method'].search(
                [('code', '=', cabecera['forma_pago'])], limit=1).id
        if cabecera['uso_cfdi'] in _opciones(Factura, 'l10n_mx_edi_usage'):
            valores['l10n_mx_edi_usage'] = cabecera['uso_cfdi']
        # En Odoo 18 el método de pago (PUE/PPD) es un cálculo sin guardar: lo
        # que se escriba se pierde. En 19 se guarda, pero en compras es
        # informativo: Odoo lo calcula en False para las facturas de proveedor
        # y lo borra al primer cambio de proveedor, fechas o plazo de pago (su
        # propio import lo pierde igual). Solo 20 lo conserva. Lo que necesite
        # el método de pago de una compra lo toma de la Bóveda
        # (cfdi.document.payment_method), no de la factura.
        politica = Factura._fields.get('l10n_mx_edi_payment_policy')
        if politica and politica.store and cabecera['metodo_pago'] in _opciones(Factura, 'l10n_mx_edi_payment_policy'):
            valores['l10n_mx_edi_payment_policy'] = cabecera['metodo_pago']
        # TipoCambio son pesos por unidad de la moneda del comprobante, así que
        # solo tiene sentido en una empresa en pesos. Odoo guarda el inverso:
        # cuántas unidades de la moneda de la factura vale un peso.
        moneda_empresa = self.company_id.currency_id
        if (self.currency_id and self.currency_id != moneda_empresa and moneda_empresa.name == 'MXN'
                and cabecera['tipo_cambio'] > 0 and 'invoice_currency_rate' in Factura._fields):
            valores['invoice_currency_rate'] = 1.0 / cabecera['tipo_cambio']
        # Odoo 20: el ValorUnitario de un CFDI siempre es antes de impuestos.
        # Sin esto, en una empresa con precios «con impuestos incluidos» la
        # factura tomaba 1,000 + IVA como 1,000 con IVA incluido. Es lo que su
        # import pone a toda factura importada; 18 y 19 no tienen el campo.
        if 'document_tax_mode' in Factura._fields:
            valores['document_tax_mode'] = 'tax_excluded'
        return valores

    def _registrar_cfdi_como_odoo(self, factura):
        """Deja la factura como si se hubiera subido el XML en Odoo: el XML
        adjunto y el documento CFDI «Recibido». De ese documento Odoo calcula
        el Folio fiscal (l10n_mx_edi_cfdi_uuid), lo muestra en la pestaña CFDI
        y verifica el estatus en el SAT. Sin él, la factura quedaba sin folio
        y la conciliación por UUID ya no la encontraba.

        El documento lo crea el mismo método de Odoo que corre al subir el XML
        en una factura (`_l10n_mx_edi_import_cfdi_invoice`). Como la factura ya
        tiene partidas, Odoo no las toca: solo registra el CFDI. Así el mapeo de
        impuestos y cuentas de la Bóveda se conserva y lo demás queda igual que
        en una factura importada por Odoo. Si Odoo no reconoce el XML (la raíz
        sin el prefijo `cfdi`), el documento se crea a mano, igual que antes.

        Se adjunta una copia del XML: el de la Bóveda se queda donde está. Si se
        moviera a la factura, borrar una factura en borrador chocaría con
        xml_attachment_id, que es ondelete='restrict'.

        Devuelve la copia del XML.
        """
        self.ensure_one()
        factura = factura.with_company(factura.company_id)
        adjunto = self.env['ir.attachment'].create({
            'name': self.xml_attachment_id.name or f"{self.uuid}.xml",
            'raw': _bytes_de_adjunto(self.xml_attachment_id),
            'mimetype': 'application/xml',
            'res_model': 'account.move',
            'res_id': factura.id,
        })
        por_importar = self._documentos_por_importar().filtered(
            lambda d: d.company_id == factura.company_id).with_env(self.env)
        decoder, file_data = self._decoder_cfdi_de_odoo(factura, adjunto)
        if decoder:
            # Ligado a la factura, Odoo 20 reutiliza el documento «Por importar»
            # de su descarga masiva en vez de crear otro, y su cron ya no crea
            # una segunda factura con este CFDI.
            por_importar.write({'invoice_ids': [Command.link(factura.id)]})
            motivo = decoder(factura, file_data, False)
            # Odoo 20 contesta con un texto cuando no acepta el XML (ya tiene
            # CFDI, el RFC no es el de la empresa...). Se lanza el error para
            # que se deshaga todo y no quede una factura sin su CFDI.
            if isinstance(motivo, str):
                raise UserError(_(
                    "Odoo no aceptó el CFDI %(uuid)s en la factura:\n\n%(motivo)s",
                    uuid=self.uuid, motivo=html2plaintext(motivo)))
            self._reponer_valores_cfdi(factura)
        else:
            self._crear_documento_cfdi_manual(factura, adjunto, por_importar[:1])

        documento = self.env['l10n_mx_edi.document'].search([
            ('move_id', '=', factura.id),
            ('attachment_id', '=', adjunto.id),
            ('state', '=', 'invoice_received'),
        ])
        if len(documento) != 1:
            raise UserError(_(
                "Odoo no registró el CFDI %s en la factura, así que no se creó nada. Para "
                "ver el motivo, sube el XML directamente en una factura de proveedor.",
                self.uuid))
        # Odoo deja el estatus SAT «No definido» hasta que su cron o el botón
        # «Actualizar SAT» lo consultan. La Bóveda ya lo sabe.
        documento.sat_state = SAT_STATE_DOCUMENTO.get(self.sat_status, 'not_defined')
        return adjunto

    def _reponer_valores_cfdi(self, factura):
        """Vuelve a poner en la factura lo que dice el comprobante, si el
        decoder de Odoo lo movió.

        El de Odoo 18 reescribe el tipo de factura aunque ya tenga partidas, y
        con eso Odoo vuelve a calcular el diario y, en cadena, lo que cuelga de
        él: la moneda (toma la del diario, si tiene), la forma de pago (toma la
        del contacto del proveedor) y el tipo de cambio (toma el de Odoo, 1:1
        si no hay tasas cargadas). Los de 19 y 20 no tocan una factura con
        partidas, así que ahí no se escribe nada.
        """
        valores = self._valores_cfdi_de_factura()
        if self.currency_id:
            valores['currency_id'] = self.currency_id.id
        movidos = {
            campo: valor for campo, valor in valores.items()
            if factura._fields[campo].convert_to_write(factura[campo], factura) != valor
        }
        if movidos:
            factura.write(movidos)

    def _decoder_cfdi_de_odoo(self, factura, adjunto):
        """(decoder, file_data) con los que Odoo importaría este XML en la
        factura, o (None, None) si Odoo no lo reconoce como CFDI.

        Cambia por versión: en 19 y 20 el archivo se arma con `_to_files_data` y
        `_get_edi_decoder` devuelve un dict; en 18 se arma con
        `_unwrap_edi_attachments` y devuelve la función. Solo se acepta el
        decoder de CFDI de l10n_mx_edi: otro (UBL, OCR) no sabría qué hacer con
        la factura ya armada.
        """
        if hasattr(factura, '_to_files_data'):
            archivos = factura._to_files_data(adjunto)
        else:
            archivos = adjunto._unwrap_edi_attachments()
        if not archivos:
            return None, None
        file_data = archivos[0]
        info = factura._get_edi_decoder(file_data, new=False)
        decoder = info.get('decoder') if isinstance(info, dict) else info
        propio = getattr(type(factura), DECODER_CFDI, None)
        if propio is None or getattr(decoder, '__func__', None) is not propio:
            return None, None
        return decoder, file_data

    def _documentos_por_importar(self):
        """Solo Odoo 20: los documentos «Por importar» que su descarga masiva
        nativa del SAT ya creó para este CFDI. En 18 y 19 ese estado no existe
        y no hay nada que reutilizar.

        Se buscan en todas las empresas que reciben con el mismo RFC, con sudo:
        la descarga nativa corre en la empresa de la solicitud, que puede ser
        la matriz de la sucursal donde está el CFDI en la Bóveda.
        """
        self.ensure_one()
        Documento = self.env['l10n_mx_edi.document']
        if 'to_import' not in _opciones(Documento, 'state'):
            return Documento
        return Documento.sudo().search([
            ('attachment_uuid', 'in', [self.uuid.upper(), self.uuid.lower()]),
            ('state', '=like', 'to_import%'),
            ('company_id', 'in', _empresas_del_mismo_rfc(self.company_id).ids),
        ])

    def _por_importar_en_otra_empresa(self):
        """Solo Odoo 20: el documento «Por importar» de este CFDI que su
        descarga nativa dejó en OTRA empresa con el mismo RFC. No se puede
        ligar a una factura de esta empresa, y el cron de Odoo crearía ahí una
        segunda factura con el mismo folio, así que no se convierte aquí."""
        return self._documentos_por_importar().filtered(
            lambda d: d.company_id != self.company_id)[:1]

    def _crear_documento_cfdi_manual(self, factura, adjunto, por_importar):
        """Respaldo para el XML que Odoo no reconoce como CFDI: el mismo
        documento que habría creado su decoder."""
        Documento = self.env['l10n_mx_edi.document']
        valores = {
            'move_id': factura.id,
            'invoice_ids': [Command.set(factura.ids)],
            'state': 'invoice_received',
            'sat_state': 'not_defined',
            'attachment_id': adjunto.id,
            'datetime': fields.Datetime.now(),
        }
        if por_importar:
            # Odoo 20: se ocupa el «Por importar», como haría su decoder.
            por_importar.write(valores)
            return por_importar
        # Odoo 20 agregó la empresa al documento, obligatoria; 18 y 19 no la tienen.
        if 'company_id' in Documento._fields:
            valores['company_id'] = factura.company_id.id
        return Documento.create(valores)

    def _copiar_pdf_a_factura(self, factura):
        """Copia del PDF del comprobante en la factura, si la Bóveda lo tiene.

        Es lo que da la vista previa: el XML nunca llega a ser el adjunto
        principal de la factura, y sin PDF el contador la abre sin ver el
        comprobante. Es copia por lo mismo que el XML.
        """
        self.ensure_one()
        pdf = self.pdf_attachment_id
        contenido = _bytes_de_adjunto(pdf) if pdf else b''
        if not contenido:
            return self.env['ir.attachment']
        return self.env['ir.attachment'].create({
            'name': pdf.name or f"{self.uuid}.pdf",
            'raw': contenido,
            'mimetype': 'application/pdf',
            'res_model': 'account.move',
            'res_id': factura.id,
        })
