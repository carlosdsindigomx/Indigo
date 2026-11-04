{
    'name': "Bóveda CFDI",
    'summary': "Repositorio de CFDI independiente de la contabilidad, con conciliación automática por UUID",
    'description': """
Bóveda CFDI
===========
Guarda todo CFDI recibido o emitido, esté o no contabilizado. Permite:

- Importar XML/ZIP de CFDI manualmente, con deduplicación por UUID.
- Conciliación automática contra facturas (account.move) por UUID.
- Tableros de discrepancias: CFDI sin factura, facturas sin CFDI, CFDI cancelados que siguen contabilizados,
  proveedores en la lista 69-B del SAT.
- Discrepancias guardadas con historial y marca de revisado (Discrepancias › Por revisar): emisores
  o receptores en la lista 69-B, avisos al convertir un CFDI en factura y estatus SAT distinto entre
  la Bóveda y Odoo. Las que dejan de aparecer se resuelven solas.
- Pagos (REP) ligados a las facturas que pagan: un renglón por documento relacionado (parcialidad,
  pagado, saldo insoluto), copia del XML en cada factura y, en las compras, el REP en su pestaña CFDI
  como «Pago recibido». Los de facturas que no están en Odoo, en su propia lista.
- Pagos (REP) recibidos ligados a su pago de Odoo, documento relacionado por documento relacionado:
  el pago (o el movimiento del banco conciliado directo) que aplicó el mismo importe a la factura,
  con copia del XML en el pago y su pestaña «Complementos de pago (REP)». Los casos ambiguos quedan
  «Por elegir» para el contador, en su propia lista.
- Discrepancia «Compra PPD pagada sin REP»: una por cada pago aplicado a una compra PPD sin su REP
  ligado, desde el día 6 del mes siguiente al pago (o a su movimiento del banco, si se conoce y es
  posterior; hora de México). Fecha de corte opcional con el parámetro
  dsi_cfdi_vault.ppd_sin_rep_desde (AAAA-MM-DD).
- Nómina: «Relacionar con póliza» (Acciones en la lista de CFDI) busca la póliza por fecha de pago,
  periodo y neto, liga los CFDI de nómina seleccionados y deja una copia de cada XML en la póliza.
- «Estado bóveda» en las facturas de clientes y proveedores, con los filtros «Diferencia SAT / bóveda»,
  «Discrepancia de estatus sin revisar» y «Cancelado en bóveda». Una compra cancelada en el SAT
  queda por revisar; el asiento no se toca.
- Verificación de estatus (vigente/cancelado) bajo demanda y automática (cron) contra el servicio público del SAT.
- Cruce contra la lista 69-B (EFOS): carga manual de CSV o refresco automático best-effort.
- Conciliación en cascada de 4 niveles: UUID exacto, RFC+total+fecha, sugerencias con score,
  y cola de asignación manual.
""",
    'version': '19.0.0.3.0',
    'category': 'Accounting/Localizations',
    'author': "DS Indigo",
    'license': 'OPL-1',
    'depends': ['account', 'l10n_mx_edi', 'mail', 'dsi_license_client'],
    'data': [
        'security/cfdi_vault_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/cfdi_efos_recompute.xml',
        'data/cfdi_payment_amounts.xml',
        'data/cfdi_payment_lines.xml',
        'data/cfdi_nomina.xml',
        'views/cfdi_inconsistency_views.xml',
        'views/cfdi_payment_line_views.xml',
        'views/cfdi_document_views.xml',
        'views/cfdi_efos_views.xml',
        'views/cfdi_efos_check_wizard_views.xml',
        'views/cfdi_document_type_views.xml',
        'views/cfdi_import_wizard_views.xml',
        'views/cfdi_match_suggestion_wizard_views.xml',
        'views/cfdi_nomina_poliza_wizard_views.xml',
        'views/account_move_views.xml',
        'views/rep_pago_views.xml',
        'views/cfdi_to_bill_actions.xml',
        'views/cfdi_vault_menus.xml',
    ],
    'installable': True,
    'application': True,
}
