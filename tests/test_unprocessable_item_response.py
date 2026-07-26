# -*- coding: utf-8 -*-
"""An item response that cannot be processed must skip the item, not the batch.

``product_meli_get_products`` fetches each publication and then indexes the
parsed body directly:

    'meli_status': rjson3['status']     # x3, unguarded
    'name': rjson3['title']             # missing branch, unguarded
    'name': str(rjson3['title'])        # creation branch, guarded only by 'id'

MercadoLibre does not always answer with an item. A 401 body carries neither
``id`` nor ``status``; a non-JSON answer (a proxy error page) makes
``_parse_response`` return the raw text, so ``rjson3`` is a ``str``. Either way
the direct index raises, and because the ``try`` wraps the WHOLE loop rather
than each item, the exception ends in the generic handler and ``MeliRollback``.

That rollback is the real damage. ``MeliCommit`` is ``flush_all()``, not
``cr.commit()``, and there is no savepoint in the wizard path, so the whole run
lives in a single transaction: one unreadable item discards every product
matched and every posting created in that run, and the remaining items are never
processed.

Minimum shape required before entering the branches — exactly the fields this
block indexes without a guard of its own:

    dict  and  'id'  and  'status'  and  'title'

``seller_custom_field``, ``attributes`` and ``variations`` are deliberately NOT
part of it: each already carries its own ``in`` check, and adding them would
turn this into schema validation.

Out of scope, kept as-is on purpose: the generic ``except`` and ``MeliRollback``
themselves (any *other* exception can still roll back a whole run — separate
finding), ``posting.py``, the CSV/report, and how skipped items are surfaced to
the user.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_ITEM_A = "MLA_BATCH_A"
_ITEM_B = "MLA_BATCH_B"
_ITEM_C = "MLA_BATCH_C"

_SKU_A = "SKU-BATCH-A"
_SKU_B = "SKU-BATCH-B"
_SKU_C = "SKU-BATCH-C"


def _item(item_id, sku, title="Test publication", status="active"):
    return {"id": item_id, "title": title, "status": status,
            "seller_custom_field": sku}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        # Mirrors _parse_response: a non-JSON body comes back as raw text.
        return self._payload


class _FakeMeli:
    """Answers each item id with whatever payload the test asked for."""

    def __init__(self, payloads):
        self.access_token = "TEST-TOKEN"
        self._payloads = payloads
        self.fetched = []

    def need_login(self):
        return False

    def redirect_login(self):
        return {}

    def get(self, path, params=None, extra_headers=None, **kwargs):
        item_id = str(path).rsplit("/", 1)[-1]
        if str(path).startswith("/items/"):
            self.fetched.append(item_id)
        return _FakeResponse(self._payloads.get(item_id, {"error": "not_found"}))


@tagged("post_install", "-at_install")
class TestUnprocessableItemResponse(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.posting_obj = self.env["mercadolibre.posting"]
        self.product_a = self.env["product.product"].create({
            "name": "Batch product A", "default_code": _SKU_A,
        })
        self.product_b = self.env["product.product"].create({
            "name": "Batch product B", "default_code": _SKU_B,
        })
        self.product_c = self.env["product.product"].create({
            "name": "Batch product C", "default_code": _SKU_C,
        })

    def _run(self, payloads, item_ids):
        fake = _FakeMeli(payloads)
        context = {
            "meli_id": ", ".join(item_ids),
            "post_state": "all",
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            res = self.company.product_meli_get_products(context=context)
        return res, fake

    def _synced_ids(self, res):
        return [row["meli_id"] for row in (res or {}).get("json_report", {}).get("synced", [])]

    # ------------------------------------------------------------------
    # shapes that must not blow up
    # ------------------------------------------------------------------
    def test_dict_with_id_and_title_but_no_status(self):
        """A dict can carry ``id`` and still be unusable here."""
        broken = {"id": _ITEM_B, "title": "No status field",
                  "seller_custom_field": _SKU_B}
        self.assertNotIn("status", broken)

        res, _fake = self._run({_ITEM_B: broken}, [_ITEM_B, _ITEM_C])

        self.assertIsNotNone(res, "the import blew up on a body without 'status'")
        self.assertNotIn(_ITEM_B, self._synced_ids(res))

    def test_dict_with_id_and_status_but_no_title(self):
        """The creation branch guards 'id' and then indexes 'title'."""
        broken = {"id": _ITEM_B, "status": "active",
                  "seller_custom_field": "SKU-DOES-NOT-EXIST-ANYWHERE"}
        self.assertNotIn("title", broken)

        res, _fake = self._run({_ITEM_B: broken}, [_ITEM_B, _ITEM_C])

        self.assertIsNotNone(res, "the import blew up on a body without 'title'")

    def test_auth_error_body(self):
        """What production actually returned: no 'id', no 'status'."""
        broken = {"message": "invalid access token"}
        self.assertNotIn("id", broken)
        self.assertNotIn("status", broken)

        res, _fake = self._run({_ITEM_B: broken}, [_ITEM_B, _ITEM_C])

        self.assertIsNotNone(res, "the import blew up on an auth error body")

    def test_non_json_body_is_a_string(self):
        """A proxy error page: ``rjson3`` is a str, so indexing raises TypeError."""
        res, _fake = self._run(
            {_ITEM_B: "<html><body>502 Bad Gateway</body></html>"},
            [_ITEM_B, _ITEM_C],
        )

        self.assertIsNotNone(res, "the import blew up on a non-JSON body")

    # ------------------------------------------------------------------
    # the damage that actually matters
    # ------------------------------------------------------------------
    def test_one_bad_item_does_not_discard_the_whole_run(self):
        """A → B(broken) → C: the valid work of A and C must survive.

        There is no commit inside the import (``MeliCommit`` is ``flush_all``)
        and no savepoint around it, so today's ``MeliRollback`` discards the
        entire run. "Survive" here means: still present in the transaction once
        ``product_meli_get_products`` has returned.
        """
        payloads = {
            _ITEM_A: _item(_ITEM_A, _SKU_A, title="Good A"),
            _ITEM_B: {"message": "invalid access token"},
            _ITEM_C: _item(_ITEM_C, _SKU_C, title="Good C"),
        }

        res, fake = self._run(payloads, [_ITEM_A, _ITEM_B, _ITEM_C])

        # The batch was not aborted at B: C was still fetched.
        self.assertEqual(
            fake.fetched, [_ITEM_A, _ITEM_B, _ITEM_C],
            "the run stopped at the broken item instead of skipping it",
        )
        # A and C kept their binding.
        self.assertTrue(self.product_a.exists(), "product A was rolled back")
        self.assertTrue(self.product_c.exists(), "product C was rolled back")
        self.assertEqual(
            self.product_a.meli_id, _ITEM_A,
            "the valid work done for A was discarded by the broken item",
        )
        self.assertEqual(
            self.product_c.meli_id, _ITEM_C,
            "the valid work done for C was discarded by the broken item",
        )
        # ...and their postings.
        self.assertEqual(len(self.posting_obj.search([("meli_id", "=", _ITEM_A)])), 1)
        self.assertEqual(len(self.posting_obj.search([("meli_id", "=", _ITEM_C)])), 1)
        # The broken one left nothing behind.
        self.assertFalse(self.posting_obj.search([("meli_id", "=", _ITEM_B)]))
        self.assertFalse(self.product_b.meli_id)
        # Reported: A and C only.
        self.assertEqual(sorted(self._synced_ids(res)), sorted([_ITEM_A, _ITEM_C]))

    # ------------------------------------------------------------------
    # regression guard
    # ------------------------------------------------------------------
    def test_healthy_items_are_unaffected(self):
        """The happy path must not move."""
        payloads = {
            _ITEM_A: _item(_ITEM_A, _SKU_A, title="Good A"),
            _ITEM_C: _item(_ITEM_C, _SKU_C, title="Good C"),
        }

        res, fake = self._run(payloads, [_ITEM_A, _ITEM_C])

        self.assertEqual(fake.fetched, [_ITEM_A, _ITEM_C])
        self.assertEqual(sorted(self._synced_ids(res)), sorted([_ITEM_A, _ITEM_C]))
        self.assertEqual(self.product_a.meli_id, _ITEM_A)
        self.assertEqual(self.product_c.meli_id, _ITEM_C)
