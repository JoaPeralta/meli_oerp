"""Strict, seller-bound Mercado Libre fiscal-document wire contract."""
import json
import re

from .meli_pack_admission import AdmissionError, pack_order_ids, remote_id


PDF_LIMIT = 1_000_000
JSON_LIMIT = 2_000_000
IDENTIFIER = re.compile(r"[1-9][0-9]{0,39}")
DOCUMENT_ID = re.compile(
    r"(?:[1-9][0-9]{0,39}_)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PDF_FILENAME = re.compile(r"invoice-[0-9a-f]{32}\.pdf")
ORDER_PATH = re.compile(r"/orders/[1-9][0-9]{0,39}")
PACK_PATH = re.compile(r"/packs/[1-9][0-9]{0,39}(?:/fiscal_documents)?")


class FiscalError(ValueError):
    """A fixed reason code, never an upstream body or exception text."""


def fiscal_id(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 40:
        raise FiscalError("invalid_identity")
    try:
        result = remote_id(value)
    except AdmissionError:
        raise FiscalError("invalid_identity") from None
    if not IDENTIFIER.fullmatch(result):
        raise FiscalError("invalid_identity")
    return result


def order_target(order, order_id, seller_id, expected_pack):
    if not isinstance(order, dict) or "pack_id" not in order or not isinstance(order.get("seller"), dict):
        raise FiscalError("order_evidence_missing")
    if (fiscal_id(order.get("id")) != order_id
            or fiscal_id(order["seller"].get("id")) != seller_id):
        raise FiscalError("order_identity_mismatch")
    pack = None if order["pack_id"] is None else fiscal_id(order["pack_id"])
    if pack != expected_pack:
        raise FiscalError("pack_identity_mismatch")
    return order_id if pack is None else pack


def verify_pack(pack, expected_pack, order_ids):
    try:
        members = pack_order_ids(pack, expected_pack)
    except (AdmissionError, ValueError):
        raise FiscalError("pack_evidence_missing") from None
    if len(members) != len(order_ids) or set(members) != set(order_ids):
        raise FiscalError("pack_membership_mismatch")


def check_pdf_envelope(pdf):
    if not isinstance(pdf, bytes) or not pdf or len(pdf) > PDF_LIMIT:
        raise FiscalError("invalid_pdf_size")
    if not re.match(rb"%PDF-[12]\.[0-9](?:\r|\n)", pdf) or not pdf.rstrip().endswith(b"%%EOF"):
        raise FiscalError("invalid_pdf_format")
    return pdf


def _document_id(value):
    return value if isinstance(value, str) and DOCUMENT_ID.fullmatch(value) else None


def pdf_documents(status, body, target, seller):
    """Only a bound complete list or a documented no-document 404 proves absence."""
    if status == 404 and isinstance(body, dict):
        prefix = "The pack_fiscal_document with pack_id: " + target
        messages = (
            prefix + " does not exist",
            prefix + " does not have any fiscal_document attached for the user_id: " + seller,
        )
        if body.get("error") == "not_found" and body.get("status") == 404 and body.get("message") in messages:
            return {}
    if status != 200 or not isinstance(body, dict) or body.get("error"):
        raise FiscalError("listing_forbidden" if status in (401, 403) else "listing_unavailable")
    if fiscal_id(body.get("pack_id")) != target or not isinstance(body.get("fiscal_documents"), list):
        raise FiscalError("listing_identity_mismatch")
    documents, seen = {}, set()
    for entry in body["fiscal_documents"]:
        identity = _document_id(entry.get("id")) if isinstance(entry, dict) else None
        if not identity or identity in seen:
            raise FiscalError("listing_invalid")
        seen.add(identity)
        mime = entry.get("file_type")
        if mime == "application/pdf":
            filename = entry.get("filename")
            documents[identity] = filename if isinstance(filename, str) and len(filename) <= 255 else None
        elif mime not in ("application/xml", "text/xml"):
            raise FiscalError("listing_invalid")
    return documents


def upload_receipt(status, body):
    if status in (200, 201) and isinstance(body, dict) and not body.get("error"):
        ids = body.get("ids")
        if isinstance(ids, list) and len(ids) == 1:
            return _document_id(ids[0])
    return None


def find_verified_document(documents, receipt_id, filename):
    # The filename is assigned once, with a private random suffix, before the
    # only POST. A different PDF never becomes a receipt for this invoice.
    if len(documents) == 1 and isinstance(filename, str) and PDF_FILENAME.fullmatch(filename):
        identity, actual_filename = next(iter(documents.items()))
        if filename == actual_filename and (not receipt_id or receipt_id == identity):
            return identity
    return None


def strict_fiscal_request(client, path, *, document=None):
    """Use the existing client's Bearer; never forward it, replay a POST, or log its payload."""
    import requests
    from urllib3.exceptions import HTTPError

    token = client.access_token
    if not isinstance(token, str) or not token or token == "PASIVA":
        return 0, None
    if not isinstance(path, str) or not (ORDER_PATH.fullmatch(path) or PACK_PATH.fullmatch(path)):
        raise FiscalError("invalid_route")
    files = None
    if document is not None:
        filename, pdf = document
        if not path.endswith("/fiscal_documents") or not PDF_FILENAME.fullmatch(filename):
            raise FiscalError("invalid_route")
        files = {"fiscal_document": (filename, check_pdf_envelope(pdf), "application/pdf")}
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    status = 0
    try:
        request = requests.Request(
            "GET" if files is None else "POST",
            "https://api.mercadolibre.com" + path,
            files=files,
            headers={
                "Authorization": "Bearer " + token, "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        ).prepare()
        # As in read_item_metadata, call the adapter, not Session.send (which
        # may consume a redirect body to determine the next request).
        with adapter.send(request, timeout=(5, 20), stream=True, verify=True, proxies={}) as response:
            status = response.status_code
            if status not in (200, 201, 404) or response.headers.get("x-content-missing"):
                return status, None
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                return status, None
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isascii() or not length.isdecimal()
                                       or len(length) > 10 or int(length) > JSON_LIMIT):
                return status, None
            raw = response.raw.read(JSON_LIMIT + 1, decode_content=False)
            if len(raw) > JSON_LIMIT or (length is not None and len(raw) != int(length)):
                return status, None
            body = json.loads(raw)
            return status, body if isinstance(body, dict) else None
    except (requests.RequestException, HTTPError, OSError, ValueError, RecursionError):
        return status, None
    finally:
        adapter.close()
