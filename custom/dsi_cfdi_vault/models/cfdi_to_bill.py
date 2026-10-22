"""Bloque 02 del plan: del comprobante a la cuenta por pagar."""
import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

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
        if not convertibles:
            detalle = "\n".join(
                _("- %(n)s porque %(motivo)s", n=len(docs), motivo=motivo)
                for motivo, docs in descartados.items())
            raise UserError(_(
                "Ninguno de los comprobantes seleccionados se puede convertir en "
                "factura de proveedor:\n\n%s", detalle))

        creadas = self.env['account.move']
        for documento in convertibles:
            creadas |= documento._crear_factura_proveedor(
                journal=documento._diario_de_compras(),
                crear_proveedor=True,
            )

        if descartados:
            # Se avisa de lo omitido, pero sin bloquear: lo que sí se podía
            # convertir ya está creado y es lo que el usuario quería.
            _logger.info(
                "Conversión a factura: %s creadas, %s omitidas.",
                len(creadas), sum(len(d) for d in descartados.values()))

        return self._accion_ver_facturas(creadas)

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

        def descartar(doc, motivo):
            descartados.setdefault(motivo, self.env['cfdi.document'])
            descartados[motivo] |= doc

        for doc in self:
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
            else:
                convertibles |= doc
        return convertibles, descartados

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

        lineas, avisos = [], []
        for concepto in conceptos:
            impuestos, avisos_concepto = self._impuestos_odoo_de_concepto(concepto, self.company_id)
            avisos.extend(avisos_concepto)

            valores = {
                'name': concepto['descripcion'],
                'quantity': concepto['cantidad'],
                'price_unit': concepto['valor_unitario'],
                'tax_ids': [(6, 0, impuestos.ids)],
            }
            if product:
                valores['product_id'] = product.id
            if account:
                valores['account_id'] = account.id
            if concepto['descuento']:
                base = concepto['cantidad'] * concepto['valor_unitario']
                if base:
                    valores['discount'] = min(100.0, concepto['descuento'] / base * 100.0)
            lineas.append((0, 0, valores))

        factura = self.env['account.move'].create({
            'move_type': TIPO_A_FACTURA[self.move_type],
            'company_id': self.company_id.id,
            'journal_id': journal.id,
            'partner_id': proveedor.id,
            'invoice_date': self.invoice_date,
            'currency_id': self.currency_id.id,
            'ref': f"{self.uuid}",
            'invoice_line_ids': lineas,
        })

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

        cuerpo = _("Creada desde el CFDI %s de la Bóveda.", self.uuid)
        diferencia = abs(factura.amount_total - self.amount_total)
        if diferencia > 0.05:
            # No se corrige sola: si los totales no cuadran es porque falta un
            # impuesto configurado o el comprobante trae algo fuera de lo común.
            # Es mejor que el contador lo vea antes de confirmar.
            avisos.append(_(
                "El total de la factura (%(factura)s) no coincide con el del comprobante "
                "(%(cfdi)s). Revisa los impuestos antes de confirmar.",
                factura=factura.amount_total, cfdi=self.amount_total))
        if avisos:
            cuerpo += "<br/><br/><b>%s</b><ul>%s</ul>" % (
                _("Revisa esto antes de confirmar:"),
                "".join(f"<li>{a}</li>" for a in avisos),
            )
            # Un aviso en el chatter es fácil de no leer, y con cientos de
            # facturas al mes nadie los abre uno por uno. `checked = False`
            # marca la factura como pendiente de revisión en la propia lista,
            # que es donde el contador sí la va a ver. Es el mismo mecanismo
            # que usa el import nativo de Odoo cuando un impuesto le resulta
            # ambiguo.
            # Odoo 19 lo marca con `checked = False`; Odoo 20 sustituyó ese
            # booleano por `review_state`, donde 'todo' significa "por revisar".
            if 'review_state' in factura._fields:
                factura.review_state = 'todo'
            elif 'checked' in factura._fields:
                factura.checked = False
        factura.message_post(body=cuerpo)
        return factura
