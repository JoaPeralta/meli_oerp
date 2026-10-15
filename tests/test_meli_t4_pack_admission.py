"""Standalone source checks; these are not native ORM acceptance evidence."""
import ast
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_admission", ROOT / "models" / "meli_pack_admission.py"
)
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def order(oid=101, seller=42, pack=900, quantity=2, price=100, variation=7):
    return {
        "id": oid, "seller": {"id": seller}, "pack_id": pack,
        "status": "paid", "currency_id": "USD", "shipping": {"id": 800},
        "order_items": [{"item": {"id": "MLA123", "variation_id": variation},
                         "quantity": quantity, "unit_price": price,
                         "currency_id": "USD"}],
    }


def association(oid=101, seller=42, requested=1, variation=7):
    return {"order_id": str(oid), "seller_id": seller, "pack_id": "900",
            "item_id": "MLA123", "variation_id": variation,
            "requested_quantity": requested}


class MutableClient:
    """A single response object reused and mutated by every GET, like NoSDK."""
    access_token = "synthetic-not-a-credential"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.status_code = None
        self.headers = {}
        self.body = None

    def get(self, path, params, extra_headers=None):
        self.calls.append((path, deepcopy(extra_headers)))
        status, body, headers = self.responses[path]
        self.status_code, self.body, self.headers = status, deepcopy(body), headers
        return self

    def json(self):
        return self.body


