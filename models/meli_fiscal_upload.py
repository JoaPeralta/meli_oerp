"""Manual invoice PDF upload; no commercial posting, payment, or scheduler."""
import io
import re
from uuid import uuid4

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.pdf import PdfFileReader
from psycopg2.errors import LockNotAvailable, SerializationFailure, UniqueViolation

from .meli_commercial_guard import _effects
from .meli_fiscal_documents import (
    FiscalError, check_pdf_envelope, find_verified_document, fiscal_id, order_target,
    pdf_documents, upload_receipt, verify_pack,
)


class FiscalUpload(models.Model):
    _name = "mercadolibre.fiscal.upload"
    _description = "Mercado Libre fiscal upload attempt"

    move_id = fields.Many2one("account.move", required=True, ondelete="restrict")
    company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    seller_id = fields.Char(required=True)
    target_id = fields.Char(required=True)
    pack_id = fields.Char()
    order_ids = fields.Json(required=True)
    filename = fields.Char(required=True)
    state = fields.Selection([("uncertain", "Pending verification"), ("verified", "Verified")],
                             required=True, default="uncertain")
    remote_document_id = fields.Char()
    uploaded_at = fields.Datetime()
    last_error = fields.Char()

    # Company is deliberately not part of the unique key: two companies using
    # the same marketplace account must not be able to send twice.
    _one_target_per_seller = models.Constraint(
        "UNIQUE(seller_id, target_id)", "The Mercado Libre fiscal target is already reserved."
    )

    @api.model
    def _operation(self, scope, operation="read", values=None):
        """Use a short side transaction; the irreversible send is never in it."""
        with self.env.registry.cursor() as cr:
            journal = api.Environment(cr, SUPERUSER_ID, {})[self._name]
            try:
                result = journal._apply_operation(scope, operation, values)
                if operation != "read":
                    cr.commit()
            except (UniqueViolation, SerializationFailure):
                if operation != "reserve":
                    raise
                cr.rollback()
                return None  # A concurrent winner owns the only POST.
            return result

    @api.model
    def _apply_operation(self, scope, operation, values):
        row = self.search([("seller_id", "=", scope["seller_id"]),
                           ("target_id", "=", scope["target_id"])], limit=1)
        if operation == "reserve":
            if row:
                return None
            # Don't wait for an uncommitted invoice in the caller's cursor.
            move = self.env["account.move"].browse(scope["move_id"]).exists()
            if not move or move.company_id.id != scope["company_id"]:
                raise FiscalError("invoice_not_committed")
            row = self.create(dict(scope, filename="invoice-" + uuid4().hex + ".pdf"))
        if not row:
            return None
        if (row.move_id.id != scope["move_id"] or row.company_id.id != scope["company_id"]
                or (row.pack_id or False) != scope["pack_id"] or row.order_ids != scope["order_ids"]):
            raise FiscalError("target_already_reserved")
        if operation == "update":
            # Concurrent reconciliation cannot reset a verified result or
            # overwrite a recorded ID with a different one.
            self.env.cr.execute("SELECT id FROM mercadolibre_fiscal_upload WHERE id = %s FOR UPDATE", (row.id,))
            row.invalidate_recordset(["state", "remote_document_id"])
            if row.state != "verified":
                new_id = values.get("remote_document_id")
                if row.remote_document_id and new_id and new_id != row.remote_document_id:
                    raise FiscalError("receipt_conflict")
                row.write(values)
        return {
            "filename": row.filename, "state": row.state,
            "remote_document_id": row.remote_document_id or None,
        }


