# -*- coding: utf-8 -*-
"""Contract tests for the requests-based MercadoLibre HTTP backend
(``MeliApiNoSDK``).

These tests exercise the backend in full isolation: the ``requests.Session`` is
replaced by a recording fake, so NO real network I/O and NO credentials are
used. They pin down the *current, observed* behaviour of ``MeliApiNoSDK`` so it
can serve as a safety net before the module's default backend is switched away
from the SDK.

Scope note: this file only READS the backend. It does not modify
``USE_MELI_SDK``, ``MELI_SDK_AVAILABLE``, the backend selection, requirements, or
any business model. Where the observed behaviour is noteworthy for the upcoming
backend switch, it is documented in the test docstring rather than "fixed".
"""

from unittest.mock import patch

import requests

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import (
    MeliApiNoSDK,
    configuration_nosdk,
)

_TOKEN = "APP_USR-FAKE-ACCESS-0001"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _RecordingSession:
    """Stands in for requests.Session; records every call, returns a canned
    response (or raises a canned exception)."""

    def __init__(self, response=None, exc=None):
        self.response = response if response is not None else _FakeResponse(json_data={})
        self.exc = exc
        self.calls = []  # list of dicts: {method, url, kwargs}

    def _record(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.response

    def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def put(self, url, **kwargs):
        return self._record("PUT", url, kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, kwargs)


def _make_client(response=None, exc=None):
    client = MeliApiNoSDK(config=configuration_nosdk)
    client._session = _RecordingSession(response=response, exc=exc)
    client.access_token = _TOKEN
    client.seller_id = "123456"
    return client


@tagged("post_install", "-at_install")
class TestHttpBackendContract(TransactionCase):

    # ---- helpers ---------------------------------------------------------

    def _last(self, client):
        self.assertTrue(client._session.calls, "no HTTP call was recorded")
        return client._session.calls[-1]

    def _auth_header(self, call):
        return (call["kwargs"].get("headers") or {}).get("Authorization")

    def _query_of(self, call):
        # For GET/POST the query is baked into the URL; for PUT it is passed as
        # the `params` kwarg. Return a single string to search for leaks.
        url = call["url"]
        params = call["kwargs"].get("params")
        return "%s | %s" % (url, params)

    # ---- 1-4: verbs use Bearer, stay on api.mercadolibre.com, no token in URL

    def test_get_uses_bearer_and_host(self):
        c = _make_client(_FakeResponse(json_data={"id": "1"}))
        c.get("/orders/123", {"access_token": _TOKEN})
        call = self._last(c)
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].startswith("https://api.mercadolibre.com/orders/123"))
        self.assertEqual(self._auth_header(call), "Bearer %s" % _TOKEN)
        self.assertNotIn("access_token", self._query_of(call))
        self.assertNotIn(_TOKEN, call["url"])

    def test_post_uses_bearer_and_host(self):
        c = _make_client(_FakeResponse(json_data={"ok": True}))
        c.post("/items", {"title": "x"}, {"access_token": _TOKEN})
        call = self._last(c)
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].startswith("https://api.mercadolibre.com/items"))
        self.assertEqual(self._auth_header(call), "Bearer %s" % _TOKEN)
        self.assertNotIn("access_token", self._query_of(call))
        # JSON body is sent via the `json` kwarg.
        self.assertEqual(call["kwargs"].get("json"), {"title": "x"})

    def test_put_uses_bearer_and_host(self):
        c = _make_client(_FakeResponse(json_data={"ok": True}))
        c.put("/items/MLA1", {"available_quantity": 5}, {"access_token": _TOKEN})
        call = self._last(c)
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].startswith("https://api.mercadolibre.com/items/MLA1"))
        self.assertEqual(self._auth_header(call), "Bearer %s" % _TOKEN)
        self.assertNotIn("access_token", self._query_of(call))

    def test_delete_uses_bearer_and_host(self):
        c = _make_client(_FakeResponse(json_data={}))
        c.delete("/items/MLA1", {"access_token": _TOKEN})
        call = self._last(c)
        self.assertEqual(call["method"], "DELETE")
        self.assertTrue(call["url"].startswith("https://api.mercadolibre.com/items/MLA1"))
        self.assertEqual(self._auth_header(call), "Bearer %s" % _TOKEN)
        self.assertNotIn("access_token", self._query_of(call))

    # ---- 5: timeouts -----------------------------------------------------

    def test_default_timeout_is_20s(self):
        c = _make_client(_FakeResponse(json_data={}))
        c.get("/orders/1", {"access_token": _TOKEN})
        self.assertEqual(self._last(c)["kwargs"].get("timeout"), 20)

    def test_custom_timeout_is_forwarded(self):
        c = _make_client(_FakeResponse(json_data={}))
        c.get("/orders/1", {"access_token": _TOKEN, "timeout": 5})
        self.assertEqual(self._last(c)["kwargs"].get("timeout"), 5)

    def test_post_put_delete_default_timeout_is_20s(self):
        for verb, call in (
            ("post", lambda c: c.post("/x", {"a": 1}, {"access_token": _TOKEN})),
            ("put", lambda c: c.put("/x", {"a": 1}, {"access_token": _TOKEN})),
            ("delete", lambda c: c.delete("/x", {"access_token": _TOKEN})),
        ):
            c = _make_client(_FakeResponse(json_data={}))
            call(c)
            self.assertEqual(
                self._last(c)["kwargs"].get("timeout"), 20,
                "%s did not forward the default 20s timeout" % verb,
            )

    # ---- 6: HTTP error statuses -----------------------------------------
    #
    # OBSERVED BEHAVIOUR (documented, not changed):
    # For HTTP 4xx/5xx, MeliApiNoSDK does NOT raise and does NOT synthesise a
    # structured error dict. It returns `self` with `self.rjson` set to the
    # PARSED SERVER BODY (pass-through) and logs a warning. Only transport-level
    # exceptions (see test_network_exception_returns_error_dict) produce the
    # synthetic {"error": ..., "status": 0, ...} envelope. Business code relies
    # on inspecting `rjson` for an "error"/"message" key, so this pass-through is
    # the current contract.

    def test_http_error_bodies_are_passed_through(self):
        for status in (400, 401, 429, 500):
            body = {"error": "some_error", "status": status, "message": "boom"}
            c = _make_client(_FakeResponse(status_code=status, json_data=body))
            result = c.get("/orders/1", {"access_token": _TOKEN})
            self.assertIs(result, c, "get() must return self even on HTTP %s" % status)
            self.assertEqual(
                c.rjson, body,
                "HTTP %s body should be passed through unchanged" % status,
            )

    def test_network_exception_returns_error_dict(self):
        c = _make_client(exc=requests.ConnectionError("boom"))
        c.get("/orders/1", {"access_token": _TOKEN})
        self.assertEqual(c.rjson.get("error"), "get error")
        self.assertEqual(c.rjson.get("status"), 0)
        self.assertEqual(c.rjson.get("cause"), "request_exception")

    # ---- 7: OAuth (mocked) ----------------------------------------------

    def test_authorize_posts_to_token_url_without_secret_in_query(self):
        token_resp = _FakeResponse(json_data={
            "access_token": "NEW-ACCESS-1", "refresh_token": "NEW-REFRESH-1",
        })
        c = _make_client(token_resp)
        c.client_id = "CLIENT-1"
        c.client_secret = "SECRET-1"
        result = c.authorize("AUTHCODE-1", redirect_uri="https://example.test/cb")
        call = self._last(c)
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], MeliApiNoSDK.TOKEN_URL)
        self.assertNotIn("SECRET-1", call["url"], "client_secret must not be in the URL")
        self.assertNotIn("access_token", call["url"])
        # secret + code travel in the form body, not the query string.
        data = call["kwargs"].get("data") or {}
        self.assertEqual(data.get("client_secret"), "SECRET-1")
        self.assertEqual(data.get("code"), "AUTHCODE-1")
        self.assertEqual(call["kwargs"].get("timeout"), 30)
        # tokens are captured on the client and returned.
        self.assertEqual(c.access_token, "NEW-ACCESS-1")
        self.assertEqual(c.refresh_token, "NEW-REFRESH-1")
        self.assertEqual(result.get("access_token"), "NEW-ACCESS-1")

    def test_get_refresh_token_uses_token_url(self):
        token_resp = _FakeResponse(json_data={
            "access_token": "REFRESHED-ACCESS", "refresh_token": "REFRESHED-REFRESH",
        })
        c = _make_client(token_resp)
        c.client_id = "CLIENT-1"
        c.client_secret = "SECRET-1"
        c.refresh_token = "OLD-REFRESH"
        result = c.get_refresh_token()
        call = self._last(c)
        self.assertEqual(call["url"], MeliApiNoSDK.TOKEN_URL)
        data = call["kwargs"].get("data") or {}
        self.assertEqual(data.get("grant_type"), "refresh_token")
        self.assertNotIn("access_token", call["url"])
        self.assertEqual(c.access_token, "REFRESHED-ACCESS")
        self.assertEqual(result.get("access_token"), "REFRESHED-ACCESS")

    # ---- 8: User Products / x-version -----------------------------------

    def test_user_product_stock_reads_x_version_header(self):
        resp = _FakeResponse(
            json_data={"available_quantity": 7},
            headers={"x-version": "42"},
        )
        c = _make_client(resp)
        data, xver = c.get_user_product_stock_with_version("UPID1", _TOKEN)
        call = self._last(c)
        self.assertEqual(xver, "42")
        self.assertEqual(data, {"available_quantity": 7})
        self.assertTrue(call["url"].endswith("/user-products/UPID1/stock"))
        self.assertEqual(self._auth_header(call), "Bearer %s" % _TOKEN)
        self.assertEqual(call["kwargs"].get("timeout"), 20)

    # ---- 9: extra headers (x-version: 2) --------------------------------

    def test_extra_headers_are_sent(self):
        c = _make_client(_FakeResponse(json_data={}))
        c.get("/orders/1", {"access_token": _TOKEN}, extra_headers={"x-version": "2"})
        headers = self._last(c)["kwargs"].get("headers") or {}
        self.assertEqual(headers.get("x-version"), "2")
        self.assertEqual(headers.get("Authorization"), "Bearer %s" % _TOKEN)

    # ---- 10: security regression ----------------------------------------
    #
    # No verb of MeliApiNoSDK may ever transport the access token as a query
    # parameter. The token must appear only in the Authorization: Bearer header.

    def test_access_token_never_in_query_string(self):
        cases = (
            ("GET", lambda c: c.get("/orders/1", {"access_token": _TOKEN, "offset": 10})),
            ("POST", lambda c: c.post("/items", {"a": 1}, {"access_token": _TOKEN, "q": "z"})),
            ("PUT", lambda c: c.put("/items/1", {"a": 1}, {"access_token": _TOKEN})),
            ("DELETE", lambda c: c.delete("/items/1", {"access_token": _TOKEN})),
        )
        for verb, run in cases:
            c = _make_client(_FakeResponse(json_data={}))
            run(c)
            call = self._last(c)
            haystack = self._query_of(call)
            self.assertNotIn("access_token", haystack, "%s leaked access_token in query" % verb)
            self.assertNotIn(_TOKEN, call["url"], "%s leaked the token value in the URL" % verb)
            self.assertEqual(
                self._auth_header(call), "Bearer %s" % _TOKEN,
                "%s did not send the token as a Bearer header" % verb,
            )
