# -*- coding: utf-8 -*-

import pytz

from odoo import models, api, fields
from odoo.tools.translate import _
from odoo.exceptions import UserError
import secrets
# Con guion bajo: mas abajo este modulo hace `from .versions import *`,
# y versions hace `from datetime import *`, con lo cual el nombre `time`
# terminaria apuntando a la CLASE datetime.time. Los wildcard imports
# omiten los nombres que empiezan con guion bajo.
from time import time as _clock_seconds

import psycopg2
import requests
from requests.adapters import HTTPAdapter
import json
try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode
import logging
_logger = logging.getLogger(__name__)


def meli_token_expiry_vals(response_info, now=None):
    """Campos de vigencia derivados de una respuesta de /oauth/token.

    MercadoLibre informa `expires_in` en cada respuesta. Sin guardarlo, el
    conector solo puede reaccionar a un 401 ya ocurrido, en medio de la
    operacion que lo haya provocado.

    NO inventa un TTL. Si la respuesta no trae expires_in, expires_at queda
    vacio y la vigencia es desconocida: preferimos "no se" antes que un numero
    fabricado del que despues se dependa.
    """
    if not isinstance(response_info, dict):
        return {}
    stamp = now or fields.Datetime.now()
    try:
        expires_in = int(response_info.get('expires_in') or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return {
        'mercadolibre_token_refreshed_at': stamp,
        'mercadolibre_token_expires_in': expires_in,
        'mercadolibre_token_expires_at': (
            stamp + timedelta(seconds=expires_in)) if expires_in > 0 else False,
    }


# ---------------------------------------------------------------------------
#  Credenciales: nunca en logs ni en la base
# ---------------------------------------------------------------------------
# La respuesta de POST /oauth/token trae access_token y refresh_token en claro.
# Volcarla con str() la manda al log (stderr -> donde la plataforma lo recolecte)
# y, en el caso de mercadolibre.notification, la PERSISTE en la base. Estas dos
# funciones son el unico camino permitido para reportar un refresh.

def meli_token_response_summary(response_info, expected_seller_id=None):
    """Resumen NO sensible de una respuesta de /oauth/token.

    Devuelve presencia, no valores. Incluye el codigo de error de ML porque
    hace falta para distinguir invalid_grant, pero nunca texto libre ni tokens.
    """
    if not isinstance(response_info, dict):
        return {"parseable": False, "type": type(response_info).__name__}
    user_id = response_info.get("user_id")
    summary = {
        "parseable": True,
        "access_token_received": bool(response_info.get("access_token")),
        "refresh_token_received": bool(response_info.get("refresh_token")),
        "expires_in": response_info.get("expires_in"),
        "token_type": response_info.get("token_type"),
        "user_id": user_id,
        "error": response_info.get("error"),
    }
    if expected_seller_id is not None:
        summary["user_id_matches"] = (
            user_id is not None and str(user_id) == str(expected_seller_id))
    return summary


class MeliTokenOutcome:
    """Resultado de un POST /oauth/token. Contrato unico para los dos backends.

    Existe porque los backends absorben los errores de transporte: MeliApiNoSDK
    captura requests.RequestException y devuelve un payload de error, asi que un
    timeout real llegaba al caller indistinguible de un rechazo de ML. No son lo
    mismo. Ante un timeout NO se puede saber si MercadoLibre consumio el refresh
    token, y como es de un solo uso, tratarlo como rechazo invita a un reintento
    que gastaria un segundo token.

    Preserva las tres cosas que hacen falta para clasificar:
        payload             el cuerpo, tal cual vino
        http_status         el codigo HTTP, o None si no hubo respuesta
        transport_uncertain True solo si el POST salio y no se supo el resultado

    `transport_uncertain` se marca UNICAMENTE ante un error de transporte
    identificado como tal. Un bug local de Python no implica que el POST haya
    quedado ambiguo, y no se disfraza de tal.
    """

    __slots__ = ('payload', 'http_status', 'transport_uncertain', 'reason')

    def __init__(self, payload=None, http_status=None,
                 transport_uncertain=False, reason=None):
        self.payload = payload
        self.http_status = http_status
        self.transport_uncertain = transport_uncertain
        self.reason = reason

    def __repr__(self):
        # Nunca el payload: trae access_token y refresh_token en claro cuando el
        # refresh sale bien, y un repr termina en un log.
        return ("MeliTokenOutcome(http_status=%r, transport_uncertain=%r, "
                "reason=%r)" % (self.http_status, self.transport_uncertain,
                                self.reason))

    __str__ = __repr__


# HTTP que NO dice nada sobre la validez del token. Tratarlos como "vencido"
# dispararia un refresh que no hace falta y gastaria el token de un solo uso.
_AUTH_INDETERMINATE_STATUS = (403, 429)

_AUTH_ISOLATION = 'read committed'

# Un solo lugar para el UPDATE de la fila auth: lo usan el camino normal y el de
# recuperacion, y tienen que escribir exactamente lo mismo.
_AUTH_UPDATE_SQL = (
    "UPDATE mercadolibre_auth SET access_token = %s, refresh_token = %s, "
    "code = %s, token_expires_in = %s, token_refreshed_at = %s, "
    "token_expires_at = %s, write_date = now() AT TIME ZONE 'UTC' "
    "WHERE id = %s"
)


class MeliAuthResult:
    """Lo que decidio la transaccion AUTH, devuelto EN MEMORIA.

    La transaccion ambiente conserva su snapshot viejo. Bajo REPEATABLE READ eso
    no tiene arreglo, y no hace falta que lo tenga: el caller usa estos valores
    directamente en vez de releer res.company esperando ver un commit que su
    snapshot nunca va a incluir.

    Estados:
        REFRESHED           se posteo, valido, se persistio y se commiteo
        ALREADY_FRESH       otro proceso ya habia rotado; NO se posteo
        REJECTED            respuesta invalida; credenciales intactas
        ABORT_REAUTH        invalid_grant; hace falta OAuth manual
        ABORT_INDETERMINATE no se puede afirmar nada sobre el token
        AUTH_UNCERTAIN      POST enviado sin respuesta; jamas se reintenta
        AUTH_CRITICAL       no se pudo serializar o no se pudo persistir
    """

    __slots__ = ('status', 'access_token', 'refresh_token', 'reason', 'posted')

    def __init__(self, status, access_token=None, refresh_token=None,
                 reason=None, posted=False):
        self.status = status
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.reason = reason
        self.posted = posted

    @property
    def usable(self):
        return self.status in ('REFRESHED', 'ALREADY_FRESH')

    def __repr__(self):
        # Nunca los valores: son credenciales, y un repr termina en un log.
        return "MeliAuthResult(status=%r, posted=%r, reason=%r)" % (
            self.status, self.posted, self.reason)


# ----------------------------------------------------------------------
# El intento OAuth.
#
# Vive aca y no en el controlador porque tiene DOS iniciadores: la entrada
# directa /meli_login y el boton res.company.meli_login(). Con una copia en
# cada lado terminarian existiendo dos formatos de intento y el callback solo
# entenderia uno.
#
# Lo que viaja a MercadoLibre es unicamente el nonce, opaco. El uid y la
# compania quedan del lado del servidor, en la sesion: son la respuesta a
# "quien pidio esto y para que cuenta", y el navegador no puede opinar.
#
# No se guarda ningun secreto OAuth en la sesion.
# ----------------------------------------------------------------------
_OAUTH_ATTEMPT_SESSION_KEY = "meli_oauth_state"
_OAUTH_ATTEMPT_TTL_SECONDS = 600


def _oauth_session():
    """La sesion HTTP en curso, o None si no hay peticion.

    odoo.http.request es un LocalProxy: fuera de una peticion no vale None,
    sino que LANZA al tocarlo. Por eso no alcanza con `is None` ni con getattr.
    """
    from odoo.http import request as _request

    try:
        return _request.session
    except Exception:
        return None


def _oauth_uid():
    from odoo.http import request as _request

    try:
        return _request.env.uid
    except Exception:
        return None


def meli_oauth_attempt_issue(company):
    """Crea el intento y devuelve el nonce a mandar a MercadoLibre.

    Exige una peticion HTTP con sesion. Sin eso no hay a que atar el intento,
    y degradarse a un state suelto seria devolver justo la vulnerabilidad que
    esto existe para cerrar: se falla explicitamente, antes de cualquier
    efecto.
    """
    session = _oauth_session()
    if session is None:
        raise UserError(_(
            "The MercadoLibre authorization can only be started from a web "
            "session."))
    # El uid se resuelve ANTES de crear el nonce y antes de tocar la sesion.
    # Un intento con uid nulo no esta ligado a nadie, y al consumirlo el
    # None == None lo leeria como coincidencia: seria un intento que cualquiera
    # completa.
    uid = _oauth_uid()
    if not uid:
        raise UserError(_(
            "The MercadoLibre authorization could not be linked to a user."))
    company.ensure_one()
    value = secrets.token_urlsafe(32)
    session[_OAUTH_ATTEMPT_SESSION_KEY] = {
        "value": value,
        "issued_at": _clock_seconds(),
        "uid": uid,
        "company_id": company.id,
    }
    return value


def meli_oauth_attempt_consume(received):
    """Valida y CONSUME el intento. Devuelve (ok, motivo, company_id).

    Se consume haya coincidido o no: un intento fallido invalida el que estaba
    en curso en vez de dejarlo disponible para seguir probando.
    """
    session = _oauth_session()
    stored = session.pop(_OAUTH_ATTEMPT_SESSION_KEY, None) if session else None
    if not received:
        return False, "no state in the callback", None
    if not stored or not stored.get("value"):
        return False, "no state was issued in this session", None
    if _clock_seconds() - stored.get("issued_at", 0) > _OAUTH_ATTEMPT_TTL_SECONDS:
        return False, "the issued state expired", None
    if not secrets.compare_digest(str(stored["value"]), str(received)):
        return False, "the state does not match the one issued", None
    current_uid = _oauth_uid()
    if not current_uid:
        return False, "there is no current user to match the state to", None
    if stored.get("uid") != current_uid:
        return False, "the state belongs to another user", None
    company_id = stored.get("company_id")
    if not company_id:
        return False, "the state carries no company", None
    return True, None, company_id


def meli_validate_refresh_response(response_info, expected_seller_id):
    """Decide si una respuesta de /oauth/token puede reemplazar credenciales.

    Un refresh cuenta como exitoso solo con access_token y refresh_token no
    vacios y un user_id que corresponda al vendedor configurado. Cualquier otra
    cosa deja intactas las credenciales guardadas: un refresh malo NO puede
    destruir una sesion que funciona.

    Es critico por el refresh token de un solo uso: si guardaramos uno vacio o
    de otra cuenta, el anterior ya quedo gastado del lado de MercadoLibre y la
    sesion no se puede renovar nunca mas sin un OAuth manual.

    Devuelve (valido, motivo). El motivo es una etiqueta corta, nunca el body.
    """
    if not isinstance(response_info, dict):
        return False, "unparseable response"
    if not response_info.get("access_token"):
        return False, "missing or empty access_token"
    if not response_info.get("refresh_token"):
        return False, "missing or empty refresh_token"
    user_id = response_info.get("user_id")
    if user_id in (None, ""):
        return False, "missing user_id"
    if str(user_id) != str(expected_seller_id):
        return False, "user_id does not match the configured seller"
    return True, None


def meli_redact(text, *secrets):
    """Reemplaza valores de secretos conocidos por *** dentro de un texto.

    Se usa en caminos de excepcion, donde el mensaje puede arrastrar el token o
    el client secret. Es preciso: solo redacta los valores que efectivamente
    tenemos en mano, sin heuristicas sobre "algo que parece un token".
    """
    out = str(text)
    for secret in secrets:
        s = str(secret or "")
        if len(s) >= 8:
            out = out.replace(s, "***")
    return out

from .meli_oerp_config import REDIRECT_URI

from urllib3.util.retry import Retry

from datetime import datetime, timedelta
from .versions import *
from . import versions as _versions


class LoggingRetry(Retry):
    def increment(self, *args, **kwargs):
        retry_number = kwargs.get('total', self.total)
        reason = kwargs.get('reason', 'Unknown reason')
        _logger.info(f"Reintentando... Intento {self.total - retry_number + 1} debido a: {reason}")
        return super().increment(*args, **kwargs)


# ---------------------------------------------------------------------------
#  Configuraciones (siempre se crean ambas; se elige al final del módulo)
# ---------------------------------------------------------------------------

# NoSDK: requests Session con retry
class MeliConfiguration:
    def __init__(self, host="https://api.mercadolibre.com"):
        self.host = host
        self.retries = LoggingRetry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[413, 429, 503],
            raise_on_status=False
        )

    def get_session(self):
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=self.retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session


