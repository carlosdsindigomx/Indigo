{
    'name': "DS Indigo · Cliente de Licencias",
    'summary': "Activa y valida localmente la licencia de los productos DS Indigo instalados en esta base",
    'version': '19.0.0.1.0',
    'category': 'Extra Tools',
    'author': "DS Indigo",
    'license': 'OPL-1',
    'depends': ['base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/dsi_license_client_state_data.xml',
        'data/ir_cron.xml',
        'views/dsi_license_client_views.xml',
        'views/dsi_license_client_notice_views.xml',
        'views/dsi_license_client_menus.xml',
    ],
    'installable': True,
    'application': False,
}
