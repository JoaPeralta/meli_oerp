"""Historical order economics, without HTTP, ORM or fiscal side effects.

The caller owns observation acquisition, identity admission and execution policy.
Strings encode decimal amounts in the versioned, JSON-serializable audit result.
An absent value is None, never an inferred zero. See docs/order-economics.md.
"""
from decimal import Decimal, InvalidOperation
import json


class EconomicEvidenceError(ValueError):
    def __init__(self, reason, status="pending"):
        super().__init__(reason)
        self.reason, self.status = reason, status


def money(value):
    if type(value) not in (int, float, str, Decimal) or value == "":
        raise EconomicEvidenceError("economic_number_missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EconomicEvidenceError("economic_number_invalid", "contradictory") from None
    if not result.is_finite() or result < 0:
        raise EconomicEvidenceError("economic_number_invalid", "contradictory")
    return result


def decimal_text(value):
    return format(value, "f")


def _id(value):
    if type(value) not in (int, str) or not str(value).strip():
        raise EconomicEvidenceError("economic_identity_missing")
    return str(value)


def _body(observation, resource):
    if not isinstance(observation, dict):
        raise EconomicEvidenceError(resource + "_observation_missing")
    status, body = observation.get("status"), observation.get("body")
    if status != 200:
        if (resource == "discounts" and status == 404 and isinstance(body, dict)
                and body.get("error") == "discount_not_found"):
            # Neither this undocumented response nor scalar price equality closes
            # the coupon/cashback evidence population. Never turn it into [].
            raise EconomicEvidenceError("discount_not_found_evidence_incomplete")
        code = str(status) if type(status) is int else "unknown"
        raise EconomicEvidenceError(resource + "_http_" + code)
    if observation.get("missing") or not isinstance(body, dict) or body.get("error"):
        raise EconomicEvidenceError(resource + "_observation_incomplete")
    return body


def _quantity(value):
    if type(value) is not int or value <= 0:
        raise EconomicEvidenceError("economic_quantity_invalid", "contradictory")
    return value


def _items(order, currency):
    rows = order.get("order_items")
    if not isinstance(rows, list) or not rows:
        raise EconomicEvidenceError("order_items_missing")
    result, identities = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("item"), dict):
            raise EconomicEvidenceError("order_item_incomplete")
        iid = _id(row["item"].get("id"))
        variant = row["item"].get("variation_id")
        key = (iid, None if variant is None else _id(variant))
        if key in identities:
            raise EconomicEvidenceError("item_identity_ambiguous")
        identities.add(key)
        if row.get("currency_id", currency) != currency:
            raise EconomicEvidenceError("item_currency_mismatch", "contradictory")
        quantity, unit = _quantity(row.get("quantity")), money(row.get("unit_price"))
        effective = unit * quantity
        reference = money(row["gross_price"]) if "gross_price" in row else None
        inline = row.get("discounts")
        if inline is not None:
            # The published example is an array of per-unit amounts.full.
            if not isinstance(inline, list):
                raise EconomicEvidenceError("inline_discount_shape_unsupported", "unsupported")
            full = sum((money(entry["amounts"]["full"]) for entry in inline), Decimal(0))
            computed = (unit + full) * quantity
            if reference is not None and reference != computed:
                raise EconomicEvidenceError("reference_price_contradiction", "contradictory")
            reference = computed
        if reference is not None and reference < effective:
            raise EconomicEvidenceError("reference_price_contradiction", "contradictory")
        result.append({"item_id": iid, "variation_id": key[1], "quantity": quantity,
                       "unit_price": decimal_text(unit), "effective_total": decimal_text(effective),
                       "reference_total": None if reference is None else decimal_text(reference),
                       # This optional field's universal historical semantics are
                       # not established; retain it but never use it as a basis.
                       "unverified_full_unit_price": None if "full_unit_price" not in row
                       else decimal_text(money(row["full_unit_price"])), "source": "orders.order_items"})
    return result


