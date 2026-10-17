"""Pure, conservative evidence rules; no transport, ORM or refund operation."""
import ast
import json
from decimal import Decimal, InvalidOperation

from .meli_pack_admission import AdmissionError, remote_id


CANCELLATION_STATES = ("cancelled", "pending_cancel", "partially_refunded")


def local_commercial_state(odoo_state, children, complete=True, amount_zero=False):
    """Classify known OWN commercial facts, never official remote pack status."""
    pending = "reconciliation_pending"
    if not complete or not children:
        return pending
    active = cancelled = 0
    for child in children:
        if not child.get("original_known"):
            return pending
        quantities = child.get("quantities") or []
        if not quantities or any(original <= 0 or quantity not in (0, original)
                                 for original, quantity in quantities):
            return pending
        if child["status"] == "paid" and all(quantity == original for original, quantity in quantities):
            active += 1
        elif (child["status"] == "cancelled" and child.get("applied_audit")
                and all(quantity == 0 for original, quantity in quantities)):
            cancelled += 1
        else:
            return pending
    if cancelled == len(children):
        return "cancelled" if odoo_state == "cancel" and amount_zero else pending
    if odoo_state not in ("draft", "sent", "sale"):
        return pending
    return "partially_cancelled" if cancelled and active else "active"


def amount(value):
    if isinstance(value, bool) or value is None:
        raise AdmissionError("An explicit finite nonnegative amount is required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AdmissionError("An explicit finite nonnegative amount is required") from None
    if not result.is_finite() or result < 0:
        raise AdmissionError("An explicit finite nonnegative amount is required")
    return result


def cancellation_evidence(payload, seller, expected_id=None, expected_pack=None):
    """Authenticate identity, not cancellation by rewriting it into a paid order."""
    if not isinstance(payload, dict) or not isinstance(payload.get("seller"), dict):
        raise AdmissionError("A complete own cancellation identity is required")
    oid = remote_id(payload.get("id"))
    if remote_id(payload["seller"].get("id")) != remote_id(seller):
        raise AdmissionError("Cancellation belongs to another seller")
    if expected_id and oid != remote_id(expected_id):
        raise AdmissionError("Cancellation order identity contradicts the requested order")
    pack = remote_id(payload["pack_id"]) if payload.get("pack_id") else False
    if expected_pack and pack != remote_id(expected_pack):
        raise AdmissionError("Cancellation pack identity changed")
    if payload.get("status") not in CANCELLATION_STATES:
        raise AdmissionError("A supported remote cancellation observation is required")
    currency = payload.get("currency_id")
    if not isinstance(currency, str) or not currency:
        raise AdmissionError("Cancellation currency is unknown")
    detail = payload.get("cancel_detail")
    if detail is not None and not isinstance(detail, dict):
        raise AdmissionError("Cancellation detail is malformed")
    if payload.get("status_detail") is not None and not isinstance(payload["status_detail"], str):
        raise AdmissionError("Remote status detail is malformed")
    # Full cancellation detail is retained, not hidden in a local-state field.
    # JSON encoding also rejects non-data objects before an audit is attempted.
    try:
        detail = json.loads(json.dumps(detail or {}, allow_nan=False))
    except (TypeError, ValueError):
        raise AdmissionError("Cancellation detail is malformed") from None
    evidence = {"id": oid, "pack_id": pack, "seller_id": remote_id(seller),
                "status": payload["status"], "status_detail": payload.get("status_detail") or "",
                "currency_id": currency, "cancel_detail": detail,
                "date_last_updated": payload.get("date_last_updated") or payload.get("last_updated"),
                "payments": []}
    for key in ("total_amount", "paid_amount"):
        if key in payload:
            evidence[key] = str(amount(payload[key]))
    for payment in payload.get("payments") or []:
        if not isinstance(payment, dict):
            raise AdmissionError("Payment evidence is malformed")
        # Do not persist URLs, tokens or a raw payment response in the UI/audit.
        fields = ("id", "status", "status_detail", "currency_id", "transaction_amount",
                  "transaction_amount_refunded", "total_paid_amount", "date_last_updated")
        evidence["payments"].append({key: payment[key] for key in fields if key in payment})
    return evidence


def reconciliation_reason(payload):
    if payload.get("status") != "cancelled":
        return "remote_cancellation_not_terminal"
    detail = payload.get("cancel_detail") or {}
    flags = str((detail, payload.get("status_detail"), payload.get("tags"))).lower()
    if any(word in flags for word in ("split", "bundle", "component", "kit")):
        return "split_or_transformation_requires_reconciliation"
    for key in ("discount_seller_amount", "financing_fee_amount", "shipping_cost",
                "shipping_seller_cost", "coupon_amount"):
        if payload.get(key) is not None and amount(payload[key]) != 0:
            return "shared_allocation_requires_reconciliation"
    for section in (payload.get("coupon"), payload.get("shipping")):
        if isinstance(section, dict) and any(amount(section[key]) != 0
                for key in ("amount", "cost", "list_cost") if key in section):
            return "shared_allocation_requires_reconciliation"
    return None


def payment_summary(rows, currency):
    """Deduplicate local captured identities. Missing/conflicting proof is unknown.

    Input rows contain only permitted local evidence, never token-bearing URLs.
    No sum is derived from child paid_amount or default-zero ORM floats.
    """
    grouped = {}
    for row in rows:
        key = str(row.get("payment_id") or "")
        if not key:
            continue
        captured = row.get("full_payment")
        if isinstance(captured, str):
            try:
                captured = json.loads(captured)
            except (ValueError, TypeError):
                try:
                    captured = ast.literal_eval(captured)
                except (ValueError, SyntaxError, TypeError):
                    captured = None
        # Compare the whole captured data for conflicting shared identities;
        # never display it or silently select one duplicate as the winner.
        grouped.setdefault(key, []).append((row.get("status"), row.get("currency_id"), captured))
    result = []
    for key, copies in sorted(grouped.items()):
        entry = {"payment_id": key, "reported_status": copies[0][0] or "Unknown",
                 "credited": "Unknown", "confirmed_refund": "Unknown"}
        if any(copy != copies[0] for copy in copies[1:]):
            entry["reported_status"] = "Unknown (conflicting captured evidence)"
            result.append(entry)
            continue
        status, row_currency, body = copies[0]
        if (not isinstance(body, dict) or str(body.get("id")) != key
                or body.get("currency_id") != currency or row_currency != currency
                or not body.get("date_last_updated") or body.get("status") != status):
            result.append(entry)
            continue
        if status == "approved" and body.get("status_detail") == "accredited":
            try:
                entry["credited"] = str(amount(body["transaction_details"]["total_paid_amount"]))
            except (KeyError, TypeError, AdmissionError):
                pass
        refunds = body.get("refunds")
        if isinstance(refunds, list) and refunds:
            seen, total, valid = {}, Decimal(0), True
            for refund in refunds:
                try:
                    rid = remote_id(refund.get("id"))
                    value = amount(refund["amount"])
                    if str(refund.get("payment_id")) != key or refund.get("status") != "approved":
                        raise AdmissionError("Refund is not confirmed")
                    if rid in seen and seen[rid] != value:
                        raise AdmissionError("Conflicting refund identity")
                    if rid not in seen:
                        total += value
                    seen[rid] = value
                except (AttributeError, KeyError, TypeError, AdmissionError):
                    valid = False
                    break
            if valid:
                entry["confirmed_refund"] = str(total)
        result.append(entry)
    return result
