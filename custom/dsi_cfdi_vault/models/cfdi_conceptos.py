"""Lectura de los conceptos (partidas) y sus impuestos desde el XML del CFDI.

Deliberadamente separado del parseo de encabezado: al importar miles de
comprobantes solo interesa el encabezado (folio, RFC, total, fecha). Los
conceptos se leen únicamente cuando alguien va a convertir ese comprobante
en una factura de proveedor, que es cuando de verdad hacen falta.
"""
from lxml import etree

from odoo import _, api, models
from odoo.exceptions import UserError

from .cfdi_document import _local

# Claves de impuesto del catálogo del SAT
IMPUESTO_ISR = '001'
IMPUESTO_IVA = '002'
IMPUESTO_IEPS = '003'

NOMBRE_IMPUESTO = {
    IMPUESTO_ISR: "ISR",
    IMPUESTO_IVA: "IVA",
    IMPUESTO_IEPS: "IEPS",
}


def _num(valor, por_omision=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return por_omision


def _bytes_de_adjunto(adjunto):
    """Contenido en bytes de un ir.attachment, en Odoo 19 y en Odoo 20.

    En Odoo 19 `attachment.raw` devuelve bytes directamente. En Odoo 20
    devuelve un LocalBinaryFile —un manejador perezoso de archivo— cuyo
    contenido se obtiene de su propiedad `content`. El getattr cubre ambas
    sin ramificar por versión.
    """
    raw = adjunto.raw
    return getattr(raw, 'content', raw)


class CfdiDocument(models.Model):
    _inherit = 'cfdi.document'

    def _get_xml_root(self):
        """Devuelve la raíz del XML guardado de este comprobante."""
        self.ensure_one()
        if not self.xml_attachment_id:
            raise UserError(_("Este comprobante no tiene su XML adjunto."))
        try:
            return etree.fromstring(_bytes_de_adjunto(self.xml_attachment_id))
        except etree.XMLSyntaxError as e:
            raise UserError(_("El XML de este comprobante no se puede leer: %s", e)) from e

    def _leer_impuestos(self, nodo_concepto):
        """Saca traslados y retenciones de un concepto.

        Devuelve dos listas de dicts con impuesto (clave SAT), tasa y importe.
        La tasa se normaliza a porcentaje (0.160000 en el XML -> 16.0), que es
        como la maneja Odoo en account.tax.
        """
        traslados, retenciones = [], []
        for nodo_impuestos in nodo_concepto:
            if _local(nodo_impuestos.tag) != 'Impuestos':
                continue
            for grupo in nodo_impuestos:
                etiqueta = _local(grupo.tag)
                destino = None
                if etiqueta == 'Traslados':
                    destino = traslados
                elif etiqueta == 'Retenciones':
                    destino = retenciones
                if destino is None:
                    continue
                for imp in grupo:
                    tasa = _num(imp.get('TasaOCuota'))
                    destino.append({
                        'impuesto': imp.get('Impuesto') or '',
                        'tipo_factor': imp.get('TipoFactor') or 'Tasa',
                        'tasa': round(tasa * 100, 4),
                        'importe': _num(imp.get('Importe')),
                        'base': _num(imp.get('Base')),
                    })
        return traslados, retenciones

    def _parse_conceptos(self):
        """Lista de partidas del comprobante, en el orden del XML."""
        self.ensure_one()
        root = self._get_xml_root()

        nodo_conceptos = next((el for el in root if _local(el.tag) == 'Conceptos'), None)
        if nodo_conceptos is None:
            return []

        conceptos = []
        for nodo in nodo_conceptos:
            if _local(nodo.tag) != 'Concepto':
                continue
            traslados, retenciones = self._leer_impuestos(nodo)
            conceptos.append({
                'descripcion': nodo.get('Descripcion') or _("Sin descripción"),
                'cantidad': _num(nodo.get('Cantidad'), 1.0),
                'valor_unitario': _num(nodo.get('ValorUnitario')),
                'importe': _num(nodo.get('Importe')),
                'descuento': _num(nodo.get('Descuento')),
                'clave_prod_serv': nodo.get('ClaveProdServ') or '',
                'clave_unidad': nodo.get('ClaveUnidad') or '',
                'no_identificacion': nodo.get('NoIdentificacion') or '',
                'traslados': traslados,
                'retenciones': retenciones,
            })
        return conceptos

    # -------------------------------------------------------------------
    # Traducción de impuestos del SAT a impuestos de Odoo
    # -------------------------------------------------------------------

    @api.model
    def _señales_de_impuesto(self, impuesto):
        """Texto donde buscar a qué impuesto del SAT corresponde un account.tax.

        El nombre del impuesto NO basta. En la localización mexicana los
        nombres son del estilo "10% WH L", "8%" o "8% IEPS": no contienen
        "IVA" ni "ISR", así que buscar subcadenas en el nombre no desempata
        nada y gana el primero por secuencia. Eso mandaba la retención de ISR
        a la cuenta de IVA retenido y el IVA 8% fronterizo a la de IEPS, en
        ambos casos cuadrando al centavo y sin avisar.

        El grupo de impuesto sí lo codifica de forma explícita y estable: en
        `l10n_mx` los grupos son `tax_group_iva_16`, `tax_group_isr_ret_10`,
        `tax_group_ieps_8`, etc. Se usa el XML ID del grupo, que es
        independiente del idioma, y se completa con los nombres como respaldo
        para catálogos que no sean el mexicano.
        """
        partes = []
        grupo = impuesto.tax_group_id
        if grupo:
            datos = self.env['ir.model.data'].sudo().search([
                ('model', '=', 'account.tax.group'), ('res_id', '=', grupo.id),
            ], limit=1)
            if datos:
                partes.append(datos.name)
            partes.append(grupo.name or '')
        partes.append(impuesto.name or '')
        return ' '.join(partes).lower()

    @api.model
    def _buscar_impuesto_odoo(self, clave_sat, tasa, es_retencion, company):
        """Busca el account.tax de compra que corresponde a un impuesto del CFDI.

        La tasa y el signo no bastan: en `l10n_mx` hay cinco colisiones de tasa
        entre impuestos de compra distintos (dos al 0%, tres al 8%, tres al
        -10%...). Confundirlos manda el importe a la cuenta contable
        equivocada con la factura perfectamente cuadrada, que es el peor caso
        posible porque nadie se entera.

        Devuelve un recordset vacío si no hay equivalente configurado; entonces
        la partida se crea sin ese impuesto y se avisa, en vez de inventar uno.
        """
        importe = -tasa if es_retencion else tasa
        candidatos = self.env['account.tax'].search([
            ('company_id', 'parent_of', company.id),
            ('type_tax_use', '=', 'purchase'),
            ('amount_type', '=', 'percent'),
            ('amount', '>=', importe - 0.01),
            ('amount', '<=', importe + 0.01),
        ])
        if not candidatos:
            return self.env['account.tax']
        if len(candidatos) == 1:
            return candidatos

        nombre_sat = (NOMBRE_IMPUESTO.get(clave_sat) or '').lower()

        def puntuar(impuesto):
            señales = self._señales_de_impuesto(impuesto)
            puntos = 0
            if nombre_sat:
                if nombre_sat in señales:
                    puntos += 4
                else:
                    # Penaliza nombrar OTRO impuesto: un candidato que dice
                    # "ieps" cuando el SAT declaró IVA es activamente erróneo,
                    # peor que uno que no dice nada.
                    otros = {n.lower() for n in NOMBRE_IMPUESTO.values()} - {nombre_sat}
                    if any(o in señales for o in otros):
                        puntos -= 4
            # Retención vs. traslado. "ret"/"wh" (withholding) cubren el
            # catálogo mexicano en español y en inglés.
            hay_ret = 'ret' in señales or ' wh' in señales or señales.startswith('wh')
            puntos += 1 if es_retencion == hay_ret else -1
            return puntos

        mejor = max(candidatos, key=puntuar)
        if puntuar(mejor) < 0:
            # Todos los candidatos contradicen lo que declaró el SAT: es
            # preferible no poner impuesto (y avisar) que elegir el equivocado.
            return self.env['account.tax']
        return mejor

    @api.model
    def _buscar_impuesto_exento(self, company):
        """Busca el impuesto de compra que representa TipoFactor="Exento".

        Exento NO es lo mismo que tasa 0%: los dos aportan cero pesos, pero van
        a renglones distintos del reporte fiscal. Antes se descartaba en
        silencio —ni impuesto ni aviso— y como aporta cero, la verificación de
        totales tampoco lo detectaba: la factura cuadraba al centavo con un
        tratamiento fiscal equivocado.

        Se desempata por `l10n_mx_factor_type`, que es literalmente el
        TipoFactor del CFDI. Es mejor discriminador que el que usa el propio
        import de Odoo (un env.ref al XML ID del grupo `_tax_group_exe_0`),
        porque ese no existe en compañías sucursal: al instalar el catálogo en
        una compañía con padre, Odoo descarta los datos de plantilla y el
        grupo nunca llega a crearse, con lo que el exento se degrada a IVA 0%.
        """
        Impuesto = self.env['account.tax']
        if 'l10n_mx_factor_type' not in Impuesto._fields:
            return Impuesto
        return Impuesto.search([
            ('company_id', 'parent_of', company.id),
            ('type_tax_use', '=', 'purchase'),
            ('l10n_mx_factor_type', '=', 'Exento'),
        ], limit=1)

    def _impuestos_odoo_de_concepto(self, concepto, company):
        """Devuelve (recordset de account.tax, lista de avisos) para una partida."""
        impuestos = self.env['account.tax']
        avisos = []
        pares = (
            [(t, False) for t in concepto['traslados']]
            + [(r, True) for r in concepto['retenciones']]
        )
        for datos, es_retencion in pares:
            nombre = NOMBRE_IMPUESTO.get(datos['impuesto'], datos['impuesto'])

            if datos['tipo_factor'] == 'Exento':
                exento = self._buscar_impuesto_exento(company)
                if exento:
                    impuestos |= exento
                else:
                    avisos.append(_(
                        "El concepto declara %(nombre)s EXENTO y no hay un impuesto de "
                        "compra con factor «Exento» configurado. La partida se creará sin "
                        "él: ojo, exento y tasa 0%% no son lo mismo en el reporte fiscal.",
                        nombre=nombre))
                continue

            if datos['tipo_factor'] == 'Cuota':
                # Cuota es un importe fijo por unidad, no un porcentaje. Un
                # account.tax de tipo 'percent' no puede representarlo, y
                # tratarlo como tasa multiplicaría el importe por 100.
                avisos.append(_(
                    "El concepto declara %(nombre)s con factor «Cuota» (importe fijo por "
                    "unidad, no porcentaje). No se puede representar como impuesto "
                    "porcentual: captúralo a mano.", nombre=nombre))
                continue

            impuesto = self._buscar_impuesto_odoo(
                datos['impuesto'], datos['tasa'], es_retencion, company)
            if impuesto:
                impuestos |= impuesto
            else:
                avisos.append(_(
                    "No hay un impuesto de compra configurado equivalente a %(nombre)s "
                    "%(signo)s%(tasa)s%%. La partida se creará sin ese impuesto.",
                    nombre=nombre,
                    signo="-" if es_retencion else "",
                    tasa=datos['tasa'],
                ))
        return impuestos, avisos
