{
    'name': 'Control de Manufactura',
    'version': '19.0.1.0.0',
    'category': 'Manufacturing',
    'summary': 'Kiosko de operarios para cierres parciales silenciosos y consola maestra de supervisión.',
    'description': """ """,
    'author': 'Obi',
    'website': 'https://www.obi-mx.com/',
    'depends': ['base','mrp'],
    'data': [
        'security/ir.model.access.csv',
        'views/mrp_master_order_views.xml',
        'views/mrp_master_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}