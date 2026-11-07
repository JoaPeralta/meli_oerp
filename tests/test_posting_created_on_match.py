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

Both catalog and order lookup use the exact item and variation. A representative
product-level MLA is not a substitute for its additional postings.
"""

import ast
import inspect
import textwrap
from unittest.mock import Mock, patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.orders import mercadolibre_orders
from ..models.versions import ProductType
from .test_no_variants_log_noise import _FakeMeli as _DirectMeli, _item_without_variants
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
    """An additional legacy variation must be registered by its exact key."""

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

    def test_variation_match_registers_posting_by_exact_key(self):
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
        self.assertEqual(posting.meli_variation_id, "VAR_SECOND")


@tagged("post_install", "-at_install", "meli_posting_identity")
class TestDurablePostingIdentity(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.product_model = self.env["product.product"]
        self.posting_model = self.env["mercadolibre.posting"]
        self.orders = self.env["mercadolibre.orders"]
        self.original = self.product_model.create({
            "name": "Durably linked product", "meli_id": "MLA_PRIMARY",
            "default_code": "SKU-EDITED",
        })
        self.decoy = self.product_model.create({
            "name": "Different product", "default_code": _SKU,
        })

    def make_posting(self, item, product=None, variation=None):
        return self.posting_model.create({
            "name": "Test posting", "meli_id": item, "meli_variation_id": variation,
            "product_id": product.id if product else False,
        })

    def resolve(self, item, variation=None, sku=None):
        return self.orders.search_meli_product(
            meli_item={"id": item, "variation_id": variation, "seller_sku": sku},
            config=self.company,
        )

    def bulk(self, item, *, create=False):
        fake = _FakeMeli({item["id"]: item})
        with patch.object(type(self.env["meli.util"]), "get_new_instance", return_value=fake):
            return self.company.product_meli_get_products(context={
                "meli_id": item["id"], "batch_processing_unit": 1,
                "force_dont_create": not create, "force_meli_pub": False,
                "force_import_images": False,
            })["json_report"]

    def test_alias_after_sku_and_name_change_ignores_reused_sku(self):
        primary = self.make_posting("MLA_PRIMARY", self.original)
        alias = self.make_posting("MLA_ALIAS", self.original)
        self.original.name = "Locally renamed product"
        for _ in range(2):
            report = self.bulk(_item("MLA_ALIAS", _SKU), create=True)
            self.assertEqual(report["synced"][0]["name"], self.original.name)
        self.assertEqual((primary | alias).product_id, self.original)
        self.assertEqual(self.original.meli_id, "MLA_PRIMARY")
        self.assertFalse(self.decoy.meli_id)
        self.assertEqual(self.posting_model.search_count([("meli_id", "=", "MLA_ALIAS")]), 1)
        self.assertEqual(self.resolve("MLA_ALIAS"), self.original)
        self.assertEqual(self.resolve("MLA_ALIAS", sku=_SKU), self.original)

    def test_variation_keys_are_exact_in_both_paths(self):
        first = self.make_posting("MLA_VARIANTS", self.original, "101")
        second = self.make_posting("MLA_VARIANTS", self.decoy, "102")
        self.make_posting("MLA_VARIANTS", self.decoy)
        report = self.bulk({
            "id": "MLA_VARIANTS", "title": "Legacy", "status": "active",
            "variations": [{"id": 101, "seller_sku": _SKU}, {"id": 102, "seller_sku": "SKU-EDITED"}],
        })
        self.assertEqual([r["meli_id_variation"] for r in report["synced"]], ["101", "102"])
        self.assertEqual(first.product_id, self.original)
        self.assertEqual(second.product_id, self.decoy)
        self.assertEqual(self.resolve("MLA_VARIANTS", 101, _SKU), self.original)
        self.assertEqual(self.resolve("MLA_VARIANTS", 102, "SKU-EDITED"), self.decoy)
        self.assertFalse(self.resolve("MLA_VARIANTS", 103))
        with self.assertRaises(ValidationError):
            self.resolve("MLA_VARIANTS", 103, _SKU)

    def test_blank_posting_can_bind_but_duplicate_exact_rows_refuse(self):
        blank = self.make_posting("MLA_UNBOUND")
        self.assertEqual(self.resolve("MLA_UNBOUND", sku=_SKU), self.decoy)
        self.bulk(_item("MLA_UNBOUND", _SKU))
        self.assertEqual(blank.product_id, self.decoy)
        another_blank = self.make_posting("MLA_UNBOUND")
        with self.assertRaises(ValidationError):
            self.resolve("MLA_UNBOUND", sku=_SKU)
        report = self.bulk(_item("MLA_UNBOUND", _SKU), create=True)
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertFalse(another_blank.product_id)

    def test_unambiguous_direct_legacy_identity_precedes_sku(self):
        self.assertEqual(self.resolve("MLA_PRIMARY", sku=_SKU), self.original)
        self.assertEqual(self.resolve("MLA_WITHOUT_IDENTITY", sku=_SKU), self.decoy)
        self.original.meli_id_variation = "201"
        self.assertEqual(self.resolve("MLA_PRIMARY", 201, _SKU), self.original)
        self.assertFalse(self.resolve("MLA_PRIMARY"))
        self.product_model.create({"name": "Another legacy candidate", "meli_id": "MLA_PRIMARY",
                                   "meli_id_variation": "201"})
        with self.assertRaises(ValidationError):
            self.resolve("MLA_PRIMARY", 201, _SKU)

    def test_legacy_direct_key_cannot_override_its_own_explicit_variant(self):
        self.make_posting("MLA_PRIMARY", self.original, "101")
        self.assertEqual(self.resolve("MLA_PRIMARY", 101, _SKU), self.original)
        with self.assertRaises(ValidationError):
            self.resolve("MLA_PRIMARY", sku=_SKU)
        report = self.bulk(_item("MLA_PRIMARY", _SKU), create=True)
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual(self.posting_model.search_count([("meli_id", "=", "MLA_PRIMARY")]), 1)

    def test_full_order_fallback_does_not_borrow_a_sibling_variant(self):
        # Run the actual retained fallback fragment with real ORM models, but
        # without unrelated full-order admission or payment fixtures.
        method = inspect.unwrap(mercadolibre_orders.orders_update_order_json)
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        fallback, = (
            node for node in ast.walk(tree) if isinstance(node, ast.If)
            and {"seller_sku", "seller_custom_field"} <= {
                value.value for value in ast.walk(node.test)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            and any(isinstance(value, ast.Name) and value.id == "product_related"
                    for value in ast.walk(node.test))
        )
        seam = ast.parse("def invoke(product_related, Item, rjson3): pass")
        seam.body[0].body = [fallback, ast.Return(value=ast.Name(id="product_related", ctx=ast.Load()))]
        scope = {
            "posting_obj": self.posting_model, "product_obj": self.product_model,
            "company": self.company, "config": self.company, "ProductType": ProductType,
            "company_domain": [("company_id", "in", [False, self.company.id])], "_logger": Mock(),
        }
        exec(compile(ast.fix_missing_locations(seam), "<native order fallback>", "exec"), scope)
        self.company.mercadolibre_create_product_from_order = False
        self.original.meli_id_variation = "101"
        sibling = self.make_posting("MLA_PRIMARY", self.original, "101")
        blank = self.make_posting("MLA_PRIMARY")
        inbound = {"item": {"id": "MLA_PRIMARY", "variation_id": None, "seller_sku": "UNMATCHED"}}
        for variations in ([{"id": 101}], ["variations"]):
            with self.subTest(variations=variations):
                item = {"id": "MLA_PRIMARY", "title": "Legacy test item", "variations": variations}
                self.assertFalse(scope["invoke"](self.product_model, inbound, item))
        self.assertFalse(blank.product_id)
        self.assertEqual(sibling.product_id, self.original)

    def test_foreign_and_archived_targets_do_not_fall_back_to_sku(self):
        foreign = self.env["res.company"].create({"name": "Other company"})
        product = self.product_model.create({"name": "Foreign product", "company_id": foreign.id})
        self.make_posting("MLA_FOREIGN", product)
        with self.assertRaises(ValidationError):
            self.resolve("MLA_FOREIGN", sku=_SKU)
        self.make_posting("MLA_ARCHIVED", self.original)
        self.original.active = False
        with self.assertRaises(ValidationError):
            self.resolve("MLA_ARCHIVED", sku=_SKU)

    def test_direct_import_cannot_reassign_existing_posting(self):
        posting = self.make_posting("MLA_PRIMARY", self.original)
        fake = _FakeMeli({"MLA_PRIMARY": _item("MLA_PRIMARY", _SKU)})
        with patch.object(type(self.env["meli.util"]), "get_new_instance", return_value=fake):
            with self.assertRaises(ValidationError):
                self.decoy.product_meli_get_product(meli_id="MLA_PRIMARY", import_images=False)
        self.assertEqual(posting.product_id, self.original)
        self.assertFalse(self.decoy.meli_id)

    def test_direct_variant_import_preserves_exact_siblings_and_can_bind_new_one(self):
        attribute = self.env["product.attribute"].create({"name": "Direct import variant"})
        values = self.env["product.attribute.value"].create([
            {"name": str(i), "attribute_id": attribute.id} for i in range(3)
        ])
        template = self.env["product.template"].create({
            "name": "Direct import test product",
            "attribute_line_ids": [(0, 0, {"attribute_id": attribute.id,
                                           "value_ids": [(6, 0, values.ids)]})],
        })
        first, second, unbound = template.product_variant_ids.sorted("id")
        first.write({"meli_id": "MLA_DIRECT", "meli_id_variation": "101", "default_code": "COLLISION"})
        second.write({"meli_id": "MLA_DIRECT", "meli_id_variation": "102", "default_code": "EDITED"})
        unbound.default_code = "NEW-VARIANT"
        exact_first = self.make_posting("MLA_DIRECT", first, "101")
        exact_second = self.make_posting("MLA_DIRECT", second, "102")
        payload = dict(_item_without_variants(), id="MLA_DIRECT", variations=[
            {"id": 101, "seller_sku": "REASSIGNED", "available_quantity": 3},
            {"id": 102, "seller_sku": "COLLISION", "available_quantity": 5},
            {"id": 103, "seller_sku": "NEW-VARIANT", "available_quantity": 7},
        ])
        self.company.mercadolibre_update_existings_variants = False
        with patch.object(type(self.env["meli.util"]), "get_new_instance", return_value=_DirectMeli(payload)):
            first.product_meli_get_product(import_images=False)
        self.assertEqual((first.meli_id_variation, second.meli_id_variation), ("101", "102"))
        self.assertEqual((first.meli_available_quantity, second.meli_available_quantity), (3, 5))
        self.assertEqual(exact_first.product_id, first)
        self.assertEqual(exact_second.product_id, second)
        self.assertEqual((unbound.meli_id, unbound.meli_id_variation), ("MLA_DIRECT", "103"))

    def test_direct_legacy_alias_without_its_own_binding_cannot_change_representative(self):
        self.original.meli_id_variation = "101"
        other = self.make_posting("MLA_ALIAS", self.decoy, "102")
        item = dict(_item("MLA_ALIAS", _SKU), variations=[{"id": 102}])
        with patch.object(type(self.env["meli.util"]), "get_new_instance",
                          return_value=_FakeMeli({"MLA_ALIAS": item})):
            with self.assertRaises(ValidationError):
                self.original.product_meli_get_product(meli_id="MLA_ALIAS", import_images=False)
        self.assertEqual((self.original.meli_id, self.original.meli_id_variation), ("MLA_PRIMARY", "101"))
        self.assertEqual(other.product_id, self.decoy)

    def test_reverse_sku_does_not_search_a_product_with_existing_posting(self):
        self.make_posting("MLA_ALIAS", self.original)
        fake = Mock()
        with patch.object(type(self.env["meli.util"]), "get_new_instance", return_value=fake):
            self.company.get_all_items_from_existing_sku(self.original)
        fake.get.assert_not_called()
        self.assertEqual(self.original.meli_id, "MLA_PRIMARY")
