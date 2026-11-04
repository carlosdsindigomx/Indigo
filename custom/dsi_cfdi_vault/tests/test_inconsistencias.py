"""Discrepancias guardadas (Discrepancias › Por revisar).

Existen porque las discrepancias eran listas calculadas al momento o mensajes
en el chatter: no quedaba cuándo apareció cada una ni quién la revisó, y una
que ya se había revisado volvía a salir igual al día siguiente. Estas pruebas
cuidan el ciclo de vida que decide todo lo demás:

  * el mismo hallazgo dos veces es un solo registro;
  * un revisado no se reabre mientras siga apareciendo;
  * lo que deja de aparecer se resuelve solo, aunque esté revisado: «revisado»
    es que alguien lo vio, «resuelto» que la causa ya no existe;
  * un resuelto que reaparece se reabre con mensaje (también un aviso al
    convertir);
  * si cambian los valores, nace otro registro y queda el historial;

quién ve y quién toca qué (multiempresa, Usuario de la Bóveda, que no puede
falsear la revisión ni la nota de otro), qué pasa si un gancho o la revisión
diaria chocan con otra transacción, además de los dos productores de esta
fase: la lista 69-B y los avisos al convertir.

Usa una empresa mexicana en pesos con el catálogo de México, como
test_folio_fiscal.py: la conversión pasa por el import de CFDI de Odoo.
"""
from datetime import date, datetime
from unittest.mock import patch

import psycopg2
from markupsafe import Markup

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency import _clave_del_hallazgo
from odoo.addons.dsi_license_client.tests.common import activar_licencia

from .test_conversion_factura import IMPUESTO_CUOTA, RFC_EMISOR, RFC_RECEPTOR, cfdi_xml
from .test_lista_69b import csv_sat

PRESUNTO, DEFINITIVO = 'PRE010101AA1', 'DEF010101AA1'
GRUPOS_BOVEDA = 'base.group_user,dsi_cfdi_vault.group_cfdi_vault_user'


def mensajes_que_registran(registros, campo):
    """Los mensajes del historial de `registros` que registran un cambio de
    `campo`: uno por cambio.

    Odoo 18 y 19 guardan cada cambio como un mail.tracking.value del mensaje.
    Odoo 20 lo pasó al módulo opcional mail_tracking: sin él, el cambio solo
    queda escrito en el cuerpo del mensaje, «antes → <b>después</b>
    <i>(Etiqueta del campo)</i>».
    """
    mensajes = registros.message_ids
    if 'tracking_value_ids' in mensajes._fields:
        return mensajes.filtered(lambda m: campo in m.tracking_value_ids.field_id.mapped('name'))
    etiqueta = registros.fields_get([campo], ['string'])[campo]['string']
    marca = Markup('<i>(%s)</i>') % etiqueta
    return mensajes.filtered(lambda m: marca in str(m.body))


