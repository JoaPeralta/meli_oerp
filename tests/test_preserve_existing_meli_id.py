# -*- coding: utf-8 -*-
"""Regression tests: matching a MercadoLibre item by SKU must not overwrite a
``meli_id`` that already points at a different publication.

A single physical product can have several MercadoLibre publications (for
example two items sharing the same ``user_product_id`` and the same SKU). The
import matches an incoming item against Odoo products by ``default_code``; when
it found a product that was already bound to another item, it used to reassign
``product.meli_id`` to the incoming item, silently losing the previous
reference.

Observed in a real import: ``MLA1651548355`` created the product, then
``MLA1651389099`` (same SKU ``SM6LRES700-6/5``) matched it and overwrote
``meli_id``, leaving ``mercadolibre.posting`` pointing at the first item and the
product pointing at the second.

These tests drive the real matching branch of
``res.company.product_meli_get_products`` with a fake MercadoLibre client, so
the code path that caused the bug is the one under test.

Modelling the additional publication (creating a posting for the second item) is
deliberately out of scope here and is handled in a follow-up change.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SKU = "SKU-TEST-MELI"
# The import only iterates when more than one id comes back from the search, so
# every scenario feeds a second, unrelated item.
_OTHER_ITEM = "MLA_OTHER_ITEM"
_OTHER_SKU = "SKU-TEST-UNRELATED"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeMeli:
    """Minimal stand-in for the MercadoLibre client used by the import."""

    def __init__(self, items):
        self.access_token = "TEST-TOKEN"
        self._items = items

    def need_login(self):
        return False

    def redirect_login(self):
        return {}

    def get(self, path, params=None, extra_headers=None, **kwargs):
        item_id = str(path).rsplit("/", 1)[-1]
        return _FakeResponse(self._items.get(item_id, {"error": "not_found"}))


def _item(item_id, sku, title="Test publication"):
    return {"id": item_id, "title": title, "status": "active", "seller_custom_field": sku}


@tagged("post_install", "-at_install")
class TestPreserveExistingMeliId(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.product = self.env["product.product"].create({
            "name": "ES700 regression product",
            "default_code": _SKU,
        })

    def _run_import(self, item_id):
        """Run the real import over `item_id`, matching only (no creation)."""
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

    def _synced_ids(self, res):
        return [row["meli_id"] for row in (res or {}).get("json_report", {}).get("synced", [])]

    def test_empty_meli_id_is_assigned(self):
        """Historical behaviour: an unbound product takes the incoming item."""
        self.assertFalse(self.product.meli_id)

        res = self._run_import("MLA_NEW")

        self.assertEqual(self.product.meli_id, "MLA_NEW")
        self.assertIn("MLA_NEW", self._synced_ids(res))

    def test_same_meli_id_stays(self):
        """Re-importing the same publication leaves the binding untouched.

        The product is found straight away by meli_id, so the SKU branch is not
        even reached; the assertion pins the resulting state either way.
        """
        self.product.meli_id = "MLA_EXISTING"

        res = self._run_import("MLA_EXISTING")

        self.assertEqual(self.product.meli_id, "MLA_EXISTING")
        self.assertIn("MLA_EXISTING", self._synced_ids(res))

    def test_different_meli_id_is_preserved(self):
        """Main regression: a second publication must not steal the binding."""
        self.product.meli_id = "MLA_FIRST"
        products_before = self.env["product.product"].search_count(
            [("default_code", "=", _SKU)]
        )

        res = self._run_import("MLA_SECOND")

        self.assertEqual(
            self.product.meli_id, "MLA_FIRST",
            "The existing MercadoLibre item id was overwritten by a second "
            "publication sharing the same SKU",
        )
        # Still treated as a match (reported as synced), not as a new product.
        self.assertIn("MLA_SECOND", self._synced_ids(res))
        self.assertEqual(
            self.env["product.product"].search_count([("default_code", "=", _SKU)]),
            products_before,
            "The match must not create a second product for the same SKU",
        )


_VAR_SKU = "SKU-TEST-VARIANT"
# The three SKU mechanisms the variation matching supports, in the order the
# import evaluates them. Their conditions are mutually exclusive: each one only
# runs when the previous ones did not match, so every payload below carries
# exactly one of them and no item-level SKU.
_VARIATION_MECHANISMS = ("seller_custom_field", "seller_sku", "attribute")


def _item_with_variation(item_id, variation_id, mechanism, sku=_VAR_SKU):
    """Item payload that can only be matched through `mechanism`."""
    var = {"id": variation_id}
    if mechanism == "seller_custom_field":
        var["seller_custom_field"] = sku
    elif mechanism == "seller_sku":
        var["seller_sku"] = sku
    elif mechanism == "attribute":
        var["attributes"] = [{"id": "SELLER_SKU", "values": [{"name": sku}]}]
    else:  # pragma: no cover - guards against typos in the test itself
        raise AssertionError("unknown mechanism %s" % mechanism)
    # No item-level seller_custom_field / attributes: that keeps the main SKU
    # search from running, so the variation branch is the one under test.
    return {
        "id": item_id,
        "title": "Variant publication",
        "status": "active",
        "variations": [var],
    }


@tagged("post_install", "-at_install")
class TestPreserveExistingMeliIdOnVariations(TransactionCase):
    """Same guarantee as above, for the three variation matching branches.

    ``meli_id_variation`` is written together with ``meli_id`` and only makes
    sense as a pair: storing a variation coming from item B while keeping the
    ``meli_id`` of item A would create a new inconsistency. Both fields must
    therefore be preserved together.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True

        # meli_id_variation is only written when the template has more than one
        # variant, so the fixture needs a real attribute line.
        attribute = self.env["product.attribute"].create({"name": "MELI test attribute"})
        values = self.env["product.attribute.value"].create([
            {"name": "Value A", "attribute_id": attribute.id},
            {"name": "Value B", "attribute_id": attribute.id},
        ])
        self.template = self.env["product.template"].create({
            "name": "ES700 variant regression product",
            "attribute_line_ids": [(0, 0, {
                "attribute_id": attribute.id,
                "value_ids": [(6, 0, values.ids)],
            })],
        })
        self.assertGreater(
            len(self.template.product_variant_ids), 1,
            "The fixture must produce more than one variant",
        )
        self.variant = self.template.product_variant_ids[0]
        self.variant.default_code = _VAR_SKU

    def _run_import(self, item_id, variation_id, mechanism):
        fake = _FakeMeli({
            item_id: _item_with_variation(item_id, variation_id, mechanism),
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

    def test_variation_branches_preserve_existing_binding(self):
        """A second publication must not steal meli_id nor meli_id_variation."""
        for mechanism in _VARIATION_MECHANISMS:
            with self.subTest(mechanism=mechanism):
                self.variant.meli_id = "MLA_FIRST"
                self.variant.meli_id_variation = "VAR_FIRST"

                self._run_import("MLA_SECOND", "VAR_SECOND", mechanism)

                self.assertEqual(
                    self.variant.meli_id, "MLA_FIRST",
                    "%s branch overwrote meli_id with a second publication" % mechanism,
                )
                self.assertEqual(
                    self.variant.meli_id_variation, "VAR_FIRST",
                    "%s branch overwrote meli_id_variation with a second "
                    "publication" % mechanism,
                )

    def test_variation_branches_assign_when_unbound(self):
        """Historical behaviour: an unbound variant takes item and variation.

        This is the counterpart of the test above: the guard must not block the
        legitimate first assignment, and it must still write meli_id_variation.
        """
        for mechanism in _VARIATION_MECHANISMS:
            with self.subTest(mechanism=mechanism):
                self.variant.meli_id = False
                self.variant.meli_id_variation = False

                self._run_import("MLA_NEW", "VAR_NEW", mechanism)

                self.assertEqual(
                    self.variant.meli_id, "MLA_NEW",
                    "%s branch did not assign meli_id on an unbound variant" % mechanism,
                )
                self.assertEqual(
                    self.variant.meli_id_variation, "VAR_NEW",
                    "%s branch did not assign meli_id_variation on an unbound "
                    "variant" % mechanism,
                )
