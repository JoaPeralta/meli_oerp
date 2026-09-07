# -*- coding: utf-8 -*-
"""Unit tests for the Shipments API shape adapter.

Two things are checked here:

1. ``normalize_shipment`` turns the post-2025-10 nested shipment body into the
   flat shape the connector reads, leaves a legacy body alone, and never
   invents a value MercadoLibre stopped sending;
2. every ``/shipments/*`` call site actually sends ``x-format-new: true``. That
   one is verified by parsing the source with ``ast`` -- the call sites need a
   live Odoo environment and a MercadoLibre token to exercise, and the header
   is a property of the source, not of a particular run.

Both parts run under the Odoo test runner and as a plain ``python -m unittest``
(no Odoo, no network, no credentials).
"""

import ast
import copy
import os
import unittest

try:  # under the Odoo test runner
    from odoo.addons.meli_oerp.models.meli_shipment_format import (
        SHIPMENTS_NEW_FORMAT_HEADERS,
        is_new_format_shipment,
        normalize_shipment,
    )
    # Inherit from Odoo's TransactionCase so the Odoo test runner tags this as a
    # meli_oerp test and actually executes it under --test-tags=/meli_oerp.
    # (A plain unittest.TestCase is untagged and would be skipped by that filter.)
    from odoo.tests.common import TransactionCase as _TestBase
    from odoo.tests import tagged
