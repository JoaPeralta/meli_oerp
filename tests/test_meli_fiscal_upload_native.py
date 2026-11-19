"""Synthetic Odoo action tests. No real localization, OAuth, or HTTP calls."""
import io
from unittest.mock import patch

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools.pdf import PdfFileWriter


RECEIPT = "17_41546004-a96d-4e38-938e-80a1c134fc5d"
FOREIGN = "17_41546004-a96d-4e38-938e-80a1c134fc5e"
ABSENT = object()


class FiscalDouble:
    def __init__(self, pack=71, outcome=(201, {"ids": [RECEIPT]}), accepted=True):
        self.pack = pack
        self.outcome = outcome
        self.accepted = accepted
        self.posts = []
        self.gets = []
        self.documents = []
        self.hide_receipt = False
        self.list_status = 200

    def _meli_fiscal_request(self, path, *, document=None):
        if document is not None:
            self.posts.append((path, document))
            if self.accepted:
                self.documents = [{"id": RECEIPT, "filename": document[0], "file_type": "application/pdf"}]
            return self.outcome
        self.gets.append(path)
        if path == "/orders/23":
            order = {"id": 23, "seller": {"id": 17}}
            if self.pack is not ABSENT:
                order["pack_id"] = self.pack
            return 200, order
        if path == "/packs/71":
            return 200, {"id": 71, "status": "released", "orders": [{"id": 23}]}
        if self.list_status != 200:
            return self.list_status, None
        target = 23 if self.pack is None else 71
        return 200, {"pack_id": target, "fiscal_documents": [] if self.hide_receipt else self.documents}


