# -*- coding: utf-8 -*-
"""Security helpers for MercadoLibre webhook notification handling.

This module is intentionally free of any Odoo import so the validation logic
can be unit-tested in isolation (plain Python), without a running Odoo
environment.

Background
----------
MercadoLibre webhook notifications carry a ``resource`` field (e.g.
``/orders/2000000000000000``). The notification processors fetch that resource
from the MercadoLibre API attaching the seller ``Authorization: Bearer`` token.

Because ``resource`` originates from an unauthenticated public webhook, it must
never be allowed to designate an arbitrary host: otherwise a forged
notification with ``resource = "https://attacker.example/steal"`` would cause
the authenticated HTTP client to send the access token to the attacker.

``safe_meli_resource_path`` enforces that a webhook-controlled ``resource`` is a
*relative* path belonging to the MercadoLibre API, rejecting anything that could
change the scheme, host, port or userinfo of the outgoing request.
"""

from urllib.parse import urlsplit

# Resource prefixes accepted from webhook notifications. Kept intentionally
# small: only the topics this connector actually fetches from the API.
DEFAULT_ALLOWED_PREFIXES = (
    "/orders/",
    "/questions/",
    "/items/",
)

# Characters that must never appear in a resource path: whitespace and control
# characters (header/URL smuggling) and backslashes (path confusion).
_FORBIDDEN_CHARS = ("\\", "\n", "\r", "\t", "\x00", " ")


def safe_meli_resource_path(resource, allowed_prefixes=None):
    """Return a safe relative MercadoLibre API path, or ``None`` if unsafe.

    A value is considered safe only when ALL of the following hold:

    * it is a non-empty string with no whitespace/control/backslash characters;
    * it is a path-absolute relative reference (starts with a single ``/``),
      so it cannot be scheme-relative (``//host``);
    * it has no URL scheme and no network location (host/userinfo/port), i.e.
      it cannot point at ``http://``/``https://`` or ``user@host``;
    * it contains no ``..`` path segment;
    * its path starts with one of ``allowed_prefixes``
      (defaults to :data:`DEFAULT_ALLOWED_PREFIXES`).

    The original value (path plus any query string) is returned unchanged when
    safe, so query parameters legitimately present in a resource are preserved.
    """
    if not resource or not isinstance(resource, str):
        return None

    value = resource.strip()
    if not value:
        return None

    if any(ch in value for ch in _FORBIDDEN_CHARS):
        return None

    # Must be a path-absolute relative reference. This rejects absolute URLs
    # ("http://...", "https://...") and scheme-relative URLs ("//host/...").
    if not value.startswith("/") or value.startswith("//"):
        return None

    parts = urlsplit(value)

    # No scheme and no network location (host, userinfo, port) allowed.
    if parts.scheme or parts.netloc:
        return None

    path = parts.path
    if not path.startswith("/") or path.startswith("//"):
        return None

    # Reject path traversal segments defensively.
    if ".." in path.split("/"):
        return None

    prefixes = allowed_prefixes if allowed_prefixes is not None else DEFAULT_ALLOWED_PREFIXES
    if not any(path.startswith(prefix) for prefix in prefixes):
        return None

    return value
