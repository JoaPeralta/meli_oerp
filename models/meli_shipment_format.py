# -*- coding: utf-8 -*-
"""Shape adapter for the MercadoLibre Shipments API.

This module is intentionally free of any Odoo import so the mapping logic can
be unit-tested in isolation (plain Python), without a running Odoo environment.

Background
----------
On 12 October 2025 MercadoLibre changed the Shipments API
(``developers.mercadolibre.com.ar/es_ar/envios``):

* the header ``x-format-new: true`` became **mandatory** on ``/shipments/*``;
* the fields ``order_id`` and ``external_reference`` were **removed** from the
  shipment response;
* what used to be a flat body became a nested one -- the transport mode moved
  into ``logistic{}``, the buyer side into ``destination{}``, the seller side
  into ``origin{}`` and the marketplace into ``source{}``.

This connector was written against the flat, pre-October body and reads those
top level keys directly. Sending the new header without adapting the reader
would break more than it fixes, so the two halves belong to the same change:
:func:`normalize_shipment` takes whichever body MercadoLibre answers with and
returns the flat shape the rest of the connector already consumes.

What this module does NOT do
----------------------------
It never invents a value. The keys MercadoLibre removed (``order_id``,
``shipping_option``, ``order_cost``, ``base_cost``, ``comments``,
``date_first_printed``, ``sender_id``, ``status_history``) are simply **absent**
from the normalized result, and the callers guard for them. A plausible-looking
substitute would be worse than a missing key: it would be indistinguishable
from real data downstream.
"""

# Documented as mandatory on every /shipments/* request since 2025-10-12. What
# the API actually answers when it is absent -- an error, or the legacy body on
# borrowed time -- is not documented and was not measured against a live
# account, so this is sent rather than relied upon in either direction:
# normalize_shipment() accepts whichever body comes back.
SHIPMENTS_NEW_FORMAT_HEADERS = {"x-format-new": "true"}

# GET /shipments/{id}/orders is the documented replacement for the order_id that
# was dropped from the shipment body. It asks for a header of its own.
SHIPMENTS_ORDERS_HEADERS = {"X-New-Domain": "true"}

# Keys that the new format does not carry. They are listed here for
# documentation and for the tests -- the normalizer never writes them, so a
# caller that needs one must use .get() and cope with its absence. Inventing a
# stand-in would silently corrupt records that the connector treats as coming
# from MercadoLibre.
REMOVED_IN_NEW_FORMAT = (
    "order_id",            # removed 2025-10-12; use /shipments/{id}/orders
    "external_reference",  # removed 2025-10-12
    "shipping_option",
    "order_cost",
    "base_cost",
    "comments",
    "date_first_printed",
    "sender_id",
    "status_history",
)


def is_new_format_shipment(ship_json):
    """Return ``True`` when ``ship_json`` is a post-2025-10 shipment body.

    The discriminator is structural, not a version flag: MercadoLibre does not
    echo ``x-format-new`` back in the body, so the shape itself has to say
    which one it is.

    A **dict-valued** ``logistic`` key is the discriminator. In the new format
    the transport mode lives in ``logistic{mode, type, direction}``; in the
    legacy format the very same information is flat, as top level ``mode`` and
    ``logistic_type``, and there is no ``logistic`` object at all.

    A body that is neither shape (an error payload, a partial answer, anything
    unexpected) is reported as *not* new, so it is passed through untouched
    rather than reshaped on a guess.
    """
    if not isinstance(ship_json, dict):
        return False
    logistic = ship_json.get("logistic")
    if not isinstance(logistic, dict):
        return False
    return "mode" in logistic or "type" in logistic


def normalize_shipment(ship_json):
    """Return ``ship_json`` in the flat shape the connector consumes.

    Accepts either the legacy body or the post-2025-10 body:

    * a legacy body is returned **unchanged** (as a new dict) -- this is the
      regression guarantee: nothing about the old path may move;
    * a new-format body is returned with the nested values also published under
      their canonical flat names, keeping the nested originals in place so a
      caller that already understands them is not deprived of them;
    * anything that is not a dict is returned as received, so this function can
      never be the thing that raises on a malformed answer.

    The input is never mutated.
    """
    if not isinstance(ship_json, dict):
        # Not a body we can reason about (``None``, an error string, a list).
        # Hand it back untouched: the callers already decide what to do with a
        # non-dict answer, and reshaping it here would only hide the problem.
        return ship_json

    normalized = dict(ship_json)

    if not is_new_format_shipment(ship_json):
        return normalized

    logistic = ship_json.get("logistic") or {}
    source = ship_json.get("source") or {}
    origin = ship_json.get("origin") or {}
    destination = ship_json.get("destination") or {}

    # --- documented mappings, new format -> canonical flat shape ------------
    # Each of these is a value MercadoLibre still sends, only from a different
    # place in the body. Only written when actually present, so a partial
    # answer produces a missing key rather than an empty one that reads like a
    # real value.
    if "type" in logistic:
        normalized["logistic_type"] = logistic.get("type")
    if "mode" in logistic:
        normalized["mode"] = logistic.get("mode")
    if "site_id" in source:
        normalized["site_id"] = source.get("site_id")
    if "receiver_id" in destination:
        normalized["receiver_id"] = destination.get("receiver_id")

    receiver_address = destination.get("shipping_address")
    if isinstance(receiver_address, dict):
        normalized["receiver_address"] = _receiver_address(destination, receiver_address)

    sender_address = origin.get("shipping_address")
    if isinstance(sender_address, dict):
        # No lift needed here: the legacy sender_address consumers read only
        # address fields, all of which stayed inside origin.shipping_address.
        normalized["sender_address"] = dict(sender_address)

    # --- deliberately NOT mapped --------------------------------------------
    # sender_id: the new format has origin.node, which identifies a logistic
    #   node, not the seller user id the legacy sender_id carried. Different
    #   thing, so no mapping. INFERRED that they differ; left absent on purpose.
    # order_cost / base_cost: the new format has a top level declared_value.
    #   Whether it equals either of the two legacy costs is NOT documented, so
    #   guessing would put a wrong number on a money field. Left absent.
    # order_id: removed outright. The authoritative link is the order the
    #   connector already holds, and /shipments/{id}/orders for the pack case.
    # shipping_option / order_cost / comments / date_first_printed /
    #   status_history: no documented equivalent in the new body. Left absent.

    return normalized


def _receiver_address(destination, shipping_address):
    """Return the legacy ``receiver_address`` dict for a new-format body.

    ``destination.shipping_address`` holds the postal address, but the two
    fields the legacy ``receiver_address`` also carried -- ``receiver_name`` and
    ``receiver_phone`` -- are documented one level up, as siblings of
    ``shipping_address`` inside ``destination``. Both are still sent; only their
    nesting changed, so they are lifted back in.

    A value already present inside ``shipping_address`` wins, since that is the
    more specific place, and an absent value stays absent rather than becoming
    an empty string.
    """
    result = dict(shipping_address)
    for key in ("receiver_name", "receiver_phone"):
        if key not in result and key in destination:
            result[key] = destination.get(key)
    return result