@tagged("post_install", "-at_install")
class TestMeliFiscalUploadNative(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.company.sudo().mercadolibre_seller_id = "17"
        cls.env.user.sudo().write({
            "group_ids": [Command.link(cls.env.ref("meli_oerp.group_mercadolibre_manager").id),
                          Command.link(cls.env.ref("account.group_account_invoice").id),
                          Command.link(cls.env.ref("sales_team.group_sale_manager").id)],
        })
        cls.sale = cls.env["sale.order"].create({
            "partner_id": cls.partner_a.id, "company_id": cls.company.id,
            "order_line": [Command.create({"product_id": cls.product_a.id, "product_uom_qty": 1,
                                           "price_unit": 100, "tax_ids": [Command.clear()]})],
        })
        cls.source = cls.env["mercadolibre.orders"].create({
            "order_id": "23", "pack_id": "71", "company_id": cls.company.id, "sale_order": cls.sale.id,
        })
        cls.sale.meli_orders = cls.source
        cls.move = cls.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": cls.sale.partner_invoice_id.id,
            "company_id": cls.company.id, "invoice_date": fields.Date.context_today(cls.sale),
            "invoice_line_ids": [Command.create({
                "product_id": cls.product_a.id, "quantity": 1, "price_unit": 100,
                "tax_ids": [Command.clear()],
                "sale_line_ids": [Command.link(cls.sale.order_line.id)],
            })],
        })
        cls.move.action_post()
        writer, stream = PdfFileWriter(), io.BytesIO()
        writer.add_blank_page(width=120, height=120)
        writer.write(stream)
        cls.pdf = stream.getvalue()

    def act(self, client, *, authorized=True, pdf=None):
        def in_test_transaction(journal, scope, operation="read", values=None):
            # Keep uncommitted test fixtures visible and do not escape the
            # test rollback. The separate PostgreSQL probe checks durability.
            return journal.sudo()._apply_operation(scope, operation, values)

        with (
            patch.object(type(self.move), "_meli_fiscal_authorized", return_value=authorized),
            patch.object(type(self.move), "_meli_fiscal_client", return_value=client),
            patch.object(type(self.env["ir.actions.report"]), "_render_qweb_pdf",
                         return_value=(self.pdf if pdf is None else pdf, "pdf")),
            patch.object(type(self.env["mercadolibre.fiscal.upload"]), "_operation", in_test_transaction),
        ):
            return self.move.action_meli_upload_fiscal_pdf()["params"]["type"]

    def attempt(self):
        model = self.env["mercadolibre.fiscal.upload"].sudo()
        model.invalidate_model()
        return model.search([("move_id", "=", self.move.id)])

    def test_authorized_upload_verifies_and_does_not_duplicate(self):
        client = FiscalDouble()
        self.assertEqual(self.act(client), "success")
        self.assertEqual(self.act(client), "success")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][0], "/packs/71/fiscal_documents")
        row = self.attempt()
        self.assertEqual(row.state, "verified")
        self.assertEqual(row.remote_document_id, RECEIPT)
        self.assertTrue(row.uploaded_at)
        self.assertFalse(row.last_error)

    def test_no_real_authorization_provider_refuses_by_default(self):
        self.assertFalse(self.move._meli_fiscal_authorized())
        with self.assertRaisesRegex(UserError, "production ARCA authorization"):
            self.act(FiscalDouble(), authorized=False)
        self.assertFalse(self.attempt())

    def test_missing_pack_and_wrong_pack_are_refused_before_post(self):
        for pack in (ABSENT, 72, None):
            with self.subTest(pack=pack), self.assertRaises(UserError):
                self.act(FiscalDouble(pack=pack))
        self.assertFalse(self.attempt())

    def test_explicit_null_uses_order_id_on_pack_resource(self):
        self.source.pack_id = False
        client = FiscalDouble(pack=None)
        self.assertEqual(self.act(client), "success")
        self.assertEqual(client.posts[0][0], "/packs/23/fiscal_documents")
        self.assertNotIn("/packs/23", client.gets)

    def test_bad_and_oversize_pdf_never_reserve_or_send(self):
        client = FiscalDouble()
        for pdf in (b"%PDF-1.7\nbogus\n%%EOF\n", self.pdf * 10000):
            with self.subTest(length=len(pdf)), self.assertRaises(UserError):
                self.act(client, pdf=pdf)
        self.assertFalse(client.posts)
        self.assertFalse(self.attempt())

    def test_preexisting_pdf_is_not_claimed_or_overwritten(self):
        client = FiscalDouble()
        client.documents = [{"id": FOREIGN, "filename": "other.pdf", "file_type": "application/pdf"}]
        self.assertEqual(self.act(client), "warning")
        self.assertFalse(client.posts)
        self.assertFalse(self.attempt())

    def test_remote_forbidden_before_reservation_does_not_post(self):
        client = FiscalDouble()
        client.list_status = 403
        with self.assertRaises(UserError):
            self.act(client)
        self.assertFalse(client.posts)
        self.assertFalse(self.attempt())

    def test_timeout_after_delivery_is_reconciled_without_resending(self):
        client = FiscalDouble(outcome=(0, None))
        client.hide_receipt = True
        self.assertEqual(self.act(client), "warning")
        self.assertEqual(self.attempt().state, "uncertain")
        client.hide_receipt = False
        self.assertEqual(self.act(client), "success")
        self.assertEqual(self.attempt().remote_document_id, RECEIPT)
        self.assertEqual(len(client.posts), 1)

    def test_409_and_403_and_5xx_or_missing_receipt_never_repost(self):
        for status in (409, 403, 503, 201):
            with self.subTest(status=status), self.env.cr.savepoint():
                client = FiscalDouble(outcome=(status, {}), accepted=False)
                self.assertEqual(self.act(client), "warning")
                self.assertEqual(self.act(client), "warning")
                self.assertEqual(self.attempt().state, "uncertain")
                self.assertEqual(len(client.posts), 1)
                self.assertFalse(self.attempt().remote_document_id)
                # The lab uses Odoo TestCursor savepoints, never a real upload.
                self.attempt().unlink()

    def test_known_receipt_must_be_visible_before_success(self):
        client = FiscalDouble()
        client.hide_receipt = True
        self.assertEqual(self.act(client), "warning")
        self.assertEqual(self.attempt().remote_document_id, RECEIPT)
        self.assertFalse(self.attempt().uploaded_at)
        client.hide_receipt = False
        self.assertEqual(self.act(client), "success")
        self.assertEqual(len(client.posts), 1)
