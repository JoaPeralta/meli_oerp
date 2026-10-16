"""Owned-lab TransactionCase proof; the harness supplies frozen synthetic data.

Functional calls retain the registered models, MRO, ACLs and record rules.
Baseline lookup probes execute its actual importer fragment, not a fake helper.
Full importer counterexamples use one upstream item/variation, never a bundle.
"""
import copy
import hashlib
import sys
from unittest.mock import patch

from psycopg2 import sql
from unittest import TestCase as UnitTestCase

from odoo import api
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .test_meli_t4_source_identity import source_operation


class SyntheticClient:
    access_token = None

    def __init__(self, fixture, payload):
        self.routes = copy.deepcopy(fixture["routes"])
        oid = str(payload["id"])
        self.routes["/orders/" + oid] = {"status": 200, "body": payload}
        self.routes["/orders/" + oid + "/discounts"] = {"status": 200, "body": {"details": []}}

    def need_login(self):
        return False

    def get(self, path, params=None, extra_headers=None, **kwargs):
        assert path in self.routes, "Unbound synthetic resource: " + path
        assert not (params or {}).get("access_token")
        route = self.routes[path]
        assert (extra_headers or {}) == route.get("headers", {})
        self.status_code = self.last_status_code = route["status"]
        self.headers = copy.deepcopy(route.get("response_headers", {}))
        self.body = copy.deepcopy(route["body"])
        return self

    def json(self):
        return self.body


