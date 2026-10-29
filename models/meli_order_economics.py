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


def _metadata(value, *, string_only=False, limit=128):
    """Retain bounded scalar metadata, never nested provider/user payloads.

    Optional null is unknown. Allowed mode strings are not an invented enum;
    identifiers may be provider strings or integers, normalized to text.
    """
    if value is None:
        return None
    if type(value) not in ((str,) if string_only else (int, str)):
        raise EconomicEvidenceError("economic_metadata_invalid")
    if type(value) is int and (value < 0 or value.bit_length() > 425):
        raise EconomicEvidenceError("economic_metadata_invalid")
    text = str(value)
    if (not text or len(text) > limit or text != text.strip()
            or not text.isprintable()):
        raise EconomicEvidenceError("economic_metadata_invalid")
    return text


def _id(value):
    if value is None:
        raise EconomicEvidenceError("economic_identity_missing")
    return _metadata(value)


def _payment_id(value):
    try:
        text = _id(value)
        if not text.isascii() or not text.isdecimal() or int(text) <= 0:
            raise EconomicEvidenceError("payment_identity_invalid")
        return str(int(text))
    except EconomicEvidenceError:
        raise EconomicEvidenceError("payment_identity_invalid") from None


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
        campaign = _metadata(supplier.get("meli_campaign"))
        campaign_id = _metadata(supplier.get("campaign_id"))
        offer = _metadata(supplier.get("offer_id"))
        funding_mode = _metadata(supplier.get("funding_mode"), string_only=True)
        if campaign is not None and campaign_id is not None and campaign != campaign_id:
            raise EconomicEvidenceError("benefit_campaign_identity_mismatch", "contradictory")
        origin = (offer if kind == "discount"
                  else (detail.get(kind) or {}).get("id"))
        origin = _id(origin)
        allocations = detail.get("items")
        if not isinstance(allocations, list) or not allocations:
            raise EconomicEvidenceError("benefit_allocation_missing")
        for allocation in allocations:
            iid = _id(allocation.get("id"))
            element_id = _metadata(allocation.get("element_id"))
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
                      "element_id": element_id,
                      "total": decimal_text(total), "seller": decimal_text(seller),
                      "marketplace": decimal_text(total - seller),
                      "campaign_id": campaign if campaign is not None else campaign_id,
                      "offer_id": offer, "funding_mode": funding_mode,
                      "incorporated_in_unit_price": kind == "discount",
                      "effect": {"discount": "effective_product_price", "coupon": "buyer_payment_benefit",
                                 "cashback": "cashback"}[kind], "source": "orders.discounts"}
            # The same identified benefit is corroboration, not another discount.
            # Conflicting duplicates never silently elect one source.
            identity = (kind, origin, iid, element_id)
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
    ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise EconomicEvidenceError("payment_population_invalid")
        pid = _payment_id(row.get("id"))
        ids.append(pid)
    if len(set(ids)) != len(ids):
        raise EconomicEvidenceError("payment_identity_ambiguous", "contradictory")
    # Identity/population errors outrank missing observations for an earlier
    # member: never consume a valid-looking prefix of an invalid population.
    result, seen = [], set(ids)
    for row, pid in zip(rows, ids):
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
            if (_payment_id(mp.get("id")) != pid or mp.get("currency_id") != currency
                    or mp.get("status") != "approved"):
                raise EconomicEvidenceError("payment_identity_mismatch", "contradictory")
            # The provider order's payment IDs already bind these observations
            # to the admitted seller/order. When MP also reports its collector,
            # it must corroborate that seller, never silently contradict it.
            if ("collector_id" in mp
                    and _id(mp["collector_id"]) != _id((order.get("seller") or {}).get("id"))):
                raise EconomicEvidenceError("payment_collector_mismatch", "contradictory")
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
    result = {"schema": "meli.order.economics/v1", "evidence_version": 2,
              "status": "pending", "reason": None,
              "commercial_status": "pending", "promotional_status": "unknown",
              "discounts_http_status": None, "discounts_error": None,
              "order_id": None, "currency_id": None, "items": [], "benefits": [], "payments": [],
              "commercial_total": None, "invoice_product_basis": None, "buyer_paid": None,
              "order_coupon_id": None, "order_coupon_id_state": "absent",
              "shipping_amount": None, "shipping_in_buyer_paid": None,
              "seller_funded": None, "marketplace_funded": None}
    try:
        observation = discounts_observation if isinstance(discounts_observation, dict) else {}
        error_body = observation.get("body")
        conditioned = (type(observation.get("status")) is int and observation["status"] == 404
                       and not observation.get("missing") and isinstance(error_body, dict)
                       and error_body.get("error") == "discount_not_found")
        result["discounts_http_status"] = observation.get("status")
        if conditioned:
            result["discounts_error"] = "discount_not_found"
            result["benefits"] = None
        body = None if conditioned else _body(discounts_observation, "discounts")
        if not isinstance(order, dict):
            raise EconomicEvidenceError("order_observation_missing")
        result["order_id"] = _id(order.get("id"))
        currency = _metadata(order.get("currency_id"), string_only=True, limit=16)
        if currency is None:
            raise EconomicEvidenceError("order_currency_missing")
        result["currency_id"] = currency
        items = _items(order, currency)
        if conditioned:
            shipping = order.get("shipping")
            unassigned = shipping is None or (type(shipping) is dict and set(shipping) == {"id"}
                                               and shipping["id"] is None)
            if (order.get("status") != "paid" or order.get("status_detail")
                    or order.get("pack_id") is not None or "pack_order" in (order.get("tags") or [])
                    or not unassigned or len(items) != 1):
                raise EconomicEvidenceError("conditioned_modality_unsupported", "unsupported")
            # A 404 is no evidence of a complete promotional population.
            # Independent MP observations are mandatory for this exception.
            if payment_observations is None:
                raise EconomicEvidenceError("payment_observation_missing")
        benefits = None if conditioned else _benefits(body, items)
        if not conditioned:
            result["promotional_status"] = "complete"
        commercial = sum((money(item["effective_total"]) for item in items), Decimal(0))
        if commercial != money(order.get("total_amount")):
            raise EconomicEvidenceError("commercial_total_mismatch", "contradictory")
        payments = _payments(order, currency, payment_observations)
        if sum((money(p["transaction_amount"]) for p in payments), Decimal(0)) != commercial:
            raise EconomicEvidenceError("payment_transaction_total_mismatch", "contradictory")
        buyer_paid = money(order.get("paid_amount"))
        if sum((money(p["buyer_paid"]) for p in payments), Decimal(0)) != buyer_paid:
            raise EconomicEvidenceError("payment_paid_total_mismatch", "contradictory")
        mp_coupons = [p["coupon_amount"] for p in payments]
        shipping_values = [p["shipping_amount"] for p in payments]
        if conditioned and (any(value is None for value in mp_coupons)
                            or any(value is None for value in shipping_values)):
            raise EconomicEvidenceError("conditioned_payment_fields_missing")
        coupon_total = (sum(map(money, mp_coupons), Decimal(0)) if conditioned else
                        sum((money(b["total"]) for b in benefits if b["type"] == "coupon"), Decimal(0)))
        coupon = order.get("coupon")
        if coupon is not None:
            if not isinstance(coupon, dict) or "amount" not in coupon:
                raise EconomicEvidenceError("order_coupon_incomplete")
            if "id" in coupon:
                coupon_id = _metadata(coupon["id"])
                result["order_coupon_id_state"] = "unknown" if coupon_id is None else "confirmed"
                result["order_coupon_id"] = coupon_id
                if coupon_id is not None and not conditioned:
                    identities = {benefit["id"] for benefit in benefits if benefit["type"] == "coupon"}
                    if coupon_id not in identities:
                        raise EconomicEvidenceError("coupon_identity_mismatch", "contradictory")
                    if len(identities) != 1:
                        raise EconomicEvidenceError("coupon_identity_ambiguous")
            if money(coupon["amount"]) != coupon_total:
                raise EconomicEvidenceError("payment_coupon_mismatch" if conditioned else
                                            "coupon_amount_mismatch", "contradictory")
        if all(value is not None for value in mp_coupons) and sum(map(money, mp_coupons), Decimal(0)) != coupon_total:
            raise EconomicEvidenceError("payment_coupon_mismatch", "contradictory")
        financing = money(order["financing_fee_amount"]) if "financing_fee_amount" in order else Decimal(0)
        if financing:
            raise EconomicEvidenceError("financing_allocation_unsupported", "unsupported")
        shipping = (sum(map(money, shipping_values), Decimal(0))
                    if all(value is not None for value in shipping_values) else None)
        difference = buyer_paid + coupon_total - commercial
        included = False
        if difference:
            if shipping is None or difference != shipping:
                raise EconomicEvidenceError("buyer_payment_difference_unexplained")
            included = True
        result.update(status="conditioned_draft" if conditioned else "validated", reason=None,
                      commercial_status="verified", items=items, benefits=benefits, payments=payments,
                      commercial_total=decimal_text(commercial), invoice_product_basis=decimal_text(commercial),
                      buyer_paid=decimal_text(buyer_paid), shipping_amount=None if shipping is None else decimal_text(shipping),
                      shipping_in_buyer_paid=included if shipping is not None else None,
                      seller_funded=None if conditioned else decimal_text(sum((money(b["seller"]) for b in benefits), Decimal(0))),
                      marketplace_funded=None if conditioned else decimal_text(sum((money(b["marketplace"]) for b in benefits), Decimal(0))))
        # Reject unrepresentable identifier metadata rather than persisting SDK
        # objects or surprising non-JSON state. No API payload is mutated.
        json.dumps(result, allow_nan=False)
    except EconomicEvidenceError as error:
        result.update(status=error.status, reason=error.reason)
        if error.status == "contradictory":
            if error.reason.startswith(("benefit_", "reference_discount_", "coupon_identity_")):
                result["promotional_status"] = "contradictory"
            else:
                result["commercial_status"] = "contradictory"
    except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation):
        result.update(status="pending", reason="economic_observation_incomplete")
    return result
