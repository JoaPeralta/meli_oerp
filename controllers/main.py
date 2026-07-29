# -*- coding: utf-8 -*-

import base64

from odoo import http, api


from odoo import fields, http
from odoo.http import Controller, Response, request, route
from odoo.exceptions import AccessError
try:
    from odoo.http import content_disposition
except ImportError:
    from odoo.addons.web.controllers.main import content_disposition
    pass;
import json
import sys
import pprint
pp = pprint.PrettyPrinter(indent=4)

import pdb
import logging
import secrets
# Con guion bajo a proposito. Mas abajo este modulo hace `from ..models.versions
# import *`, y versions hace `from datetime import *`, con lo cual el nombre
# `time` termina apuntando a la CLASE datetime.time y `time.time()` revienta.
# Los import wildcard omiten los nombres que empiezan con guion bajo, asi que
# este alias no lo puede pisar ninguno.
from time import time as _clock_seconds
_logger = logging.getLogger(__name__)

# El intento de OAuth vive en models/meli_util.py: lo crean DOS iniciadores
# distintos -esta ruta y el boton res.company.meli_login()- y con una copia en
# cada lado terminarian existiendo dos formatos que el callback no entenderia
# por igual.



from ..models.versions import *
from ..models.meli_util import (
    meli_oauth_attempt_consume,
    meli_oauth_attempt_issue,
    meli_token_expiry_vals,
    meli_token_response_summary,
    meli_validate_refresh_response,
)

def _get_headers(filename, filetype, content):
    return [
        ('Content-Type', filetype),
        ('Content-Length', len(content)),
        ('Content-Disposition', content_disposition(filename)),
        ('X-Content-Type-Options', 'nosniff'),
    ]
    
class MercadoLibre(http.Controller):
    @http.route('/meli/', auth='public')
    def index(self):
        company = request.env.user.company_id
        meli_util_model = request.env['meli.util']
        meli = meli_util_model.get_new_instance(company)
        if meli.need_login():
            return "<a href='"+meli.auth_url()+"'>Login Please</a>"

        return "MercadoLibre Publisher for Odoo - Copyright Moldeo Interactive 2021"

    # csrf=False is required because this endpoint is a webhook called
    # from MercadoLibre servers — they cannot provide an Odoo CSRF token.
    # Authentication is done inside the handler by validating the
    # notification payload (user_id/app_id).
    #
    # The /odoo/meli_notify aliases exist so the webhook works no matter
    # whether the ML app was configured with https://<host>/meli_notify
    # or https://<host>/odoo/meli_notify (Odoo 17+ backend prefix).
    @http.route(
        ['/meli_notify', '/odoo/meli_notify'],
        type=route_typejson, auth='public', methods=["POST"], csrf=False,
    )
    def meli_notify(self,**kw):
        _logger.info("meli_notify")
        #_logger.info(kw)
        company = request.env.user.company_id
        _logger.info(request.env.user)
        _logger.info(company)
        #_logger.info(company.display_name)
        #_logger.info(kw)
        #_logger.info(request)
        data = json.loads(request.httprequest.data)
        _logger.info(data)
        result = company.meli_notifications(data)
        if (result and "error" in result):
            return Response(result["error"],content_type='text/html;charset=utf-8',status=result["status"])
        else:
            return ""

    @http.route(['/meli_notify', '/odoo/meli_notify'], type='http', auth='public', methods=["GET"])
    def meli_notify_http(self,**kw):
        _logger.info("meli_notify_http")
        #_logger.info(kw)
        company = request.env.user.company_id
        _logger.info(request.env.user)
        _logger.info(company)
        #_logger.info(company.display_name)
        #_logger.info(kw)
        #_logger.info(request)
        #data = json.loads(request.httprequest.data)
        #_logger.info(data)
        #result = company.meli_notifications(data)
        #if (result and "error" in result):
        #    return Response(result["error"],content_type='text/html;charset=utf-8',status=result["status"])
        #else:
        return ""

    @http.route('/meli/image/<int:product_id>', type='http', auth="public")
    @http.route('/meli/image/<int:product_id>/<int:image_id>', type='http', auth="public")
    def meli_image(self, product_id, image_id=None, **kw):

        #browse and read image data to browser
        product = request.env["product.product"].browse(int(product_id))

        if image_id:
            filename = '%s_%s' % ("product.image".replace('.', '_'), str(product_id)+str("_")+str(image_id))
            product_image = request.env["product.image"].browse( int(image_id) )
            if product_image:
                filecontent = base64.b64decode( get_image_full( product_image ) )
            else:
                return ""
        else:
            filename = '%s_%s' % ("meli.image".replace('.', '_'), product_id)
            filecontent = base64.b64decode( get_image_full( product ) )

        return request.make_response(filecontent,
                                     [('Content-Type', 'application/octet-stream'),
                                      ('Content-Disposition', content_disposition(filename))])


