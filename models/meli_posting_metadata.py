"""Validate item evidence and fetch metadata without an OAuth refresh or a write."""

from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qsl, urlsplit


ITEM_ID = re.compile(r"ML[A-Z][0-9]{1,32}\Z")
USER_PRODUCT_ID = re.compile(r"ML[A-Z]U[0-9]{1,32}\Z")
STOREFRONTS = (
    "mercadolibre.com.ar", "mercadolibre.com.bo", "mercadolibre.cl",
    "mercadolibre.com.co", "mercadolibre.co.cr", "mercadolibre.com.do",
    "mercadolibre.com.ec", "mercadolibre.com.gt", "mercadolibre.com.hn",
    "mercadolibre.com.mx", "mercadolibre.com.ni", "mercadolibre.com.pa",
    "mercadolibre.com.pe", "mercadolibre.com.py", "mercadolibre.com.sv",
    "mercadolibre.com.uy", "mercadolibre.com.ve", "mercadolivre.com.br",
)
STATUSES = frozenset({
    "active", "paused", "closed", "inactive", "under_review", "payment_required",
    "not_yet_active", "pending", "held", "deleted",
})
METADATA_FIELDS = (
    "meli_status", "meli_permalink", "meli_price", "meli_seller_custom_field",
)
CORE_FIELDS = METADATA_FIELDS[:3]
ABSENT = object()
MAX_BODY = 512 * 1024


def numeric_id(value):
    if type(value) in (str, int):
        value = str(value)
        if re.fullmatch(r"[1-9][0-9]{0,31}", value):
            return value
    return None


def _status(value):
    return value if isinstance(value, str) and value in STATUSES else None


def _price(value):
    if type(value) not in (int, float, str):
        return None
    # Bound both input and decimal scale before formatting it as a Char.
    try:
        raw = str(value)
        if not re.fullmatch(r"[0-9]{1,25}(?:\.[0-9]{1,12})?", raw):
            return None
        price = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return format(price, "f") if price > 0 else None