def _benefits(body, items):
    details = body.get("details")
    if not isinstance(details, list):
        raise EconomicEvidenceError("discounts_details_missing")
    benefits, seen = [], {}
    for detail in details:
        if not isinstance(detail, dict) or detail.get("type") not in ("discount", "coupon", "cashback"):
            raise EconomicEvidenceError("benefit_type_unsupported", "unsupported")
        kind = detail["type"]
        supplier = detail.get("supplier", {})
        if not isinstance(supplier, dict):
            raise EconomicEvidenceError("benefit_supplier_incomplete")
        origin = (supplier.get("offer_id") if kind == "discount"
                  else (detail.get(kind) or {}).get("id"))
        origin = _id(origin)
        allocations = detail.get("items")
        if not isinstance(allocations, list) or not allocations:
            raise EconomicEvidenceError("benefit_allocation_missing")
        for allocation in allocations:
            iid = _id(allocation.get("id"))
            candidates = [item for item in items if item["item_id"] == iid]
            if len(candidates) != 1:
                raise EconomicEvidenceError("benefit_item_unbound")
            quantity = _quantity(allocation.get("quantity"))
            if quantity > candidates[0]["quantity"]:
                raise EconomicEvidenceError("benefit_quantity_contradiction", "contradictory")
            amounts = allocation.get("amounts")
            if not isinstance(amounts, dict) or "total" not in amounts or "seller" not in amounts:
                raise EconomicEvidenceError("benefit_funding_missing")
            total, seller = money(amounts["total"]), money(amounts["seller"])
            if seller > total:
                raise EconomicEvidenceError("benefit_funding_contradiction", "contradictory")
            record = {"type": kind, "id": origin, "item_id": iid, "quantity": quantity,
                      "element_id": allocation.get("element_id"),
                      "total": decimal_text(total), "seller": decimal_text(seller),
                      "marketplace": decimal_text(total - seller),
                      "campaign_id": supplier.get("meli_campaign", supplier.get("campaign_id")),
                      "offer_id": supplier.get("offer_id"), "funding_mode": supplier.get("funding_mode"),
                      "incorporated_in_unit_price": kind == "discount",
                      "effect": {"discount": "effective_product_price", "coupon": "buyer_payment_benefit",
                                 "cashback": "cashback"}[kind], "source": "orders.discounts"}
            # The same identified benefit is corroboration, not another discount.
            # Conflicting duplicates never silently elect one source.
            identity = (kind, origin, iid, allocation.get("element_id"))
            if identity in seen:
                if seen[identity] != record:
                    raise EconomicEvidenceError("benefit_duplicate_contradiction", "contradictory")
                continue
            seen[identity] = record
            benefits.append(record)
    for item in items:
        price_discount = sum((money(b["total"]) for b in benefits
                              if b["item_id"] == item["item_id"] and b["type"] == "discount"), Decimal(0))
        if (item["reference_total"] is not None
                and money(item["reference_total"]) - money(item["effective_total"]) != price_discount):
            raise EconomicEvidenceError("reference_discount_mismatch", "contradictory")
    return benefits


def _payments(order, currency, observations):
    rows = order.get("payments")
    if not isinstance(rows, list) or not rows:
        raise EconomicEvidenceError("payments_missing")
    result, seen = [], set()
    for row in rows:
        pid = _id(row.get("id"))
        if pid in seen:
            raise EconomicEvidenceError("payment_identity_ambiguous", "contradictory")
        seen.add(pid)
        if row.get("status") != "approved":
            raise EconomicEvidenceError("payment_status_unsupported", "unsupported")
        if row.get("currency_id") != currency:
            raise EconomicEvidenceError("payment_currency_mismatch", "contradictory")
        transaction = money(row.get("transaction_amount"))
        paid = money(row["total_paid_amount"]) if "total_paid_amount" in row else None
        shipping = coupon = net = None
        source = "orders.payments"
        if observations is not None:
            mp = _body(observations.get(pid), "payment")
            if (_id(mp.get("id")) != pid or mp.get("currency_id") != currency
                    or mp.get("status") != "approved"):
                raise EconomicEvidenceError("payment_identity_mismatch", "contradictory")
            if money(mp.get("transaction_amount")) != transaction:
                raise EconomicEvidenceError("payment_transaction_mismatch", "contradictory")
            details = mp.get("transaction_details")
            if not isinstance(details, dict):
                raise EconomicEvidenceError("payment_details_missing")
            mp_paid = money(details.get("total_paid_amount"))
            if paid is not None and paid != mp_paid:
                raise EconomicEvidenceError("payment_paid_mismatch", "contradictory")
            paid = mp_paid
            shipping = money(mp["shipping_amount"]) if "shipping_amount" in mp else None
            coupon = money(mp["coupon_amount"]) if "coupon_amount" in mp else None
            net = money(details["net_received_amount"]) if "net_received_amount" in details else None
            if "transaction_amount_refunded" in mp and money(mp["transaction_amount_refunded"]):
                raise EconomicEvidenceError("refunded_payment_unsupported", "unsupported")
            source = "orders.payments+mercadopago.payment"
        if paid is None:
            raise EconomicEvidenceError("payment_paid_missing")
        result.append({"id": pid, "transaction_amount": decimal_text(transaction),
                       "buyer_paid": decimal_text(paid), "shipping_amount": None if shipping is None else decimal_text(shipping),
                       "coupon_amount": None if coupon is None else decimal_text(coupon),
                       "net_received": None if net is None else decimal_text(net), "source": source})
    if observations is not None and set(observations) != seen:
        raise EconomicEvidenceError("payment_population_mismatch", "contradictory")
    return result