class MercadoLibreLogin(http.Controller):

    @http.route(['/meli_login'], type='http', auth="user", methods=['GET'], website=True)
    def index(self, **codes ):
        meli_util_model = request.env['meli.util']

        codes.setdefault('code','none')
        codes.setdefault('error','none')

        def _refused():
            # Mensaje neutro: no dice si la compania existe, quien inicio el
            # intento ni que vendedor se esperaba.
            return ("<h5>MercadoLibre authorization rejected.</h5>"
                    "Nothing was exchanged or stored.")

        def _client_for(company):
            """Constructor puro. Solo despues de pasar la frontera del intento.

            Este camino todavia NO esta autenticado: pedirle un cliente no debe
            poder rotar credenciales, y lo unico que necesita de el son
            auth_url() y authorize().
            """
            client = meli_util_model._build_client(company)
            client.AUTH_URL = company.get_ML_AUTH_URL(meli=client)
            return client
        if codes['error']!='none':
            # Una respuesta OAuth con error igual consume el intento: dejarlo
            # vivo permitiria reusarlo despues de que el usuario ya rechazo.
            # No se intercambia ningun code y no se construye ningun cliente.
            meli_oauth_attempt_consume(codes.get('state'))
            _logger.error("OAuth callback returned an error from MercadoLibre")
            return ("<h5>MercadoLibre authorization was not completed.</h5>"
                    "Nothing was exchanged or stored. Start the login again "
                    "from Odoo.")

        if codes['code']!='none':
            # El vendedor configurado es una PRECONDICION, no algo que este
            # callback descubra. Sin el no hay contra que validar, y vincular la
            # compania a la cuenta que haya contestado es exactamente la
            # confusion que esta validacion existe para evitar.
            # Antes que nada: probar que este code responde a un pedido que
            # hicimos nosotros. Si no, no se intercambia -- authorize() ni
            # siquiera se llama, asi que no se pide ninguna credencial.
            state_ok, state_reason, attempt_company_id = (
                meli_oauth_attempt_consume(codes.get('state')))
            if not state_ok:
                _logger.error("OAuth callback rejected: %s", state_reason)
                return ("<h5>MercadoLibre authorization rejected.</h5>"
                        "This callback could not be matched to an authorization "
                        "request from this session. Nothing was exchanged or "
                        "stored. Start the login again from Odoo.")

            # El destino sale del intento validado, NUNCA de la compania activa
            # ni de un company_id del navegador: si no, iniciar el flujo para B
            # y volver con A activa escribiria las credenciales en A.
            company = request.env['res.company'].browse(attempt_company_id)
            if not company.exists():
                _logger.error("OAuth callback rejected: the attempt's company "
                              "no longer exists")
                return _refused()

            # La MISMA frontera privada que usa el boton: grupo y multiempresa
            # se comprueban una sola vez, en un solo lugar, y se comprueban de
            # nuevo AL VOLVER. Quien era administrador al iniciar y dejo de
            # serlo no puede completar el intercambio. El intento ya se
            # consumio mas arriba, asi que este rechazo no lo deja reutilizable.
            try:
                company._meli_require_credentials_admin()
            except AccessError:
                _logger.error("OAuth callback rejected: the user is no longer "
                              "allowed to manage this connection")
                return _refused()

            meli = _client_for(company)

            expected_seller = company.mercadolibre_seller_id
            if not expected_seller:
                _logger.error("OAuth callback rejected: no MercadoLibre seller "
                              "id configured on company %s", company.id)
                return ("<h5>MercadoLibre authorization rejected.</h5>"
                        "Configure the MercadoLibre seller id on the company "
                        "before authorizing, so the account that answers can be "
                        "verified.")

            resp = meli.authorize( codes['code'], company.mercadolibre_redirect_uri)

            # Misma regla que en el refresh: una respuesta solo puede reemplazar
            # credenciales si trae access_token y refresh_token no vacios y un
            # user_id que corresponda al vendedor configurado.
            valid, reason = meli_validate_refresh_response(resp, expected_seller)
            if not valid:
                # Nunca el body ni los valores: el resumen reporta presencia,
                # jamas contenido.
                _logger.error(
                    "OAuth callback rejected (%s): %s", reason,
                    meli_token_response_summary(resp, expected_seller))
                return ("<h5>MercadoLibre authorization rejected.</h5>"
                        "The response did not belong to the configured seller, "
                        "or was incomplete. Nothing was stored.")

            token_vals = { 'mercadolibre_access_token': resp["access_token"],
                           'mercadolibre_refresh_token': resp["refresh_token"],
                           'mercadolibre_code': codes['code'],
                           'mercadolibre_cron_refresh': True }
            # El intercambio del code tambien informa expires_in: la vigencia se
            # guarda desde el primer token, no recien desde el primer refresh.
            token_vals.update(meli_token_expiry_vals(resp))
            company.write( token_vals )
            # Never render the authorization code, access token or refresh token
            # in the response: they are stored server-side on res.company above.
            # Only a neutral success confirmation is returned.
            return 'MercadoLibre authorization completed successfully. You can close this window.<br>MercadoLibre Publisher for Odoo - Copyright Moldeo Interactive <br><a href="javascript:window.history.go(-2);">Volver a Odoo</a> <script>window.history.go(-2)</script>'
        else:
            company = request.env.user.company_id
            # auth="user" es autenticacion, no autorizacion. Sin esto cualquier
            # usuario logueado cruzaba _build_client -una capacidad privada con
            # sudo() angosto sobre el client secret y la fila auth- y se llevaba
            # una URL OAuth utilizable. Los groups de campo llegan tarde: la
            # frontera de capacidad ya quedo atras.
            try:
                company._meli_require_credentials_admin()
            except AccessError:
                _logger.error("Direct OAuth start rejected: the user may not "
                              "manage this connection")
                return _refused()
            meli = _client_for(company)
            state = meli_oauth_attempt_issue(company)
            return "<a href='"+meli.auth_url(state=state)+"'>Try to Login Again Please</a>"

class MercadoLibreAuthorize(http.Controller):
    @http.route('/meli_authorize/', auth='public')
    def index(self):
        return "AUTHORIZE: MercadoLibre for Odoo - Moldeo Interactive"


class MercadoLibreLogout(http.Controller):
    @http.route('/meli_logout/', auth='public')
    def index(self):
        return "LOGOUT: MercadoLibre for Odoo - Moldeo Interactive"
