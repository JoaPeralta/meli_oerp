# -*- coding: utf-8 -*-
"""Reading a posting must not crash when MercadoLibre answers with an error.

``mercadolibre.posting.posting_update`` is a non-stored computed field:

    posting_update = fields.Char( compute=_posting_update, store=False )

so every read of a posting record calls ``posting_query_questions()``, which
fetches ``/items/{meli_id}`` and then decides what to do by inspecting keys of
the parsed body:

    if "error" in product_json:
        ML_status = product_json["error"]
    else:
        ML_status = product_json["status"]      # unguarded

That is the same body-key heuristic that failed for authentication. A 401 from
MercadoLibre carries neither ``error`` nor ``status``, so the ``else`` runs and
raises ``KeyError``. A non-JSON answer makes ``_parse_response`` return the raw
text, so ``product_json`` is a ``str``: ``"error" in product_json`` silently
becomes a substring test and the index raises ``TypeError``.

Observed in production on 2026-07-26 at 08:06:34, simply from browsing the
postings list while the token was invalid.

Minimum shape indexed here without a guard of its own: ``dict`` and ``status``
(``error``, ``permalink`` and ``price`` already carry their own ``in`` check).

Out of scope, deliberately: that a computed field performs HTTP calls and
writes at all — a much larger design finding — and everything outside
``posting_query_questions``.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_ITEM = "MLA_POSTING_UPDATE"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeMeli:
    def __init__(self, payload):
        self.access_token = "TEST-TOKEN"
        self.payload = payload
        self.fetched = []

    def need_login(self):
        return False

    def redirect_login(self):
        return {}

    def get(self, path, params=None, extra_headers=None, **kwargs):
        self.fetched.append(str(path))
        return _FakeResponse(self.payload)


@tagged("post_install", "-at_install")
class TestPostingUpdateErrorResponse(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        # Keep the flow inside the item block: the questions endpoint is a
        # separate concern and is not what these tests are about.
        self.company.mercadolibre_cron_get_questions = False
        self.product = self.env["product.product"].create({
            "name": "Posting update product", "default_code": "SKU-POSTING-UPD",
        })
        self.posting = self.env["mercadolibre.posting"].create({
            "meli_id": _ITEM,
            "product_id": self.product.id,
            "name": "Post (%s)" % _ITEM,
        })

    def _read_with(self, payload):
        fake = _FakeMeli(payload)
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            self.posting.invalidate_recordset()
            value = self.posting.posting_update
        return value, fake

    def test_auth_error_body_without_error_or_status(self):
        """What production returned: neither key is present."""
        body = {"message": "invalid access token"}
        self.assertNotIn("error", body)
        self.assertNotIn("status", body)

        value, fake = self._read_with(body)

        self.assertTrue(fake.fetched, "the item was never fetched")
        self.assertEqual(value, "ok", "reading the posting raised instead of coping")

    def test_non_json_body_is_a_string(self):
        """A proxy error page: product_json is a str."""
        value, _fake = self._read_with("<html><body>502 Bad Gateway</body></html>")

        self.assertEqual(value, "ok", "reading the posting raised on a non-JSON body")

    def test_nothing_is_written_from_an_unusable_response(self):
        """An unreadable answer must not overwrite what we already know."""
        self.posting.meli_status = "active"

        self._read_with({"message": "invalid access token"})

        self.assertEqual(
            self.posting.meli_status, "active",
            "an error response overwrote the stored status",
        )

    # ------------------------------------------------------------------
    # regression guards: the two paths that already worked
    # ------------------------------------------------------------------
    def test_error_body_is_still_tolerated(self):
        """A body carrying 'error' was already handled; it must stay that way."""
        value, _fake = self._read_with({"error": "not_found", "message": "Item not found"})

        self.assertEqual(value, "ok")

    def test_valid_item_still_updates_the_posting(self):
        """The happy path must not move."""
        payload = {
            "id": _ITEM, "status": "paused",
            "permalink": "https://articulo.mercadolibre.com.ar/MLA-1",
            "price": 1234.0,
        }

        value, _fake = self._read_with(payload)

        self.assertEqual(value, "ok")
        self.assertEqual(self.posting.meli_status, "paused")
        self.assertEqual(self.posting.meli_permalink, payload["permalink"])
        self.assertEqual(self.posting.meli_price, payload["price"])