def interpret_order_economics(order, discounts_observation, payment_observations=None):
    """Interpret only documented inputs; unsupported/evidence gaps are explicit.

    Transport observations are {status, body, missing}. MP is optional corroboration
    when order payment fields suffice; if supplied, every observation must agree.
    This result is not an authorization to import, invoice, confirm or ship.
    """
    result = {"schema": "meli.order.economics/v1", "status": "pending", "reason": None,
              "order_id": None, "currency_id": None, "items": [], "benefits": [], "payments": [],
              "commercial_total": None, "invoice_product_basis": None, "buyer_paid": None,
              "shipping_amount": None, "shipping_in_buyer_paid": None,
              "seller_funded": None, "marketplace_funded": None}
    try:
        body = _body(discounts_observation, "discounts")
        if not isinstance(order, dict):
            raise EconomicEvidenceError("order_observation_missing")
        result["order_id"] = _id(order.get("id"))
        currency = order.get("currency_id")
        if not isinstance(currency, str) or not currency.strip():
            raise EconomicEvidenceError("order_currency_missing")
        result["currency_id"] = currency
        items = _items(order, currency)
        benefits = _benefits(body, items)
        commercial = sum((money(item["effective_total"]) for item in items), Decimal(0))
        if commercial != money(order.get("total_amount")):
            raise EconomicEvidenceError("commercial_total_mismatch", "contradictory")
        payments = _payments(order, currency, payment_observations)
        if sum((money(p["transaction_amount"]) for p in payments), Decimal(0)) != commercial:
            raise EconomicEvidenceError("payment_transaction_total_mismatch", "contradictory")
        buyer_paid = money(order.get("paid_amount"))
        if sum((money(p["buyer_paid"]) for p in payments), Decimal(0)) != buyer_paid:
            raise EconomicEvidenceError("payment_paid_total_mismatch", "contradictory")
        coupon_total = sum((money(b["total"]) for b in benefits if b["type"] == "coupon"), Decimal(0))
        coupon = order.get("coupon")
        if coupon is not None:
            if not isinstance(coupon, dict) or "amount" not in coupon:
                raise EconomicEvidenceError("order_coupon_incomplete")
            if money(coupon["amount"]) != coupon_total:
                raise EconomicEvidenceError("coupon_amount_mismatch", "contradictory")
        mp_coupons = [p["coupon_amount"] for p in payments]
        if all(value is not None for value in mp_coupons) and sum(map(money, mp_coupons), Decimal(0)) != coupon_total:
            raise EconomicEvidenceError("payment_coupon_mismatch", "contradictory")
        financing = money(order["financing_fee_amount"]) if "financing_fee_amount" in order else Decimal(0)
        if financing:
            raise EconomicEvidenceError("financing_allocation_unsupported", "unsupported")
        shipping_values = [p["shipping_amount"] for p in payments]
        shipping = (sum(map(money, shipping_values), Decimal(0))
                    if all(value is not None for value in shipping_values) else None)
        difference = buyer_paid + coupon_total - commercial
        included = False
        if difference:
            if shipping is None or difference != shipping:
                raise EconomicEvidenceError("buyer_payment_difference_unexplained")
            included = True
        result.update(status="validated", reason=None, items=items, benefits=benefits, payments=payments,
                      commercial_total=decimal_text(commercial), invoice_product_basis=decimal_text(commercial),
                      buyer_paid=decimal_text(buyer_paid), shipping_amount=None if shipping is None else decimal_text(shipping),
                      shipping_in_buyer_paid=included if shipping is not None else None,
                      seller_funded=decimal_text(sum((money(b["seller"]) for b in benefits), Decimal(0))),
                      marketplace_funded=decimal_text(sum((money(b["marketplace"]) for b in benefits), Decimal(0))))
        # Reject unrepresentable identifier metadata rather than persisting SDK
        # objects or surprising non-JSON state. No API payload is mutated.
        json.dumps(result, allow_nan=False)
    except EconomicEvidenceError as error:
        result.update(status=error.status, reason=error.reason)
    except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation):
        result.update(status="pending", reason="economic_observation_incomplete")
    return result
