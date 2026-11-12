"""Native regressions for local-only reads and explicit posting metadata refresh."""

from unittest.mock import patch

from psycopg2.errors import LockNotAvailable

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from ..models.meli_posting_metadata import CORE_FIELDS


FIRST = "MLA1651548355"
SECOND = "MLA1651389099"
SELLER = "7123456"
SKU = "METADATA-TEST-SKU"


def item(item_id=FIRST, price=780000, **extra):
    return {
        "id": item_id, "seller_id": int(SELLER), "title": "Synthetic offer",
        "status": "active", "price": price,
        "permalink": "https://articulo.mercadolibre.com.ar/MLA-" + item_id[3:] + "-synthetic",
        "attributes": [{"id": "SELLER_SKU", "value_name": SKU}],
        "available_quantity": 0, "variations": [],
        **extra,
    }


class _ItemClient:
    access_token = "synthetic-token"
    last_status_code = 200

    def __init__(self, items):
        self.items = items
        self.paths = []

    def get(self, path, _params):
        self.paths.append(path)
        payload = self.items[path.removeprefix("/items/")]
        return type("Response", (), {"status_code": 200, "headers": {},
                                     "json": lambda self: payload})()

    def need_login(self):
        return False


@tagged("post_install", "-at_install")
class TestPostingUpdateErrorResponse(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            "mercadolibre_seller_id": SELLER, "mercadolibre_cron_get_questions": False,
            "mercadolibre_import_search_sku": True,
        })
        self.manager = new_test_user(
            self.env, login="posting-metadata-manager",
            groups="base.group_user,meli_oerp.group_mercadolibre_manager",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        self.product = self.env["product.product"].create({
            "name": "Posting metadata product", "default_code": SKU, "standard_price": 123,
            "company_id": self.company.id, "meli_id": FIRST,
        })
        self.postings = self.env["mercadolibre.posting"]

    def _row(self, item_id=FIRST, **values):
        return self.postings.create({
            "meli_id": item_id, "product_id": self.product.id, "name": "Synthetic offer", **values,
        })

    def _refresh(self, rows, reader):
        with patch.object(type(self.postings), "_meli_metadata_reader", return_value=reader):
            return self.postings.with_user(self.manager)._meli_refresh_metadata(rows.ids)

    def _bulk(self, items):
        client = _ItemClient(items)
        context = {
            "meli_id": ",".join(items), "post_state": "all", "force_dont_create": True,
            "force_meli_pub": False, "force_import_images": False,
            "batch_processing_unit": 10, "batch_processing_unit_offset": 0,
        }
        with patch.object(type(self.env["meli.util"]), "get_new_instance", return_value=client):
            self.company.product_meli_get_products(context=context)
        return client

    def test_read_of_effective_form_fields_does_not_fetch_or_write(self):
        row = self._row()
        view = self.env.ref("meli_oerp.view_meli_posting_form")
        self.assertIn("posting_update", self.postings.get_view(view_id=view.id, view_type="form")["arch"])
        with patch.object(type(self.env["meli.util"]), "get_new_instance",
                          side_effect=AssertionError("Unexpected OAuth or GET")), patch.object(
                              type(self.postings), "write", side_effect=AssertionError("Unexpected write")):
            self.assertEqual(row.read(["posting_update"])[0]["posting_update"], "incomplete")

    def test_bulk_match_initializes_offer_prices_independently(self):
        remote = {FIRST: item(), SECOND: item(SECOND, price=944585)}
        client = self._bulk(remote)
        rows = self.postings.search([("product_id", "=", self.product.id)])
        self.assertEqual(set(rows.mapped("meli_id")), set(remote))
        self.assertEqual({row.meli_id: row.meli_price for row in rows},
                         {FIRST: "780000", SECOND: "944585"})
        self.assertEqual(set(rows.mapped("meli_seller_custom_field")), {SKU})
        self.assertEqual(set(rows.mapped("meli_status")), {"active"})
        self.assertTrue(all(row.meli_permalink for row in rows))
        self.assertEqual(client.paths, ["/items/" + FIRST, "/items/" + SECOND])
        self.assertEqual(self.product.standard_price, 123)
        self.assertEqual(self.product.meli_id, FIRST)

    def test_bulk_match_preserves_unavailable_fields(self):
        client = self._bulk({FIRST: item(price=None, permalink=None)})
        row = self.postings.search([("meli_id", "=", FIRST)])
        self.assertEqual(row.meli_status, "active")
        self.assertFalse(row.meli_price)
        self.assertFalse(row.meli_permalink)
        self.assertEqual(row.posting_update, "incomplete")
        self.assertEqual(client.paths, ["/items/" + FIRST])

    def test_refresh_is_independent_of_questions_and_does_not_reprice_product(self):
        row = self._row(meli_status="paused", meli_price="3", meli_permalink=item()["permalink"])
        list_price = self.product.lst_price
        with patch.object(type(self.postings), "posting_query_questions",
                          side_effect=AssertionError("Unexpected questions call")):
            result = self._refresh(row, lambda _item: (item(), None))
        self.assertEqual(result, {"updated": 1, "unchanged": 0, "skipped": 0})
        self.assertEqual(row.meli_price, "780000")
        self.assertEqual(row.posting_update, "stored")
        self.assertEqual(self.product.lst_price, list_price)
        self.assertEqual(self.product.standard_price, 123)

    def test_incomplete_history_is_not_an_ordinary_refresh(self):
        row = self._row()
        result = self._refresh(row, lambda _item: self.fail("Unexpected read"))
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(row.meli_status)

    def test_authentication_rejection_and_malformed_data_do_not_write(self):
        row = self._row(meli_status="paused", meli_price="3", meli_permalink=item()["permalink"])
        before = row.read(list(CORE_FIELDS))[0]
        for response in ((None, "authentication_rejected"), ("not JSON", None),
                         (item(seller_id=123), None), (item(price="NaN"), None)):
            with self.subTest(response=response):
                self.assertEqual(self._refresh(row, lambda _item: response)["skipped"], 1)
                self.assertEqual(row.read(list(CORE_FIELDS))[0], before)

    def test_read_only_actor_cannot_request_metadata(self):
        row = self._row()
        reader = new_test_user(
            self.env, login="posting-metadata-reader",
            groups="base.group_user,meli_oerp.group_mercadolibre_reader",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        with self.assertRaises(AccessError):
            self.postings.with_user(reader)._meli_refresh_metadata(row.ids)

    def _repair(self, rows, offers, **kw):
        def read_item(item_id):
            return offers[item_id], None

        self.manager.write({"group_ids": [(4, self.env.ref("base.group_system").id)]})
        with patch.object(type(self.postings), "_meli_metadata_reader", return_value=read_item), patch.object(
            type(self.env["meli.util"]), "get_new_instance", side_effect=AssertionError("Unexpected OAuth")
        ):
            return self.postings.with_user(self.manager)._meli_repair_metadata(rows.ids, **kw)

    def test_historical_preview_apply_and_replay_keep_offer_prices_distinct(self):
        first = self._row(meli_price="770000")
        second = self._row(SECOND)
        untouched = self._row("MLA11111")
        offers = {FIRST: item(), SECOND: item(SECOND, price=944585)}
        before = self.product.read(["standard_price", "lst_price", "meli_id", "qty_available"])[0]
        preview = self._repair(first | second, offers)
        self.assertTrue(preview["dry_run"])
        self.assertEqual([entry["result"] for entry in preview["rows"]], ["proposed", "proposed"])
        self.assertEqual(preview["rows"][1]["changes"]["meli_price"],
                         {"before": None, "proposed": "944585"})
        self.assertNotIn(SKU, str(preview))
        self.assertFalse(first.meli_status)
        self.assertFalse(second.meli_price)
        applied = self._repair(first | second, offers, dry_run=False, reviewed_plan=preview["plan"])
        self.assertEqual([entry["result"] for entry in applied["rows"]], ["applied", "applied"])
        self.assertEqual(first.meli_price, "770000")
        self.assertEqual(second.meli_price, "944585")
        self.assertEqual(first.product_id, second.product_id)
        self.assertFalse(untouched.meli_status)
        self.assertEqual(self.product.read(list(before))[0], before)
        repeated = self._repair(first | second, {})
        self.assertEqual([entry["reason"] for entry in repeated["rows"]],
                         ["no_missing_fields", "no_missing_fields"])
        with self.assertRaises(ValidationError):
            self._repair(first | second, {}, dry_run=False, reviewed_plan=preview["plan"])

    def test_stale_preview_applies_no_rows(self):
        first, second = self._row(), self._row(SECOND)
        offers = {FIRST: item(), SECOND: item(SECOND, price=944585)}
        preview = self._repair(first | second, offers)
        second.meli_price = "900"
        with self.assertRaises(ValidationError):
            self._repair(first | second, offers, dry_run=False, reviewed_plan=preview["plan"])
        self.assertFalse(first.meli_status)
        self.assertEqual(second.meli_price, "900")

    def test_lock_conflict_applies_no_rows(self):
        first, second = self._row(), self._row(SECOND)
        offers = {FIRST: item(), SECOND: item(SECOND, price=944585)}
        preview = self._repair(first | second, offers)
        original_lock = type(self.postings)._meli_metadata_lock

        def lock(row, product):
            if row == second:
                raise LockNotAvailable("synthetic lock conflict")
            return original_lock(row, product)

        with patch.object(type(self.postings), "_meli_metadata_lock", lock), self.assertRaises(ValidationError):
            self._repair(first | second, offers, dry_run=False, reviewed_plan=preview["plan"])
        self.assertFalse(first.meli_status)
        self.assertFalse(second.meli_status)

    def test_repair_rejects_foreign_seller_or_uncorroborated_sibling(self):
        first, second = self._row(), self._row(SECOND)
        offers = {
            FIRST: item(seller_id=9876543),
            SECOND: item(SECOND, attributes=[], seller_custom_field=SKU),
        }
        preview = self._repair(first | second, offers)
        self.assertEqual([entry["reason"] for entry in preview["rows"]],
                         ["unproven_item_or_seller", "binding_unproven"])
        self.assertFalse(first.meli_status)
        self.assertFalse(second.meli_status)

    def test_repair_denies_non_system_manager_and_apply_without_preview(self):
        row = self._row()
        with self.assertRaises(AccessError):
            self.postings.with_user(self.manager)._meli_repair_metadata(row.ids)
        with self.assertRaises(ValidationError):
            self._repair(row, {}, dry_run=False)

    def test_shared_and_foreign_company_bindings_do_not_fetch(self):
        other_company = self.env["res.company"].create({"name": "Synthetic foreign company"})
        other_product = self.env["product.product"].create({
            "name": "Synthetic foreign product", "company_id": other_company.id,
        })
        shared_product = self.env["product.product"].create({"name": "Synthetic shared product"})
        first = self._row(product_id=other_product.id)
        second = self._row(SECOND, product_id=shared_product.id)
        preview = self._repair(first | second, {})
        self.assertEqual([row["reason"] for row in preview["rows"]],
                         ["binding_unproven", "binding_unproven"])
        self.assertFalse(first.meli_status)
        self.assertFalse(second.meli_status)

    def test_exact_variation_mismatch_is_not_repaired(self):
        self.product.meli_id_variation = "23"
        row = self._row(meli_variation_id="23")
        preview = self._repair(row, {FIRST: item(variations=[{"id": 24, "price": 500}])})
        self.assertEqual(preview["rows"][0]["reason"], "unproven_variation")
        self.assertFalse(row.meli_price)

    def test_sibling_offer_can_use_exact_user_product_without_sku(self):
        self.product.meli_user_product_id = "MLAU3747606772"
        row = self._row(SECOND)
        offers = {SECOND: item(SECOND, price=944585, attributes=[],
                               user_product_id=self.product.meli_user_product_id)}
        preview = self._repair(row, offers)
        self.assertEqual(preview["rows"][0]["result"], "proposed")
        self.assertEqual(preview["rows"][0]["field_states"]["meli_seller_custom_field"], "absent")
        self._repair(row, offers, dry_run=False, reviewed_plan=preview["plan"])
        self.assertEqual(row.meli_price, "944585")
        self.assertFalse(row.meli_seller_custom_field)
        self.assertEqual(self.product.meli_id, FIRST)

    def test_metadata_refresh_and_repair_leave_automation_inactive(self):
        records = self.env["ir.model.data"].search(
            [("module", "=", "meli_oerp"), ("model", "=", "ir.cron")],
        )
        crons = self.env["ir.cron"].browse(records.mapped("res_id"))
        self.assertTrue(crons)
        row = self._row()
        preview = self._repair(row, {FIRST: item()})
        self._repair(row, {FIRST: item()}, dry_run=False, reviewed_plan=preview["plan"])
        self._refresh(row, lambda _item: (item(price=800000), None))
        self.assertFalse(crons.filtered("active"))
        self.assertFalse(self.company.mercadolibre_cron_get_questions)
