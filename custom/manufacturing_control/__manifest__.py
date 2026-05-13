{
    'name': 'Control de Manufactura',
    'version': '19.0.1.1.0',
    'category': 'Manufacturing',
    'summary': 'Panel ejecutivo de producción',
    'description': """ """,
    'author': 'Obi',
    'website': 'https://www.obi-mx.com/',
    'depends': ['base', 'mrp', 'kiosco_production'],
    'data': [
        'security/ir.model.access.csv',
        'views/mrp_dashboard_action.xml',
        'views/mrp_master_order_views.xml',
        'views/mrp_declarations_views.xml',
        'views/mrp_shift_views.xml',
        'views/mrp_master_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'manufacturing_control/static/src/css/mfg_dashboard.css',
            'manufacturing_control/static/src/components/mfg_dashboard/mfg_dashboard.js',
            'manufacturing_control/static/src/components/mfg_dashboard/mfg_dashboard.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
