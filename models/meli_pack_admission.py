"""Order-led admission for the documented flat shipment membership subset.

No ORM, SDK construction or commercial projection belongs in this module.
Logistic requested quantities never replace an order's commercial quantities.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation


class AdmissionError(ValueError):
    """Essential ownership/identity is missing or the shape is unsupported."""


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
