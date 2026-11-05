import csv
import io
import logging
import unicodedata

import requests

from odoo.addons.dsi_license_client.models.dsi_license_client_state import _param
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# El SAT publica la lista en este servidor, que por https no responde (la
# conexión se agota) y por http sí. Se intenta primero https por si lo
# arreglan y luego http. Los datos son públicos; lo que se pierde por http es
# la garantía de que no se alteraron en el camino.
DEFAULT_69B_URLS = (
    "https://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv",
    "http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv",
)
# (conexión, lectura). El archivo pesa ~4.5 MB; si el servidor ni siquiera
# acepta la conexión, conviene rendirse pronto y pasar a la siguiente dirección.
REQUEST_TIMEOUT = (10, 60)
CSV_ENCODINGS = ('utf-8-sig', 'cp1252', 'latin-1')

# Situaciones que ya NO son riesgo: el contribuyente desvirtuó la presunción
# ante el SAT o ganó en tribunales, así que sus operaciones se reconocieron
# como reales. Siguen en el listado completo del SAT (casi el 14% de sus
# filas), por eso no basta con que el RFC aparezca. Cualquier otra situación,
# incluida una que no se pudo clasificar, cuenta como riesgo: ante la duda, se
# avisa.
EFOS_CLEARED_SITUATIONS = ('desvirtuado', 'sentencia_favorable')

SITUATION_KEYWORDS = [
    ('presunto', 'presunto'),
    ('definitivo', 'definitivo'),
    ('desvirtuado', 'desvirtuado'),
    ('sentencia favorable', 'sentencia_favorable'),
    ('favorable', 'sentencia_favorable'),
]


def _normalize(text):
    text = (text or '').strip().lower()
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return text


class CfdiEfosEntry(models.Model):
    _name = 'dsi.cfdi.efos.entry'
    _description = "Contribuyente en la lista 69-B del SAT (EFOS)"
    _rec_name = 'rfc'

    rfc = fields.Char(string="RFC", required=True, index=True)
    name = fields.Char(string="Razón social")
    situation = fields.Selection([
        ('presunto', "Presunto"),
        ('definitivo', "Definitivo"),
        ('desvirtuado', "Desvirtuado"),
        ('sentencia_favorable', "Sentencia favorable"),
        ('other', "Otro / sin clasificar"),
    ], string="Situación")

    _rfc_uniq = models.Constraint('unique(rfc)', "Este RFC ya está en la lista 69-B cargada.")

    # -------------------------------------------------------------------
    # Carga de la lista (REQ: cruce contra 69-B, Fase 3 del plan)
    # -------------------------------------------------------------------

    @api.model
    def _cron_refresh_from_sat(self):
        """Intento de refresco automático, best-effort. La lista 69-B del SAT
        cambia de URL/formato con cierta frecuencia y puede no ser alcanzable
        desde todos los entornos de hosting — por eso nunca debe tumbar el
        cron ni bloquear nada; la vía confiable es la carga manual de CSV
        desde el asistente (Bóveda CFDI > Discrepancias > Lista 69-B)."""
        try:
            result = self._refresh_from_url()
            if not result.get('ok'):
                _logger.warning("Refresco automático de la lista 69-B falló: %s", result.get('error'))
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Refresco automático de la lista 69-B falló de forma inesperada")

    @api.model
    def _refresh_from_url(self, url=None):
        """Descarga la lista y la carga. Con una URL explícita o configurada
        en el parámetro 'dsi_cfdi_vault.efos_list_url' solo se intenta esa; si
        no, las del SAT en orden, hasta que una responda con un CSV legible."""
        url = url or _param(self.env, 'dsi_cfdi_vault.efos_list_url')
        errors = []
        for candidate in ((url,) if url else DEFAULT_69B_URLS):
            try:
                response = requests.get(candidate, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                count = self._import_csv_bytes(response.content)
            except (requests.RequestException, UserError) as e:
                # Un UserError aquí es una respuesta que no es el CSV (p. ej.
                # una página de mantenimiento). Salta antes de tocar la lista
                # cargada, así que se puede probar la siguiente dirección.
                errors.append(f"{candidate}: {e}")
                continue
            return {'ok': True, 'count': count, 'url': candidate}
        return {'ok': False, 'error': "; ".join(errors)}

    @api.model
    def _import_csv_bytes(self, csv_bytes):
        """Reemplaza la lista completa a partir de un CSV. Tolerante a encoding
        y a la posición exacta de columnas: detecta el encabezado buscando una
        columna que contenga 'rfc', en vez de asumir un índice fijo — el SAT
        ha cambiado el layout exacto de este archivo entre publicaciones."""
        text = None
        for encoding in CSV_ENCODINGS:
            try:
                text = csv_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise UserError(_(
                "No se pudo leer el archivo: no está en UTF-8, CP1252 ni Latin-1."))

        rows = list(csv.reader(io.StringIO(text)))
        header_idx = next(
            (i for i, row in enumerate(rows) if any('rfc' in _normalize(cell) for cell in row)),
            None,
        )
        if header_idx is None:
            raise UserError(_(
                "No se encontró una columna de RFC en el archivo. Verifica que sea el CSV "
                "de la lista 69-B publicada por el SAT (Artículo 69-B del CFF)."))

        header = [_normalize(cell) for cell in rows[header_idx]]
        rfc_col = next(i for i, h in enumerate(header) if 'rfc' in h)
        name_col = next((i for i, h in enumerate(header) if 'nombre' in h or 'contribuyente' in h), None)
        situation_col = next((i for i, h in enumerate(header) if 'situacion' in h), None)

        entries_by_rfc = {}
        for row in rows[header_idx + 1:]:
            if len(row) <= rfc_col:
                continue
            rfc = (row[rfc_col] or '').strip().upper()
            if not rfc or len(rfc) < 10:
                continue
            situation = 'other'
            if situation_col is not None and len(row) > situation_col:
                raw_situation = _normalize(row[situation_col])
                for keyword, code in SITUATION_KEYWORDS:
                    if keyword in raw_situation:
                        situation = code
                        break
            # El mismo RFC puede repetirse en el archivo del SAT (una fila por
            # cada actualización de su situación) — se conserva la última.
            entries_by_rfc[rfc] = {
                'rfc': rfc,
                'name': (row[name_col].strip() if name_col is not None and len(row) > name_col else False),
                'situation': situation,
            }

        if not entries_by_rfc:
            raise UserError(_("El archivo no contiene ningún RFC válido."))

        self.search([]).unlink()
        self.create(list(entries_by_rfc.values()))
        self.env['cfdi.document']._recompute_efos_risk_all()
        return len(entries_by_rfc)