except Exception:  # standalone (no Odoo available): load by file path
    import importlib.util

    _MODELS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
    )
    _spec = importlib.util.spec_from_file_location(
        "meli_shipment_format",
        os.path.join(_MODELS_DIR, "meli_shipment_format.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    SHIPMENTS_NEW_FORMAT_HEADERS = _mod.SHIPMENTS_NEW_FORMAT_HEADERS
    is_new_format_shipment = _mod.is_new_format_shipment
    normalize_shipment = _mod.normalize_shipment

    # No Odoo: fall back to a plain TestCase and a no-op tag decorator so the
    # file still runs with `python tests/test_shipment_format.py`.
    _TestBase = unittest.TestCase

    def tagged(*args, **kwargs):
        return lambda cls: cls


_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# A pre-2025-10 body, trimmed to the keys fetch_shipment actually reads.
LEGACY_SHIPMENT = {
    "id": 40000000000,
    "mode": "me2",
    "logistic_type": "fulfillment",
    "site_id": "MLA",
    "order_id": 2000000000000001,
    "status": "delivered",
    "substatus": None,
    "date_created": "2025-09-01T10:00:00.000-04:00",
    "last_updated": "2025-09-03T18:00:00.000-04:00",
    "tracking_number": "44444444444",
    "tracking_method": "Custom",
    "order_cost": 15000.0,
    "base_cost": 1200.0,
    "comments": "",
    "date_first_printed": "2025-09-01T12:00:00.000-04:00",
    "sender_id": 246057399,
    "receiver_id": 111111111,
    "shipping_option": {"name": "Normal a domicilio", "cost": 0.0, "list_cost": 2500.0},
    "status_history": {"date_shipped": "2025-09-02T09:00:00.000-04:00"},
    "receiver_address": {
        "id": 1122334455,
        "receiver_name": "Comprador Legacy",
        "receiver_phone": "1155550000",
        "address_line": "Calle Falsa 123",
        "comment": "",
        "street_name": "Calle Falsa",
        "street_number": "123",
        "zip_code": "1414",
        "city": {"id": "TUxBQ0NBUGZlZG1h", "name": "Capital Federal"},
        "state": {"id": "AR-C", "name": "Capital Federal"},
        "country": {"id": "AR", "name": "Argentina"},
        "latitude": -34.6,
        "longitude": -58.4,
    },
    "sender_address": {
        "id": 9988776655,
        "address_line": "Av Vendedor 900",
        "comment": "",
        "street_name": "Av Vendedor",
        "street_number": "900",
        "city": {"id": "TUxBQ1ZJQTgwMTU", "name": "Villa Adelina"},
        "state": {"id": "AR-B", "name": "Buenos Aires"},
        "country": {"id": "AR", "name": "Argentina"},
        "latitude": -34.5,
        "longitude": -58.5,
    },
}


# A post-2025-10 body. Note what is NOT here: order_id, external_reference,
# shipping_option, order_cost, base_cost, comments, date_first_printed,
# sender_id and status_history.
NEW_FORMAT_SHIPMENT = {
    "id": 40000000001,
    "status": "delivered",
    "substatus": None,
    "date_created": "2026-08-01T10:00:00.000-04:00",
    "last_updated": "2026-08-03T18:00:00.000-04:00",
    "tracking_number": "55555555555",
    "tracking_method": "Custom",
    "declared_value": 15000.0,
    "dimensions": {"height": 10, "width": 20, "length": 30, "weight": 900},
    "tags": ["self_service_in"],
    "items_types": ["item"],
    "quotation": 3500.0,
    "priority_class": {"id": "standard"},
    "lead_time": {
        "shipping_method": {"id": 100009, "type": "standard", "name": "Normal"},
        "estimated_delivery_time": {"type": "known_frame", "date": "2026-08-05T21:00:00.000-04:00"},
    },
    "logistic": {"mode": "me2", "type": "xd_drop_off", "direction": "forward"},
    "origin": {
        "node": "seller_address",
        "shipping_address": {
            "id": 9988776655,
            "address_line": "Av Vendedor 900",
            "comment": "",
            "street_name": "Av Vendedor",
            "street_number": "900",
            "city": {"id": "TUxBQ1ZJQTgwMTU", "name": "Villa Adelina"},
            "state": {"id": "AR-B", "name": "Buenos Aires"},
            "country": {"id": "AR", "name": "Argentina"},
            "latitude": -34.5,
            "longitude": -58.5,
        },
        "snapshot": {},
    },
    "destination": {
        "receiver_id": 222222222,
        "receiver_name": "Compradora Nueva",
        "receiver_phone": "1155551111",
        "shipping_address": {
            "id": 1122334466,
            "address_line": "Calle Nueva 456",
            "comment": "timbre 2",
            "street_name": "Calle Nueva",
            "street_number": "456",
            "zip_code": "1425",
            "city": {"id": "TUxBQ0NBUGZlZG1h", "name": "Capital Federal"},
            "state": {"id": "AR-C", "name": "Capital Federal"},
            "country": {"id": "AR", "name": "Argentina"},
            "neighborhood": {"id": "TUxBQlBBTDI2MTZa", "name": "Palermo"},
            "latitude": -34.58,
            "longitude": -58.42,
        },
        "snapshot": {},
    },
    "source": {
        "site_id": "MLA",
        "market_place": "MELI",
        "customer_id": 246057399,
        "application_id": 3069131366650174,
    },
}

# The keys MercadoLibre removed. None of them may appear in a normalized
# new-format body: a stand-in would be indistinguishable from real data.
_REMOVED_KEYS = (
    "order_id",
    "external_reference",
    "shipping_option",
    "order_cost",
    "base_cost",
    "comments",
    "date_first_printed",
    "sender_id",
    "status_history",
)


@tagged("post_install", "-at_install")
class TestNormalizeShipment(_TestBase):

    # --- regression guard: the legacy path must not move ---------------------

    def test_legacy_payload_passes_through_unchanged(self):
        """A pre-2025-10 body comes back byte-for-byte equal."""
        result = normalize_shipment(LEGACY_SHIPMENT)
        self.assertEqual(result, LEGACY_SHIPMENT)

    def test_legacy_payload_is_not_detected_as_new_format(self):
        self.assertFalse(is_new_format_shipment(LEGACY_SHIPMENT))

    def test_legacy_payload_returns_a_new_dict(self):
        """The result must not be the caller's dict, so writes cannot leak back."""
        result = normalize_shipment(LEGACY_SHIPMENT)
        self.assertIsNot(result, LEGACY_SHIPMENT)

    # --- the new format is mapped onto the canonical flat keys ---------------

    def test_new_payload_is_detected(self):
        self.assertTrue(is_new_format_shipment(NEW_FORMAT_SHIPMENT))

    def test_logistic_type_comes_from_logistic_type(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(result["logistic_type"], "xd_drop_off")

    def test_mode_comes_from_logistic_mode(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(result["mode"], "me2")

    def test_site_id_comes_from_source(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(result["site_id"], "MLA")

    def test_receiver_id_comes_from_destination(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(result["receiver_id"], 222222222)

    def test_receiver_address_comes_from_destination_shipping_address(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        receiver_address = result["receiver_address"]
        self.assertEqual(receiver_address["address_line"], "Calle Nueva 456")
        self.assertEqual(receiver_address["street_number"], "456")
        self.assertEqual(receiver_address["zip_code"], "1425")
        self.assertEqual(receiver_address["city"]["name"], "Capital Federal")
        self.assertEqual(receiver_address["country"]["id"], "AR")

    def test_receiver_name_and_phone_are_lifted_from_destination(self):
        """They live one level up in the new body; the flat consumer needs them in."""
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        receiver_address = result["receiver_address"]
        self.assertEqual(receiver_address["receiver_name"], "Compradora Nueva")
        self.assertEqual(receiver_address["receiver_phone"], "1155551111")

    def test_sender_address_comes_from_origin_shipping_address(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        sender_address = result["sender_address"]
        self.assertEqual(sender_address["address_line"], "Av Vendedor 900")
        self.assertEqual(sender_address["state"]["name"], "Buenos Aires")

    def test_untouched_top_level_keys_survive(self):
        """Keys whose name did not change must still be readable afterwards."""
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(result["id"], 40000000001)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["tracking_number"], "55555555555")
        self.assertEqual(result["lead_time"]["shipping_method"]["name"], "Normal")

    # --- nothing is invented -------------------------------------------------

    def test_new_payload_without_order_id_does_not_raise(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertIsInstance(result, dict)

    def test_new_payload_does_not_invent_order_id(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertNotIn(
            "order_id", result,
            "order_id was removed from the Shipments response on 2025-10-12; "
            "producing one here would be indistinguishable from real data.",
        )

    def test_removed_keys_stay_absent(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        for key in _REMOVED_KEYS:
            self.assertNotIn(
                key, result,
                "%s does not exist in the new Shipments format and must not be "
                "fabricated by the normalizer." % key,
            )

    def test_declared_value_is_not_mapped_onto_a_cost_field(self):
        """declared_value is not documented as order_cost or base_cost."""
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertNotIn("order_cost", result)
        self.assertNotIn("base_cost", result)
        self.assertEqual(result["declared_value"], 15000.0)

    def test_origin_node_is_not_mapped_onto_sender_id(self):
        """origin.node names a logistic node, not the seller user id."""
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertNotIn("sender_id", result)

    # --- the input is never mutated ------------------------------------------

    def test_new_payload_input_is_not_mutated(self):
        before = copy.deepcopy(NEW_FORMAT_SHIPMENT)
        normalize_shipment(NEW_FORMAT_SHIPMENT)
        self.assertEqual(NEW_FORMAT_SHIPMENT, before)

    def test_legacy_payload_input_is_not_mutated(self):
        before = copy.deepcopy(LEGACY_SHIPMENT)
        normalize_shipment(LEGACY_SHIPMENT)
        self.assertEqual(LEGACY_SHIPMENT, before)

    def test_nested_receiver_address_is_a_copy(self):
        """Writing on the result must not reach back into destination."""
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        result["receiver_address"]["address_line"] = "MUTATED"
        self.assertEqual(
            NEW_FORMAT_SHIPMENT["destination"]["shipping_address"]["address_line"],
            "Calle Nueva 456",
        )

    def test_nested_sender_address_is_a_copy(self):
        result = normalize_shipment(NEW_FORMAT_SHIPMENT)
        result["sender_address"]["address_line"] = "MUTATED"
        self.assertEqual(
            NEW_FORMAT_SHIPMENT["origin"]["shipping_address"]["address_line"],
            "Av Vendedor 900",
        )

    # --- anything else degrades instead of raising ---------------------------

    def test_payload_of_neither_shape_is_returned_untouched(self):
        """No logistic{} and no logistic_type: reshaping it would be a guess."""
        odd = {"id": 1, "status": "pending", "whatever": {"nested": True}}
        result = normalize_shipment(odd)
        self.assertEqual(result, odd)
        self.assertIsNot(result, odd)
        self.assertFalse(is_new_format_shipment(odd))

    def test_empty_dict_does_not_raise(self):
        self.assertEqual(normalize_shipment({}), {})

    def test_error_payload_is_returned_untouched(self):
        error = {"error": "not_found", "message": "shipment not found", "status": 404}
        self.assertEqual(normalize_shipment(error), error)

    def test_non_dict_payloads_do_not_raise(self):
        self.assertIsNone(normalize_shipment(None))
        self.assertEqual(normalize_shipment(""), "")
        self.assertEqual(normalize_shipment("<html>bad gateway</html>"), "<html>bad gateway</html>")
        self.assertEqual(normalize_shipment([1, 2]), [1, 2])

    def test_logistic_present_but_not_a_dict_is_treated_as_legacy(self):
        """A scalar `logistic` is not the new format and must not be unpacked."""
        odd = {"id": 1, "logistic": "me2", "mode": "me2", "logistic_type": "fulfillment"}
        self.assertFalse(is_new_format_shipment(odd))
        self.assertEqual(normalize_shipment(odd), odd)

    def test_logistic_dict_without_mode_or_type_is_not_new_format(self):
        """The discriminator needs the fields it actually reads, not just the key."""
        self.assertFalse(is_new_format_shipment({"id": 1, "logistic": {"direction": "forward"}}))
        self.assertTrue(is_new_format_shipment({"id": 1, "logistic": {"type": "fulfillment"}}))
        self.assertTrue(is_new_format_shipment({"id": 1, "logistic": {"mode": "me2"}}))

    def test_nested_logistic_wins_over_a_stale_flat_value(self):
        """When both shapes are present the nested one is the current truth."""
        mixed = dict(LEGACY_SHIPMENT)
        mixed["logistic"] = {"mode": "me1", "type": "cross_docking"}
        result = normalize_shipment(mixed)
        self.assertEqual(result["logistic_type"], "cross_docking")
        self.assertEqual(result["mode"], "me1")

    def test_partial_new_payload_leaves_missing_keys_absent(self):
        """A new body with only logistic{} must not gain empty flat keys."""
        partial = {"id": 7, "logistic": {"mode": "me2"}}
        result = normalize_shipment(partial)
        self.assertEqual(result["mode"], "me2")
        self.assertNotIn("logistic_type", result)
        self.assertNotIn("site_id", result)
        self.assertNotIn("receiver_id", result)
        self.assertNotIn("receiver_address", result)
        self.assertNotIn("sender_address", result)


def _shipments_calls(source_path):
    """Yield every ``meli.get`` call whose path starts with ``/shipments/``.

    Returns ``(lineno, ast.Call)`` pairs. The path is recognised through the
    string concatenations this codebase uses, e.g.
    ``"/shipments/" + str(ship_id) + "/costs"``.
    """
    with open(source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=source_path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            continue
        if not node.args:
            continue
        if _leftmost_string(node.args[0]).startswith("/shipments/"):
            yield node.lineno, node


def _leftmost_string(node):
    """Return the leftmost string constant of a (possibly nested) ``+`` chain."""
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        node = node.left
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _extra_headers_names(call):
    """Return the names passed as ``extra_headers=`` on ``call``, if any."""
    for kw in call.keywords:
        if kw.arg != "extra_headers":
            continue
        if isinstance(kw.value, ast.Name):
            return kw.value.id
        if isinstance(kw.value, ast.Dict):
            return "<inline dict>"
        return "<expression>"
    return None


@tagged("post_install", "-at_install")
class TestShipmentsCallSitesSendNewFormatHeader(_TestBase):
    """Every /shipments/* GET must carry the header the API now demands."""

    # /shipments/{id}/orders is a different endpoint with its own header
    # (X-New-Domain), so it is excluded from the x-format-new requirement.
    _OWN_HEADER_SUFFIXES = ("/orders",)

    def _assert_file(self, relative_path, expected_calls):
        source_path = os.path.join(_MODULE_DIR, relative_path)
        calls = list(_shipments_calls(source_path))

        # Guard against the check silently passing on zero call sites: if the
        # matcher stops finding them, the header assertion below is vacuous.
        self.assertEqual(
            len(calls), expected_calls,
            "expected %d /shipments/ meli.get call sites in %s, found %d -- if the "
            "call sites moved, update this test instead of letting it pass on nothing."
            % (expected_calls, relative_path, len(calls)),
        )

        for lineno, call in calls:
            if _is_orders_endpoint(call, self._OWN_HEADER_SUFFIXES):
                continue
            headers = _extra_headers_names(call)
            self.assertEqual(
                headers, "SHIPMENTS_NEW_FORMAT_HEADERS",
                "%s:%d calls a /shipments/ endpoint without "
                "extra_headers=SHIPMENTS_NEW_FORMAT_HEADERS; x-format-new: true "
                "is mandatory on /shipments/* since 2025-10-12."
                % (relative_path, lineno),
            )

    def test_shipment_py_call_sites(self):
        # GET /shipments/{id}, /costs, /items and /orders.
        self._assert_file(os.path.join("models", "shipment.py"), 4)

    def test_orders_py_call_site(self):
        # The receiver-address fallback.
        self._assert_file(os.path.join("models", "orders.py"), 1)

    def test_header_constant_is_the_documented_one(self):
        self.assertEqual(SHIPMENTS_NEW_FORMAT_HEADERS, {"x-format-new": "true"})


def _is_orders_endpoint(call, suffixes):
    """True when the call path ends with one of ``suffixes`` as a literal."""
    node = call.args[0]
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            if right.value in suffixes:
                return True
        node = node.left
    return False


if __name__ == "__main__":
    unittest.main()
