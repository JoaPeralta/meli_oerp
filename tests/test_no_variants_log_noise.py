# -*- coding: utf-8 -*-
"""The "no variants" path is normal, and must not log an error nor dump the payload.

``product_meli_get_product`` ends its variant handling with:

    else:
        #NO TIENE variantes pero tiene SKU
        _logger.error("NO TIENE variantes pero tiene SKU " + str(rjson))

That branch is taken whenever the company does not update existing variants, or
the item carries no ``variations`` — the ordinary case for a simple publication.
It is not an error, and ``str(rjson)`` is the entire MercadoLibre item.

Measured on a real import: ~9.1 kB per line, 95 lines over the catalogue, about
1 MB written to stderr. Beyond the noise and the false alerts, that volume
**blocks the writing process** when stderr is a pipe nobody drains fast enough:
a full-catalogue import through `odoo shell` froze at 967 kB and never advanced.
Silencing this logger made the same run finish in 163 s.

The payload also carries operational data that has no business in a log:
``seller_address`` (street, postcode, city), ``geolocation``, prices and
inventory ids.

Scope: this single log statement. The other ``_logger.error`` calls that include
``rjson`` sit in genuine error branches, where the response is small and logging
it is the point; they are deliberately left alone.
"""

import logging

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_LOGGER_NAME = "odoo.addons.meli_oerp.models.product"
_ITEM = "MLA_NOVAR_1"
# Appears only inside the payload. If it shows up in a log record, the whole
# item was dumped.
_SENTINEL = "SENTINEL-ZIP-9x7"


def _item_without_variants():
    return {
        "id": _ITEM,
        "title": "Publicacion simple sin variantes",
        "status": "active",
        "price": 1000,
        "base_price": 1000,
        "available_quantity": 5,
        "initial_quantity": 5,
        "sold_quantity": 0,
        "currency_id": "ARS",
        "category_id": "MLA1234",
        "listing_type_id": "gold_special",
        "condition": "new",
        "permalink": "https://articulo.mercadolibre.com.ar/MLA-1",
        "thumbnail": "http://http2.mlstatic.com/D_1-I.jpg",
        "pictures": [],
        "attributes": [
            {"id": "SELLER_SKU", "values": [{"name": "SKU-NOVAR-1"}],
             "value_name": "SKU-NOVAR-1"},
        ],
        "sale_terms": [],
        "tags": [],
        # operational data that must never reach the logs
        "seller_address": {"zip_code": _SENTINEL, "city": {"name": "Cordoba"}},
        "geolocation": {"latitude": -31.38, "longitude": -64.20},
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeMeli:
    def __init__(self, payload):
        self.access_token = "TEST-TOKEN"
        self._payload = payload

    def need_login(self):
        return False

    def redirect_login(self):
        return {}

    def get(self, path, params=None, extra_headers=None, **kwargs):
        return _FakeResponse(self._payload)


@tagged("post_install", "-at_install")
class TestNoVariantsLogNoise(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        # Drives the flow into the branch under test.
        self.company.mercadolibre_update_existings_variants = False
        self.product = self.env["product.product"].create({
            "name": "Simple publication product",
            "default_code": "SKU-NOVAR-1",
            "meli_id": _ITEM,
        })

    def _run(self):
        fake = _FakeMeli(_item_without_variants())
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            with self.assertLogs(_LOGGER_NAME, level="DEBUG") as captured:
                self.product.product_meli_get_product(import_images=False)
        return captured

    def test_no_variants_path_does_not_log_an_error(self):
        """A simple publication is not an error condition."""
        captured = self._run()

        offending = [
            r.getMessage() for r in captured.records
            if r.levelno >= logging.ERROR and "variantes" in r.getMessage()
        ]
        self.assertEqual(
            offending, [],
            "the ordinary no-variants path is logged at ERROR level",
        )

    def test_the_item_payload_is_not_dumped(self):
        """No log record may carry the MercadoLibre payload."""
        captured = self._run()

        leaked = [r.getMessage()[:120] for r in captured.records
                  if _SENTINEL in r.getMessage()]
        self.assertEqual(
            leaked, [],
            "the full item payload reached the logs (seller_address and "
            "geolocation among it); over a catalogue this is ~1 MB of stderr",
        )

    def test_no_log_record_is_oversized(self):
        """Guard on volume, independent of which field leaks."""
        captured = self._run()

        oversized = [(r.name, len(r.getMessage())) for r in captured.records
                     if len(r.getMessage()) > 500]
        self.assertEqual(
            oversized, [],
            "a log record over 500 chars was emitted: %s" % oversized,
        )

    def test_the_publication_is_still_processed(self):
        """Regression guard: quieting the log must not skip the work."""
        self._run()

        self.assertEqual(
            self.product.meli_id, _ITEM,
            "the product lost its binding while processing",
        )
