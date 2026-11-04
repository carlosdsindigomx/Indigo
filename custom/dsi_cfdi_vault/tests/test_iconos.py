"""Los iconos de los botones en las vistas del ADD: la Bóveda, la descarga del
SAT y el cliente de licencias (los que estén instalados).

Existe porque Odoo 20 quitó Font Awesome: ahí el icono de un botón
(icon="...") es el nombre de un Material Symbols (payments, warning...), y un
fa-*, que no es un nombre de su fuente, sale roto. En 18 y 19 es al revés: un
icono que no empieza con fa- ni con oi- se toma como la ruta de una imagen. No
hay un valor que sirva en las tres ramas, así que las vistas difieren en eso, y
esta prueba, igual en las tres, revisa cada icono contra lo que pinta la
versión en curso.
"""
from lxml import etree

from odoo.tests.common import TransactionCase, tagged

try:
    # Odoo 20: los Material Symbols que trae su fuente.
    from odoo.addons.web.icons import ICONS
except ImportError:
    # 18 y 19: Font Awesome.
    ICONS = None


def _icono_valido(icono):
    """Si el web client de esta versión pinta `icono` como icono de botón."""
    if ICONS is None:
        return icono.startswith(('fa-', 'oi-'))
    return icono in ICONS or icono.startswith('oi_')


# Los módulos del ADD cuyas vistas se revisan.
MODULOS_DEL_ADD = ('dsi_cfdi_vault', 'dsi_cfdi_sat_sync', 'dsi_license_client')


@tagged('post_install', '-at_install')
class TestIconos(TransactionCase):

    def _instalados(self):
        return self.env['ir.module.module'].search([
            ('name', 'in', MODULOS_DEL_ADD), ('state', '=', 'installed')]).mapped('name')

    def test_los_botones_usan_iconos_de_esta_version(self):
        datos = self.env['ir.model.data'].search([
            ('module', 'in', MODULOS_DEL_ADD), ('model', '=', 'ir.ui.view')])
        iconos = []
        for dato in datos:
            arch = etree.fromstring(self.env['ir.ui.view'].browse(dato.res_id).arch)
            for nodo in arch.iter(etree.Element):
                if nodo.get('icon'):
                    iconos.append((dato.complete_name, nodo.get('icon')))
                if nodo.tag == 'attribute' and nodo.get('name') == 'icon':
                    iconos.append((dato.complete_name, (nodo.text or '').strip()))
        # Que la prueba no pase en vacío: los tres módulos ponen iconos en
        # botones.
        self.assertEqual({vista.split('.')[0] for vista, _icono in iconos},
                         set(MODULOS_DEL_ADD) & set(self._instalados()))
        self.assertEqual([(vista, icono) for vista, icono in iconos if not _icono_valido(icono)], [])
