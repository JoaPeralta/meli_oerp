# -*- coding: utf-8 -*-
"""Regression tests: matching an item against an existing product must register
its ``mercadolibre.posting``.

The creation branch of the import already records one posting per item
(``product_meli_get_product``), but the matching branch did not. With two
publications of the same physical product (same ``user_product_id`` and SKU) the
second one had no persistent representation at all:

    product.product ES700
    |-- meli_id = MLA1651548355
    `-- posting  MLA1651548355          <- MLA1651389099 was nowhere

Posting identity in the product import flow is ``meli_id`` alone: both existing
creation sites search with ``[('meli_id','=',item_id)]`` and neither writes
``meli_variation_id`` (only the orders flow uses item + variation). These tests
pin down that same semantics.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .test_preserve_existing_meli_id import _FakeMeli, _item, _OTHER_ITEM, _OTHER_SKU

_SKU = "SKU-ES700-POSTING"


@tagged("post_install", "-at_install")
class TestPostingCreatedOnMatch(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.posting_obj = self.env["mercadolibre.posting"]
        self.product = self.env["product.product"].create({
            "name": "ES700 posting regression product",
            "default_code": _SKU,
        })

    def _run_import(self, item_id):
        fake = _FakeMeli({
            item_id: _item(item_id, _SKU),
            _OTHER_ITEM: _item(_OTHER_ITEM, _OTHER_SKU, title="Unrelated"),
        })
        context = {
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ), patch.object(
            type(self.env["res.company"]), "fetch_list_meli_ids",
            return_value=[item_id, _OTHER_ITEM],
        ):
            return self.company.product_meli_get_products(context=context)

    def _postings(self, meli_id):
        return self.posting_obj.search([("meli_id", "=", meli_id)])

    def _synced_ids(self, res):
        return [row["meli_id"] for row in (res or {}).get("json_report", {}).get("synced", [])]

    def test_second_publication_gets_its_own_posting(self):
        """A second publication of the same product must be registered."""
        self.product.meli_id = "MLA_FIRST"
        first_posting = self.posting_obj.create({
            "meli_id": "MLA_FIRST",
            "product_id": self.product.id,
            "name": "Post (MLA_FIRST)",
        })
        products_before = self.env["product.product"].search_count(
            [("default_code", "=", _SKU)]
        )

        res = self._run_import("MLA_SECOND")

        # The product itself is untouched (guaranteed by the previous change).
        self.assertEqual(self.product.meli_id, "MLA_FIRST")
        self.assertEqual(
            self.env["product.product"].search_count([("default_code", "=", _SKU)]),
            products_before,
            "Matching must not create a second product",
        )
        # The original posting survives...
        self.assertTrue(first_posting.exists())
        self.assertEqual(first_posting.product_id, self.product)
        # ...and the incoming publication is now registered against the same product.
        second = self._postings("MLA_SECOND")
        self.assertEqual(
            len(second), 1,
            "The matched publication MLA_SECOND was not registered as a posting",
        )
        self.assertEqual(second.product_id, self.product)
        self.assertIn("MLA_SECOND", self._synced_ids(res))

    def test_reimport_is_idempotent(self):
        """Re-processing the same item must not duplicate its posting."""
        self.product.meli_id = "MLA_FIRST"

        self._run_import("MLA_SECOND")
        self.assertEqual(len(self._postings("MLA_SECOND")), 1)

        self._run_import("MLA_SECOND")
        self.assertEqual(
            len(self._postings("MLA_SECOND")), 1,
            "Re-importing the same publication duplicated its posting",
        )

    def test_unbound_product_gets_meli_id_and_posting(self):
        """An unbound product takes the item id and gets its posting."""
        self.assertFalse(self.product.meli_id)
        products_before = self.env["product.product"].search_count(
            [("default_code", "=", _SKU)]
        )

        self._run_import("MLA_NEW")

        self.assertEqual(self.product.meli_id, "MLA_NEW")
        posting = self._postings("MLA_NEW")
        self.assertEqual(len(posting), 1)
        self.assertEqual(posting.product_id, self.product)
        self.assertEqual(
            self.env["product.product"].search_count([("default_code", "=", _SKU)]),
            products_before,
        )

    def test_posting_of_another_product_is_not_reassigned(self):
        """An existing posting pointing elsewhere must not be silently stolen."""
        other_product = self.env["product.product"].create({
            "name": "Another product holding the posting",
            "default_code": "SKU-OTHER-OWNER",
        })
        posting = self.posting_obj.create({
            "meli_id": "MLA_SECOND",
            "product_id": other_product.id,
            "name": "Post (MLA_SECOND)",
        })
        self.product.meli_id = "MLA_FIRST"

        self._run_import("MLA_SECOND")

        self.assertEqual(
            posting.product_id, other_product,
            "The posting was reassigned to a different product",
        )
        self.assertEqual(
            len(self._postings("MLA_SECOND")), 1,
            "A duplicate posting was created instead of reusing the existing one",
        )

    def test_direct_meli_id_match_registers_missing_posting(self):
        """Reproduces the state a pre-fix import left in the real database.

            product.product ES700
            |-- meli_id = MLA_SECOND     <- the second item overwrote the first
            `-- posting  MLA_FIRST       <- MLA_SECOND has no posting at all

        Product and posting point at *different* publications. Re-importing
        MLA_SECOND is matched by ``product.product.meli_id`` at the top of the
        loop, so the SKU search never runs (it is guarded by ``not posting_id``)
        and the flow reaches the registration point through the meli_id branch.
        """
        self.product.meli_id = "MLA_SECOND"
        first_posting = self.posting_obj.create({
            "meli_id": "MLA_FIRST",
            "product_id": self.product.id,
            "name": "Post (MLA_FIRST)",
        })
        self.assertFalse(
            self._postings("MLA_SECOND"),
            "The fixture must start without a posting for the incoming item",
        )
        products_before = self.env["product.product"].search_count(
            [("default_code", "=", _SKU)]
        )

        res = self._run_import("MLA_SECOND")

        # No second product, and the existing binding is left alone.
        self.assertEqual(
            self.env["product.product"].search_count([("default_code", "=", _SKU)]),
            products_before,
            "Matching must not create a second product",
        )
        self.assertEqual(self.product.meli_id, "MLA_SECOND")
        # The other publication keeps its posting, still on the same product.
        self.assertTrue(first_posting.exists())
        self.assertEqual(first_posting.product_id, self.product)
        self.assertEqual(len(self._postings("MLA_FIRST")), 1)
        # The incoming publication is registered exactly once.
        second = self._postings("MLA_SECOND")
        self.assertEqual(
            len(second), 1,
            "The publication matched by meli_id was not registered as a posting",
        )
        self.assertEqual(second.product_id, self.product)
        # Both publications coexist, without duplicates.
        self.assertEqual(
            sorted(self.posting_obj.search(
                [("product_id", "=", self.product.id)]
            ).mapped("meli_id")),
            ["MLA_FIRST", "MLA_SECOND"],
        )
        # The match came from meli_id, not from the SKU search: `seller_sku` is
        # only ever set inside the SKU branch and is reported as `meli_sku`.
        rows = [
            row for row in (res or {}).get("json_report", {}).get("synced", [])
            if row["meli_id"] == "MLA_SECOND"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["meli_sku"], "",
            "The SKU search ran: the item was not matched directly by meli_id",
        )


@tagged("post_install", "-at_install")
class TestPostingCreatedOnVariationMatch(TransactionCase):
    """The variation branches converge on the same code, so one path is enough
    to pin the semantics: identity is the item id, not item + variation."""

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.posting_obj = self.env["mercadolibre.posting"]

        attribute = self.env["product.attribute"].create({"name": "MELI posting attribute"})
        values = self.env["product.attribute.value"].create([
            {"name": "Value A", "attribute_id": attribute.id},
            {"name": "Value B", "attribute_id": attribute.id},
        ])
        self.template = self.env["product.template"].create({
            "name": "ES700 variant posting product",
            "attribute_line_ids": [(0, 0, {
                "attribute_id": attribute.id,
                "value_ids": [(6, 0, values.ids)],
            })],
        })
        self.variant = self.template.product_variant_ids[0]
        self.variant.default_code = _SKU

    def test_variation_match_registers_posting_by_item_id(self):
        self.variant.meli_id = "MLA_FIRST"
        self.variant.meli_id_variation = "VAR_FIRST"

        item = {
            "id": "MLA_SECOND",
            "title": "Variant publication",
            "status": "active",
            "variations": [{"id": "VAR_SECOND", "seller_custom_field": _SKU}],
        }
        fake = _FakeMeli({
            "MLA_SECOND": item,
            _OTHER_ITEM: _item(_OTHER_ITEM, _OTHER_SKU, title="Unrelated"),
        })
        context = {
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ), patch.object(
            type(self.env["res.company"]), "fetch_list_meli_ids",
            return_value=["MLA_SECOND", _OTHER_ITEM],
        ):
            self.company.product_meli_get_products(context=context)

        # Binding preserved (previous change) and publication registered.
        self.assertEqual(self.variant.meli_id, "MLA_FIRST")
        self.assertEqual(self.variant.meli_id_variation, "VAR_FIRST")
        posting = self.posting_obj.search([("meli_id", "=", "MLA_SECOND")])
        self.assertEqual(len(posting), 1)
        self.assertEqual(posting.product_id, self.variant)
        # Identity is the item id: the import flow does not store the variation
        # on the posting (only the orders flow does).
        self.assertFalse(posting.meli_variation_id)
