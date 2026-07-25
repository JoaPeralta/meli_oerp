# -*- coding: utf-8 -*-
"""Regression test: setting a product price from MercadoLibre must not use
``lst_price`` on a ``product.template``.

``lst_price`` is defined only on ``product.product`` (catalog value plus variant
extra); ``product.template`` exposes ``list_price``. Reading or writing
``lst_price`` on a template raises::

    AttributeError: 'product.template' object has no attribute 'lst_price'

That is exactly what happened during a mass import when the company had no
``mercadolibre_pricelist`` configured: ``_meli_set_product_price`` falls back to
writing the price directly on the template, the AttributeError was swallowed by
a broad ``except`` in ``product_meli_get_products``, the transaction was rolled
back, and the import silently produced no products and an empty CSV report.

These tests exercise the no-pricelist branch directly.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestProductTemplateListPrice(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        # The branch under test only runs when no MercadoLibre pricelist is set.
        self.company.mercadolibre_pricelist = False
        # Keep the price conversion deterministic: with no sale taxes on the
        # template, _meli_set_product_price leaves the ML price untouched.
        self.template = self.env["product.template"].create({
            "name": "MELI regression product",
            "list_price": 1.0,
            "taxes_id": [(5, 0, 0)],
        })
        self.variant = self.template.product_variant_ids[:1]

    def test_template_price_is_set_when_list_price_is_low(self):
        """With no pricelist and list_price <= 1, the ML price is written."""
        self.assertFalse(self.company.mercadolibre_pricelist)
        self.assertLessEqual(self.template.list_price, 1.0)

        # Must not raise AttributeError on product.template.lst_price.
        self.variant._meli_set_product_price(
            self.template, 1234.56, config=self.company
        )

        self.assertAlmostEqual(
            self.template.list_price, 1234.56, places=2,
            msg="The MercadoLibre price was not written to product.template.list_price",
        )

    def test_template_price_is_preserved_when_list_price_is_set(self):
        """An existing list_price above 1 must not be overwritten."""
        self.template.list_price = 500.0

        self.variant._meli_set_product_price(
            self.template, 999.0, config=self.company
        )

        self.assertAlmostEqual(
            self.template.list_price, 500.0, places=2,
            msg="An already priced template must keep its list_price",
        )

    def test_product_template_has_no_lst_price_field(self):
        """Guard: product.template must not be assumed to expose lst_price.

        Fails if the module ever goes back to relying on that attribute, and
        documents why list_price is the correct field on templates.
        """
        self.assertNotIn(
            "lst_price", self.env["product.template"]._fields,
            "product.template unexpectedly exposes lst_price",
        )
        self.assertIn(
            "lst_price", self.env["product.product"]._fields,
            "product.product should expose lst_price",
        )
