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
# Matches the explicit character set in the recovery SQL (not locale-dependent).
WHITESPACE = "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f\x20\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def is_missing(value):
    return value is None or value is False or isinstance(value, str) and not value.strip(WHITESPACE)


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
    if is_missing(value):
        return None, "empty"
    normalized = validate(value)
    return (normalized, "valid") if normalized is not None else (None, "invalid")


def _same_item_seller(item, item_id, seller_id):
    seller = numeric_id(seller_id)
    return (isinstance(item_id, str) and bool(ITEM_ID.fullmatch(item_id)) and bool(seller)
            and isinstance(item, dict) and "error" not in item and item.get("id") == item_id
            and numeric_id(item.get("seller_id")) == seller)


def user_product_id(value, item_id):
    if isinstance(value, str) and USER_PRODUCT_ID.fullmatch(value) and value[:3] == item_id[:3]:
        return value
    return None


def catalogue_variations(item):
    """None means omitted; reject malformed or duplicate variation keys."""
    if "variations" not in item:
        return None
    variations = item["variations"]
    if not isinstance(variations, list):
        raise ValueError("Invalid variation list")
    keys = [numeric_id(row.get("id")) if isinstance(row, dict) else None for row in variations]
    if None in keys or len(set(keys)) != len(keys):
        raise ValueError("Invalid or repeated variation key")
    return variations


def sku_identity(source):
    """Check all same-namespace SKU forms, not just the preferred value."""
    values = _sku_values(source)
    explicit = [_field(value, _sku) for group in values[:2] for value in group]
    known = sorted({value for value, state in explicit if state == "valid"})
    invalid = len(known) > 1 or any(state == "invalid" for _, state in explicit)
    return known, invalid


def admitted_catalogue_item(item, response_status, item_id, seller_id):
    if (type(response_status) is not int or response_status != 200
            or not _same_item_seller(item, item_id, seller_id)):
        return False
    try:
        catalogue_variations(item)
    except ValueError:
        return False
    return True


def metadata_evidence(item, item_id, variation_id, seller_id):
    """Return validated changes, per-field availability, and identity or reason."""
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id) or not numeric_id(seller_id):
        return {}, {}, None, "invalid_selection"
    if not _same_item_seller(item, item_id, seller_id):
        return {}, {}, None, "unproven_item_or_seller"
    try:
        variations = catalogue_variations(item)
    except ValueError:
        return {}, {}, None, "unproven_variation"
    variant = None
    if not is_missing(variation_id):
        key = numeric_id(variation_id)
        variant = next((row for row in variations or () if numeric_id(row["id"]) == key), None)
        if variant is None:
            return {}, {}, None, "unproven_variation"
    source = variant if variant is not None else item
    up = user_product_id(source.get("user_product_id"), item_id)
    listing_with_variants = variant is None and bool(variations)
    scope_unknown = variant is None and variations is None and not up
    normalized = {
        "meli_status": _field(item.get("status", ABSENT), _status),
        "meli_permalink": _field(item.get("permalink", ABSENT),
                                 lambda value: _permalink(value, item_id)),
        "meli_price": _field(source.get("price", ABSENT), _price),
    }
    sku, sku_kind = _effective_sku(source)
    sku_keys, sku_conflict = sku_identity(source)
    legacy_reference = _sku(source.get("seller_custom_field"))
    up_invalid = not is_missing(source.get("user_product_id")) and not up
    if listing_with_variants or scope_unknown:
        # Listing status/link are item-level; root SKU and price need exact offer scope.
        state = "not_applicable" if listing_with_variants else "unproven"
        normalized["meli_price"] = None, state
        sku, sku_kind = (None, state), None
        sku_keys, sku_conflict = [], False
        legacy_reference = None
    normalized["meli_seller_custom_field"] = sku
    identity = {
        "sku": sku[0],
        "sku_state": sku[1],
        "sku_kind": sku_kind,
        "sku_keys": sku_keys,
        "sku_conflict": sku_conflict,
        "legacy_reference": legacy_reference,
        "user_product_id": up,
        "user_product_invalid": up_invalid,
        "listing_with_variants": listing_with_variants,
    }
    return ({field: value for field, (value, state) in normalized.items() if state == "valid"},
            {field: state for field, (_, state) in normalized.items()}, identity, None)


def imported_metadata(item, response_status, item_id, variation_id, seller_id):
    """Do not admit statusless or incomplete transport evidence into postings."""
    if not admitted_catalogue_item(item, response_status, item_id, seller_id):
        return {}
    values, _, _, _ = metadata_evidence(item, item_id, variation_id, seller_id)
    return values


def import_response_status(response):
    headers = getattr(response, "headers", {}) or {}
    if headers.get("x-content-missing") or getattr(response, "last_response_content_missing", False) is True:
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
    from urllib3.exceptions import HTTPError

    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    try:
        request = requests.Request(
            "GET", "https://api.mercadolibre.com/items/" + item_id,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                     "Accept-Encoding": "identity"},
            params={"include_attributes": "all"},
        ).prepare()
        # Session.send() may consume the body to prepare a redirect, even
        # when allow_redirects=False. Send through the adapter directly.
        with adapter.send(request, timeout=(5, 10), stream=True, verify=True, proxies={}) as response:
            if response.status_code in (401, 403):
                return None, "authentication_rejected"
            if response.status_code != 200 or response.headers.get("x-content-missing"):
                return None, "http_unavailable"
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                return None, "invalid_response"
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isascii() or not length.isdecimal()
                                       or len(length) > 12 or int(length) > MAX_BODY):
                return None, "invalid_response"
            body = response.raw.read(MAX_BODY + 1, decode_content=False)
            if len(body) > MAX_BODY or (length is not None and len(body) != int(length)):
                return None, "invalid_response"
            data = json.loads(body)
            return (data, None) if isinstance(data, dict) else (None, "invalid_response")
    except (requests.RequestException, HTTPError, OSError):
        return None, "transport_unavailable"
    except (ValueError, RecursionError):
        return None, "invalid_response"
    finally:
        adapter.close()