class FiscalInvoice(models.Model):
    _inherit = "account.move"

    def _meli_fiscal_authorized(self):
        """Require a production authorization recorded by the supported ARCA provider."""
        self.ensure_one()
        provider = "l10n_ar.arca.attempt"
        required = {"l10n_ar_arca_state", "l10n_ar_arca_cae", "l10n_ar_arca_cae_due_date"}
        if provider not in self.env or not required.issubset(self._fields):
            return False
        cae = self.l10n_ar_arca_cae
        if (self.l10n_ar_arca_state != "authorized" or not self.l10n_ar_arca_cae_due_date
                or not isinstance(cae, str) or not re.fullmatch(r"[0-9]{14}", cae)):
            return False
        # No certificate, endpoint, response payload, or secrets are read.
        return bool(self.env[provider].sudo().search_count([
            ("move_id", "=", self.id), ("company_id", "=", self.company_id.id),
            ("environment", "=", "production"), ("state", "=", "authorized"),
            ("cae", "=", cae), ("cae_due_date", "=", self.l10n_ar_arca_cae_due_date),
        ], limit=1))

    def _meli_fiscal_scope(self):
        self.ensure_one()
        self.check_access("write")
        if (not self.env.user.has_group("meli_oerp.group_mercadolibre_manager")
                or not self.env.user.has_group("account.group_account_invoice")
                or self.company_id not in self.env.companies):
            raise AccessError(_("You are not authorized to upload this fiscal document."))
        if self.state != "posted" or self.move_type != "out_invoice" or not self._meli_fiscal_authorized():
            raise FiscalError("invoice_not_authorized")

        commercial = self.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        sale = commercial.sale_line_ids.order_id
        if (not commercial or any(not line.sale_line_ids for line in commercial)
                or len(sale) != 1 or sale.company_id != self.company_id
                or sale.partner_invoice_id != self.partner_id or sale.currency_id != self.currency_id):
            raise FiscalError("invoice_sale_mismatch")
        sale.check_access("read")
        sale_lines = sale.order_line.filtered(lambda line: not line.display_type)
        if (commercial.sale_line_ids != sale_lines or sale._meli_posted_invoices() != self
                or self.currency_id.compare_amounts(sale.amount_total, self.amount_total)):
            raise FiscalError("invoice_sale_mismatch")
        _effects(sale)
        orders = sale.meli_orders
        orders.check_access("read")
        if (not orders or any(order.company_id != self.company_id or order.sale_order != sale
                              for order in orders)):
            raise FiscalError("order_provenance_missing")
        keys = sorted(fiscal_id(order.order_id) for order in orders)
        packs = {fiscal_id(order.pack_id) if order.pack_id else False for order in orders}
        if len(packs) != 1 or len(set(keys)) != len(keys):
            raise FiscalError("pack_identity_mismatch")
        pack = packs.pop()
        if not pack and len(keys) != 1:
            raise FiscalError("pack_identity_mismatch")
        return {
            "move_id": self.id, "company_id": self.company_id.id,
            "seller_id": fiscal_id(self.company_id.mercadolibre_seller_id),
            "order_ids": keys, "pack_id": pack, "target_id": pack or keys[0],
        }

    def _meli_fiscal_client(self, scope):
        client = self.env["meli.util"].get_new_instance(self.company_id)
        if (client.need_login() or not client.access_token
                or fiscal_id(client.seller_id) != scope["seller_id"]):
            raise FiscalError("seller_session_unavailable")
        return client

    def _meli_fiscal_verify_remote(self, client, scope):
        for key in scope["order_ids"]:
            status, body = client._meli_fiscal_request("/orders/" + key)
            if status != 200 or body is None or body.get("error"):
                raise FiscalError("order_unavailable")
            if order_target(body, key, scope["seller_id"], scope["pack_id"] or None) != scope["target_id"]:
                raise FiscalError("pack_identity_mismatch")
        if scope["pack_id"]:
            status, body = client._meli_fiscal_request("/packs/" + scope["pack_id"])
            if status != 200 or body is None or body.get("error"):
                raise FiscalError("pack_unavailable")
            verify_pack(body, scope["pack_id"], scope["order_ids"])

    def _meli_fiscal_pdf(self):
        try:
            data, report_type = self.env["ir.actions.report"].with_context(
                report_pdf_no_attachment=True
            )._render_qweb_pdf("account.account_invoices", res_ids=self.ids)
        except Exception:
            raise FiscalError("pdf_render_failed") from None
        if report_type != "pdf":
            raise FiscalError("invalid_pdf_format")
        check_pdf_envelope(data)
        try:
            pdf = PdfFileReader(io.BytesIO(data), strict=True)
            if pdf.is_encrypted or not len(pdf.pages):
                raise ValueError
        except Exception:
            raise FiscalError("invalid_pdf_format") from None
        return data

    def _meli_fiscal_listing(self, client, route, scope):
        status, data = client._meli_fiscal_request(route)
        return pdf_documents(status, data, scope["target_id"], scope["seller_id"])

    def _meli_fiscal_reconcile(self, client, route, scope, attempt, error=None):
        journal = self.env["mercadolibre.fiscal.upload"]
        try:
            listed = self._meli_fiscal_listing(client, route, scope)
        except FiscalError as exc:
            journal._operation(scope, "update", {"last_error": str(exc)})
            return "uncertain"
        confirmed = find_verified_document(listed, attempt["remote_document_id"], attempt["filename"])
        if not confirmed:
            journal._operation(scope, "update", {"last_error": error or
                                                 ("different_pdf_exists" if listed else "receipt_not_visible")})
            return "uncertain"
        journal._operation(scope, "update", {
            "state": "verified", "remote_document_id": confirmed,
            "uploaded_at": fields.Datetime.now(), "last_error": False,
        })
        return "verified"

    def _meli_fiscal_run(self):
        scope = self._meli_fiscal_scope()
        # Keep the validated invoice from being reset/cancelled during this
        # action. NO KEY UPDATE permits the journal's separate FK insert.
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute("SELECT id FROM account_move WHERE id = %s FOR NO KEY UPDATE NOWAIT", (self.id,))
        except LockNotAvailable:
            return "uncertain"
        client = self._meli_fiscal_client(scope)
        self._meli_fiscal_verify_remote(client, scope)
        route = "/packs/" + scope["target_id"] + "/fiscal_documents"
        journal = self.env["mercadolibre.fiscal.upload"]
        existing = journal._operation(scope)
        if existing:
            return self._meli_fiscal_reconcile(client, route, scope, existing)
        if self._meli_fiscal_listing(client, route, scope):
            return "existing"
        pdf = self._meli_fiscal_pdf()
        attempt = journal._operation(scope, "reserve")
        if not attempt:
            return "uncertain"
        # The committed reservation is a hard fence. Nothing in this or a
        # subsequent RPC can release it on a timeout, rollback, 409 or 5xx.
        try:
            if self._meli_fiscal_listing(client, route, scope):
                journal._operation(scope, "update", {"last_error": "different_pdf_exists"})
                return "existing"
        except FiscalError as exc:
            journal._operation(scope, "update", {"last_error": str(exc)})
            return "uncertain"
        status, data = client._meli_fiscal_request(route, document=(attempt["filename"], pdf))
        receipt = upload_receipt(status, data)
        if receipt:
            attempt = journal._operation(scope, "update", {"remote_document_id": receipt})
        # Only fixed error codes reach storage and the receipt is independent
        # of response prose. Unknown outcomes are reconciled by GET only.
        cause = {
            0: "upload_transport_uncertain", 403: "upload_forbidden", 409: "upload_conflict",
            200: "upload_receipt_invalid", 201: "upload_receipt_invalid",
        }.get(status, "upload_unverified")
        return self._meli_fiscal_reconcile(client, route, scope, attempt, None if receipt else cause)

    def action_meli_upload_fiscal_pdf(self):
        """Manual button. An ambiguous attempt never releases the send fence."""
        self.ensure_one()
        try:
            result = self._meli_fiscal_run()
        except FiscalError as exc:
            message = {
                "invoice_not_authorized": _("A posted customer invoice with production ARCA authorization is required."),
                "invalid_pdf_size": _("The fiscal PDF must not exceed 1 MB."),
                "invalid_pdf_format": _("Odoo did not produce a valid fiscal PDF."),
                "target_already_reserved": _("This Mercado Libre fiscal target is reserved for a different invoice."),
            }.get(str(exc), _("The invoice, seller, order, pack, or fiscal document could not be verified."))
            raise UserError(message) from None
        options = {
            "verified": ("success", _("The fiscal PDF is verified in Mercado Libre.")),
            "existing": ("warning", _("A fiscal PDF already exists. No replacement was sent.")),
            "uncertain": ("warning", _("The fiscal upload is not confirmed. Run this action again to check; it will not resend.")),
        }
        level, message = options[result]
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": level, "message": message, "sticky": level != "success"}}
