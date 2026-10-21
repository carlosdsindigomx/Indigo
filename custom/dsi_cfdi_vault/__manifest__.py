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
- Verificación de estatus (vigente/cancelado) bajo demanda y automática (cron) contra el servicio público del SAT.
- Cruce contra la lista 69-B (EFOS): carga manual de CSV o refresco automático best-effort.
- Conciliación en cascada de 4 niveles: UUID exacto, RFC+total+fecha, sugerencias con score,
  y cola de asignación manual.
""",
    'version': '19.0.0.1.0',
    'category': 'Accounting/Localizations',
    'author': "DS Indigo",
    'license': 'OPL-1',
    'depends': ['account', 'l10n_mx_edi', 'mail', 'dsi_license_client'],
    'data': [
        'security/cfdi_vault_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/cfdi_efos_recompute.xml',
        'views/cfdi_document_views.xml',
        'views/cfdi_efos_views.xml',
        'views/cfdi_efos_check_wizard_views.xml',
        'views/cfdi_document_type_views.xml',
        'views/cfdi_import_wizard_views.xml',
        'views/cfdi_match_suggestion_wizard_views.xml',
        'views/account_move_views.xml',
        'views/cfdi_to_bill_actions.xml',
        'views/cfdi_vault_menus.xml',
    ],
    'installable': True,
    'application': True,
}
