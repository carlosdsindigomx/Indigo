{
    'name': 'SAT XML Auditor',
    'version': '1.0',
    'category': 'Accounting/Localizations',
    'summary': 'Importación, lectura y auditoría de archivos XML del SAT',
    'description': """""",
    'author': 'OBI',
    'depends': ['base', 'account', 'mail',],
    'assets': {
        'web.assets_backend': [
            'sat_xml_auditor/static/src/js/sat_list_controller.js',
            'sat_xml_auditor/static/src/xml/sat_list_buttons.xml',
        ],
    },
    'data': [
        'security/ir.model.access.csv',
        'views/sat_xml_import_wizard_views.xml',
        'views/sat_xml_raw_views.xml',
        'views/sat_hub_tenant_views.xml',
        'views/sat_xml_download_request_views.xml',
        'views/menus_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}