def _permalink(value, item_id):
    if (not isinstance(value, str) or len(value) > 512
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            or "\\" in value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        # The only query supported here is the public offer selector. Do not
        # save opaque query parameters, fragments or URL user information.
        if (parts.scheme != "https" or parts.netloc != host
                or not any(host in (domain, "www." + domain, "articulo." + domain,
                                    "produto." + domain) for domain in STOREFRONTS)
                or not parts.path.startswith("/") or parts.path.startswith("//")
                or parts.fragment):
            return None
        if parts.query and parse_qsl(parts.query, strict_parsing=True) != [
            ("pdp_filters", "item_id:" + item_id),
        ]:
            return None
        if not parts.query and not re.search(
            r"(?:^|/)" + re.escape(item_id[:3] + "-") + re.escape(item_id[3:]) + r"(?:$|[-/])",
            parts.path,
        ):
            return None
    except ValueError:
        return None
    return value


def _sku(value):
    if not isinstance(value, str) or len(value) > 256:
        return None
    if not value or not value.isprintable() or value != value.strip():
        return None
    return value


def _sku_values(source):
    values = []
    attributes = source.get("attributes")
    if isinstance(attributes, list):
        for attribute in attributes:
            if not isinstance(attribute, dict) or attribute.get("id") != "SELLER_SKU":
                continue
            if "value_name" in attribute:
                values.append(attribute["value_name"])
            if isinstance(attribute.get("values"), list):
                values.extend(entry["name"] for entry in attribute["values"]
                              if isinstance(entry, dict) and "name" in entry)
    return [
        values,
        [source["seller_sku"]] if "seller_sku" in source else [],
        [source["seller_custom_field"]] if "seller_custom_field" in source else [],
    ]


def _effective_sku(source):
    missing = "absent"
    for kind, values in zip(("attribute", "sku", "legacy_custom"), _sku_values(source)):
        parts = [_field(value, _sku) for value in values]
        known = {value for value, state in parts if state == "valid"}
        if len(known) > 1 or any(state == "invalid" for _, state in parts):
            return (None, "invalid"), kind
        if known:
            return (next(iter(known)), "valid"), kind
        if parts:
            missing = "empty"
    return (None, missing), None


def _field(value, validate):
    if value is ABSENT:
        return None, "absent"
    if value is None or value is False or isinstance(value, str) and not value.strip():
        return None, "empty"
    normalized = validate(value)
    return (normalized, "valid") if normalized is not None else (None, "invalid")


def metadata_evidence(item, item_id, variation_id, seller_id):
    """Return validated changes, per-field availability, and identity or reason."""
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id) or not numeric_id(seller_id):
        return {}, {}, None, "invalid_selection"
    if (not isinstance(item, dict) or "error" in item or item.get("id") != item_id
            or numeric_id(item.get("seller_id")) != numeric_id(seller_id)):
        return {}, {}, None, "unproven_item_or_seller"
    variations = item.get("variations", [])
    if not isinstance(variations, list):
        return {}, {}, None, "unproven_variation"
    variation_keys = [numeric_id(row.get("id")) if isinstance(row, dict) else None
                      for row in variations]
    if None in variation_keys or len(set(variation_keys)) != len(variation_keys):
        return {}, {}, None, "unproven_variation"
    variant = None
    if variation_id is not False and variation_id is not None and variation_id != "":
        key = numeric_id(variation_id)
        if key is None or key not in variation_keys:
            return {}, {}, None, "unproven_variation"
        variant = variations[variation_keys.index(key)]
    source = variant if variant is not None else item
    listing_with_variants = variant is None and bool(variations)
    normalized = {
        "meli_status": _field(item.get("status", ABSENT), _status),
        "meli_permalink": _field(item.get("permalink", ABSENT),
                                 lambda value: _permalink(value, item_id)),
        "meli_price": _field(source.get("price", ABSENT), _price),
    }
    sku, sku_kind = _effective_sku(source)
    if listing_with_variants:
        # A legacy listing with variants is not a variant SKU or price.
        normalized["meli_price"] = None, "not_applicable"
        sku, sku_kind = (None, "not_applicable"), None
    normalized["meli_seller_custom_field"] = sku
    up = source.get("user_product_id")
    identity = {
        "sku": sku[0],
        "sku_state": sku[1],
        "sku_kind": sku_kind,
        "user_product_id": up if isinstance(up, str) and USER_PRODUCT_ID.fullmatch(up) else None,
        "listing_with_variants": listing_with_variants,
    }
    return ({field: value for field, (value, state) in normalized.items() if state == "valid"},
            {field: state for field, (_, state) in normalized.items()}, identity, None)


def imported_metadata(item, response_status, item_id, variation_id, seller_id):
    """Do not admit statusless or incomplete transport evidence into postings."""
    if type(response_status) is not int or response_status != 200:
        return {}
    values, _, _, _ = metadata_evidence(item, item_id, variation_id, seller_id)
    return values


def import_response_status(response):
    if (getattr(response, "headers", {}) or {}).get("x-content-missing"):
        return None
    status = getattr(response, "status_code", None)
    return getattr(response, "last_status_code", None) if status is None else status


def read_item_metadata(item_id, token):
    """One bounded canonical-host GET; no redirects, ambient proxies or retries."""
    import requests

    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        return None, "invalid_selection"
    if not isinstance(token, str) or not token or token == "PASIVA":
        return None, "authentication_unavailable"
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                "https://api.mercadolibre.com/items/" + item_id,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                params={"include_attributes": "all"},
                timeout=(5, 10), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code in (401, 403):
                    return None, "authentication_rejected"
                if response.status_code != 200 or response.headers.get("x-content-missing"):
                    return None, "http_unavailable"
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        return None, "invalid_response"
                data = json.loads(body)
                return (data, None) if isinstance(data, dict) else (None, "invalid_response")
    except requests.RequestException:
        return None, "transport_unavailable"
    except (ValueError, RecursionError):
        return None, "invalid_response"