class OrderAdmissionTests(unittest.TestCase):
    def test_order_without_shipping_is_valid(self):
        payload = order(pack=None)
        del payload["shipping"]
        self.assertEqual(admission.verify_order(payload, 42)["id"], 101)

    def test_snapshot_is_not_the_mutable_input(self):
        payload = order()
        result = admission.verify_order(payload, 42, expected_id=101)
        payload["order_items"][0]["quantity"] = 99
        self.assertEqual(result["order_items"][0]["quantity"], 2)

    def test_current_seller_is_required(self):
        payload = order()
        del payload["seller"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_foreign_order_is_rejected(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(seller=43), 42)

    def test_order_identity_cannot_change(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(102), 42, expected_id=101)

    def test_boolean_identity_is_not_an_integer_id(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(True), 42)

    def test_missing_commercial_items_is_rejected(self):
        payload = order()
        payload["order_items"] = []
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_invalid_quantity_is_rejected(self):
        for quantity in (0, -1, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(admission.AdmissionError):
                admission.verify_order(order(quantity=quantity), 42)

    def test_nonfinite_price_is_rejected(self):
        for price in (float("nan"), float("inf"), -1):
            with self.subTest(price=price), self.assertRaises(admission.AdmissionError):
                admission.verify_order(order(price=price), 42)

    def test_nullable_variation_is_valid(self):
        self.assertIsNone(admission.verify_order(order(variation=None), 42)
                          ["order_items"][0]["item"]["variation_id"])

    def test_duplicate_commercial_item_identity_is_not_guessed(self):
        payload = order()
        payload["order_items"].append(deepcopy(payload["order_items"][0]))
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_bundle_parent_is_blocked(self):
        payload = order()
        payload["order_items"][0]["bundle"] = {"parent_item": {"id": "MLA9"}}
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_split_and_component_cancellation_are_blocked(self):
        for code in ("pack_splitted", "bundle_component"):
            payload = order()
            payload["cancel_detail"] = {"code": code}
            with self.subTest(code=code), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_cancelled_child_is_pending_not_whole_pack_cancel(self):
        payload = order()
        payload["status"] = "cancelled"
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_missing_status_is_rejected(self):
        payload = order(pack=None)
        del payload["shipping"]
        del payload["status"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_empty_status_is_rejected(self):
        payload = order()
        payload["status"] = ""
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_whitespace_status_is_rejected(self):
        for status in (" ", "\t\r\n"):
            payload = order()
            payload["status"] = status
            with self.subTest(status=status), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_nonstring_status_is_rejected(self):
        for status in (None, False, 0, 123, [], {}):
            payload = order()
            payload["status"] = status
            with self.subTest(status=status), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_status_guard_does_not_invent_provider_enumeration(self):
        payload = order()
        payload["status"] = "future_provider_state"
        self.assertEqual(admission.verify_order(payload, 42)["status"], "future_provider_state")

    def test_malformed_optional_objects_fail_closed(self):
        for key in ("shipping", "cancel_detail", "tags"):
            payload = order()
            payload[key] = "malformed"
            with self.subTest(key=key), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_already_discounted_unit_price_is_preserved(self):
        payload = order(price=80)
        payload["order_items"][0]["gross_price"] = 100
        payload["order_items"][0]["discounts"] = [{"amount": 20}]
        self.assertEqual(admission.verify_order(payload, 42)["order_items"][0]["unit_price"], 80)

    def test_buyer_enrichment_cannot_replace_commercial_snapshot(self):
        current = order(quantity=2, price=100)
        refreshed = order(quantity=9, price=999)
        refreshed["buyer"] = {"id": 77, "first_name": "Synthetic"}
        result = admission.enrich_buyer(current, refreshed, 42)
        self.assertEqual(result["order_items"], current["order_items"])
        self.assertEqual(result["buyer"]["id"], 77)
        self.assertNotIn("buyer", current)

    def test_buyer_refresh_cannot_change_pack_membership(self):
        with self.assertRaises(admission.AdmissionError):
            admission.enrich_buyer(order(pack=None), order(pack=900), 42)

    def test_buyer_refresh_cannot_change_source_item(self):
        refreshed = order()
        refreshed["order_items"][0]["item"]["id"] = "MLA999"
        with self.assertRaises(admission.AdmissionError):
            admission.enrich_buyer(order(), refreshed, 42)

    def test_delivered_history_is_not_new_initial_admission(self):
        payload = order()
        payload["tags"] = ["delivered"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)


class MembershipTests(unittest.TestCase):
    def test_actual_nosdk_status_attribute_is_snapshotted(self):
        response = SimpleNamespace(last_status_code=206, json=lambda: order())
        body, metadata = admission.snapshot_response(response)
        self.assertEqual(metadata["status"], 206)
        self.assertEqual(body["id"], 101)

    def collect(self, entries, extra=None, status=200, headers=None):
        responses = {"/shipments/800/orders": (status, entries, headers or {})}
        responses.update(extra or {})
        client = MutableClient(responses)
        result = admission.collect_membership(
            client, 800, order(), 42,
            headers={"X-New-Domain": "true"},
        )
        return result, client

    def test_repeated_publication_keeps_each_source_order(self):
        result, client = self.collect(
            [association(), association(102, requested=3)],
            {"/orders/102": (200, order(102, quantity=5, price=250), {})},
        )
        self.assertEqual([entry["id"] for entry in result["orders"]], [101, 102])
        self.assertEqual(result["orders"][1]["order_items"][0]["quantity"], 5)
        self.assertEqual(result["orders"][1]["order_items"][0]["unit_price"], 250)
        self.assertEqual(result["associations"][1]["requested_quantity"], 3)
        self.assertEqual(client.calls[0][1], {"X-New-Domain": "true"})

    def test_equal_economic_values_do_not_collapse_identity(self):
        result, _ = self.collect([association(), association(102)],
                                {"/orders/102": (200, order(102), {})})
        self.assertEqual(len(result["orders"]), 2)

    def test_foreign_member_is_never_requested_or_retained(self):
        result, client = self.collect([association(), association(999, seller=43)])
        self.assertEqual([row["id"] for row in result["orders"]], [101])
        self.assertEqual(len(result["associations"]), 1)
        self.assertTrue(result["complete"])
        self.assertNotIn("/orders/999", [call[0] for call in client.calls])

    def test_unknown_owner_is_not_invented_or_probed(self):
        unknown = {"order_id": "999"}
        result, client = self.collect([association(), unknown])
        self.assertFalse(result["complete"])
        self.assertEqual([row["id"] for row in result["orders"]], [101])
        self.assertEqual(len(client.calls), 1)

    def test_invisible_own_member_does_not_discard_verified_subset(self):
        result, _ = self.collect([association(), association(102)],
                                {"/orders/102": (403, {"error": "forbidden"}, {})})
        self.assertEqual([row["id"] for row in result["orders"]], [101])
        self.assertFalse(result["complete"])

    def test_206_missing_seller_fails_closed(self):
        payload = order(102)
        del payload["seller"]
        with self.assertRaises(admission.AdmissionError):
            self.collect([association(), association(102)],
                         {"/orders/102": (206, payload, {"X-Content-Missing": "seller"})})

    def test_206_with_essential_data_is_verified(self):
        result, _ = self.collect([association(), association(102)],
                                {"/orders/102": (206, order(102),
                                                 {"X-Content-Missing": "feedback"})})
        self.assertEqual(len(result["orders"]), 2)

    def test_204_is_pending_not_an_empty_successful_pack(self):
        result, _ = self.collect(None, status=204)
        self.assertFalse(result["complete"])
        self.assertEqual(result["orders"][0]["id"], 101)
        self.assertEqual(result["associations"], [])

    def test_snapshot_retains_response_status_before_next_get(self):
        result, client = self.collect([association(), association(102)],
                                     {"/orders/102": (206, order(102), {})})
        self.assertEqual(result["responses"][0]["status"], 200)
        self.assertEqual(result["responses"][1]["status"], 206)
        self.assertEqual(client.status_code, 206)

    def test_logistics_cannot_override_commercial_identity(self):
        entry = association()
        entry["item_id"] = "MLA999"
        with self.assertRaises(admission.AdmissionError):
            self.collect([entry])

    def test_association_pack_mismatch_is_rejected(self):
        entry = association()
        entry["pack_id"] = "901"
        with self.assertRaises(admission.AdmissionError):
            self.collect([entry])

    def test_member_order_pack_mismatch_is_rejected(self):
        with self.assertRaises(admission.AdmissionError):
            self.collect([association(), association(102)],
                         {"/orders/102": (200, order(102, pack=901), {})})

    def test_duplicate_logistics_identity_is_rejected(self):
        with self.assertRaises(admission.AdmissionError):
            self.collect([association(), association()])

    def test_invalid_membership_body_is_not_silently_discarded(self):
        with self.assertRaises(admission.AdmissionError):
            self.collect({"orders": [association()]})

    def test_member_order_shipping_contradiction_is_rejected(self):
        other = order(102)
        other["shipping"]["id"] = 801
        with self.assertRaises(admission.AdmissionError):
            self.collect([association(), association(102)],
                         {"/orders/102": (200, other, {})})

    def test_unclassifiable_seller_is_pending_not_permission_to_probe(self):
        other = association(102)
        other["seller_id"] = "unknown"
        result, client = self.collect([association(), other])
        self.assertFalse(result["complete"])
        self.assertEqual(len(client.calls), 1)


class SourceBoundaryTests(unittest.TestCase):
    def method(self, filename, name):
        tree = ast.parse((ROOT / "models" / filename).read_text(encoding="utf-8"))
        return next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_actor_and_history_guard_precede_capable_client(self):
        method = self.method("orders.py", "orders_update_order_json")
        source = ast.unparse(method)
        self.assertLess(source.index("_meli_assert_order_admission_actor"),
                        source.index("get_new_instance"))
        self.assertLess(source.index("_meli_preflight_order_history"),
                        source.index("get_new_instance"))
        self.assertLess(source.index("_meli_order_admission"), source.index("buyers_obj"))

    def test_old_by_item_membership_map_is_removed(self):
        source = ast.unparse(self.method("shipment.py", "_shipment_items_with_order_id"))
        self.assertNotIn("by_item", source)
        self.assertIn("verified_order", source)

    def test_generic_owner_hook_does_not_depend_on_bridge(self):
        source = ast.unparse(self.method("orders.py", "_meli_assert_order_admission_actor"))
        self.assertIn("env.su", source)
        self.assertNotIn("viarengo", source)
        self.assertNotIn("sudo", source)


class ExecutedGuardAndIngressTests(unittest.TestCase):
    """Run extracted real guard bodies; fake records cannot prove Odoo ACLs."""
    method = SourceBoundaryTests.method

    def guard(self, name):
        tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
        node = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == name)
        node.decorator_list = []
        scope = {"AccessError": PermissionError, "ValidationError": ValueError,
                 "remote_id": admission.remote_id, "AdmissionError": admission.AdmissionError,
                 "ast": ast}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "real-guard", "exec"), scope)
        return scope[name]

    def actor(self, **changes):
        company = SimpleNamespace(id=1, _fields={}, mercadolibre_seller_id="42")
        actor = SimpleNamespace(id=5, active=True, share=False, company_id=company)
        company.mercadolibre_seller_user = actor
        env = SimpleNamespace(su=False, uid=5, company=company, companies=company)
        for key, value in changes.items():
            setattr(env, key, value)
        return SimpleNamespace(env=env), company

    def test_configured_non_superuser_is_admitted(self):
        probe, company = self.actor()
        self.assertEqual(self.guard("_meli_assert_order_admission_actor")(probe, company), "42")

    def test_superuser_and_other_actor_are_denied(self):
        for changes in ({"su": True}, {"uid": 6}, {"companies": SimpleNamespace(id=2)}):
            probe, company = self.actor(**changes)
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                self.guard("_meli_assert_order_admission_actor")(probe, company)

    def test_missing_remote_seller_cannot_authorize_fetch(self):
        probe, company = self.actor()
        company.mercadolibre_seller_id = None
        with self.assertRaises(ValueError):
            self.guard("_meli_assert_order_admission_actor")(probe, company)

    def history(self, root_changes=None, seller="{'id': 42}"):
        class Records(list):
            def __or__(self, other):
                return Records([*self, *[row for row in other if row not in self]])

        _, company = self.actor()
        root = SimpleNamespace(company_id=company, state="draft", picking_ids=[],
                               invoice_ids=[], meli_update_forbidden=False)
        for key, value in (root_changes or {}).items():
            setattr(root, key, value)
        record = SimpleNamespace(company_id=company, seller_id=company.mercadolibre_seller_user,
                                 seller=seller, sale_order=Records([root]), shipment=False)
        model = SimpleNamespace(search=lambda domain: Records([root]))
        shipments = SimpleNamespace(search=lambda domain: Records())
        probe = SimpleNamespace(env={"sale.order": model, "mercadolibre.shipment": shipments},
                                search=lambda domain: Records([record]))
        return probe, company

    def test_known_draft_history_is_admitted(self):
        probe, company = self.history()
        self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_processed_history_is_denied_before_any_writes(self):
        for changes in ({"state": "sale"}, {"picking_ids": [1]}, {"invoice_ids": [1]},
                        {"meli_update_forbidden": True}):
            probe, company = self.history(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_unknown_and_foreign_stored_seller_are_not_adopted(self):
        for seller in ("", "{'id': 43}"):
            probe, company = self.history(seller=seller)
            with self.subTest(seller=seller), self.assertRaises(ValueError):
                self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_foreign_company_root_is_denied(self):
        probe, company = self.history({"company_id": SimpleNamespace(id=2)})
        with self.assertRaises(PermissionError):
            self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_pack_pending_precedes_any_legacy_commercial_mutation(self):
        source = ast.unparse(self.method("orders.py", "orders_update_order_json"))
        self.assertLess(source.index("admission_pending"), source.index("prepare_ml_order_vals"))

    def test_outer_ingresses_guard_before_constructing_client(self):
        for name in ("orders_import_order", "orders_update_order", "orders_query_iterate",
                     "orders_resync_status", "update_order_status"):
            with self.subTest(name=name):
                source = ast.unparse(self.method("orders.py", name))
                self.assertLess(source.index("_meli_assert_order_admission_actor"),
                                source.index("get_new_instance"))

    def test_direct_status_update_checks_pending_before_writing_status(self):
        source = ast.unparse(self.method("orders.py", "update_order_status"))
        self.assertLess(source.index("admission_pending"), source.index("order.status ="))

    def test_admission_error_cannot_enter_legacy_effect_fallback(self):
        node = self.method("orders.py", "orders_update_order")
        handlers = [handler for child in ast.walk(node) if isinstance(child, ast.Try)
                    for handler in child.handlers]
        rejection = next(handler for handler in handlers
                         if isinstance(handler.type, ast.Tuple)
                         and "AccessError" in ast.unparse(handler.type))
        self.assertIsInstance(rejection.body[0], ast.Raise)

    def test_shipment_existing_identity_is_not_elevated(self):
        source = ast.unparse(self.method("shipment.py", "fetch_shipment"))
        self.assertFalse("shipment_obj.sudo()" in source)
        self.assertFalse("SELECT id" in source)

    def test_history_checks_existing_shipping_root(self):
        source = ast.unparse(self.method("orders.py", "_meli_preflight_order_history"))
        self.assertTrue("mercadolibre.shipment" in source)
        self.assertTrue("shipping_id" in source)
        self.assertNotIn("sudo", source)


class SamePackParentHistoryTests(unittest.TestCase):
    """Source ownership is not implied by the shipment's configured actor."""
    guard = ExecutedGuardAndIngressTests.guard
    actor = ExecutedGuardAndIngressTests.actor

    def history(self, seller="{'id': 42}", actor_id=None):
        class Records(list):
            def __or__(self, other):
                return Records([*self, *[row for row in other if row not in self]])

            def __getattr__(self, name):
                if len(self) != 1:
                    raise AttributeError(name)
                return getattr(self[0], name)

        _, company = self.actor()
        parent_actor = (company.mercadolibre_seller_user if actor_id is None
                        else SimpleNamespace(id=actor_id))
        parent = Records([SimpleNamespace(
            order_id="102", pack_id="900", company_id=company,
            seller_id=parent_actor, seller=seller,
            sale_order=Records(), shipment=False,
        )])
        shipment = SimpleNamespace(
            order=parent, company_id=parent.company_id,
            seller_id=company.mercadolibre_seller_user, sale_order=Records(),
        )
        probe = SimpleNamespace(
            search=lambda domain: Records(),
            env={
                "sale.order": SimpleNamespace(search=lambda domain: Records()),
                "mercadolibre.shipment": SimpleNamespace(
                    search=lambda domain: Records([shipment])),
            },
        )
        return probe, company

    def test_same_pack_parent_requires_stored_seller(self):
        probe, company = self.history(seller="")
        with self.assertRaises(ValueError):
            self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_same_pack_parent_rejects_foreign_remote_seller(self):
        probe, company = self.history(seller="{'id': 43}")
        with self.assertRaises(ValueError):
            self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_same_pack_parent_rejects_different_odoo_actor(self):
        probe, company = self.history(actor_id=6)
        with self.assertRaises(PermissionError):
            self.guard("_meli_preflight_order_history")(probe, order(), company)

    def test_same_pack_parent_with_verified_ownership_is_admitted(self):
        probe, company = self.history()
        self.assertIsNone(
            self.guard("_meli_preflight_order_history")(probe, order(), company))




class ShipmentResourceBindingTests(unittest.TestCase):
    """Execute real shipment methods, stopping before main-resource effects."""

    class MainResourceRead(RuntimeError):
        pass

    def body(self, filename, name):
        node = SourceBoundaryTests.method(self, filename, name)
        node.decorator_list = []
        scope = {
            "ast": ast, "ValidationError": ValueError,
            "AdmissionError": admission.AdmissionError,
            "remote_id": admission.remote_id, "verify_order": admission.verify_order,
            "snapshot_response": admission.snapshot_response,
            "_logger": SimpleNamespace(info=lambda *args: None),
            "SHIPMENTS_NEW_FORMAT_HEADERS": {"x-format-new": "true"},
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "real-shipment-binding", "exec"), scope)
        return scope[name]

    def membership(self, source):
        return {"orders": [source], "associations": [association()],
                "complete": True, "pending": None, "responses": []}

    def fetch_fixture(self, source, stored_shipping="800"):
        test = self

        class Client(MutableClient):
            def get(self, path, params, extra_headers=None):
                if path in self.responses:
                    return super().get(path, params, extra_headers)
                self.calls.append((path, deepcopy(extra_headers)))
                raise test.MainResourceRead(path)

        row = association()
        row["pack_id"] = None
        client = Client({"/shipments/800/orders": (200, [row], {})})
        company = SimpleNamespace(id=1, _fields={})
        stored = SimpleNamespace(
            id=1, order_id="101", seller="{'id': 42}", pack_id=False,
            shipping_id=stored_shipping, _fields={"shipping_id": True},
        )
        shipment_model = SimpleNamespace(
            _meli_admit_shipment_orders=lambda meli, sid, current, seller:
                admission.collect_membership(
                    meli, sid, current, seller, headers={"X-New-Domain": "true"}),
        )
        orders_model = SimpleNamespace(
            env={"mercadolibre.shipment": shipment_model},
            _meli_assert_order_admission_actor=lambda config: "42",
            _meli_preflight_order_history=lambda *args, **kwargs: None,
        )
        orders_model._meli_order_admission = lambda current, config, meli: self.body(
            "orders.py", "_meli_order_admission")(orders_model, current, config, meli)
        env = {name: SimpleNamespace() for name in (
            "sale.order", "sale.order.line", "product.product",
            "product.pricelist", "res.partner",
        )}
        env.update({"mercadolibre.orders": orders_model,
                    "mercadolibre.shipment": shipment_model})
        return SimpleNamespace(env=env), stored, company, client

    def test_fetch_rejects_changed_fresh_shipment_before_any_resource_get(self):
        source = order(pack=None)
        probe, stored, company, client = self.fetch_fixture(source, "900")
        with self.assertRaises(ValueError):
            self.body("shipment.py", "fetch_shipment")(
                probe, stored, meli=client, config=company, verified_order=source)
        self.assertEqual(client.calls, [])

    def test_fetch_requires_fresh_and_stored_shipment_identity(self):
        for fresh, stored_id in ((None, "900"), ({}, "900"), ({"id": 800}, None)):
            source = order(pack=None)
            source["shipping"] = fresh
            probe, stored, company, client = self.fetch_fixture(source, stored_id)
            with self.subTest(fresh=fresh, stored=stored_id):
                with self.assertRaises(ValueError):
                    self.body("shipment.py", "fetch_shipment")(
                        probe, stored, meli=client, config=company, verified_order=source)
                self.assertEqual(client.calls, [])

    def test_admitted_shortcut_requires_the_same_fresh_resource(self):
        source = order()
        missing = deepcopy(source)
        missing.pop("shipping")
        other = deepcopy(source)
        other["shipping"]["id"] = 900
        cases = ((900, source, source), (800, missing, source), (800, source, other))
        for requested, verified, admitted_source in cases:
            with self.subTest(requested=requested, verified=verified.get("shipping"),
                              admitted=admitted_source.get("shipping")):
                with self.assertRaises(ValueError):
                    self.body("shipment.py", "_shipment_items_with_order_id")(
                        SimpleNamespace(), [], ship_id=requested, verified_order=verified,
                        seller_id="42", admitted=self.membership(admitted_source))

    def test_matching_fetch_reaches_only_the_verified_main_resource(self):
        source = order(pack=None)
        probe, stored, company, client = self.fetch_fixture(source)
        with self.assertRaisesRegex(self.MainResourceRead, "/shipments/800$"):
            self.body("shipment.py", "fetch_shipment")(
                probe, stored, meli=client, config=company, verified_order=source)
        self.assertEqual([path for path, _ in client.calls],
                         ["/shipments/800/orders", "/shipments/800"])

    def test_matching_admitted_resource_keeps_explicit_logistic_rows(self):
        source = order()
        rows = self.body("shipment.py", "_shipment_items_with_order_id")(
            SimpleNamespace(), [], ship_id="800", verified_order=source,
            seller_id="42", admitted=self.membership(source))
        self.assertEqual(rows, [{"order_id": "101", "item_id": "MLA123",
                                "variation_id": 7, "quantity": 1,
                                "description": "MLA123"}])
if __name__ == "__main__":
    unittest.main()
