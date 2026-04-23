{
    'name': 'Kiosco de Producción',
    'version': '19.0.1.0.0',
    'category': 'Manufacturing',
    'summary': 'Kiosco de operarios para cierres parciales silenciosos y consola maestra de supervisión.',
    'description': """ """,
    'author': 'Obi',
    'website': 'https://www.obi-mx.com/',
    'depends': ['base', 'mrp', 'stock', 'hr'],
    'data': [
        'security/ir.model.access.csv',
        'views/kiosk_action.xml',
        'views/mrp_shift_declaration_views.xml',
        'views/mrp_production_views.xml',
        'views/mrp_workorder_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'kiosco_production/static/src/kiosk/**/*',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}