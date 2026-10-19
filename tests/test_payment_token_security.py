"""Execute the actual payment-enrichment block with synthetic HTTP and ORM boundaries.

No Odoo, database, credentials, or real transport is used by these focused tests.
"""
import ast
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-payment-token-not-a-credential"
PAYMENT = {
    "id": 987, "transaction_amount": 125, "total_paid_amount": 128,
    "currency_id": "USD", "status": "approved",
    "date_created": "2026-10-19T01:00:00Z",
    "date_last_modified": "2026-10-19T02:00:00Z",
}
URL = "https://api.mercadopago.com/v1/payments/987"
FULL = {
    "shipping_amount": 3, "taxes_amount": 4,
    "transaction_details": {"total_paid_amount": 128.5},
    "payment_method_id": "visa", "payment_type_id": "credit_card",
    "installments": 3, "installment_amount": 42.8,
    "operation_type": "regular_payment", "date_approved": PAYMENT["date_created"],
    "authorization_code": "synthetic-code", "statement_descriptor": "SYNTHETIC",
    "coupon_amount": 0, "overpaid_amount": 0, "payer": {"id": 55},
    "issuer_id": 99, "marketplace_fee": 2,
    "card": {"first_six_digits": "111111", "last_four_digits": "1111"},
    "fee_details": [
        {"type": "application_fee", "fee_payer": "collector", "amount": 7},
        {"type": "financing_fee", "fee_payer": "payer", "amount": 2},
    ],
    "charges_details": [
        {"type": "fee", "name": "flat_fee", "amounts": {"original": 8}},
        {"type": "shipping", "name": "shipping", "amounts": {"original": 6}},
    ],
}


class TransportError(Exception):
    pass


class PaymentRows:
    def __init__(self, existing=False):
        self.existing = existing
        self.calls = []
        self.saved = []

    def search(self, domain):
        self.calls.append(deepcopy(domain))
        return self if self.existing else []

    def create(self, values):
        self.saved.append(("create", deepcopy(values)))
        return self

    def write(self, values):
        self.saved.append(("write", deepcopy(values)))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def payment_block():
    """Compile only the real importer branch, never a rewritten payment producer."""
    path = ROOT / "models/orders.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Constant)
        and node.test.left.value == "payments"
        and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.In)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Name)
        and node.test.comparators[0].id == "order_json"
    ]
    if len(matches) != 1:
        raise AssertionError("exactly one actual importer payments branch required")
    return compile(ast.Module(body=matches, type_ignores=[]), str(path), "exec")


class TestPaymentTokenSecurity(unittest.TestCase):
    def run_block(self, *, existing=False, error=None, data=None):
        rows = PaymentRows(existing)
        get = Mock(return_value=SimpleNamespace(json=lambda: deepcopy(FULL)))
        if error is not None:
            get.side_effect = error
        log = logging.Logger("synthetic-payment-enrichment")
        capture = LogCapture()
        log.addHandler(capture)
        order = SimpleNamespace(id=12, fee_amount=0, financing_fee_amount=0,
                                shipping_seller_cost=0, coupon_amount=0)
        sale = SimpleNamespace()
        body = deepcopy({"payments": [PAYMENT]} if data is None else data)
        original = deepcopy(body)
        scope = {
            "order_json": body, "order": order, "sorder": sale,
            "payments_obj": rows, "meli": SimpleNamespace(access_token=TOKEN),
            "requests": SimpleNamespace(get=get, exceptions=SimpleNamespace(
                RequestException=TransportError)),
            "_logger": log, "urlencode": urlencode,
            "ml_datetime": lambda value: "parsed:" + value if value else False,
        }
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            exec(payment_block(), scope)
        self.assertEqual(body, original)
        return rows, get, capture.messages, order, sale

    def test_new_payment_persists_only_token_free_url_and_source_fields(self):
        rows, _, _, _, _ = self.run_block()
        operation, saved = rows.saved[0]
        self.assertEqual(operation, "create")
        self.assertEqual(saved["mercadopago_url"], URL)
        self.assertNotIn(TOKEN, repr(rows.saved))
        self.assertEqual(saved["payment_id"], PAYMENT["id"])
        self.assertEqual(saved["transaction_amount"], 125)
        self.assertEqual(saved["currency_id"], "USD")
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(saved["date_created"], "parsed:" + PAYMENT["date_created"])
        self.assertEqual(saved["date_last_modified"],
                         "parsed:" + PAYMENT["date_last_modified"])

    def test_request_authenticates_only_with_bearer_header_and_keeps_timeout(self):
        _, get, _, _, _ = self.run_block()
        get.assert_called_once()
        args, kwargs = get.call_args
        self.assertEqual(args, (URL,))
        self.assertNotIn("params", kwargs)
        self.assertEqual(kwargs["headers"], {
            "Accept": "application/json", "User-Agent": "Odoo",
            "Content-type": "application/json", "Authorization": "Bearer " + TOKEN,
        })
        self.assertEqual(kwargs["timeout"], 10)

    def test_request_exception_never_logs_token_url_or_exception_message(self):
        failure = TransportError(URL + "?access_token=" + TOKEN)
        rows, get, messages, _, _ = self.run_block(error=failure)
        get.assert_called_once()
        self.assertEqual(len(messages), 1)
        self.assertNotIn(TOKEN, messages[0])
        self.assertNotIn(URL, messages[0])
        self.assertNotIn(str(failure), messages[0])
        self.assertIn("TransportError", messages[0])
        saved = rows.saved[0][1]
        self.assertEqual(saved["full_payment"], "")
        self.assertEqual(saved["total_paid_amount"], 128)
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(saved["fee_amount"], 0)

    def test_enrichment_full_response_fees_and_sale_totals_are_preserved(self):
        rows, _, _, order, sale = self.run_block()
        saved = rows.saved[0][1]
        self.assertEqual(saved["full_payment"], FULL)
        self.assertEqual(saved["total_paid_amount"], 128.5)
        self.assertEqual(saved["shipping_amount"], 3)
        self.assertEqual(saved["taxes_amount"], 4)
        self.assertEqual(saved["fee_amount"], 8)
        self.assertEqual(saved["financing_fee_amount"], 2)
        self.assertEqual(saved["shipping_seller_cost"], 6)
        self.assertEqual(saved["payment_method_id"], "visa")
        self.assertEqual(saved["payer_id"], "55")
        self.assertEqual(saved["issuer_id"], "99")
        self.assertEqual(saved["card_last_four_digits"], "1111")
        self.assertEqual((order.fee_amount, order.financing_fee_amount), (8, 2))
        self.assertEqual((sale.meli_fee_amount, sale.meli_financing_fee_amount,
                          sale.meli_shipping_seller_cost), (8, 2, 6))

    def test_existing_payment_updates_without_persisting_credentials(self):
        rows, _, _, _, _ = self.run_block(existing=True)
        self.assertEqual(rows.calls, [[("payment_id", "=", 987), ("order_id", "=", 12)]])
        self.assertEqual(len(rows.saved), 1)
        self.assertEqual(rows.saved[0][0], "write")
        self.assertEqual(rows.saved[0][1]["mercadopago_url"], URL)
        self.assertNotIn(TOKEN, repr(rows.saved))

    def test_absent_payments_never_calls_transport_or_persistence(self):
        rows, get, messages, _, _ = self.run_block(data={})
        get.assert_not_called()
        self.assertEqual(rows.saved, [])
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
