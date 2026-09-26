# -*- coding: utf-8 -*-
"""The stock cron selectors use product_template for company ownership."""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "meli_cron_identity")
class TestStockCompanySchema(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_cron_post_update_stock = True
        self.other_company = self.env["res.company"].create({
            "name": "Stock selector other company",
        })

        # The selector is global SQL. Keep unrelated published products out of
        # this transaction so only these three ownership cases are exercised.
        self.env["product.product"].search([("meli_pub", "=", True)]).write({
            "meli_pub": False,
        })
        self.owned = self._product(self.company, "OWN")
        self.shared = self._product(False, "SHARED")
        self.foreign = self._product(self.other_company, "FOREIGN")

        self.env["product.product"].flush_model([
            "meli_pub", "meli_id", "meli_stock_update",
            "meli_shipping_logistic_type",
        ])
        self.env["product.template"].flush_model(["company_id"])

    def _product(self, company, suffix):
        template = self.env["product.template"].create({
            "name": "Stock selector %s" % suffix,
            "company_id": company.id if company else False,
        })
        product = template.product_variant_id
        product.write({
            "default_code": "STOCK-SELECTOR-%s" % suffix,
            "meli_pub": True,
            "meli_id": "MLA_STOCK_SELECTOR_%s" % suffix,
            "meli_shipping_logistic_type": "fulfillment",
            "meli_stock_error": False,
            "meli_stock_update": False,
        })
        return product

    def _assert_stock_selection(self, method_name):
        # The real cron method executes the SQL and visits the selected
        # products. Fulfillment items get an observable skip marker and never
        # invoke the outbound stock writer. The normal cron's separate safety
        # diagnostic is outside this selector regression.
        with patch.object(
            type(self.env["product.product"]), "product_post_stock"
        ) as outbound, patch.object(
            type(self.env["res.company"]), "meli_stock_diagnostic",
            return_value={},
        ):
            result = getattr(self.company, method_name)(meli=object())

        self.assertEqual(result, {})
        outbound.assert_not_called()
        self.assertEqual(self.owned.meli_stock_error, "fulfillment")
        self.assertEqual(self.shared.meli_stock_error, "fulfillment")
        self.assertFalse(self.foreign.meli_stock_error)

    def test_stock_selects_owned_and_shared_but_not_foreign(self):
        self._assert_stock_selection("meli_update_remote_stock")

    def test_stock_rt_selects_owned_and_shared_but_not_foreign(self):
        self._assert_stock_selection("meli_update_remote_stock_rt")
