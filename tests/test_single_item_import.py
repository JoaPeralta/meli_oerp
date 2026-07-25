# -*- coding: utf-8 -*-
"""Regression tests: a single publication must be imported.

``product_meli_get_products`` selected the publications to process inside

    if (totalmax>1):
        ...
        for item_id in meli_ids:
            results.append( item_id )

with no ``else`` branch. A list holding exactly one item id skipped the block
entirely, ``results`` stayed empty, and the import ended without processing
anything and without a report (the CSV is only built when something was
processed, so the wizard just closed).

It is the natural case once explicit item ids are honoured: asking for one
publication is the normal thing to do. It also affected an account whose whole
catalogue is a single active publication.

The companion defect (the explicit ids field being sent to the seller search)
was fixed separately; these tests cover the off-by-one alone, through both
entry points: explicit ids and the general search.

Note: every case below uses ``batch_processing_unit > 0`` on purpose. With 0 or
an empty value, ``results`` is replaced by ``list_meli_ids(filter_ids=results)``
further down — a separate, untouched defect that would otherwise change which
items get processed and mask what these tests measure.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .test_preserve_existing_meli_id import _item
from .test_explicit_meli_ids import _RecordingMeli

_ONLY = "MLA_ONLY_ONE"
_SECOND = "MLA_SECOND_ONE"

_SKU_ONLY = "SKU-ONLY-ONE"
_SKU_SECOND = "SKU-SECOND-ONE"


@tagged("post_install", "-at_install")
class TestSingleItemImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.product_only = self.env["product.product"].create({
            "name": "Single publication product",
            "default_code": _SKU_ONLY,
        })
        self.product_second = self.env["product.product"].create({
            "name": "Second publication product",
            "default_code": _SKU_SECOND,
        })

    def _run(self, explicit_ids, search_returns):
        fake = _RecordingMeli({
            _ONLY: _item(_ONLY, _SKU_ONLY, title="The only publication"),
            _SECOND: _item(_SECOND, _SKU_SECOND, title="A second publication"),
        })
        context = {
            "meli_id": explicit_ids,
            "post_state": "all",
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            # Must stay > 0: see the module docstring.
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ), patch.object(
            type(self.env["res.company"]), "fetch_list_meli_ids",
            return_value=list(search_returns),
        ):
            res = self.company.product_meli_get_products(context=context)
        return res, fake

    def _synced_ids(self, res):
        return [row["meli_id"] for row in (res or {}).get("json_report", {}).get("synced", [])]

    def test_single_explicit_id_is_processed(self):
        """The headline case: one explicit id must actually be imported."""
        res, fake = self._run(_ONLY, [])

        self.assertEqual(
            fake.fetched_item_ids(), [_ONLY],
            "A single explicit publication was never fetched from MercadoLibre",
        )
        self.assertEqual(self._synced_ids(res), [_ONLY])
        self.assertEqual(self.product_only.meli_id, _ONLY)

    def test_single_id_from_general_search_is_processed(self):
        """Same guarantee for an account holding one single publication."""
        res, fake = self._run(False, [_ONLY])

        self.assertEqual(fake.fetched_item_ids(), [_ONLY])
        self.assertEqual(self._synced_ids(res), [_ONLY])
        self.assertEqual(self.product_only.meli_id, _ONLY)

    def test_single_item_produces_a_report(self):
        """The wizard closed without a CSV because nothing was reported."""
        res, _fake = self._run(_ONLY, [])

        self.assertTrue(res, "The import returned an empty result")
        self.assertIn("json_report", res)
        report = res["json_report"]
        self.assertEqual(
            len(report["synced"]) + len(report["missing"]) + len(report["duplicates"]),
            1,
            "The single publication produced no report row",
        )

    def test_several_ids_are_still_processed(self):
        """Guard against over-correcting: the multi-item path is unchanged."""
        res, fake = self._run("%s, %s" % (_ONLY, _SECOND), [])

        self.assertEqual(fake.fetched_item_ids(), [_ONLY, _SECOND])
        self.assertEqual(sorted(self._synced_ids(res)), sorted([_ONLY, _SECOND]))

    def test_no_ids_processes_nothing(self):
        """An empty selection must stay a no-op, not become a full import."""
        _res, fake = self._run(False, [])

        self.assertEqual(
            fake.fetched_item_ids(), [],
            "An empty selection fetched publications anyway",
        )
