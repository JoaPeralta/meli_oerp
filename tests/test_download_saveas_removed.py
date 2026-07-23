# -*- coding: utf-8 -*-
"""Regression test for the removal of the unsafe public ``/download/saveas``
endpoint.

The former ``Download`` controller in ``controllers/main.py`` exposed a route
with ``auth="public"`` that took a model name and a method name from the request
query string and executed ``getattr(env[model].browse(id), method)()``. That
allowed an anonymous caller to invoke arbitrary methods on arbitrary models.

The endpoint was example/copy-paste boilerplate with no functional consumer in
this module, so it was removed. This test fails if the route is ever
reintroduced, so the vulnerability cannot silently come back.
"""

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged("post_install", "-at_install")
class TestDownloadSaveasRemoved(HttpCase):

    def test_download_saveas_route_is_not_registered(self):
        """The public /download/saveas route must not exist anymore.

        A removed route yields an HTTP 404. If the controller is reintroduced,
        the request would resolve (200/302/500) instead, failing this test.
        """
        response = self.url_open(
            "/download/saveas"
            "?model=res.users&record_id=1&method=read",
            allow_redirects=False,
        )
        self.assertEqual(
            response.status_code,
            404,
            "The /download/saveas endpoint is reachable again: the unsafe "
            "dynamic getattr(model, method)() download controller must stay "
            "removed.",
        )

    def test_download_controller_class_is_absent(self):
        """The Download controller class must not be defined in the module."""
        from odoo.addons.meli_oerp.controllers import main as meli_main

        self.assertFalse(
            hasattr(meli_main, "Download"),
            "controllers/main.py still defines a Download controller; the "
            "unsafe /download/saveas endpoint must not be reintroduced.",
        )