@tagged("post_install", "-at_install", "meli_t4_source_identity")
class TestMeliT4SourceIdentity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = copy.deepcopy(globals()["T4C1_FROZEN_FIXTURE"])
        cls.actor = api.Environment(cls.env.cr, 5, {
            "allowed_company_ids": [1], "viarengo_meli_root_circuit": "a",
            "default_viarengo_circuit_code": "a", "tracking_disable": True,
            "mail_create_nolog": True, "mail_notrack": True,
            "mail_create_nosubscribe": True,
        }, su=False)
        cls.company = cls.actor.company
        cls.model = cls.actor["mercadolibre.orders"]
        cls.item_model = cls.actor["mercadolibre.order_items"]
        cls.product = cls.actor["product.product"].browse(2).exists()
        assert cls.product and cls.company.id == 1 and not cls.actor.su
        assert "meli_source_order_item_id" in cls.actor["sale.order.line"]._fields
        assert cls.model._meli_assert_order_admission_actor(cls.company) == str(cls.fixture["payload"]["seller"]["id"])
        cls.operation = staticmethod(source_operation())

    def make_order(self, remote="996100000001", seller=None):
        return self.model.create({
            "name": "T4C1 synthetic source", "order_id": remote,
            "company_id": 1, "seller_id": 5, "status": "paid",
            "seller": repr({"id": seller or self.fixture["payload"]["seller"]["id"]}),
            "currency_id": "USD",
        })

    def values(self, order, variation="101", quantity=2):
        return {
            "order_id": order.id, "product_id": self.product.id,
            "order_item_id": self.fixture["item"],
            "order_item_variation_id": variation,
            "quantity": quantity, "unit_price": "100", "currency_id": "USD",
        }

    def lookup(self, order, values=None, model=None):
        model = self.model if model is None else model
        values = self.values(order) if values is None else values
        if hasattr(model, "_meli_upsert_source_item"):
            return model._meli_upsert_source_item(order.with_env(model.env), values, model.env.company)
        return self.operation(model, order.with_env(model.env), values, model.env.company)

    def snapshot(self, records):
        observed = records.with_env(self.env)
        observed.invalidate_recordset()
        return observed.read(["order_id", "product_id", "order_item_id",
                              "order_item_variation_id", "quantity", "unit_price"], load=None)

    def atomic_snapshot(self):
        """Observe every stored row; the functional call still uses the actor."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        names = [row[0] for row in cr.fetchall()]
        self.assertEqual(len(names), 590)
        snapshot = {}
        for name in names:
            cr.execute(sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(name)))
            digest, count = hashlib.sha256(), 0
            for (row,) in cr.fetchall():
                digest.update(row.encode())
                digest.update(b"\n")
                count += 1
            snapshot[name] = (count, digest.hexdigest())
        return snapshot

    def caller_sentinel(self):
        sentinel = self.make_order("996100000099")
        sentinel.write({"name": "T4C1 caller-owned sentinel"})
        return sentinel

    def assert_atomic_refusal(self, order, exception_types):
        sentinel = self.caller_sentinel()
        before = self.atomic_snapshot()
        with UnitTestCase.assertRaises(self, exception_types):
            self.import_order(order, quantity=4)
        self.assertEqual(self.atomic_snapshot(), before)
        self.assertEqual(sentinel.name, "T4C1 caller-owned sentinel")

    def import_order(self, order, quantity=2):
        payload = copy.deepcopy(self.fixture["payload"])
        payload.update(id=int(order.order_id), total_amount=100 * quantity, paid_amount=100 * quantity)
        payload["order_items"][0].update(quantity=quantity)
        transaction_codes = {
            method.__code__ for cls in type(self.env.cr).__mro__
            for name in ("rollback", "commit")
            if (method := cls.__dict__.get(name)) and hasattr(method, "__code__")
        }
        transaction_calls = []
        previous = sys.getprofile()
        def observe(frame, event, argument):
            if event == "call" and (frame.f_code in transaction_codes
                                     or frame.f_code.co_name == "MeliRollback"):
                transaction_calls.append(frame.f_code.co_name)
        sys.setprofile(observe)
        try:
            return self.model.orders_update_order_json({
                "id": order.id, "remote_order_id": order.order_id, "order_json": payload,
            }, config=self.company, meli=SyntheticClient(self.fixture, payload))
        finally:
            sys.setprofile(previous)
            self.assertEqual(transaction_calls, [], "Importer must not abort or commit the caller transaction")

    def test_full_importer_stale_variation_refuses_without_source_overwrite(self):
        order = self.make_order()
        source = self.item_model.create(self.values(order, variation="102", quantity=9))
        before = self.snapshot(source)
        self.assert_atomic_refusal(order, ValidationError)
        self.assertEqual(self.snapshot(source), before)
        self.assertEqual(self.item_model.search_count([("order_id", "=", order.id)]), 1)

    def test_full_importer_duplicate_identity_refuses_without_winner(self):
        order = self.make_order()
        sources = self.item_model.create([self.values(order), self.values(order)])
        before = self.snapshot(sources)
        self.assert_atomic_refusal(order, ValidationError)
        self.assertEqual(self.snapshot(sources), before)

    def test_full_importer_first_replay_preserves_source_order_and_sibling(self):
        order, sibling = self.make_order(), self.make_order("996100000002")
        other = self.item_model.create(self.values(sibling, quantity=9))
        before = self.snapshot(other)
        ids = []
        for quantity in (2, 4):
            result = self.import_order(order, quantity)
            self.assertFalse(result.get("error") or result.get("admission_pending"), result)
            sources = self.item_model.search([("order_id", "=", order.id)])
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources.quantity, quantity)
            ids.append(sources.id)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.snapshot(other), before)

    def test_rule_hidden_source_refuses_without_sudo_or_duplicate(self):
        order = self.make_order()
        source = self.item_model.create(self.values(order))
        before = self.snapshot(source)
        self.env["ir.rule"].create({
            "name": "T4C1 rollback-only restrictive source rule",
            "model_id": self.env["ir.model"]._get_id("mercadolibre.order_items"),
            "domain_force": repr([("id", "!=", source.id)]),
        })
        self.assert_atomic_refusal(order, (AccessError, ValidationError))
        self.assertEqual(self.snapshot(source), before)

    def test_ordinary_sudo_b_and_foreign_company_contexts_are_denied(self):
        order = self.make_order()
        source = self.item_model.create(self.values(order))
        before = self.snapshot(source)
        models = [
            self.model.with_user(6), self.model.sudo(),
            self.model.with_context(viarengo_meli_root_circuit="b"),
            self.model.with_context(allowed_company_ids=[2]),
        ]
        for model in models:
            with self.subTest(uid=model.env.uid, su=model.env.su, context=model.env.context):
                with UnitTestCase.assertRaises(self, (AccessError, ValidationError)):
                    self.lookup(order, model=model)
        self.assertEqual(self.snapshot(source), before)

    def test_foreign_remote_seller_and_wrong_local_order_are_denied(self):
        order = self.make_order(seller=800000099)
        with UnitTestCase.assertRaises(self, (AccessError, ValidationError)):
            self.lookup(order)
        own = self.make_order("996100000002")
        with UnitTestCase.assertRaises(self, (AccessError, ValidationError)):
            self.lookup(own, self.values(order))

    def test_nullable_local_variation_replays_without_new_source(self):
        order = self.make_order()
        source = self.item_model.create(self.values(order, variation=False))
        self.lookup(order, self.values(order, variation="", quantity=4))
        self.assertEqual(self.item_model.search_count([("order_id", "=", order.id)]), 1)
        self.assertEqual(source.quantity, 4)

    def test_installed_b1_bound_anchor_refusal_is_unchanged(self):
        source = self.item_model.browse(6).exists()
        line = self.actor["sale.order.line"].browse(4).exists()
        self.assertTrue(source and line and line.meli_source_order_item_id == source)
        before = self.snapshot(source)
        values = {name: before[0][name] for name in ("order_item_id", "order_item_variation_id",
                                                    "quantity", "unit_price")}
        values.update(order_id=source.order_id.id, product_id=self.product.id + 1000000)
        with UnitTestCase.assertRaises(self, (AccessError, ValidationError)):
            self.lookup(source.order_id, values)
        self.assertEqual(self.snapshot(source), before)

    def test_late_source_write_exception_rolls_back_only_this_import(self):
        order = self.make_order()
        source = self.item_model.create(self.values(order))
        before = self.snapshot(source)
        original = type(self.item_model).write
        def fail_after_source_write(records, values):
            result = original(records, values)
            if source.id in records.ids:
                raise RuntimeError("T4C1 injected late source-write failure")
            return result
        with patch.object(type(self.item_model), "write", fail_after_source_write):
            self.assert_atomic_refusal(order, RuntimeError)
        self.assertEqual(self.snapshot(source), before)
