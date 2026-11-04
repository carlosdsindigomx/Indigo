{
    'name': "Bóveda CFDI · Descarga Masiva SAT",
    'summary': "Custodia de la e.firma y descarga automática de CFDI directamente del SAT (Fase 4)",
    'description': """
Bóveda CFDI · Descarga Masiva SAT
==================================
Extiende Bóveda CFDI con:

- Custodia cifrada de la e.firma (.cer/.key/contraseña) — la llave privada nunca se
  guarda en texto plano, y el módulo valida que el certificado y la llave hagan
  pareja y que el RFC coincida con la compañía antes de aceptarlos.
- Cliente del Servicio de Descarga Masiva de CFDI del SAT (autenticación, solicitud,
  verificación y descarga), firmando las peticiones SOAP con la e.firma.
- Procesamiento automático de los paquetes descargados hacia la Bóveda CFDI.
- Metadatos del SAT: se leen y se guardan (Bóveda CFDI › Metadatos del SAT), con el TXT adjunto a
  su solicitud para volver a leerlo. Dan el estatus (vigente o cancelado, con fecha de
  cancelación) a los CFDI de la Bóveda y el «Estado bóveda» a las facturas, también a las que no
  tienen su XML en la Bóveda. Una cancelación es definitiva. Refresco semanal de los últimos 90
  días de emitidos y recibidos, para ver las cancelaciones de lo ya descargado.
- «Actualizar SAT» automático en las facturas de cliente con solicitud de cancelación que los
  metadatos o la consulta pública ya dan por canceladas: si el SAT lo confirma, Odoo cancela la
  factura. Nunca en facturas de proveedor. Lo que no se pudo registrar queda como discrepancia.

⚠️ Fase de mayor riesgo del proyecto (custodia de la identidad legal del contribuyente
ante el SAT). Requiere revisión de seguridad externa antes de usarse con e.firma real
de producción — ver security/README dentro del módulo.
""",
    'version': '19.0.0.2.0',
    'category': 'Accounting/Localizations',
    'author': "DS Indigo",
    'license': 'OPL-1',
    'depends': ['dsi_cfdi_vault'],
    'data': [
        'security/sat_sync_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/sat_metadata_views.xml',
        'views/sat_credential_views.xml',
        'views/sat_download_request_views.xml',
        'views/sat_sync_schedule_views.xml',
        'views/sat_sync_menus.xml',
    ],
    'installable': True,
    'application': False,
}