@tagged('post_install', '-at_install')
class TestInconsistencias(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        mxn = cls.env.ref('base.MXN')
        mxn.active = True
        cls.company = cls.env['res.company'].create({
            'name': "Empresa MX de prueba",
            'country_id': cls.env.ref('base.mx').id,
            'currency_id': mxn.id,
        })
        cls.company.partner_id.vat = RFC_RECEPTOR
        cls.env['account.chart.template'].try_loading('mx', cls.company, install_demo=False)
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        activar_licencia(cls, cls.company)
        cls.empresa_b = cls.env['res.company'].create({'name': "Empresa B de prueba"})
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
        ], order='sequence, id', limit=1)
        mx = cls.env.ref('base.mx')
        cls.proveedor = cls.env['res.partner'].create({
            'name': "Proveedor de Prueba", 'vat': RFC_EMISOR, 'supplier_rank': 1, 'country_id': mx.id,
        })
        cls.usuario_a = new_test_user(
            cls.env, login='boveda-a', groups=GRUPOS_BOVEDA,
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.usuario_b = new_test_user(
            cls.env, login='boveda-b', groups=GRUPOS_BOVEDA,
            company_id=cls.empresa_b.id, company_ids=[Command.set(cls.empresa_b.ids)])
        cls.Inconsistencia = cls.env['dsi.cfdi.inconsistency']
        cls.efos = cls.env['dsi.cfdi.efos.entry']
        cls.contador = 0

    # -- Ayudantes -------------------------------------------------------

    def _cfdi(self, company=None, rfc_emisor=RFC_EMISOR, impuestos_xml='', amount_total=1000.0):
        type(self).contador += 1
        uuid = f"FFFFFFFF-3333-4000-8000-{self.contador:012d}"
        company = company or self.company
        xml = cfdi_xml(impuestos_xml=impuestos_xml).replace('{uuid}', uuid)
        adjunto = self.env['ir.attachment'].create({
            'name': f'{uuid}.xml', 'raw': xml.encode(), 'mimetype': 'text/xml',
        })
        doc = self.env['cfdi.document'].create({
            'company_id': company.id,
            'direction': 'received',
            'uuid': uuid,
            'move_type': 'I',
            'invoice_date': date(2026, 9, 10),
            'amount_total': amount_total,
            'currency_id': company.currency_id.id,
            'partner_rfc_issuer': rfc_emisor,
            'partner_name_issuer': "Proveedor de Prueba",
            'partner_rfc_receiver': RFC_RECEPTOR,
            'sat_status': 'valid',
            'xml_attachment_id': adjunto.id,
        })
        adjunto.write({'res_model': 'cfdi.document', 'res_id': doc.id})
        return doc

    def _hallazgo(self, doc, huella='boveda=cancelled|odoo=valid', **valores):
        return dict({
            'company_id': doc.company_id.id,
            'dedupe_key': _clave_del_hallazgo('sat_distinto', cfdi=doc),
            'fingerprint': huella,
            'cfdi_document_id': doc.id,
            'uuid': doc.uuid,
            'vault_value': "Cancelado",
            'odoo_value': "Vigente",
        }, **valores)

    def _sincronizar(self, docs, *hallazgos, source='sat_check'):
        """Una revisión de `docs` que encontró `hallazgos` (ninguno: ya no
        aparece nada en ellos)."""
        return self.Inconsistencia._sincronizar(
            'sat_distinto', [('cfdi_document_id', 'in', docs.ids)], list(hallazgos), source)

    def _registros(self, docs, kind='sat_distinto'):
        return self.Inconsistencia.search([('kind', '=', kind), ('cfdi_document_id', 'in', docs.ids)])

    def _confirmar_historial(self):
        """Lo que hace el commit: escribir los mensajes de seguimiento."""
        self.env.flush_all()
        self.env.cr.precommit.run()

    def _mensajes(self, registro):
        return " ".join(str(m.body) for m in registro.message_ids)

    def _como(self, usuario, registros):
        return registros.with_user(usuario).with_context(allowed_company_ids=usuario.company_ids.ids)

    def _revisiones_programadas(self):
        """Las veces que se pidió correr la revisión de discrepancias."""
        cron = self.env.ref('dsi_cfdi_vault.ir_cron_detect_inconsistencies')
        return self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])

    def _por_revisar(self, factura):
        if 'review_state' in factura._fields:
            return factura.review_state in ('todo', 'anomaly')
        return not factura.checked

    def _revisar_factura(self, factura):
        """El contador marca la factura como revisada en su lista."""
        if 'review_state' in factura._fields:
            factura.review_state = 'reviewed'
        else:
            factura.checked = True

    # -- Ciclo de vida -----------------------------------------------------

    def test_el_mismo_hallazgo_dos_veces_es_un_registro(self):
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        registro.last_seen = datetime(2026, 1, 1)
        primera_vez = registro.first_seen

        otra_vez = self._sincronizar(doc, self._hallazgo(doc), source='cron')

        self.assertEqual(otra_vez, registro)
        self.assertEqual(self._registros(doc), registro)
        self.assertGreater(registro.last_seen, datetime(2026, 1, 1), "se mueve «Vista por última vez»")
        self.assertEqual(registro.first_seen, primera_vez)
        self.assertEqual(registro.state, 'open')
        self.assertEqual(registro.source, 'sat_check', "el origen es el de la primera vez")
        self.assertEqual(registro.severity, 'critical')

    def test_lo_que_ya_no_aparece_se_resuelve_solo(self):
        doc, otro = self._cfdi(), self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        del_otro = self._sincronizar(otro, self._hallazgo(otro))
        self._confirmar_historial()

        self._sincronizar(doc, source='cron')
        self._confirmar_historial()

        self.assertEqual(registro.state, 'resolved')
        self.assertTrue(registro.resolved_date)
        self.assertIn("se resolvió sola", self._mensajes(registro))
        self.assertEqual(del_otro.state, 'open', "lo que no se revisó no se toca")

    def test_un_revisado_no_se_reabre_pero_se_resuelve_si_deja_de_aparecer(self):
        """«Revisado» es que alguien lo vio y lo dejó así; «resuelto», que la
        causa ya no existe. Mientras siga apareciendo no vuelve a Pendiente ni
        se duplica; cuando deja de aparecer se resuelve, y el mensaje dice
        quién lo había revisado. Si vuelve, se reabre como pendiente."""
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        self._como(self.usuario_a, registro).action_marcar_revisado()
        registro.last_seen = datetime(2026, 1, 1)
        self._confirmar_historial()

        self._sincronizar(doc, self._hallazgo(doc), source='cron')
        self.assertEqual(registro.state, 'reviewed', "alguien ya decidió dejarlo así")
        self.assertGreater(registro.last_seen, datetime(2026, 1, 1))
        self.assertEqual(self._registros(doc), registro)

        self._sincronizar(doc, source='cron')
        self._confirmar_historial()
        self.assertEqual(registro.state, 'resolved', "la causa ya no existe")
        self.assertTrue(registro.resolved_date)
        mensajes = self._mensajes(registro)
        self.assertIn("se resolvió sola", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario_a.name}", mensajes)

        self._sincronizar(doc, self._hallazgo(doc), source='cron')
        self.assertEqual(registro.state, 'open', "un resuelto que reaparece vuelve a Pendiente")
        self.assertFalse(registro.reviewed_by_id)
        self.assertFalse(registro.reviewed_date)

    def test_un_resuelto_que_reaparece_se_reabre_con_mensaje(self):
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        self._confirmar_historial()
        self._sincronizar(doc, source='cron')
        self._confirmar_historial()
        self.assertEqual(registro.state, 'resolved')

        otra_vez = self._sincronizar(doc, self._hallazgo(doc), source='cron')
        self._confirmar_historial()

        self.assertEqual(otra_vez, registro, "es el mismo registro, no uno nuevo")
        self.assertEqual(registro.state, 'open')
        self.assertFalse(registro.resolved_date)
        self.assertIn("Volvió a aparecer", self._mensajes(registro))

    def test_huella_distinta_es_otro_registro(self):
        """Si cambian los valores es otro hallazgo: el anterior queda resuelto
        con su fotografía y el historial muestra los dos."""
        doc = self._cfdi()
        viejo = self._sincronizar(doc, self._hallazgo(doc))
        nuevo = self._sincronizar(doc, self._hallazgo(
            doc, huella='boveda=cancelled|odoo=not_found', odoo_value="No encontrado"))

        self.assertNotEqual(nuevo, viejo)
        self.assertEqual(self._registros(doc), viejo | nuevo)
        self.assertEqual((viejo.state, nuevo.state), ('resolved', 'open'))
        self.assertEqual(viejo.odoo_value, "Vigente", "la fotografía del anterior no cambia")
        self._confirmar_historial()
        self.assertIn("Cambiaron los valores", self._mensajes(viejo))

    def test_choque_al_crear_no_rompe_la_transaccion(self):
        """Si al crear choca con la restricción única y el registro que ya
        está se ve desde esta transacción, el choque se deshace en su
        savepoint y se queda con ese registro. (Si no lo ve, ver la prueba
        siguiente.)"""
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))

        mismo = self.Inconsistencia._crear_o_encontrar(
            [dict(self._hallazgo(doc), kind='sat_distinto', source='cron')])

        self.assertEqual(mismo, registro)
        self.assertEqual(self._registros(doc), registro)

        # En un lote, el que choca no impide crear los demás.
        nuevo = self._cfdi()
        lote = self.Inconsistencia._crear_o_encontrar([
            dict(self._hallazgo(doc), kind='sat_distinto', source='cron'),
            dict(self._hallazgo(nuevo), kind='sat_distinto', source='cron'),
        ])
        self.assertEqual(lote[0], registro)
        self.assertEqual(lote[1], self._registros(nuevo))
        self.assertEqual(len(lote), 2)
        # La transacción sigue sana: se puede seguir leyendo y escribiendo.
        otro = self._cfdi()
        self.assertTrue(self._sincronizar(otro, self._hallazgo(otro)))
        self.assertEqual(registro.state, 'open')

    def test_choque_con_un_registro_que_esta_transaccion_no_ve(self):
        """El cron y el gancho de un CFDI detectan lo mismo a la vez y el otro
        confirma primero: esta transacción choca con la restricción única pero
        no ve ese registro (REPEATABLE READ). Antes devolvía vacío como si no
        hubiera nada; ahora es un choque con otra transacción: el gancho lo
        deshace, deja un aviso sin traceback y programa la revisión. Aquí la
        búsqueda que no lo ve se simula; con dos transacciones reales se
        comprobó aparte."""
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        Modelo = type(self.Inconsistencia)

        def no_lo_ve(modelo, *_args, **_kwargs):
            return modelo.browse()

        with patch.object(Modelo, 'search', no_lo_ve), self.assertRaises(psycopg2.errors.UniqueViolation):
            self.Inconsistencia._crear_o_encontrar([dict(self._hallazgo(doc), kind='sat_distinto', source='cron')])

        programadas = self._revisiones_programadas()
        with patch.object(Modelo, 'search', no_lo_ve), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency', level='WARNING') as log:
            self.Inconsistencia._sincronizar_desde_gancho(
                '_sincronizar', 'sat_distinto', [('cfdi_document_id', '=', doc.id)], [self._hallazgo(doc)], 'link')
        self.assertEqual([r.levelname for r in log.records], ['WARNING'])
        self.assertIn("UniqueViolation", log.output[0])
        self.assertIsNone(log.records[0].exc_info, "sin traceback")
        self.assertEqual(self._revisiones_programadas(), programadas + 1)
        self.assertEqual(self._registros(doc), registro)
        self.assertEqual(registro.state, 'open')

    # -- Quién ve y quién toca qué ---------------------------------------

    def test_cada_empresa_ve_solo_las_suyas(self):
        doc_a, doc_b = self._cfdi(), self._cfdi(company=self.empresa_b)
        de_a = self._sincronizar(doc_a, self._hallazgo(doc_a))
        de_b = self._sincronizar(doc_b, self._hallazgo(doc_b))

        visibles = self._como(self.usuario_b, self.Inconsistencia).search([])
        self.assertIn(de_b, visibles)
        self.assertNotIn(de_a, visibles)
        with self.assertRaises(AccessError):
            self._como(self.usuario_b, de_a).read(['state'])

    def test_el_usuario_de_la_boveda_revisa_pero_no_crea_ni_borra(self):
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        como_usuario = self._como(self.usuario_a, registro)

        antes = fields.Datetime.now()
        como_usuario.action_marcar_revisado()
        self.assertEqual(registro.state, 'reviewed')
        self.assertEqual(registro.reviewed_by_id, self.usuario_a)
        self.assertGreaterEqual(registro.reviewed_date, antes, "la fecha la pone el servidor")
        como_usuario.review_note = "Ya se habló con el proveedor."
        self.assertEqual(registro.review_note, "Ya se habló con el proveedor.")

        como_usuario.action_reabrir()
        self.assertEqual(registro.state, 'open')
        self.assertFalse(registro.reviewed_by_id)

        with self.assertRaises(AccessError):
            como_usuario.write({'observation': "Otra cosa"})
        with self.assertRaises(AccessError):
            como_usuario.unlink()
        with self.assertRaises(AccessError):
            self._como(self.usuario_a, self.Inconsistencia).create(dict(
                self._hallazgo(doc, huella='otra'), kind='sat_distinto'))

        # Una de otra empresa no la puede revisar.
        doc_b = self._cfdi(company=self.empresa_b)
        de_b = self._sincronizar(doc_b, self._hallazgo(doc_b))
        with self.assertRaises(AccessError):
            self._como(self.usuario_a, de_b).action_marcar_revisado()
        self.assertEqual(de_b.state, 'open')

    def test_el_usuario_de_la_boveda_no_puede_falsear_la_revision(self):
        """El estado, quién revisó y cuándo solo cambian con «Marcar como
        revisado» y «Reabrir»: por RPC no puede poner a otro como revisor, una
        fecha cualquiera ni darla por resuelta. Antes sí podía."""
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        como_usuario = self._como(self.usuario_a, registro)
        admin = self.env.ref('base.user_admin')

        for valores in (
            {'state': 'reviewed', 'reviewed_by_id': admin.id, 'reviewed_date': '2020-01-01 00:00:00'},
            {'state': 'reviewed'},
            {'state': 'resolved'},
            {'reviewed_by_id': admin.id},
            {'reviewed_date': '2020-01-01 00:00:00'},
            {'review_note': "Nota", 'state': 'resolved'},
        ):
            with self.subTest(valores=valores), self.assertRaises(AccessError):
                como_usuario.write(valores)
        self.assertEqual(registro.state, 'open')
        self.assertFalse(registro.reviewed_by_id)

        como_usuario.action_marcar_revisado()
        with self.assertRaises(AccessError):
            como_usuario.write({'reviewed_by_id': admin.id, 'reviewed_date': '2020-01-01 00:00:00'})
        self.assertEqual(registro.reviewed_by_id, self.usuario_a)
        self.assertGreater(registro.reviewed_date, datetime(2020, 1, 1))

    def test_la_nota_de_una_revision_es_de_quien_la_hizo(self):
        """Otro Usuario de la Bóveda podía reescribir la nota de una revisión
        ajena sin dejar rastro: el registro decía «Revisado por A» con un
        texto que A no escribió. Ahora, una vez revisada, solo quien la revisó
        cambia la nota (otro la reabre para dejar la suya) y cada cambio queda
        en el historial."""
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        otro = new_test_user(
            self.env, login='boveda-c', groups=GRUPOS_BOVEDA,
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        como_a, como_otro = self._como(self.usuario_a, registro), self._como(otro, registro)

        # Mientras está pendiente, cualquiera escribe la nota.
        como_otro.review_note = "Lo veo mañana."
        self._confirmar_historial()
        como_a.review_note = "El proveedor confirmó la cancelación; se canceló el pago."
        como_a.action_marcar_revisado()
        self._confirmar_historial()
        with self.assertRaises(AccessError):
            como_otro.write({'review_note': "Todo en orden, no hacer nada."})
        self.assertEqual(registro.review_note, "El proveedor confirmó la cancelación; se canceló el pago.")
        como_a.review_note = "El proveedor confirmó la cancelación; se canceló el pago el 3 de octubre."
        self._confirmar_historial()
        notas = mensajes_que_registran(registro, 'review_note')
        self.assertEqual(len(notas), 3, "cada cambio de la nota queda en el historial")
        self.assertEqual(set(notas.author_id.ids), {otro.partner_id.id, self.usuario_a.partner_id.id})

        # Otro la reabre (queda en el historial) y deja la suya al revisarla.
        como_otro.action_reabrir()
        como_otro.review_note = "Se revisó otra vez con el proveedor."
        como_otro.action_marcar_revisado()
        self.assertEqual(registro.reviewed_by_id, otro)

        # Resuelta, la nota sigue siendo de quien la revisó. El gestor contable puede todo.
        self._sincronizar(doc, source='cron')
        self.assertEqual(registro.state, 'resolved')
        with self.assertRaises(AccessError):
            como_a.write({'review_note': "Otra cosa"})
        gestor = new_test_user(
            self.env, login='gestor-nota', groups=GRUPOS_BOVEDA + ',account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        self._como(gestor, registro).review_note = "Corregida por el gestor."
        self.assertEqual(registro.review_note, "Corregida por el gestor.")

    def test_el_gestor_contable_puede_todo(self):
        doc = self._cfdi()
        registro = self._sincronizar(doc, self._hallazgo(doc))
        gestor = new_test_user(
            self.env, login='gestor-boveda', groups=GRUPOS_BOVEDA + ',account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])

        self._como(gestor, registro).write({'observation': "Corregida a mano"})
        self.assertEqual(registro.observation, "Corregida a mano")
        self._como(gestor, registro).write({'state': 'resolved', 'reviewed_by_id': gestor.id})
        self.assertEqual((registro.state, registro.reviewed_by_id), ('resolved', gestor))
        self._como(gestor, registro).unlink()
        self.assertFalse(registro.exists())

    def test_la_revision_en_lote_solo_toca_las_pendientes(self):
        doc, otro = self._cfdi(), self._cfdi()
        pendiente = self._sincronizar(doc, self._hallazgo(doc))
        resuelta = self._sincronizar(otro, self._hallazgo(otro))
        self._sincronizar(otro)
        self.assertEqual(resuelta.state, 'resolved')

        self._como(self.usuario_a, pendiente | resuelta).action_marcar_revisado()

        self.assertEqual(pendiente.state, 'reviewed')
        self.assertEqual(resuelta.state, 'resolved', "una resuelta no necesita revisión")

    # -- Choques con otra transacción ------------------------------------

    def test_un_choque_en_un_gancho_no_tumba_al_que_llama(self):
        """Un gancho (vincular, verificar o convertir un CFDI) sincroniza en su
        propio savepoint. Si choca con otra transacción, por ejemplo la
        revisión diaria corriendo a la vez, se deshace solo su parte, queda un
        aviso sin traceback y se programa la revisión de discrepancias. La
        transacción de quien llamó sigue sana."""
        doc, otro = self._cfdi(), self._cfdi()
        registro = self._sincronizar(otro, self._hallazgo(otro))
        programadas = self._revisiones_programadas()

        def sincroniza_y_choca(modelo, *args, **kwargs):
            modelo._sincronizar('sat_distinto', [('cfdi_document_id', '=', doc.id)], [self._hallazgo(doc)], 'link')
            modelo.search([('cfdi_document_id', '=', otro.id)]).write({'review_note': "a medias"})
            raise psycopg2.errors.SerializationFailure("could not serialize access due to concurrent update")

        with patch.object(type(self.Inconsistencia), '_sincronizar_estado_sat', sincroniza_y_choca), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency', level='WARNING') as log:
            self.Inconsistencia._sincronizar_desde_gancho('_sincronizar_estado_sat', doc.move_id, 'link')

        self.assertEqual([r.levelname for r in log.records], ['WARNING'])
        self.assertIn("SerializationFailure", log.output[0])
        self.assertIsNone(log.records[0].exc_info, "sin traceback")
        self.assertFalse(self._registros(doc), "lo del gancho se deshizo")
        self.assertFalse(registro.review_note)
        self.assertEqual(self._revisiones_programadas(), programadas + 1)
        # La transacción sigue sana.
        self.assertTrue(self._sincronizar(doc, self._hallazgo(doc)))

    def test_lo_pendiente_de_quien_llama_no_es_un_choque_del_gancho(self):
        """Lo que quien llama tiene pendiente se escribe antes del savepoint
        del gancho. Si eso falla, el error es suyo y le llega tal cual. Antes
        fallaba dentro del try, sin savepoint al cual volver: el gancho lo
        tomaba por un choque propio (aviso falso), programaba la revisión
        sobre la transacción abortada y quien llamó recibía «current
        transaction is aborted» en vez de su error (con dos transacciones
        reales, un SerializationFailure que Odoo reintentaría). Aquí el
        pendiente es un UUID que ya tiene otro CFDI."""
        doc, otro = self._cfdi(), self._cfdi()
        doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        programadas = self._revisiones_programadas()

        with self.assertNoLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency', level='WARNING'), \
                mute_logger('odoo.sql_db'), self.assertRaises(psycopg2.errors.UniqueViolation), \
                self.env.cr.savepoint():
            # Archivarlo pasa por el gancho; el UUID repetido se escribe al flush.
            doc.write({'uuid': otro.uuid, 'active': False})

        self.assertEqual(self._revisiones_programadas(), programadas)
        self.assertTrue(doc.active)

    def test_un_choque_en_la_revision_diaria_es_un_aviso(self):
        """Si un detector choca con otra transacción no es una falla: los demás
        siguen, queda un aviso sin traceback y la revisión se vuelve a
        programar. Otra excepción sí se registra como error."""
        programadas = self._revisiones_programadas()
        corrieron = []

        def detector(nombre, error=None):
            def correr(_modelo):
                corrieron.append(nombre)
                if error:
                    raise error
            return correr

        detectores = {
            '_detectar_riesgo_69b': detector('69b', psycopg2.errors.SerializationFailure("concurrent update")),
            '_detectar_avisos_de_conversion': detector('avisos'),
            '_detectar_estado_sat': detector('estado'),
        }
        with patch.multiple(type(self.Inconsistencia), **detectores), \
                self.assertLogs('odoo.addons.dsi_cfdi_vault.models.cfdi_inconsistency', level='WARNING') as log:
            self.Inconsistencia._cron_detectar_inconsistencias()

        self.assertEqual(corrieron, ['69b', 'avisos', 'estado'])
        self.assertEqual([r.levelname for r in log.records], ['WARNING'])
        self.assertIn("_detectar_riesgo_69b", log.output[0])
        self.assertEqual(self._revisiones_programadas(), programadas + 1)

    # -- Avisos al convertir ---------------------------------------------

    def test_convertir_con_cuota_registra_el_aviso(self):
        """IEPS por cuota no se puede representar como impuesto porcentual: el
        aviso queda en el chatter, la factura por revisar y, ahora, como
        discrepancia. El total no cuadra (falta el IEPS) y es un segundo aviso."""
        doc = self._cfdi(impuestos_xml=IMPUESTO_CUOTA, amount_total=1019.90)
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)

        avisos = self.Inconsistencia.search([('kind', '=', 'aviso_conversion'), ('move_id', '=', factura.id)])
        self.assertEqual(len(avisos), 2)
        cuota = avisos.filtered(lambda r: r.fingerprint.startswith('impuesto:'))
        total = avisos - cuota
        self.assertIn("Cuota", cuota.observation)
        self.assertIn("1,019.90", total.vault_value)
        self.assertIn("1,000.00", total.odoo_value)
        self.assertEqual(set(avisos.mapped('state')), {'open'})
        self.assertEqual(set(avisos.mapped('severity')), {'warning'})
        self.assertEqual(set(avisos.mapped('source')), {'conversion'})
        self.assertEqual(avisos.cfdi_document_id, doc)
        self.assertEqual(avisos.partner_id, self.proveedor)
        self.assertEqual(set(avisos.mapped('uuid')), {doc.uuid})

        # Lo de antes se queda: el mensaje en el chatter y la marca por revisar.
        self.assertIn("Cuota", " ".join(str(m.body) for m in factura.message_ids))
        if 'review_state' in factura._fields:
            self.assertEqual(factura.review_state, 'todo')
        else:
            self.assertFalse(factura.checked)

        # Los botones de la factura y del CFDI las cuentan.
        self.assertEqual(factura.dsi_inconsistency_count, 2)
        self.assertEqual(doc.dsi_inconsistency_count, 2)
        accion = factura.action_view_inconsistencies()
        self.assertEqual(self.Inconsistencia.search(accion['domain']), avisos)
        self.assertEqual(accion['context'], {'search_default_filter_open': 1})

    def test_la_revision_diaria_resuelve_los_avisos_que_dejaron_de_importar(self):
        doc = self._cfdi(impuestos_xml=IMPUESTO_CUOTA, amount_total=1019.90)
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        avisos = self.Inconsistencia.search([('kind', '=', 'aviso_conversion'), ('move_id', '=', factura.id)])
        cuota = avisos.filtered(lambda r: r.fingerprint.startswith('impuesto:'))
        total = avisos - cuota

        # El contador captura a mano el IEPS: el total ya cuadra.
        factura.write({'invoice_line_ids': [Command.create({
            'name': "IEPS por cuota", 'quantity': 1, 'price_unit': 19.90, 'tax_ids': [Command.clear()],
        })]})
        self.assertAlmostEqual(factura.amount_total, 1019.90)
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(total.state, 'resolved')
        self.assertEqual(cuota.state, 'open', "nada en la factura dice si ya se capturó el impuesto")

        factura.button_cancel()
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(cuota.state, 'resolved', "con la factura cancelada ya no importa")

    def test_un_aviso_resuelto_vuelve_si_la_causa_regresa(self):
        """Como cualquier resuelto que reaparece: la factura cancelada se
        vuelve a publicar (el IEPS sigue sin capturar y el total sin cuadrar)
        o el total deja de cuadrar otra vez. Se reabren con su mensaje y la
        compra vuelve a «Por revisar». Antes la revisión diaria solo miraba
        los no resueltos y nunca volvían."""
        doc = self._cfdi(impuestos_xml=IMPUESTO_CUOTA, amount_total=1019.90)
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        avisos = self.Inconsistencia.search([('kind', '=', 'aviso_conversion'), ('move_id', '=', factura.id)])
        cuota = avisos.filtered(lambda r: r.fingerprint.startswith('impuesto:'))
        total = avisos - cuota
        self._como(self.usuario_a, cuota).action_marcar_revisado()

        factura.button_cancel()
        self.Inconsistencia._cron_detectar_inconsistencias()
        self._confirmar_historial()
        self.assertEqual(set(avisos.mapped('state')), {'resolved'})

        factura.button_draft()
        factura.action_post()
        self._revisar_factura(factura)
        self.Inconsistencia._cron_detectar_inconsistencias()
        self._confirmar_historial()
        self.assertEqual(set(avisos.mapped('state')), {'open'})
        self.assertFalse(cuota.reviewed_by_id, "quién la revisó quedó en el mensaje con que se resolvió")
        for aviso in avisos:
            self.assertIn("Volvió a aparecer", self._mensajes(aviso))
        self.assertTrue(self._por_revisar(factura), "vuelve a «Por revisar», como al convertirla")
        self.assertEqual(
            self.Inconsistencia.search([('kind', '=', 'aviso_conversion'), ('move_id', '=', factura.id)]), avisos,
            "son los mismos registros")

        # El total cuadra (se capturó el IEPS) y después deja de cuadrar.
        factura.button_draft()
        ieps = Command.create({'name': "IEPS por cuota", 'quantity': 1, 'price_unit': 19.90,
                               'tax_ids': [Command.clear()]})
        factura.write({'invoice_line_ids': [ieps]})
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual((total.state, cuota.state), ('resolved', 'open'))
        factura.invoice_line_ids.filtered(lambda l: l.name == "IEPS por cuota").unlink()
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(total.state, 'open')

    def test_compra_con_avisos_sigue_por_revisar_al_publicarla(self):
        """La conversión deja la compra con avisos «Por revisar», pero en
        borrador, y al publicarla Odoo la daba por revisada: la marca se
        perdía y el filtro «Por revisar» de Odoo (que no muestra borradores)
        nunca la enseñaba. Ahora se le vuelve a poner mientras algún aviso
        siga pendiente en la Bóveda. Si ya los revisaron, no."""
        doc = self._cfdi(impuestos_xml=IMPUESTO_CUOTA, amount_total=1019.90)
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        self.assertTrue(self._por_revisar(factura))
        factura.action_post()
        self.assertTrue(self._por_revisar(factura))

        otro = self._cfdi(impuestos_xml=IMPUESTO_CUOTA, amount_total=1019.90)
        otra = otro._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        self._como(self.usuario_a, self.Inconsistencia.search([('move_id', '=', otra.id)])).action_marcar_revisado()
        otra.action_post()
        self.assertFalse(self._por_revisar(otra))

    def test_sin_avisos_no_registra_nada(self):
        doc = self._cfdi()
        factura = doc._crear_factura_proveedor(journal=self.journal, partner=self.proveedor)
        self.assertFalse(self.Inconsistencia.search([('move_id', '=', factura.id)]))

    # -- Lista 69-B --------------------------------------------------------

    def test_cargar_la_lista_69b_registra_el_riesgo_y_desvirtuado_lo_resuelve(self):
        doc = self._cfdi(rfc_emisor=PRESUNTO)
        limpio = self._cfdi()

        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Presunto')]))
        presunto = self._registros(doc, 'riesgo_69b')
        self.assertEqual(len(presunto), 1)
        self.assertEqual(presunto.state, 'open')
        self.assertEqual(presunto.fingerprint, f"{PRESUNTO}=presunto")
        self.assertEqual(presunto.source, 'efos')
        self.assertIn("Presunto", presunto.vault_value)
        self.assertIn(PRESUNTO, presunto.observation)
        self.assertFalse(self._registros(limpio, 'riesgo_69b'))

        # Volver a cargar la misma lista no duplica.
        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Presunto')]))
        self.assertEqual(self._registros(doc, 'riesgo_69b'), presunto)

        # Presunto que pasa a definitivo: otro registro, y el anterior resuelto.
        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Definitivo')]))
        definitivo = self._registros(doc, 'riesgo_69b') - presunto
        self.assertEqual(definitivo.fingerprint, f"{PRESUNTO}=definitivo")
        self.assertEqual((presunto.state, definitivo.state), ('resolved', 'open'))

        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Desvirtuado')]))
        self.assertEqual(definitivo.state, 'resolved')
        self.assertEqual(doc.dsi_inconsistency_count, 0)

    def test_la_revision_del_periodo_registra_facturas_sin_cfdi(self):
        """El asistente revisa también las facturas de proveedor sin CFDI en la
        Bóveda; lo que encuentra ya no se pierde al cerrarlo. La que sí tiene
        su CFDI en riesgo no se registra dos veces."""
        self.efos._import_csv_bytes(csv_sat([(DEFINITIVO, 'Definitivo')]))
        proveedor = self.env['res.partner'].create({
            'name': "Proveedor en la lista", 'vat': DEFINITIVO, 'is_company': True,
            'country_id': self.env.ref('base.mx').id,
        })

        def factura():
            return self.env['account.move'].create({
                'move_type': 'in_invoice',
                'company_id': self.company.id,
                'partner_id': proveedor.id,
                'invoice_date': date(2026, 9, 15),
                'invoice_line_ids': [Command.create({'name': "Servicio", 'quantity': 1, 'price_unit': 100.0})],
            })

        sin_cfdi, con_cfdi = factura(), factura()
        doc = self._cfdi(rfc_emisor=DEFINITIVO)
        doc.move_id = con_cfdi
        self.Inconsistencia._sincronizar_riesgo_69b(cfdis=doc)

        wizard = self.env['dsi.cfdi.efos.check.wizard'].create({
            'company_id': self.company.id,
            'date_from': date(2026, 9, 1),
            'date_to': date(2026, 9, 30),
            'refresh_list': False,
        })
        wizard.action_check()

        de_facturas = self.Inconsistencia.search([
            ('kind', '=', 'riesgo_69b'), ('cfdi_document_id', '=', False),
            ('move_id', 'in', (sin_cfdi | con_cfdi).ids),
        ])
        self.assertEqual(de_facturas.move_id, sin_cfdi)
        self.assertEqual(de_facturas.partner_id, proveedor)
        self.assertEqual(de_facturas.fingerprint, f"{DEFINITIVO}=definitivo")
        self.assertEqual(self._registros(doc, 'riesgo_69b').state, 'open')
        # La del CFDI también es de su factura: el botón de la factura la cuenta.
        self.assertEqual(con_cfdi.dsi_inconsistency_count, 1)

        # Se desvirtúa: la recarga resuelve la del CFDI y la revisión diaria,
        # la de la factura.
        self.efos._import_csv_bytes(csv_sat([(DEFINITIVO, 'Desvirtuado')]))
        self.assertEqual(self._registros(doc, 'riesgo_69b').state, 'resolved')
        self.assertEqual(de_facturas.state, 'open')
        self.Inconsistencia._cron_detectar_inconsistencias()
        self.assertEqual(de_facturas.state, 'resolved')

    def test_un_riesgo_69b_revisado_se_resuelve_al_desvirtuarse(self):
        """Revisado no es resuelto: el presunto que alguien revisó se resuelve
        cuando pasa a definitivo (cambió: lo sustituye otro registro) y el
        definitivo revisado, cuando el RFC se desvirtúa. Antes el revisado
        quedaba vivo para siempre."""
        doc = self._cfdi(rfc_emisor=PRESUNTO)
        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Presunto')]))
        presunto = self._registros(doc, 'riesgo_69b')
        self._como(self.usuario_a, presunto).action_marcar_revisado()
        self._confirmar_historial()

        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Definitivo')]))
        self._confirmar_historial()
        definitivo = self._registros(doc, 'riesgo_69b') - presunto
        self.assertEqual((presunto.state, definitivo.state), ('resolved', 'open'))
        mensajes = self._mensajes(presunto)
        self.assertIn("Cambiaron los valores", mensajes)
        self.assertIn(f"Estaba revisada por {self.usuario_a.name}", mensajes)

        self._como(self.usuario_a, definitivo).action_marcar_revisado()
        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Desvirtuado')]))
        self._confirmar_historial()
        self.assertFalse(doc.is_efos_risk)
        self.assertEqual(definitivo.state, 'resolved')
        self.assertIn("se resolvió sola", self._mensajes(definitivo))
        self.assertFalse(self._registros(doc, 'riesgo_69b').filtered(lambda r: r.state != 'resolved'))

    def test_la_revision_diaria_registra_los_cfdi_nuevos_en_riesgo(self):
        """Un CFDI que llega después de cargar la lista ya sale en rojo, pero su
        registro lo crea la revisión diaria."""
        self.efos._import_csv_bytes(csv_sat([(PRESUNTO, 'Presunto')]))
        doc = self._cfdi(rfc_emisor=PRESUNTO)
        self.assertTrue(doc.is_efos_risk)

        self.Inconsistencia._cron_detectar_inconsistencias()

        registro = self._registros(doc, 'riesgo_69b')
        self.assertEqual(registro.state, 'open')
        self.assertEqual(registro.source, 'cron')
