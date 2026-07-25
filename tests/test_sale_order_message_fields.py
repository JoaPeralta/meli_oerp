# -*- coding: utf-8 -*-
"""Behaviour of ``sale.order.meli_unread_messages`` / ``meli_messages_link``.

This area had zero coverage, and it is the only place where our fork and
upstream diverge: both implement the same two fields on the ``sale_order`` class
of ``models/orders.py``, with different mechanisms.

    ours      meli_unread_messages = related "meli_order.meli_unread_messages"
    upstream  meli_unread_messages = compute reading meli_orders[0]

These tests deliberately assert BEHAVIOUR only — never `related` vs `compute`,
never the name of a compute method — so the exact same file can be run against
either implementation and decide the question on evidence.

The last test is a source-level guard: a textual merge of upstream leaves both
definitions in the class, which Python silently collapses to the last one. No
runtime introspection can see that (``_fields`` holds a single entry), so the
guard parses the module with ``ast``.
"""

import ast
import inspect

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

import odoo.addons.meli_oerp.models.orders as orders_module


@tagged("post_install", "-at_install")
class TestSaleOrderMessageFields(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "ML buyer"})
        self.sale = self.env["sale.order"].create({"partner_id": self.partner.id})
        self.orders_obj = self.env["mercadolibre.orders"]

    def _ml_order(self, order_id, unread=0):
        return self.orders_obj.create({
            "name": "ML order %s" % order_id,
            "order_id": order_id,
            "meli_unread_messages": unread,
        })

    # ------------------------------------------------------------------
    # cardinality
    # ------------------------------------------------------------------
    def test_no_related_ml_order(self):
        """No ML order linked: a count of zero and no link."""
        self.assertFalse(self.sale.meli_orders)
        self.assertEqual(self.sale.meli_unread_messages, 0)
        self.assertFalse(self.sale.meli_messages_link)

    def test_single_ml_order_without_messages(self):
        morder = self._ml_order("ORD-1", unread=0)
        self.sale.meli_orders = [(6, 0, morder.ids)]

        self.assertEqual(self.sale.meli_unread_messages, 0)
        self.assertEqual(self.sale.meli_messages_link, morder.meli_messages_link)
        self.assertTrue(self.sale.meli_messages_link)

    def test_single_ml_order_with_unread_messages(self):
        morder = self._ml_order("ORD-1", unread=3)
        self.sale.meli_orders = [(6, 0, morder.ids)]

        self.assertEqual(self.sale.meli_unread_messages, 3)
        self.assertEqual(self.sale.meli_messages_link, morder.meli_messages_link)

    def test_several_ml_orders_use_the_same_one_as_meli_order(self):
        """With N orders the sale must report the one it calls its own.

        Which one is picked is an implementation detail; that both fields and
        ``meli_order`` agree on it is not — otherwise the header would show one
        order's counter next to another order's link.
        """
        first = self._ml_order("ORD-1", unread=1)
        second = self._ml_order("ORD-2", unread=7)
        self.sale.meli_orders = [(6, 0, (first + second).ids)]

        self.assertTrue(self.sale.meli_order)
        self.assertEqual(
            self.sale.meli_unread_messages, self.sale.meli_order.meli_unread_messages,
            "The reported counter does not belong to the selected ML order",
        )
        self.assertEqual(
            self.sale.meli_messages_link, self.sale.meli_order.meli_messages_link,
            "The reported link does not belong to the selected ML order",
        )

    def test_selection_is_deterministic(self):
        """Same linked set, same answer."""
        first = self._ml_order("ORD-1", unread=1)
        second = self._ml_order("ORD-2", unread=7)
        self.sale.meli_orders = [(6, 0, (first + second).ids)]
        picked = self.sale.meli_unread_messages

        self.sale.invalidate_recordset()
        self.assertEqual(self.sale.meli_unread_messages, picked)

    # ------------------------------------------------------------------
    # propagation
    # ------------------------------------------------------------------
    def test_unread_count_change_propagates(self):
        morder = self._ml_order("ORD-1", unread=0)
        self.sale.meli_orders = [(6, 0, morder.ids)]
        self.assertEqual(self.sale.meli_unread_messages, 0)

        morder.meli_unread_messages = 5

        self.assertEqual(
            self.sale.meli_unread_messages, 5,
            "A new unread count on the ML order did not reach the sale order",
        )

    def test_messages_link_change_propagates(self):
        """The link is derived from pack_id/order_id on the ML order."""
        morder = self._ml_order("ORD-1")
        self.sale.meli_orders = [(6, 0, morder.ids)]
        before = self.sale.meli_messages_link

        morder.pack_id = "PACK-99"

        self.assertNotEqual(self.sale.meli_messages_link, before)
        self.assertEqual(self.sale.meli_messages_link, morder.meli_messages_link)
        self.assertIn("PACK-99", self.sale.meli_messages_link)

    def test_linking_an_order_propagates(self):
        self.assertEqual(self.sale.meli_unread_messages, 0)
        morder = self._ml_order("ORD-1", unread=4)

        self.sale.meli_orders = [(6, 0, morder.ids)]

        self.assertEqual(self.sale.meli_unread_messages, 4)

    def test_unlinking_every_order_resets(self):
        morder = self._ml_order("ORD-1", unread=4)
        self.sale.meli_orders = [(6, 0, morder.ids)]
        self.assertEqual(self.sale.meli_unread_messages, 4)

        self.sale.meli_orders = [(5, 0, 0)]

        self.assertEqual(self.sale.meli_unread_messages, 0)
        self.assertFalse(self.sale.meli_messages_link)

    # ------------------------------------------------------------------
    # store / search
    # ------------------------------------------------------------------
    def test_unread_count_is_searchable(self):
        """The KPI filter needs the value in the database, not only in memory."""
        morder = self._ml_order("ORD-1", unread=2)
        self.sale.meli_orders = [(6, 0, morder.ids)]
        self.sale.flush_recordset()

        found = self.env["sale.order"].search([
            ("id", "=", self.sale.id), ("meli_unread_messages", ">", 0),
        ])
        self.assertEqual(found, self.sale, "meli_unread_messages is not searchable")

    # ------------------------------------------------------------------
    # merge guard
    # ------------------------------------------------------------------
    def test_message_fields_are_defined_exactly_once(self):
        """Guard for the upstream sync.

        A textual merge of upstream leaves our definition and theirs in the same
        class; git reports no conflict and Python keeps the last one, so the
        module loads and every other test here still passes while one of the two
        implementations is silently dead code. Only the source shows it.
        """
        source = inspect.getsource(orders_module)
        tree = ast.parse(source)

        sale_order_classes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "sale_order"
        ]
        self.assertEqual(len(sale_order_classes), 1, "expected one sale_order class")

        counts = {"meli_unread_messages": 0, "meli_messages_link": 0}
        for stmt in sale_order_classes[0].body:
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in counts:
                    counts[target.id] += 1

        for field_name, count in counts.items():
            self.assertEqual(
                count, 1,
                "%s is defined %d times in class sale_order: a merge left two "
                "implementations in place and Python silently keeps the last "
                "one" % (field_name, count),
            )