configuration_nosdk = MeliConfiguration(host="https://api.mercadolibre.com")

# SDK: meli.Configuration (solo si el SDK está instalado)
configuration_sdk = None
_meli_sdk = None
_ApiClient = None
_ApiException = None
if _versions.MELI_SDK_AVAILABLE:
    try:
        import meli as _meli_sdk
        from meli.rest import ApiException as _ApiException
        from meli.api_client import ApiClient as _ApiClient
        configuration_sdk = _meli_sdk.Configuration(host="https://api.mercadolibre.com")
        configuration_sdk.retries = LoggingRetry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[413, 429, 503],
            raise_on_status=False
        )
    except Exception as e:
        _logger.warning("meli SDK import falló: %s", str(e))
        _versions.MELI_SDK_AVAILABLE = False
        _versions.USE_MELI_SDK = False


# ---------------------------------------------------------------------------
#  MeliApiNoSDK — implementación con requests puro
# ---------------------------------------------------------------------------
class MeliApiNoSDK:
    """
    Cliente API de MercadoLibre sin dependencia del SDK oficial.
    Usa requests directamente para todas las operaciones HTTP y OAuth.
    """

    AUTH_URL = "https://auth.mercadolibre.com.ar/authorization"
    TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

    needlogin_state = True

    client_id = ""
    client_secret = ""
    access_token = ""
    refresh_token = ""
    redirect_uri = ""
    seller_id = ""

    response = ""
    code = ""
    rjson = {}

    user = {}

    # Benchmarking support - set to True to enable API timing logs
    _benchmark_enabled = False
    _benchmark_stats = {
        'get': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'get_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'post': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'post_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'put': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'put_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        'delete': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
    }
    _benchmark_slow_threshold = 2.0  # seconds

    @classmethod
    def enable_benchmark(cls, enabled=True, slow_threshold=2.0):
        """Enable or disable API benchmarking"""
        cls._benchmark_enabled = enabled
        cls._benchmark_slow_threshold = slow_threshold
        if enabled:
            cls.reset_benchmark_stats()
            _logger.info("MELI API BENCHMARK: ENABLED (slow threshold: %.1fs)", slow_threshold)
        else:
            _logger.info("MELI API BENCHMARK: DISABLED")

    @classmethod
    def reset_benchmark_stats(cls):
        """Reset benchmark statistics"""
        cls._benchmark_stats = {
            'get': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'get_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'post': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'post_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'put': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'put_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'delete': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        }

    @classmethod
    def get_benchmark_stats(cls):
        """Get benchmark statistics summary"""
        stats = cls._benchmark_stats
        total_calls = sum(s['count'] for s in stats.values())
        total_time = sum(s['total_time'] for s in stats.values())
        summary = {
            'total_calls': total_calls,
            'total_time': total_time,
            'by_method': {}
        }
        for method, s in stats.items():
            if s['count'] > 0:
                summary['by_method'][method] = {
                    'count': s['count'],
                    'total_time': round(s['total_time'], 2),
                    'avg_time': round(s['total_time'] / s['count'], 3),
                    'slow_calls': len(s['slow_calls'])
                }
        return summary

    @classmethod
    def log_benchmark_stats(cls):
        """Log benchmark statistics"""
        if not cls._benchmark_enabled:
            return
        stats = cls.get_benchmark_stats()
        _logger.info("MELI API BENCHMARK SUMMARY: total_calls=%d total_time=%.2fs",
                    stats['total_calls'], stats['total_time'])
        for method, s in stats['by_method'].items():
            _logger.info("  %s: calls=%d time=%.2fs avg=%.3fs slow=%d",
                        method, s['count'], s['total_time'], s['avg_time'], s['slow_calls'])

    def _record_benchmark(self, method, path, elapsed):
        """Record benchmark data for an API call"""
        if not MeliApiNoSDK._benchmark_enabled:
            return
        stats = MeliApiNoSDK._benchmark_stats.get(method)
        if stats:
            stats['count'] += 1
            stats['total_time'] += elapsed
            if elapsed > MeliApiNoSDK._benchmark_slow_threshold:
                stats['slow_calls'].append({'path': path, 'time': elapsed})
                _logger.warning("MELI API SLOW %s: %s took %.2fs", method.upper(), path, elapsed)

    def __init__(self, config=None):
        """
        Inicializa el cliente API.

        Args:
            config: MeliConfiguration opcional. Si no se pasa, usa la configuración global.
        """
        self.config = config or configuration
        self.base_url = self.config.host
        self._session = self.config.get_session()
        # Codigo HTTP de la ultima llamada de este cliente. El body de ML no es
        # una senal fiable de sesion invalida (un 401 real no trae 'error' ni
        # 'status'), asi que el status se conserva para poder consultarlo.
        self.last_status_code = None

    def _abs_url(self, path):
        """Convierte un path relativo a URL absoluta"""
        if not path:
            return path
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _parse_response(self, resp):
        """Parsea la respuesta HTTP a JSON o texto.

        Una respuesta vacia con error HTTP ya no se lee como EXITO. ML devuelve
        429 (y a veces 5xx) con **body vacio**. Aca se devolvia `resp.text` ==
        '', y todos los llamadores hacen `if rjson and "error" in rjson`: con ''
        eso es False, asi que el push reportaba *updated/Ok* **sin haber escrito
        nada en ML**, y sin dejar un solo log. Ahora un status >= 400 con body
        vacio se convierte en un dict de error explicito.

        Portado de ctmil/meli_oerp a24b8179 (RPM 532, 4-ago-2026). De ese commit
        se toma solo esta mitad: la otra arregla que `post_mini`/`put_mini` de la
        rama SDK descarten el proxy de la empresa, y esa rama toca el armado del
        cliente, donde vive nuestro `_build_client`.
        """
        try:
            return resp.json()
        except Exception:
            texto = resp.text
            status = getattr(resp, "status_code", 200) or 200
            if not texto and status >= 400:
                _logger.warning("respuesta VACIA con status %s (%s) -- se reporta como error, "
                                "antes se interpretaba como exito",
                                status, getattr(resp, "url", "?"))
                return {"error": "http_%s" % status,
                        "status": status,
                        "message": "respuesta vacia con status %s" % status}
            return texto

    def need_login(self):
        return self.needlogin_state

    def json(self):
        return self.rjson

    def get(self, path, params={}, extra_headers=None, **kwargs):
        """
        GET genérico sin SDK.
        - Firma: get(self, path, params={}, extra_headers=None)
        - Mantiene self.response y self.rjson
        - Retorna self
        - Absorbe kwargs desconocidos de forma segura (nunca TypeError por firma)
        """
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        _original_path = path

        # Extrae datos de params sin mutar el original
        atok = params.get("access_token", "") or ""
        if atok == "PASIVA":
            atok = ""

        headers = (params.get("headers") or {}).copy()
        if extra_headers:
            headers.update(extra_headers)
        timeout = params.get("timeout", 20)
        scroll_id = params.get("scroll_id", None)
        qparams = params.get("query", None)

        # Compatibilidad con viejo estilo de query
        if qparams is None:
            reserved = {"access_token", "headers", "timeout", "scroll_id", "query"}
            qparams = {k: v for k, v in params.items() if k not in reserved}
            if not qparams:
                qparams = None

        # Construye query string
        url = self._abs_url(path)
        query_parts = []
        if qparams:
            query_parts.append(urlencode(qparams))
        if scroll_id:
            query_parts.append(f"scroll_id={scroll_id}")
        if query_parts:
            sep = "&" if ("?" in url) else "?"
            url = f"{url}{sep}{'&'.join(query_parts)}"

        # Headers finales
        final_headers = {"Accept": "application/json"}
        if atok:
            final_headers["Authorization"] = f"Bearer {atok}"
        final_headers.update(headers)

        try:
            resp = self._session.get(
                url,
                headers=final_headers,
                timeout=timeout,
                allow_redirects=True
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response
            self.last_status_code = resp.status_code

            # Log según status code
            if resp.status_code == 404:
                _logger.debug("GET %s: 404 Not Found", path)
            elif resp.status_code in (401, 403):
                _logger.warning(
                    "GET %s: Auth error status=%s | Seller ID: %s",
                    path, resp.status_code, self.seller_id
                )
            elif resp.status_code >= 400:
                _logger.warning(
                    "GET %s falló: status=%s body=%s",
                    path, resp.status_code, str(self.rjson)[:200]
                )

        except requests.RequestException as e:
            _logger.warning("GET %s error: %s", path, str(e))
            self.last_status_code = 0
            self.rjson = {
                "error": "get error",
                "status": 0,
                "cause": "request_exception",
                "message": str(e),
                "get_url": path
            }
            self.response = self.rjson
        finally:
            if MeliApiNoSDK._benchmark_enabled:
                self._record_benchmark('get', _original_path, _time_module.time() - _t_start)

        return self

    def get_mini(self, path, params={}, extra_headers=None, **kwargs):
        """GET via requests — alias de get() para compatibilidad.
        Firma alineada con get(): acepta extra_headers y absorbe kwargs
        desconocidos de forma segura (no debe romper por firma)."""
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        result = self.get(path, params, extra_headers=extra_headers)
        if MeliApiNoSDK._benchmark_enabled:
            self._record_benchmark('get_mini', path, _time_module.time() - _t_start)
        return result

    def post(self, path, body=None, params={}, extra_headers=None, **kwargs):
        """
        POST genérico sin SDK.
        - Firma: post(self, path, body=None, params={}, extra_headers=None)
        - Mantiene self.response y self.rjson
        - Retorna self
        """
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        _original_path = path

        # Extrae y NO muta el dict original
        atok = params.get("access_token", "") or ""
        headers = (params.get("headers") or {}).copy()
        if extra_headers:
            headers.update(extra_headers)
        timeout = params.get("timeout", 20)
        files = params.get("files", None)
        qparams = params.get("query", None)

        # Compatibilidad: si no se pasó 'query', construyo con el resto
        if qparams is None:
            reserved = {"access_token", "headers", "timeout", "files", "query"}
            qparams = {k: v for k, v in params.items() if k not in reserved}
            if not qparams:
                qparams = None

        # Construcción de URL
        url = self._abs_url(path)
        if qparams:
            sep = "&" if ("?" in url) else "?"
            url = f"{url}{sep}{urlencode(qparams)}"

        # Headers finales
        final_headers = {"Accept": "application/json"}
        if atok:
            final_headers["Authorization"] = f"Bearer {atok}"
        if isinstance(body, (dict, list)) and not files:
            if "Content-Type" not in {k.title(): v for k, v in headers.items()}:
                final_headers["Content-Type"] = "application/json"
        final_headers.update(headers)

        try:
            resp = self._session.post(
                url,
                headers=final_headers,
                json=body if (isinstance(body, (dict, list)) and not files) else None,
                data=None if (isinstance(body, (dict, list)) and not files) else body,
                files=files,
                timeout=timeout,
                allow_redirects=True,
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response

            if resp.status_code >= 400:
                _logger.warning(
                    "POST %s falló: status=%s body=%s",
                    path, resp.status_code, str(self.rjson)[:200]
                )

        except requests.RequestException as e:
            _logger.warning("POST %s error: %s", path, str(e))
            self.rjson = {
                "error": "post error",
                "status": 0,
                "cause": "request_exception",
                "message": str(e),
                "post_url": path
            }
            self.response = self.rjson
        finally:
            if MeliApiNoSDK._benchmark_enabled:
                self._record_benchmark('post', _original_path, _time_module.time() - _t_start)

        return self

    def post_mini(self, path, body=None, params={}, extra_headers=None, **kwargs):
        """POST via requests — alias de post() para compatibilidad.
        Firma alineada con post(): acepta extra_headers y absorbe kwargs
        desconocidos de forma segura."""
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        result = self.post(path, body, params, extra_headers=extra_headers)
        if MeliApiNoSDK._benchmark_enabled:
            self._record_benchmark('post_mini', path, _time_module.time() - _t_start)
        return result

    def put(self, path, body=None, params={}, extra_headers=None, **kwargs):
        """
        PUT genérico sin SDK.
        - Firma: put(self, path, body=None, params={}, extra_headers=None)
        - Mantiene self.response y self.rjson
        - Retorna self
        """
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        _original_path = path

        atok = params.get("access_token", "") or ""
        headers = (params.get("headers") or {}).copy()
        if extra_headers:
            headers.update(extra_headers)
        timeout = params.get("timeout", 20)
        qparams = params.get("query", None)

        url = self._abs_url(path)

        # Headers finales
        final_headers = {"Accept": "application/json"}
        if atok:
            final_headers["Authorization"] = f"Bearer {atok}"
        if isinstance(body, (dict, list)) and "Content-Type" not in {k.title(): v for k, v in headers.items()}:
            final_headers["Content-Type"] = "application/json"
        final_headers.update(headers)

        try:
            resp = self._session.put(
                url,
                headers=final_headers,
                json=body if isinstance(body, (dict, list)) else None,
                data=None if isinstance(body, (dict, list)) else body,
                params=qparams,
                timeout=timeout,
                allow_redirects=True,
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response

            if resp.status_code >= 400:
                _logger.warning(
                    "PUT %s falló: status=%s body=%s",
                    path, resp.status_code, str(self.rjson)[:200]
                )

        except requests.RequestException as e:
            _logger.warning("PUT %s error: %s", path, str(e))
            self.rjson = {
                "error": "put error",
                "status": 0,
                "cause": "request_exception",
                "message": str(e)
            }
            self.response = self.rjson
        finally:
            if MeliApiNoSDK._benchmark_enabled:
                self._record_benchmark('put', _original_path, _time_module.time() - _t_start)

        return self

    def put_mini(self, path, body=None, params={}, extra_headers=None, **kwargs):
        """PUT via requests — alias de put() para compatibilidad.
        Firma alineada con put(): acepta extra_headers y absorbe kwargs
        desconocidos de forma segura."""
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        result = self.put(path, body, params, extra_headers=extra_headers)
        if MeliApiNoSDK._benchmark_enabled:
            self._record_benchmark('put_mini', path, _time_module.time() - _t_start)
        return result

    def delete(self, path, params={}, extra_headers=None, **kwargs):
        """
        DELETE genérico sin SDK.
        - Firma: delete(self, path, params={}, extra_headers=None)
        - Mantiene self.response y self.rjson
        - Retorna self
        """
        import time as _time_module
        _t_start = _time_module.time() if MeliApiNoSDK._benchmark_enabled else 0
        _original_path = path

        atok = params.get("access_token", "") or ""
        headers = (params.get("headers") or {}).copy()
        if extra_headers:
            headers.update(extra_headers)
        timeout = params.get("timeout", 20)

        url = self._abs_url(path)

        final_headers = {"Accept": "application/json"}
        if atok:
            final_headers["Authorization"] = f"Bearer {atok}"
        final_headers.update(headers)

        try:
            resp = self._session.delete(
                url,
                headers=final_headers,
                timeout=timeout,
                allow_redirects=True
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response

            if resp.status_code >= 400:
                _logger.warning(
                    "DELETE %s falló: status=%s body=%s",
                    path, resp.status_code, str(self.rjson)[:200]
                )

        except requests.RequestException as e:
            _logger.warning("DELETE %s error: %s", path, str(e))
            self.rjson = {
                "error": "delete error",
                "status": 0,
                "cause": "request_exception",
                "message": str(e)
            }
            self.response = self.rjson
        finally:
            if MeliApiNoSDK._benchmark_enabled:
                self._record_benchmark('delete', _original_path, _time_module.time() - _t_start)

        return self

    def upload(self, path, files, params={}):
        """
        Upload de archivos usando multipart/form-data (sin SDK).
        Los archivos se pasan en el parámetro files.
        """
        atok = params.get("access_token", "") or ""
        timeout = params.get("timeout", 60)

        url = self._abs_url(path)

        # Para upload legacy, usamos query param access_token
        if atok:
            sep = "&" if ("?" in url) else "?"
            url = f"{url}{sep}access_token={atok}"

        try:
            resp = self._session.post(
                url,
                files=files,
                timeout=timeout
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response

        except requests.RequestException as e:
            _logger.warning("UPLOAD %s error: %s", path, str(e))
            self.rjson = {"error": str(e)}
            self.response = self.rjson

        return self

    def uploadfiles(self, path, files, params={}):
        """
        Upload de archivos usando Authorization Bearer (sin SDK).
        """
        atok = params.get("access_token", "") or ""
        timeout = params.get("timeout", 60)

        url = self._abs_url(path)

        headers = {"Accept": "application/json"}
        if atok:
            headers["Authorization"] = f"Bearer {atok}"

        try:
            resp = self._session.post(
                url,
                files=files,
                headers=headers,
                timeout=timeout
            )
            self.response = self._parse_response(resp)
            self.rjson = self.response

        except requests.RequestException as e:
            _logger.warning("UPLOADFILES %s error: %s", path, str(e))
            self.rjson = {"error": str(e)}
            self.response = self.rjson

        return self

    def auth_url(self, redirect_URI=None, state=None):
        """Genera la URL de autorización OAuth para login"""
        if redirect_URI:
            self.redirect_uri = redirect_URI
        # El state lo emite el controlador, que es quien puede atarlo a la
        # sesion del usuario y consumirlo una sola vez. El fallback existe solo
        # para no romper llamadas que no lo pasan; no protege de nada.
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'state': state or str(datetime.now())
        }
        url = self.AUTH_URL + '?' + urlencode(params)
        return url

    # redirect_login se elimino en PR G. Era la unica via por la que una
    # accion comercial podia construir una URL de autorizacion, y con eso
    # convertirse en un iniciador de OAuth fuera del flujo protegido.
    # Reconectar se hace desde res.company.meli_login o /meli_login.

    def authorize(self, code, redirect_uri=None):
        """
        Obtiene access_token usando authorization_code (sin SDK).
        POST a https://api.mercadolibre.com/oauth/token
        """
        if redirect_uri:
            self.redirect_uri = redirect_uri

        data = {
            'grant_type': 'authorization_code',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
            'redirect_uri': self.redirect_uri
        }

        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        try:
            resp = self._session.post(
                self.TOKEN_URL,
                data=data,
                headers=headers,
                timeout=30
            )
            response_info = self._parse_response(resp)

            if isinstance(response_info, dict) and 'access_token' in response_info:
                self.access_token = response_info['access_token']
                self.refresh_token = response_info.get('refresh_token', '')
            else:
                _logger.warning("authorize falló: %s", str(response_info)[:200])

            return response_info

        except requests.RequestException as e:
            _logger.error("authorize error: %s", str(e))
            return {"error": "authorize_error", "message": str(e)}

    def get_refresh_token(self, code=None, redirect_uri=None):
        """
        Renueva access_token usando refresh_token (sin SDK).
        POST a https://api.mercadolibre.com/oauth/token
        """
        data = {
            'grant_type': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token
        }

        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        try:
            resp = self._session.post(
                self.TOKEN_URL,
                data=data,
                headers=headers,
                timeout=30
            )
            response_info = self._parse_response(resp)

            # Este metodo hace el POST y devuelve lo que contesto ML. Nada mas:
            # no toca las credenciales del cliente. Decidir si la respuesta
            # merece reemplazar la sesion es del caller, que valida primero y
            # recien despues asigna (get_new_instance).
            #
            # Cuando la asignacion vivia aca corria ANTES de esa validacion, asi
            # que un refresh rechazado igual se llevaba puesto al cliente en
            # memoria: no se persistia, pero se usaba. "No guardar" y "no usar"
            # son dos propiedades distintas.
            if not (isinstance(response_info, dict) and response_info.get('access_token')):
                # Nunca el body: trae credenciales cuando el refresh sale bien.
                _logger.warning("get_refresh_token failed: %s",
                                meli_token_response_summary(response_info, self.seller_id))

            return MeliTokenOutcome(payload=response_info,
                                    http_status=resp.status_code)

        except requests.RequestException as e:
            # El POST salio y no sabemos como termino. Se marca como incierto en
            # vez de devolver un payload de error, que era indistinguible de un
            # rechazo de MercadoLibre y llevaba a clasificarlo mal.
            _logger.error("get_refresh_token transport failure: %s",
                          type(e).__name__)
            return MeliTokenOutcome(transport_uncertain=True,
                                    reason=type(e).__name__)

    def get_sale_terms(self, category_id=None, sale_term_id=None, productjson=None):
        """Obtiene los términos de venta para una categoría"""
        sale_terms_by_id = {}

        if category_id:
            url = f"/categories/{category_id}/sale_terms"
            res = self.get(url)

            if res and res.rjson and isinstance(res.rjson, list):
                for rj in res.rjson:
                    stid = rj.get("id")
                    if stid:
                        sale_terms_by_id[stid] = rj

        if sale_term_id:
            # Buscar en el JSON del producto si se proporcionó
            if productjson and "sale_terms" in productjson:
                for st in productjson["sale_terms"]:
                    if st.get("id") == sale_term_id:
                        return st
                return False

            # Buscar en los términos de la categoría
            if sale_term_id in sale_terms_by_id:
                return sale_terms_by_id[sale_term_id]

        return sale_terms_by_id

    def get_user_product_stock_with_version(self, up_id, access_token):
        """
        Obtiene stock de user-product con x-version header (sin SDK).
        Retorna (data, x_version)
        """
        url = self._abs_url(f"user-products/{up_id}/stock")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        try:
            resp = self._session.get(url, headers=headers, timeout=20)
            data = self._parse_response(resp)
            xver = resp.headers.get('x-version') or resp.headers.get('X-Version')
            return data, xver

        except requests.RequestException as e:
            _logger.warning("get_user_product_stock_with_version error: %s", str(e))
            return {"error": str(e)}, None


# ---------------------------------------------------------------------------
#  MeliApiSDK — implementación con el SDK oficial de MercadoLibre
#  Solo se define si el SDK está disponible.
# ---------------------------------------------------------------------------
if _versions.MELI_SDK_AVAILABLE and _meli_sdk and _ApiClient:
    class MeliApiSDK(_meli_sdk.RestClientApi):
        """Cliente API de MercadoLibre usando el SDK oficial (meli)."""

        AUTH_URL = "https://auth.mercadolibre.com.ar/authorization"
        needlogin_state = True
        client_id = ""
        client_secret = ""
        access_token = ""
        refresh_token = ""
        redirect_uri = ""
        seller_id = ""
        response = ""
        code = ""
        rjson = {}
        user = {}
        # Espeja el contrato de MeliApiNoSDK. Esta clase no define __init__
        # propio (hereda el del SDK), asi que se declara a nivel de clase como
        # el resto de su estado.
        last_status_code = None

        # Benchmarking support - class-level attributes (for compatibility with MeliApiNoSDK)
        _benchmark_enabled = False
        _benchmark_stats = {
            'get': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'get_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'post': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'post_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'put': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'put_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            'delete': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
        }
        _benchmark_slow_threshold = 2.0

        @classmethod
        def enable_benchmark(cls, enabled=True, slow_threshold=2.0):
            """Enable or disable API benchmarking"""
            cls._benchmark_enabled = enabled
            cls._benchmark_slow_threshold = slow_threshold
            if enabled:
                cls.reset_benchmark_stats()
                _logger.info("MELI API BENCHMARK (SDK): ENABLED (slow threshold: %.1fs)", slow_threshold)
            else:
                _logger.info("MELI API BENCHMARK (SDK): DISABLED")

        @classmethod
        def reset_benchmark_stats(cls):
            """Reset benchmark statistics"""
            cls._benchmark_stats = {
                'get': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'get_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'post': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'post_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'put': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'put_mini': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
                'delete': {'count': 0, 'total_time': 0.0, 'slow_calls': []},
            }

        @classmethod
        def get_benchmark_stats(cls):
            """Get benchmark statistics summary"""
            return cls._benchmark_stats

        def __init__(self, *args, **kwargs):
            super(MeliApiSDK, self).__init__(*args, **kwargs)
            self.api_auth_client = _meli_sdk.OAuth20Api(self.api_client)

        def need_login(self):
            return self.needlogin_state

        def json(self):
            return self.rjson

        def get(self, path, params={}, extra_headers=None, **kwargs):
            # When custom headers are required (e.g. x-version for versioned
            # endpoints) the SDK's resource_get does not expose a per-call
            # header hook, so fall back to the requests-based client which
            # supports arbitrary headers.
            # NOTA: **kwargs absorbe cualquier keyword extra desconocido de
            # forma segura — nunca debe tirar TypeError por firma (ver
            # ERROR extra_headers/get_billing_info, meli_oerp 26.70 Aramid).
            if extra_headers:
                return self.get_mini(path, params, extra_headers=extra_headers)
            try:
                atok = ("access_token" in params and params["access_token"]) or ""
                if atok == "PASIVA":
                    atok = ""
                    del params["access_token"]
                scroll_id = ("scroll_id" in params and params["scroll_id"]) or None
                if atok:
                    del params["access_token"]
                if scroll_id:
                    del params["scroll_id"]
                if params:
                    path += "?" + urlencode(params)
                    if scroll_id:
                        path += "&scroll_id=" + scroll_id
                self.response = self.resource_get(resource=path, access_token=atok)
                self.rjson = self.response
                # Status HTTP real del SDK cuando lo expone. NO se asume 200:
                # justamente lo que se busca es dejar de inferir el status.
                self.last_status_code = getattr(
                    getattr(getattr(self, "api_client", None), "last_response", None),
                    "status", None)
            except _ApiException as e:
                self.last_status_code = getattr(e, "status", None)
                self.rjson = {
                    "error": "get error",
                    "status": getattr(e, "status", None),
                    "cause": getattr(e, "reason", None),
                    "message": getattr(e, "body", None),
                }
            except:
                pass
            return self

        # get_mini y post_mini usan requests directo (como en la versión original)
        def get_mini(self, path, params={}, extra_headers=None, **kwargs):
            """GET sin SDK (requests directo) - para compatibilidad"""
            _nosdk = MeliApiNoSDK(config=configuration_nosdk)
            _nosdk.__dict__.update({k: v for k, v in self.__dict__.items()
                                     if k in ('client_id', 'client_secret', 'access_token',
                                              'refresh_token', 'redirect_uri', 'seller_id')})
            _nosdk.get(path, params, extra_headers=extra_headers)
            self.response = _nosdk.response
            self.rjson = _nosdk.rjson
            self.last_status_code = _nosdk.last_status_code
            return self

        def post(self, path, body=None, params={}, extra_headers=None, **kwargs):
            # Misma estrategia que get(): resource_post no expone un hook de
            # headers por-llamada, así que si se piden extra_headers delegamos
            # al cliente requests puro (get_mini/post_mini pattern).
            if extra_headers:
                return self.post_mini(path, body, params, extra_headers=extra_headers)
            try:
                atok = ("access_token" in params and params["access_token"]) or ""
                if atok:
                    del params["access_token"]
                if params:
                    path += "?" + urlencode(params)
                self.response = self.resource_post(resource=path, access_token=atok, body=body)
                self.rjson = self.response
            except _ApiException as e:
                self.rjson = {"error": "post error", "status": e.status, "cause": e.reason, "message": e.body}
            except:
                pass
            return self

        def post_mini(self, path, body=None, params={}, extra_headers=None, **kwargs):
            """POST sin SDK (requests directo) - para compatibilidad"""
            _nosdk = MeliApiNoSDK(config=configuration_nosdk)
            _nosdk.__dict__.update({k: v for k, v in self.__dict__.items()
                                     if k in ('client_id', 'client_secret', 'access_token',
                                              'refresh_token', 'redirect_uri', 'seller_id')})
            _nosdk.post(path, body, params, extra_headers=extra_headers)
            self.response = _nosdk.response
            self.rjson = _nosdk.rjson
            return self

        def put(self, path, body=None, params={}, extra_headers=None, **kwargs):
            try:
                atok = params.get("access_token", "") or ""
                headers = params.get("headers", {}) or {}
                if extra_headers:
                    headers = dict(headers)
                    headers.update(extra_headers)
                self.response = self.resource_put(resource=path, access_token=atok, body=body, headers=headers)
                self.rjson = self.response
            except _ApiException as e:
                self.rjson = {"error": "put error", "status": e.status, "cause": e.reason, "message": e.body}
            except:
                pass
            return self

        def put_mini(self, path, body=None, params={}, extra_headers=None, **kwargs):
            """PUT sin SDK (requests directo) - para compatibilidad"""
            _nosdk = MeliApiNoSDK(config=configuration_nosdk)
            _nosdk.__dict__.update({k: v for k, v in self.__dict__.items()
                                     if k in ('client_id', 'client_secret', 'access_token',
                                              'refresh_token', 'redirect_uri', 'seller_id')})
            _nosdk.put(path, body, params, extra_headers=extra_headers)
            self.response = _nosdk.response
            self.rjson = _nosdk.rjson
            return self

        def delete(self, path, params={}, extra_headers=None, **kwargs):
            try:
                atok = ("access_token" in params and params["access_token"]) or ""
                self.response = self.resource_delete(resource=path, access_token=atok)
                self.rjson = self.response
            except _ApiException as e:
                self.rjson = {"error": str(e), "status": e.status, "cause": e.reason, "message": e.body}
            except:
                pass
            return self

        def upload(self, path, files, params={}):
            try:
                atok = ("access_token" in params and params["access_token"]) or ""
                uri = configuration_sdk.host + str(path)
                self.response = requests.post(uri, files=files, params=urlencode({"access_token": atok}), headers={})
                self.rjson = self.response.json()
            except Exception as e:
                self.rjson = {"error": str(e)}
            return self

        def uploadfiles(self, path, files, params={}):
            try:
                atok = ("access_token" in params and params["access_token"]) or ""
                uri = configuration_sdk.host + str(path)
                headers = {'Authorization': 'Bearer ' + atok}
                self.response = requests.post(uri, files=files, params={}, headers=headers)
                self.rjson = self.response.json()
            except Exception as e:
                self.rjson = {"error": str(e)}
            return self

        def auth_url(self, redirect_URI=None, state=None):
            if redirect_URI:
                self.redirect_uri = redirect_URI
            # Mismo criterio que el backend NoSDK: el state lo emite el
            # controlador, que es quien puede atarlo a la sesion.
            params = {'client_id': self.client_id, 'response_type': 'code',
                      'redirect_uri': self.redirect_uri,
                      'state': state or str(datetime.now())}
            return self.AUTH_URL + '?' + urlencode(params)

        # redirect_login se elimino en PR G. Era la unica via por la que una
        # accion comercial podia construir una URL de autorizacion, y con eso
        # convertirse en un iniciador de OAuth fuera del flujo protegido.
        # Reconectar se hace desde res.company.meli_login o /meli_login.

        def authorize(self, code, redirect_uri=None):
            api_client = _ApiClient()
            api_auth_client = _meli_sdk.OAuth20Api(api_client)
            if redirect_uri:
                self.redirect_uri = redirect_uri
            response_info = api_auth_client.get_token(
                grant_type='authorization_code', client_id=self.client_id,
                client_secret=self.client_secret, redirect_uri=self.redirect_uri,
                code=code, refresh_token=self.refresh_token)
            if 'access_token' in response_info:
                self.access_token = response_info['access_token']
                self.refresh_token = response_info.get('refresh_token', '')
            return response_info

        def get_refresh_token(self, code=None, redirect_uri=None):
            api_client = _ApiClient()
            api_auth_client = _meli_sdk.OAuth20Api(api_client)
            # Mismo contrato que el backend NoSDK: el POST no toca las
            # credenciales del cliente, y devuelve MeliTokenOutcome para que el
            # caller pueda distinguir un rechazo de una incertidumbre de
            # transporte. El SDK no expone el codigo HTTP en el camino feliz.
            try:
                response_info = api_auth_client.get_token(
                    grant_type='refresh_token', client_id=self.client_id,
                    client_secret=self.client_secret,
                    refresh_token=self.refresh_token)
            except Exception as e:
                status = getattr(e, 'status', None)
                if status is None:
                    # Sin status: el POST salio y no se supo el resultado.
                    _logger.error("get_refresh_token transport failure: %s",
                                  type(e).__name__)
                    return MeliTokenOutcome(transport_uncertain=True,
                                            reason=type(e).__name__)
                return MeliTokenOutcome(payload=getattr(e, 'body', None),
                                        http_status=status,
                                        reason=type(e).__name__)
            return MeliTokenOutcome(payload=response_info, http_status=200)

        def get_sale_terms(self, category_id=None, sale_term_id=None, productjson=None):
            sale_terms_by_id = {}
            if category_id:
                res = self.get("/categories/" + str(category_id) + "/sale_terms")
                if res and res.rjson:
                    for rj in res.rjson:
                        stid = "id" in rj and rj["id"]
                        if stid:
                            sale_terms_by_id[stid] = rj
            if sale_term_id:
                if productjson and "sale_terms" in productjson:
                    for st in productjson["sale_terms"]:
                        if "id" in st and st["id"] == sale_term_id:
                            return st
                    return False
                if sale_term_id in sale_terms_by_id:
                    return sale_terms_by_id[sale_term_id]
            return sale_terms_by_id

        def get_user_product_stock_with_version(self, up_id, access_token):
            data, status, headers = self.resource_get_with_http_info(
                resource="user-products/{}/stock".format(up_id),
                access_token=access_token, _return_http_data_only=False)
            xver = None
            if headers:
                xver = headers.get('x-version') or headers.get('X-Version')
            return data, xver

else:
    # SDK no disponible: MeliApiSDK es None
    MeliApiSDK = None


# ---------------------------------------------------------------------------
#  Selección de implementación según USE_MELI_SDK
# ---------------------------------------------------------------------------
if _versions.USE_MELI_SDK and MeliApiSDK is not None:
    MeliApi = MeliApiSDK
    configuration = configuration_sdk
    _logger.info("MeliApi: using legacy SDK HTTP backend")
else:
    MeliApi = MeliApiNoSDK
    configuration = configuration_nosdk
    _logger.info("MeliApi: using NoSDK HTTP backend")


# Flag de proceso: loguear UNA sola vez que se omite el refresh por neutralización
# (evita spam del cron 'Get Meli State' cada 10 min). Se resetea al reiniciar el
# worker de Odoo (aceptable).
_NEUTRALIZED_REFRESH_LOGGED = False


class MeliUtil(models.AbstractModel):

    _name = 'meli.util'
    _description = 'Utilidades para Mercado Libre'

    def get_meli_state(self):
        return self.get_new_instance()

    def _meli_is_neutralized(self):
        """True si la DB está neutralizada (staging/duplicado en Odoo.sh).

        En esas DBs NUNCA se debe rotar el refresh_token de MercadoLibre: el POST
        grant_type=refresh_token rota el token del lado de ML (token rotativo de un
        solo uso) e invalidaría el access_token de PRODUCCIÓN. Odoo marca las copias
        neutralizadas con ir.config_parameter 'database.is_neutralized'.
        """
        try:
            val = self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized')
        except Exception:
            return False
        return str(val).strip().lower() in ('1', 'true', 't', 'yes')

    def _meli_log_neutralized_skip(self):
        """Loguea una sola vez por proceso que se omite el refresh por neutralización."""
        global _NEUTRALIZED_REFRESH_LOGGED
        if not _NEUTRALIZED_REFRESH_LOGGED:
            _logger.info("DB neutralizada: se omite refresh de token ML para no invalidar producción")
            _NEUTRALIZED_REFRESH_LOGGED = True

    # ------------------------------------------------------------------
    #  Refresh aislado y serializado
    # ------------------------------------------------------------------
    def _meli_auth_cursor(self):
        """Cursor propio para la transaccion AUTH.

        Metodo aparte a proposito: los tests lo reemplazan para poder observar
        QUE statements se emiten y EN QUE ORDEN, que es la parte sutil.
        """
        return self.env.registry.cursor()

    def _meli_persist_recovered(self, company_id, posted_refresh, access_token,
                                refresh_token, expiry_vals):
        """Persiste credenciales ya rotadas por ML, en una transaccion NUEVA.

        Sin un segundo POST, nunca. MercadoLibre ya consumio el refresh token
        anterior; volver a postear gastaria tambien el nuevo. Estos valores son
        lo unico que todavia puede alcanzar a MercadoLibre.

        Se vuelve a tomar exclusion y se distingue que se esta mirando:

            la fila ya tiene R2         el commit original si habia entrado
            la fila todavia tiene R1    persistir exactamente A2/R2
            la fila tiene otra cosa     hay una tercera generacion: NO pisarla
            no hay fila / rowcount != 1 no se puede afirmar nada

        El caller debe haber cerrado su cursor ANTES de llamar a esto: si la
        transaccion original siguiera viva podria conservar el FOR UPDATE sobre
        la misma fila y la recuperacion se bloquearia contra si misma.
        """
        cr = self._meli_auth_cursor()
        try:
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cr.execute("SHOW transaction_isolation")
            row = cr.fetchone()
            if str((row or [''])[0]).strip().lower() != _AUTH_ISOLATION:
                _logger.error("AUTH recovery refused: wrong isolation level")
                return False

            cr.execute(
                "SELECT id, access_token, refresh_token "
                "FROM mercadolibre_auth WHERE company_id = %s FOR UPDATE",
                (company_id,))
            auth_row = cr.fetchone()
            if not auth_row:
                _logger.error("AUTH recovery refused: no auth row for company "
                              "%s", company_id)
                return False

            auth_id, _stored_access, stored_refresh = auth_row

            if stored_refresh == refresh_token:
                # El commit original si habia entrado; lo que fallo fue saberlo.
                _logger.info("AUTH recovery: the rotated credentials were "
                             "already stored")
                return True

            if posted_refresh and stored_refresh != posted_refresh:
                # Otro proceso roto despues que nosotros. Pisarlo destruiria una
                # sesion mas nueva y viva. No se toca.
                _logger.error("AUTH recovery refused: the row holds a newer "
                              "generation than the one we rotated")
                return False

            cr.execute(_AUTH_UPDATE_SQL, (
                access_token, refresh_token, '',
                expiry_vals.get('mercadolibre_token_expires_in') or 0,
                expiry_vals.get('mercadolibre_token_refreshed_at') or None,
                expiry_vals.get('mercadolibre_token_expires_at') or None,
                auth_id))
            if cr.rowcount != 1:
                _logger.error("AUTH recovery refused: UPDATE affected %s rows",
                              cr.rowcount)
                return False
            cr.commit()
            return True
        except Exception as e:
            # Sin el detalle: el mensaje puede arrastrar las credenciales que
            # acabamos de recibir.
            _logger.error("AUTH recovery failed (%s): rotated credentials could "
                          "not be persisted", type(e).__name__)
            return False
        finally:
            cr.close()

    def _meli_refresh_locked(self, cr, company, api_client):
        """Todo lo que ocurre bajo el lock, hasta tener credenciales validas.

        Devuelve (early, auth_id, new_access, new_refresh, posted_refresh,
        response, summary). Si `early` no es None, es el resultado final y no
        hubo rotacion: el caller devuelve eso y no persiste nada.

        Esta separado del persist a proposito. Desde que MercadoLibre contesta
        una respuesta valida, R1 esta gastado y A2/R2 solo existen en memoria;
        esa parte necesita un manejo de fallos distinto, y mezclarlas hacia que
        un error de escritura se pareciera a un rechazo.
        """
        seen_refresh = api_client.refresh_token
        old_access = api_client.access_token
        old_refresh = seen_refresh
        seller_id = company.mercadolibre_seller_id
        nothing = (None, None, None, None, None, None)

        def early(status, reason=None, posted=False):
            return (MeliAuthResult(status, old_access, old_refresh,
                                   reason=reason, posted=posted),) + nothing

        # 1. Nada antes de esto.
        cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        # 2. Y comprobarlo: un SET que no aplico degradaria en silencio al
        #    primitivo que se MIDIO fallando, con apariencia de funcionar.
        cr.execute("SHOW transaction_isolation")
        row = cr.fetchone()
        level = str((row or [''])[0]).strip().lower()
        if level != _AUTH_ISOLATION:
            cr.rollback()
            _logger.error("AUTH transaction is at %r, not read committed: "
                          "refusing to refresh", level)
            return early('AUTH_CRITICAL', 'isolation level is %r' % level)

        # 3. Exclusion sobre el recurso exacto que se va a mutar.
        try:
            cr.execute(
                "SELECT id, access_token, refresh_token "
                "FROM mercadolibre_auth WHERE company_id = %s FOR UPDATE",
                (company.id,))
            auth_row = cr.fetchone()
        except psycopg2.errors.SerializationFailure as e:
            # READ COMMITTED no levanta 40001 en un FOR UPDATE. Si llega, la
            # transaccion no esta corriendo donde cree: abortar fuerte.
            cr.rollback()
            _logger.error("AUTH serialization failure (40001) under read "
                          "committed: %s", type(e).__name__)
            return early('ABORT_INDETERMINATE', 'serialization failure')

        if not auth_row:
            # Un FOR UPDATE que no matchea ninguna fila NO bloquea nada y no da
            # error. Seguir seria un refresh sin serializar con apariencia de
            # serializado.
            cr.rollback()
            _logger.error("no mercadolibre.auth row for company %s: refusing "
                          "to refresh unserialised", company.id)
            return early('AUTH_CRITICAL',
                         'no auth row for company %s' % company.id)

        auth_id, stored_access, stored_refresh = auth_row

        # 4. Re-evaluar por IDENTIDAD del refresh token, no por reloj: sin
        #    dependencia del clock skew, y sigue funcionando cuando ML no
        #    informa expires_in.
        if seen_refresh and stored_refresh and stored_refresh != seen_refresh:
            cr.rollback()
            _logger.info("refresh already performed by another process; not "
                         "posting")
            return (MeliAuthResult('ALREADY_FRESH', stored_access,
                                   stored_refresh),) + nothing

        # 5. La fila bloqueada es la UNICA autoridad sobre que token postear. El
        #    token del caller es solo marcador de generacion, nunca un fallback:
        #    postear uno que la fila no respalda es exactamente el
        #    comportamiento sin serializar que el lock existe para evitar.
        if not stored_refresh:
            cr.rollback()
            _logger.error("the locked auth row carries no refresh token: "
                          "manual OAuth required")
            return early('ABORT_REAUTH', 'no refresh token stored')

        # 6. Como mucho UN POST, con lo re-leido bajo el lock.
        api_client.refresh_token = stored_refresh
        api_client.access_token = stored_access or old_access
        try:
            outcome = api_client.get_refresh_token()
        except Exception as e:
            # Un fallo local de Python NO es una ambiguedad de transporte: el
            # backend marca esa condicion explicitamente. Aca no se puede
            # afirmar nada, asi que se aborta en vez de inventar.
            cr.rollback()
            _logger.error("refresh raised locally (%s)", type(e).__name__)
            return early('AUTH_CRITICAL',
                         'local failure during the token request')

        if not isinstance(outcome, MeliTokenOutcome):
            cr.rollback()
            _logger.error("token backend returned %s, not MeliTokenOutcome",
                          type(outcome).__name__)
            return early('AUTH_CRITICAL', 'token backend broke its contract')

        # 7. POST enviado sin respuesta: no se puede saber si ML consumio el
        #    token. Jamas se reintenta automaticamente.
        if outcome.transport_uncertain:
            cr.rollback()
            _logger.error("refresh uncertain (%s): the token request got no "
                          "answer", outcome.reason)
            return early('AUTH_UNCERTAIN', 'no answer to the token request',
                         posted=True)

        response = outcome.payload
        summary = meli_token_response_summary(response, seller_id)

        # 8. invalid_grant es la unica negativa que realmente habla del token.
        if isinstance(response, dict) and response.get('error') == 'invalid_grant':
            cr.rollback()
            _logger.error("refresh rejected: invalid_grant %s", summary)
            return early('ABORT_REAUTH', 'invalid_grant', posted=True)

        # 9. Clasificar por CODIGO HTTP, no por el cuerpo. 403/429/5xx no dicen
        #    nada sobre la validez del token; tratarlos como vencimiento
        #    dispararia un refresh innecesario sobre un token de un solo uso.
        status = outcome.http_status
        if status in _AUTH_INDETERMINATE_STATUS or (
                isinstance(status, int) and status >= 500):
            cr.rollback()
            _logger.error("refresh indeterminate: HTTP %s", status)
            return early('ABORT_INDETERMINATE', 'HTTP %s' % status,
                         posted=True)

        # 10. Validar antes de confiar, y antes de persistir.
        valid, reason = meli_validate_refresh_response(response, seller_id)
        if not valid:
            cr.rollback()
            _logger.error("refresh rejected (%s): %s", reason, summary)
            return early('REJECTED', reason, posted=True)

        return (None, auth_id, response['access_token'],
                response['refresh_token'], stored_refresh, response, summary)

    def _meli_refresh_credentials(self, company, api_client):
        """Renueva el token en su PROPIA transaccion, serializado en la fila auth.

        El orden es obligatorio y esta asertado por tests. Bajo REPEATABLE READ
        el snapshot lo fija el PRIMER statement de la transaccion, asi que
        cualquier consulta previa al SET lo congelaria y el re-read posterior al
        lock no podria ver el commit del proceso que tuvo el lock antes. Eso se
        MIDIO contra PostgreSQL real con dos conexiones: con advisory lock bajo
        REPEATABLE READ el que espera sigue viendo R1 y postea un token ya
        gastado. Por eso READ COMMITTED + FOR UPDATE.
        """
        cr = self._meli_auth_cursor()
        early = None
        auth_id = new_access = new_refresh = posted_refresh = None
        response = summary = None
        expiry = {}
        persisted = False
        try:
            (early, auth_id, new_access, new_refresh, posted_refresh,
             response, summary) = self._meli_refresh_locked(
                cr, company, api_client)

            if early is None:
                # FASE PROTEGIDA. Desde aca MercadoLibre YA rotó: R1 esta
                # gastado y A2/R2 solo existen en memoria. Derivar la vigencia,
                # el UPDATE y el COMMIT son una sola fase: si cualquiera falla,
                # se recupera persistiendo esos mismos valores, nunca posteando
                # de nuevo.
                try:
                    expiry = meli_token_expiry_vals(response)
                    cr.execute(_AUTH_UPDATE_SQL, (
                        new_access, new_refresh, '',
                        expiry.get('mercadolibre_token_expires_in') or 0,
                        expiry.get('mercadolibre_token_refreshed_at') or None,
                        expiry.get('mercadolibre_token_expires_at') or None,
                        auth_id))
                    cr.commit()
                    persisted = True
                except Exception as e:
                    _logger.error(
                        "AUTH persist failed after MercadoLibre rotated the "
                        "token (%s); recovering without a second POST",
                        type(e).__name__)
        finally:
            # El cursor original se libera SIEMPRE, y en particular antes de
            # abrir la recuperacion: si siguiera vivo podria conservar el FOR
            # UPDATE sobre la misma fila y la recuperacion se bloquearia contra
            # si misma.
            cr.close()

        if early is not None:
            return early

        if persisted:
            _logger.info("refresh committed: %s", summary)
            return MeliAuthResult('REFRESHED', new_access, new_refresh,
                                  posted=True)

        if self._meli_persist_recovered(company.id, posted_refresh, new_access,
                                        new_refresh, expiry):
            return MeliAuthResult(
                'REFRESHED', new_access, new_refresh, posted=True,
                reason='persisted by recovery after a failed persist')
        return MeliAuthResult(
            'AUTH_CRITICAL', new_access, new_refresh, posted=True,
            reason='rotated credentials could not be persisted')

    def _meli_require_reconnect(self):
        """Corta la operacion: la conexion necesita intervencion administrativa.

        Existe para que ninguna accion comercial tenga que decidir por su
        cuenta que hacer cuando need_login() es verdadero. Antes cada una
        devolvia meli.redirect_login(), o sea que publicar, pausar, cerrar,
        subir una imagen, importar una categoria o imprimir un envio ERAN
        iniciadores de OAuth, alcanzables por quien pudiera correr esa accion y
        por fuera del flujo protegido.

        Reconectar es un acto administrativo y vive en la configuracion de la
        compania, no en el formulario de un producto.

        El mensaje es deliberadamente neutro: no nombra URL, state, vendedor,
        compania, token, client secret ni code.
        """
        raise UserError(_(
            "MercadoLibre requires a new authorization. A system administrator "
            "must reconnect the account from the company settings."))

    def _build_client(self, company):
        """Arma el cliente y nada mas: sin red, sin refresh, sin escrituras.

        Separado de get_new_instance a proposito. Obtener un objeto cliente y
        rotar credenciales son dos cosas distintas, y mezclarlas hacia que
        cualquier camino que solo necesitaba un cliente pudiera terminar
        gastando un refresh token de un solo uso.

        Lo usan el callback de OAuth y cualquier camino no autenticado.

        Es TAMBIEN la unica frontera privada de capacidad: el codigo interno
        consume las credenciales a traves de este cliente, no leyendolas como
        datos. Privado a proposito -el guion bajo lo deja fuera de RPC- y con
        la compania explicita, nunca deducida del usuario en curso.
        """
        # Una sola compania: con un recordset de varias no hay forma de saber
        # de cual son las credenciales que se estan por cargar.
        company.ensure_one()

        # Proxy de rescate: si la empresa tiene configurado un host alternativo,
        # rutear la API (y el OAuth, via _abs_url) por ese reverse proxy.
        api_host = company.mercadolibre_http_proxy or "https://api.mercadolibre.com"
        use_custom_host = api_host != "https://api.mercadolibre.com"

        # Crear instancia de MeliApi segun modo activo (SDK o requests)
        if _versions.USE_MELI_SDK and MeliApiSDK is not None:
            if use_custom_host:
                sdk_config = _meli_sdk.Configuration(host=api_host)
                # Host de rescate: sin auto-retry (los 429 agravan el rate-limit del proxy).
                sdk_config.retries = False
                api_client = _ApiClient(configuration=sdk_config)
            else:
                api_client = _ApiClient(configuration=configuration_sdk)
            api_rest_client = MeliApi(api_client)
        else:
            if use_custom_host:
                config = MeliConfiguration(host=api_host)
                # Host de rescate: sin auto-retry (los 429 agravan el rate-limit del proxy).
                # Espeja la rama SDK: el config fresco por-host NO debe heredar el Retry
                # por defecto (status_forcelist=[413,429,503]); reintentar contra el proxy
                # solo amplifica el bloqueo. Resiliencia = sin reintentos in-band.
                config.retries = False
            else:
                config = configuration_nosdk
            api_rest_client = MeliApi(config=config)
        # ---- La frontera de capacidad ----------------------------------
        # Los secretos NO se leen por la fachada publica de res.company. Esos
        # campos van a quedar restringidos a base.group_system, y todo proceso
        # interno que corre como usuario comercial -un cron, una importacion de
        # pedidos, una notificacion- dejaria de poder autenticarse.
        #
        # El sudo() es angosto a proposito: la fila auth de ESTA compania y el
        # campo concreto del client secret. Nada de pedidos, productos,
        # publicaciones ni ninguna operacion comercial.
        #
        # Lo que sale de aca es un cliente configurado, no un diccionario de
        # secretos: devolver los valores solo correria la exposicion una
        # llamada mas afuera.
        auth = company._meli_auth_row()
        api_rest_client.client_id = company.mercadolibre_client_id
        api_rest_client.client_secret = company.sudo().mercadolibre_secret_key
        api_rest_client.access_token = (auth and auth.access_token) or ''
        api_rest_client.refresh_token = (auth and auth.refresh_token) or False
        api_rest_client.redirect_uri = company.mercadolibre_redirect_uri
        api_rest_client.seller_id = company.mercadolibre_seller_id
        # AUTH_URL queda en el default de clase a proposito. Resolverlo llama a
        # get_ML_AUTH_URL -> _get_ML_sites, que hace un GET /sites: red, y este
        # constructor no la toca. Lo resuelven los dos consumidores que de verdad
        # lo necesitan (get_new_instance y el callback de OAuth).
        api_rest_client.needlogin_state = False
        return api_rest_client

    def _meli_identity_probe(self, api_rest_client, company):
        """GET /users/{seller}. No refresca, no escribe.

        Devuelve (status_code, rjson, response). rjson es None si el cuerpo no
        se puede parsear: un cuerpo ilegible no dice nada sobre el token.
        """
        response = api_rest_client.get(
            "/users/" + str(company.mercadolibre_seller_id),
            {'access_token': api_rest_client.access_token})
        try:
            rjson = response.json()
        except Exception:
            rjson = None
        status = getattr(api_rest_client, "last_status_code", None)
        return status, rjson, response

    def _meli_refresh_is_due(self, company, refresh_force=False):
        """Decide si corresponde renovar, ANTES de mirar ninguna respuesta.

        Solo dos razones se pueden saber de antemano: que alguien lo pida
        explicitamente, o que la vigencia que informo MercadoLibre ya haya
        pasado. La tercera, un 401 en el probe de identidad, se evalua despues.

        Deliberadamente NO son razones: 403, timeout, cuerpo ilegible, 5xx, ni
        la presencia de una clave 'error'. Ninguna dice que el token vencio, y
        cada renovacion gasta una credencial de un solo uso.
        """
        if refresh_force:
            return "refresh_force"
        expires_at = company.mercadolibre_token_expires_at
        if expires_at and expires_at <= fields.Datetime.now():
            return "reported expiry already past"
        return None

    def _meli_save_seller_tags(self, company, rjson):
        """Guarda lo que el probe sano devolvio sobre la cuenta."""
        if not isinstance(rjson, dict):
            return
        if "mercadolibre_user_product_seller" in company._fields:
            value = ("tags" in rjson and "user_product_seller" in rjson["tags"])
            if company.mercadolibre_user_product_seller != value:
                company.mercadolibre_user_product_seller = value
        if "mercadolibre_multiwarehouse" in company._fields:
            value = ("tags" in rjson and "multiwarehouse" in rjson["tags"])
            if company.mercadolibre_multiwarehouse != value:
                company.mercadolibre_multiwarehouse = value

    @api.model
    def get_new_instance(self, company=None, refresh_force=False):
        """Frontera autenticada central. Renueva COMO MUCHO UNA VEZ por llamada.

        Disparadores, y solo estos tres:
            refresh_force
            la vigencia informada por ML ya vencida
            el probe de identidad devuelve 401

        Nunca: 403, timeout, cuerpo ilegible, 5xx, ni una clave 'error' en el
        body. El gate anterior era exactamente eso ultimo, con lo cual un 403
        podia renovar y un 401 real de ML -que no trae 'error'- no.
        """
        if not company:
            company = self.env.user.company_id

        api_rest_client = self._build_client(company)
        api_rest_client.AUTH_URL = company.get_ML_AUTH_URL(meli=api_rest_client)
        last_token = api_rest_client.access_token
        message = "Login to ML needed in Odoo."

        try:
            if company.mercadolibre_seller_id == False or api_rest_client.access_token == '':
                api_rest_client.needlogin_state = True
            else:
                reason = self._meli_refresh_is_due(company, refresh_force)

                if reason is None:
                    status, rjson, response = self._meli_identity_probe(
                        api_rest_client, company)

                    # Indeterminado: no se puede afirmar nada del token.
                    # Se devuelve el cliente tal cual, sin renovar.
                    body_status = isinstance(rjson, dict) and rjson.get("status")
                    body_cause = isinstance(rjson, dict) and rjson.get("cause")
                    if body_status == 429:
                        return api_rest_client
                    if body_status == 500 and body_cause == "Internal Server Error":
                        return api_rest_client
                    if body_status == 504 and body_cause == "Gateway Time-out":
                        return api_rest_client
                    if body_cause and body_status and int(body_status) >= 500:
                        return api_rest_client
                    if status and int(status) >= 500:
                        return api_rest_client

                    # 403 marca la sesion como no utilizable en ESTE probe, pero
                    # no renueva: no dice que el token haya vencido.
                    if status == 403:
                        api_rest_client.needlogin_state = True
                        return api_rest_client

                    right_access_token = (
                        ("-" + str(api_rest_client.seller_id))
                        in str(api_rest_client.access_token))
                    if not right_access_token:
                        api_rest_client.needlogin_state = True
                        return api_rest_client

                    if status == 401:
                        reason = "identity probe returned 401"
                    else:
                        # Sesion sana. Nada que renovar.
                        if isinstance(rjson, dict):
                            response.user = rjson
                            self._meli_save_seller_tags(company, rjson)
                        return api_rest_client

                # ---- Renovacion. Una sola vez, pase lo que pase despues. ----
                api_rest_client.needlogin_state = True

                if self._meli_is_neutralized():
                    # DB neutralizada (staging/duplicado): NUNCA rotar el refresh
                    # token, porque el POST lo rota server-side en ML y le roba la
                    # sesion a PRODUCCION.
                    self._meli_log_neutralized_skip()
                    return api_rest_client

                internals = {
                    "application_id": company.mercadolibre_client_id,
                    "user_id": company.mercadolibre_seller_id,
                    "topic": "internal",
                    "resource": "get_new_instance #" + str(company.name),
                    "state": "PROCESSING",
                }
                noti = self.env["mercadolibre.notification"].start_internal_notification(internals)
                errors = ""
                logs = "refresh due: %s\n" % reason

                try:
                    # El refresh corre en su PROPIA transaccion, serializado sobre
                    # la fila auth, y vuelve EN MEMORIA: el snapshot de esta
                    # transaccion es anterior a ese commit y nunca lo va a incluir.
                    auth = self._meli_refresh_credentials(company, api_rest_client)
                    logs += "refresh: %s\n" % auth.status
                    if auth.access_token:
                        api_rest_client.access_token = auth.access_token
                    if auth.refresh_token:
                        api_rest_client.refresh_token = auth.refresh_token
                    if auth.usable:
                        api_rest_client.code = ''
                        # Una unica validacion de identidad. Si vuelve a fallar se
                        # informa, pero NO se renueva otra vez: eso gastaria la
                        # credencial que acabamos de obtener.
                        status, rjson, response = self._meli_identity_probe(
                            api_rest_client, company)
                        if status == 200 and isinstance(rjson, dict):
                            api_rest_client.needlogin_state = False
                            response.user = rjson
                            self._meli_save_seller_tags(company, rjson)
                            logs += "identity revalidated\n"
                        else:
                            logs += "identity still not usable after refresh\n"
                            message = "identity check failed after refresh"
                    else:
                        errors += "refresh %s: %s\n" % (auth.status, auth.reason or "")
                        message = auth.reason or auth.status
                        _logger.error("refresh not usable: %s (%s)",
                                      auth.status, auth.reason)
                except Exception as e:
                    safe = meli_redact(
                        e, api_rest_client.access_token,
                        api_rest_client.refresh_token,
                        api_rest_client.client_secret)
                    errors += safe
                    logs += safe
                    _logger.error("refresh raised: %s", safe)

                noti.stop_internal_notification(errors=errors, logs=logs)


            #        except requests.exceptions.HTTPError as e:
            #            _logger.info( "And you get an HTTPError:", e.message )

        except requests.exceptions.ConnectionError as e:
            #raise osv.except_osv( _('MELI WARNING'), _('NO INTERNET CONNECTION TO API.MERCADOLIBRE.COM: complete the Cliend Id, and Secret Key and try again'))
            api_rest_client.needlogin_state = True
            error_msg = 'MELI WARNING: NO INTERNET CONNECTION TO API.MERCADOLIBRE.COM: complete the Cliend Id, and Secret Key and try again '
            _logger.error(error_msg)

        if api_rest_client.access_token=='' or api_rest_client.access_token==False:
            api_rest_client.needlogin_state = True

        try:
            if api_rest_client.needlogin_state:
                _logger.warning("Need login for "+str(company.name))

                # IMPORTANTE: NO se borran mercadolibre_access_token/refresh_token/code
                # ni se apaga mercadolibre_cron_refresh. El refresh_token de ML sigue
                # siendo válido aunque el access_token haya vencido, y conservarlo
                # permite recuperar la conexión en la próxima corrida del cron.
                if (company.mercadolibre_cron_refresh and company.mercadolibre_cron_mail):
                    # we put the job_exception in context to be able to print it inside
                    # the email template
                    context = {
                        'job_exception': message,
                        'dbname': MeliCr( self ).dbname,
                    }

                    _logger.info(
                        "Sending scheduler error email with context=%s", context)
                    _logger.info("Sending to company:" + str(company.name)+ " mail:" + str(company.email)  )
                    rese = self.env['mail.template'].browse(
                                company.mercadolibre_cron_mail.id
                            ).with_context(context).sudo().send_mail( (company.id), force_send=True)
                    _logger.info("Result sending:" + str(rese) )

        except Exception as e:
            _logger.error(e)

        for comp in company:
            if (last_token!=comp.mercadolibre_access_token):#comp.mercadolibre_state!=api_rest_client.needlogin_state:
                _logger.info("mercadolibre_state : "+str(api_rest_client.needlogin_state))
                comp.mercadolibre_state = api_rest_client.needlogin_state
            #else:
            #    _logger.info("mercadolibre_state already set: "+str(api_rest_client.needlogin_state))

        return api_rest_client

    def convert_to_datetime(self, date_str):
        if not date_str:
            return False
        date_str = date_str.replace('T', ' ')
        date_convert = fields.Datetime.from_string(date_str)
        fields_model = self.env['ir.fields.converter']
        from_zone = fields_model._input_tz()
        to_zone = pytz.UTC
        #si no hay informacion de zona horaria, establecer la zona horaria
        if not date_convert.tzinfo:
            date_convert = from_zone.localize(date_convert)
        date_convert = date_convert.astimezone(to_zone)
        return date_convert




"""    {
"id": "WARRANTY_TYPE",
"name": "Tipo de garantía",
"tags": {
},
"hierarchy": "SALE_TERMS",
"relevance": 2,
"value_type": "list",
"values": [
{
"id": "2230280",
"name": "Garantía del vendedor"
},
{
"id": "2230279",
"name": "Garantía de fábrica"
},
{
"id": "6150835",
"name": "Sin garantía"
}
],
"attribute_group_id": "OTHERS",
"attribute_group_name": "Otros"
}
"""
