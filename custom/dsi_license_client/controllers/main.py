from odoo import http
from odoo.http import request


class DsiLicenseClientController(http.Controller):

    @http.route('/dsi/license/v1/challenge', type='http', auth='public', methods=['GET'], csrf=False)
    def challenge(self, id=None, **kw):
        """El servidor de licencias llama aquí al recibir una solicitud de
        prueba: si esta base contesta el secreto que viajó en la solicitud,
        queda probado que la pidió quien controla esta dirección, no alguien
        que la escribió a mano."""
        secret = request.env['dsi.license.client.state'].sudo()._trial_challenge_secret(id)
        if not secret:
            raise request.not_found()
        return request.make_response(secret, headers=[
            ('Content-Type', 'text/plain; charset=utf-8'),
            ('Cache-Control', 'no-store'),
        ])
