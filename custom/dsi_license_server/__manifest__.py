{
    'name': "DS Indigo · Servidor de Licencias",
    'summary': "Emite, valida, renueva y revoca licencias de los productos DS Indigo, ancladas al RFC del cliente",
    'version': '19.0.0.1.0',
    'category': 'Extra Tools',
    'author': "DS Indigo",
    'license': 'OPL-1',
    'depends': ['base', 'mail'],
    'data': [
        'security/dsi_license_server_security.xml',
        'security/ir.model.access.csv',
        'views/dsi_license_views.xml',
        'views/dsi_license_menus.xml',
    ],
    'installable': True,
    'application': True,
}
