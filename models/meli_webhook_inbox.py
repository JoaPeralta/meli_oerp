"""Bounded, Odoo-free webhook envelope validation and receipt identities.

Payload identity agreement selects a route; it is not origin authentication.
Keep the legacy resource helper's query compatibility unchanged.
"""

import base64
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from .meli_webhook_security import safe_meli_resource_path


WEBHOOK_MAX_BYTES = 16384
_MAX_ID = 2**63 - 1
_DECIMAL_ID = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
_RESOURCE_PATTERNS = {
    "orders_v2": r"/orders/([1-9][0-9]{0,18})",
    "questions": r"/questions/([1-9][0-9]{0,18})",
    "shipments": r"/shipments/([1-9][0-9]{0,18})",
    "items": r"/items/MLA([1-9][0-9]{0,18})",
}


class WebhookIntakeError(ValueError):
    """Only fixed codes and status numbers may reach the public response."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def canonical_remote_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WebhookIntakeError(400, "invalid_identity")
    value = str(value)
    if not _DECIMAL_ID.fullmatch(value) or int(value) > _MAX_ID:
        raise WebhookIntakeError(400, "invalid_identity")
    return value


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WebhookIntakeError(400, "duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise WebhookIntakeError(400, "invalid_json")


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise WebhookIntakeError(400, "invalid_metadata")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise WebhookIntakeError(400, "invalid_metadata") from None


def parse_webhook_body(raw):
    """Drop unknown fields; require a small raw JSON object and exact grammar."""
    if not isinstance(raw, bytes):
        raise WebhookIntakeError(400, "invalid_json")
    if len(raw) > WEBHOOK_MAX_BYTES:
        raise WebhookIntakeError(413, "body_too_large")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                          parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise WebhookIntakeError(400, "invalid_json") from None
    if not isinstance(data, dict):
        raise WebhookIntakeError(400, "invalid_json")
    result = {key: canonical_remote_id(data.get(key)) for key in ("application_id", "user_id")}
    topic = data.get("topic")
    if not isinstance(topic, str) or topic not in _RESOURCE_PATTERNS:
        # Collections cannot be consumed safely without independently proven
        # ownership. Reject rather than expand subscriptions or queue commerce.
        raise WebhookIntakeError(422, "unsupported_topic")
    resource = data.get("resource")
    prefix = {"orders_v2": "/orders/", "items": "/items/", "questions": "/questions/",
              "shipments": "/shipments/"}[topic]
    safe = safe_meli_resource_path(resource, allowed_prefixes=(prefix,))
    match = re.fullmatch(_RESOURCE_PATTERNS[topic], resource, flags=re.ASCII) if isinstance(resource, str) else None
    if safe != resource or not match or int(match.group(1)) > _MAX_ID:
        raise WebhookIntakeError(400, "invalid_resource")
    result.update(topic=topic, resource=resource)
    if "_id" in data:
        provider_id = data["_id"]
        if (not isinstance(provider_id, str) or not provider_id or not provider_id.isprintable()
                or len(provider_id.encode("utf-8")) > 256):
            raise WebhookIntakeError(400, "invalid_provider_id")
        result["_id"] = provider_id
    if "attempts" in data:
        attempts = data["attempts"]
        if type(attempts) is not int or not 0 <= attempts <= 1000000:
            raise WebhookIntakeError(400, "invalid_metadata")
        result["attempts"] = attempts
    for key in ("sent", "received"):
        if key in data:
            result[key] = _timestamp(data[key])
    return result


def webhook_receipt_id(data):
    if "_id" not in data:
        return "receipt:" + str(uuid4())
    opaque = base64.urlsafe_b64encode(data["_id"].encode("utf-8")).decode("ascii").rstrip("=")
    return "ml:%s:%s:%s" % (data["application_id"], data["user_id"], opaque)


def is_inbox_receipt(identity):
    return isinstance(identity, str) and identity.startswith(("ml:", "receipt:"))


def same_webhook_binding(row, data, binding):
    """Legacy numeric fields may be integer/string, never ambiguous coercions."""
    try:
        return (
            canonical_remote_id(row.application_id) == data["application_id"]
            and canonical_remote_id(row.user_id) == data["user_id"]
            and row.topic == data["topic"] and row.resource == data["resource"]
            and row.company_id.id == binding["company_id"]
            and row.seller_id.id == binding["actor_id"]
        )
    except (WebhookIntakeError, AttributeError):
        return False


def webhook_timestamp_value(value):
    """Odoo stores UTC datetimes without timezone; remote time is advisory."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) if value else False
