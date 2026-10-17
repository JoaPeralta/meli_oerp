"""Order-led admission for the documented flat shipment membership subset.

No ORM, SDK construction or commercial projection belongs in this module.
Logistic requested quantities never replace an order's commercial quantities.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from math import isfinite


class AdmissionError(ValueError):
    """Essential ownership/identity is missing or the shape is unsupported."""


def fetch_pack_snapshot(meli, pack_id):
    """Shared read boundary; callers retain their existing snapshot/drift policy."""
    body, metadata = snapshot_response(meli.get("/packs/" + remote_id(pack_id),
                                                {"access_token": meli.access_token}))
    if metadata["status"] != 200 or metadata["missing"]:
        raise AdmissionError("A complete fresh pack response is unavailable")
    return body


def remote_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AdmissionError("A positive remote identity is required")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise AdmissionError("A positive remote identity is required")
    return str(int(text))


def item_key(item_id, variation_id):
    if not isinstance(item_id, str) or not item_id.strip():
        raise AdmissionError("An explicit publication identity is required")
    return item_id, None if variation_id is None else remote_id(variation_id)


def verify_order(payload, seller_id, expected_id=None, expected_pack=None):
    """Copy and verify one own order; never infer essential fields from siblings."""
    if not isinstance(payload, dict):
        raise AdmissionError("An individual order object is required")
    result = deepcopy(payload)
    for name in ("shipping", "cancel_detail"):
        if result.get(name) is not None and not isinstance(result[name], dict):
            raise AdmissionError("Optional order objects must retain their documented shape")
    if result.get("tags") is not None and not isinstance(result["tags"], list):
        raise AdmissionError("Order tags must retain their documented array shape")
    oid = remote_id(result.get("id"))
    seller = result.get("seller")
    if not isinstance(seller, dict) or remote_id(seller.get("id")) != remote_id(seller_id):
        raise AdmissionError("Individual order seller does not match the authorized seller")
    if expected_id is not None and oid != remote_id(expected_id):
        raise AdmissionError("Individual order identity changed")
    pack = result.get("pack_id")
    if pack is not None:
        pack = remote_id(pack)
    if expected_pack is not None and pack != remote_id(expected_pack):
        raise AdmissionError("Individual order pack identity changed")
    status = result.get("status")
    if not isinstance(status, str) or not status.strip():
        raise AdmissionError("An explicit nonempty individual order status is required")
    if result.get("status") in ("cancelled", "invalid"):
        raise AdmissionError("Cancellation/invalid order projection is pending T4-C")
    if "delivered" in (result.get("tags") or []):
        raise AdmissionError("Delivered order history is outside T4-A admission")
    if (result.get("cancel_detail") or {}).get("code") in ("pack_splitted", "bundle_component"):
        raise AdmissionError("Split/component order projection is unsupported")
    if result.get("status_detail") in ("splitted", "pack_splitted", "bundle_component"):
        raise AdmissionError("Split/component order projection is unsupported")
    items = result.get("order_items")
    if not isinstance(items, list) or not items:
        raise AdmissionError("Individual commercial order items are required")
    seen = set()
    for line in items:
        if not isinstance(line, dict) or not isinstance(line.get("item"), dict):
            raise AdmissionError("Individual commercial item identity is required")
        if line.get("bundle") or line["item"].get("bundle"):
            raise AdmissionError("Bundle/kit projection is unsupported")
        key = item_key(line["item"].get("id"), line["item"].get("variation_id"))
        if key in seen:
            raise AdmissionError("Duplicate source item identity requires an explicit policy")
        seen.add(key)
        quantity = line.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise AdmissionError("A positive integral commercial quantity is required")
        try:
            price = Decimal(str(line["unit_price"]))
        except (KeyError, ValueError, InvalidOperation):
            raise AdmissionError("An explicit finite commercial unit price is required") from None
        if not price.is_finite() or price < 0:
            raise AdmissionError("An explicit finite commercial unit price is required")
        if not result.get("currency_id") or line.get("currency_id") != result["currency_id"]:
            raise AdmissionError("Commercial currency identity is missing or inconsistent")
    return result


def enrich_buyer(current, refreshed, seller_id):
    """Enrich a buyer without replacing the captured commercial snapshot."""
    current = verify_order(current, seller_id)
    refreshed = verify_order(refreshed, seller_id, expected_id=current["id"])
    keys = lambda order: {item_key(line["item"]["id"], line["item"].get("variation_id"))
                          for line in order["order_items"]}
    if current.get("pack_id") != refreshed.get("pack_id") or keys(current) != keys(refreshed):
        raise AdmissionError("Source membership changed during buyer enrichment")
    if not isinstance(refreshed.get("buyer"), dict):
        raise AdmissionError("Verified individual buyer enrichment is unavailable")
    current["buyer"] = refreshed["buyer"]
    return current


def pack_order_ids(pack, expected_pack):
    """IDs describe only this integration-visible pack, never seller authority."""
    if (not isinstance(pack, dict) or remote_id(pack.get("id")) != remote_id(expected_pack)
            or pack.get("status") != "released" or pack.get("status_detail")):
        raise AdmissionError("A released, unchanged visible pack is required")
    rows = pack.get("orders")
    if not isinstance(rows, list) or not rows:
        raise AdmissionError("Explicit visible pack membership is required")
    ids = [remote_id(row.get("id")) for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or len(set(ids)) != len(ids):
        raise AdmissionError("Visible pack membership is invalid or duplicated")
    return ids


def verify_pack_commerce(pack, snapshots, seller_id):
    """Validate the complete returned OWN population, not an invisible world.

    This function confers no provenance: its caller must collect responses and
    authorize every fetch independently. Unsupported allocation stays refused.
    """
    if not isinstance(pack, dict) or not isinstance(snapshots, list) or not snapshots:
        raise AdmissionError("An explicit pack and captured own order population is required")
    ids = pack_order_ids(pack, pack.get("id"))
    if not isinstance(pack.get("buyer"), dict) or "shipment" not in pack:
        raise AdmissionError("Explicit pack buyer and optional-shipment evidence is required")
    buyer = remote_id(pack["buyer"].get("id"))
    shipment = pack.get("shipment")
    if shipment is not None and not isinstance(shipment, dict):
        raise AdmissionError("Pack shipment must retain its documented object shape")
    shipping_id = remote_id(shipment["id"]) if shipment and shipment.get("id") else None
    verified = [verify_order(row, seller_id, expected_pack=pack["id"]) for row in snapshots]
    seen = [remote_id(row["id"]) for row in verified]
    if len(seen) != len(set(seen)) or set(seen) != set(ids):
        raise AdmissionError("Individual snapshots do not exactly cover the visible pack")

    def money(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise AdmissionError("An explicit finite nonnegative amount is required")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise AdmissionError("An explicit finite nonnegative amount is required") from None
        if not number.is_finite() or number < 0 or not isfinite(float(number)):
            raise AdmissionError("An explicit finite nonnegative amount is required")
        return number

    identity = None
    total, paid, fees = Decimal(0), Decimal(0), Decimal(0)
    for row in verified:
        customer = row.get("buyer") or {}
        context = row.get("context") or {}
        if (not isinstance(customer, dict) or not isinstance(context, dict)
                or not isinstance(row.get("coupon") or {}, dict)
                or not isinstance(row.get("payments", []), list)):
            raise AdmissionError("Explicit compatible buyer/channel objects are required")
        if remote_id(customer.get("id")) != buyer or not context.get("channel"):
            raise AdmissionError("Pack buyer/channel identity is incompatible")
        common = (row.get("site_id"), row["currency_id"], context["channel"], customer.get("billing_info"))
        if not common[0] or (identity is not None and identity != common):
            raise AdmissionError("Pack company pricing/billing context is incompatible")
        identity = common
        if row["status"] != "paid" or row.get("status_detail") or len(row["order_items"]) != 1:
            raise AdmissionError("Only paid single-source child orders are supported")
        own_shipping = (row.get("shipping") or {}).get("id")
        if own_shipping is not None and remote_id(own_shipping) != shipping_id:
            raise AdmissionError("Child shipment contradicts the visible pack")
        if shipping_id and own_shipping is None:
            raise AdmissionError("The child shipment relation is not proven")
        item = row["order_items"][0]
        price = money(item["unit_price"])
        subtotal = item["quantity"] * price
        if (money(row.get("total_amount")) != subtotal
                or money(row.get("paid_amount")) != subtotal
                or money((row.get("coupon") or {}).get("amount", 0))
                or money(row.get("financing_fee_amount", 0))
                or money(item.get("full_unit_price", item["unit_price"])) != price
                or money((row.get("shipping") or {}).get("cost", 0))):
            raise AdmissionError("Discount, shipping or financing allocation is not supported")
        total += subtotal
        paid += money(row["paid_amount"])
        fees += money(item.get("sale_fee", 0))
    ordered = {remote_id(row["id"]): row for row in verified}
    return {"orders": [ordered[oid] for oid in sorted(ids, key=int)], "pack_id": remote_id(pack["id"]),
            "buyer_id": buyer, "shipping_id": shipping_id, "currency": identity[1],
            "channel": identity[2], "total": total, "paid": paid, "fees": fees,
            "membership_scope": "configured-seller-visible-pack"}


def snapshot_response(response):
    """NoSDK may return itself: capture status/headers/body before another GET."""
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "last_status_code", None)
    headers = deepcopy(dict(getattr(response, "headers", {}) or {}))
    missing = next((value for key, value in headers.items()
                    if key.lower() == "x-content-missing"), None)
    body = None if status == 204 else deepcopy(response.json())
    return body, {"status": status, "missing": missing,
                  "headers_available": hasattr(response, "headers")}


def collect_membership(meli, shipping_id, current_order, seller_id, headers):
    """Admit only explicitly own flat associations and independently own orders.

    Unknown ownership is pending, not foreign and not permission to probe an ID.
    A forbidden/not-found sibling preserves the already verified own subset.
    Returned snapshots contain no foreign order bodies or credential headers.
    """
    current = verify_order(current_order, seller_id)
    current_id = remote_id(current["id"])
    shipping_id = remote_id(shipping_id)
    seller_id = remote_id(seller_id)
    response = meli.get("/shipments/" + shipping_id + "/orders",
                        {"access_token": meli.access_token}, extra_headers=headers)
    rows, metadata = snapshot_response(response)
    result = {"orders": [current], "associations": [], "complete": False,
              "responses": [metadata], "pending": "logistics_membership_pending"}
    if metadata["status"] == 204:
        return result
    if metadata["status"] != 200 or not isinstance(rows, list):
        raise AdmissionError("Flat shipment membership response is unavailable or invalid")
    own_rows = []
    unknown = False
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("seller_id") is None:
            unknown = True
            continue
        try:
            row_seller = remote_id(row["seller_id"])
        except AdmissionError:
            unknown = True
            continue
        if row_seller != seller_id:
            continue  # Never request a member already identified as foreign.
        oid = remote_id(row.get("order_id"))
        key = item_key(row.get("item_id"), row.get("variation_id"))
        identity = (oid, key)
        if identity in seen:
            raise AdmissionError("Duplicate logistic source identity is unsupported")
        seen.add(identity)
        requested = row.get("requested_quantity")
        if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
            raise AdmissionError("An explicit positive logistic quantity is required")
        pack = current.get("pack_id")
        if pack is not None and remote_id(row.get("pack_id")) != remote_id(pack):
            raise AdmissionError("Shipment association contradicts the own pack identity")
        own_rows.append(deepcopy(row))
    verified = {current_id: current}
    invisible = set()
    for row in own_rows:
        oid = remote_id(row["order_id"])
        if oid in verified or oid in invisible:
            continue
        response = meli.get("/orders/" + oid, {"access_token": meli.access_token})
        payload, metadata = snapshot_response(response)
        result["responses"].append(metadata)
        if metadata["status"] in (403, 404):
            invisible.add(oid)
            continue
        if metadata["status"] not in (200, 206):
            raise AdmissionError("Individual own order response is unavailable")
        verified[oid] = verify_order(payload, seller_id, expected_id=oid,
                                     expected_pack=current.get("pack_id"))
    for row in own_rows:
        oid = remote_id(row["order_id"])
        if oid not in verified:
            continue
        source_shipping = (verified[oid].get("shipping") or {}).get("id")
        if source_shipping is not None and remote_id(source_shipping) != shipping_id:
            raise AdmissionError("Individual order shipping identity contradicts the association")
        source_keys = {item_key(line["item"]["id"], line["item"].get("variation_id"))
                       for line in verified[oid]["order_items"]}
        if item_key(row["item_id"], row.get("variation_id")) not in source_keys:
            raise AdmissionError("Logistics item does not belong to its individual source order")
        result["associations"].append(row)
    result["orders"] = list(verified.values())
    result["complete"] = (not unknown and not invisible and
                          any(remote_id(row["order_id"]) == current_id
                              for row in result["associations"]))
    result["pending"] = None if result["complete"] else "logistics_membership_pending"
    return result
