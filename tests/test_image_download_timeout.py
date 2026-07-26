# -*- coding: utf-8 -*-
"""Image downloads from MercadoLibre must be bounded.

``models/product.py`` pulls publication pictures with

    image = urlopen(thumbnail_url).read()

``urllib.request.urlopen`` has **no default timeout**: if the remote host
accepts the connection and then stalls, the call blocks forever. These run
inside the product import loop, once per picture, holding an Odoo worker and an
open transaction — the same transaction whose rollback discards a whole run.

The module already fixes a bound for its own HTTP client:

    timeout = params.get("timeout", 20)      # MeliApiNoSDK.get/post/put/delete

so the same 20 s is applied here rather than inventing a new number.

Out of scope: the retry/al backoff policy, the ``requests.post`` upload paths in
``meli_util``, ``shipment.py`` (which reads ``file://`` URLs, not network), and
whether image import should happen inside the import transaction at all.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

import odoo.addons.meli_oerp.models.product as product_module


class _FakeImage:
    def read(self):
        return b"fake-image-bytes"


@tagged("post_install", "-at_install")
class TestImageDownloadTimeout(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        # The method returns early unless this is on.
        self.company.mercadolibre_remove_unsync_images = True
        self.product = self.env["product.product"].create({
            "name": "Image timeout product", "default_code": "SKU-IMG-TIMEOUT",
        })

    def test_picture_download_is_bounded(self):
        """The download must carry a timeout; unbounded blocks a worker."""
        pictures = [{"id": "PIC-1", "url": "https://http2.mlstatic.com/pic-1.jpg"}]

        with patch.object(
            product_module, "urlopen", return_value=_FakeImage()
        ) as fake_urlopen:
            self.product._meli_remove_images_unsync(
                self.product.product_tmpl_id, pictures, config=self.company)

        self.assertTrue(fake_urlopen.called, "the picture was never fetched")
        _args, kwargs = fake_urlopen.call_args
        self.assertIn(
            "timeout", kwargs,
            "urlopen was called without a timeout: a stalled host blocks the "
            "worker and the open transaction indefinitely",
        )
        self.assertGreater(kwargs["timeout"], 0)

    def test_every_picture_download_is_bounded(self):
        """Several pictures: none of them may be unbounded."""
        pictures = [
            {"id": "PIC-1", "url": "https://http2.mlstatic.com/pic-1.jpg"},
            {"id": "PIC-2", "url": "https://http2.mlstatic.com/pic-2.jpg"},
        ]

        with patch.object(
            product_module, "urlopen", return_value=_FakeImage()
        ) as fake_urlopen:
            self.product._meli_remove_images_unsync(
                self.product.product_tmpl_id, pictures, config=self.company)

        self.assertEqual(fake_urlopen.call_count, 2)
        for call in fake_urlopen.call_args_list:
            self.assertIn("timeout", call.kwargs, "an unbounded download slipped through")

    def test_no_bare_urlopen_call_remains_in_the_module(self):
        """Source-level guard.

        Only one of the five call sites is reachable from a test without
        building a full MercadoLibre item payload, so the others are pinned by
        reading the source. Without this, four unbounded downloads would stay
        green.
        """
        import inspect
        import re

        source = inspect.getsource(product_module)
        # urlopen( ... ) calls that do not mention a timeout on the same line.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"(?<!\w)urlopen\s*\(", line)
            and not line.strip().startswith("#")
            and "timeout" not in line
            and "import" not in line
        ]
        self.assertEqual(
            offenders, [],
            "these urlopen calls are unbounded: %s" % offenders,
        )
