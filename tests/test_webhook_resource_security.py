# -*- coding: utf-8 -*-
"""Unit tests for the webhook resource-path validator.

These tests exercise ``safe_meli_resource_path`` directly. The validator lives
in an Odoo-free module, so this test runs both under the Odoo test runner and as
a plain ``python -m unittest`` / direct execution (no Odoo, no network, no
credentials).
"""

import os
import unittest

try:  # under the Odoo test runner
    from odoo.addons.meli_oerp.models.meli_webhook_security import (
        safe_meli_resource_path,
    )
    # Inherit from Odoo's TransactionCase so the Odoo test runner tags this as a
    # meli_oerp test and actually executes it under --test-tags=/meli_oerp.
    # (A plain unittest.TestCase is untagged and would be skipped by that filter.)
    from odoo.tests.common import TransactionCase as _TestBase
    from odoo.tests import tagged
except Exception:  # standalone (no Odoo available): load by file path
    import importlib.util

    _MODELS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
    )
    _spec = importlib.util.spec_from_file_location(
        "meli_webhook_security",
        os.path.join(_MODELS_DIR, "meli_webhook_security.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    safe_meli_resource_path = _mod.safe_meli_resource_path

    # No Odoo: fall back to a plain TestCase and a no-op tag decorator so the
    # file still runs with `python tests/test_webhook_resource_security.py`.
    _TestBase = unittest.TestCase

    def tagged(*args, **kwargs):
        return lambda cls: cls


@tagged("post_install", "-at_install")
class TestSafeMeliResourcePath(_TestBase):

    # --- legitimate relative resources are accepted, value preserved ---------

    def test_valid_order_resource_allowed(self):
        self.assertEqual(
            safe_meli_resource_path("/orders/2000000000000000", ("/orders/",)),
            "/orders/2000000000000000",
        )

    def test_valid_question_resource_allowed(self):
        self.assertEqual(
            safe_meli_resource_path("/questions/123456789", ("/questions/",)),
            "/questions/123456789",
        )

    def test_valid_item_resource_allowed(self):
        self.assertEqual(
            safe_meli_resource_path("/items/MLA123456789", ("/items/",)),
            "/items/MLA123456789",
        )

    def test_query_string_preserved(self):
        self.assertEqual(
            safe_meli_resource_path("/items/MLA1?include_attributes=all", ("/items/",)),
            "/items/MLA1?include_attributes=all",
        )

    # --- exfiltration vectors are rejected -----------------------------------

    def test_absolute_https_url_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("https://attacker.example/steal", ("/orders/",))
        )

    def test_absolute_http_url_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("http://attacker.example/steal", ("/orders/",))
        )

    def test_scheme_relative_url_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("//attacker.example/steal", ("/orders/",))
        )

    def test_userinfo_url_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path(
                "https://user@attacker.example/steal", ("/orders/",)
            )
        )

    def test_url_with_port_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("https://attacker.example:8080/x", ("/orders/",))
        )

    # --- malformed / hostile shapes ------------------------------------------

    def test_prefix_not_in_allowlist_rejected(self):
        self.assertIsNone(safe_meli_resource_path("/unknown/1", ("/orders/",)))

    def test_path_traversal_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("/orders/../../etc/passwd", ("/orders/",))
        )

    def test_whitespace_rejected(self):
        self.assertIsNone(safe_meli_resource_path("/orders/ 1", ("/orders/",)))

    def test_newline_rejected(self):
        self.assertIsNone(
            safe_meli_resource_path("/orders/1\nHost: evil", ("/orders/",))
        )

    def test_backslash_rejected(self):
        self.assertIsNone(safe_meli_resource_path("/orders/\\evil", ("/orders/",)))

    def test_relative_without_leading_slash_rejected(self):
        self.assertIsNone(safe_meli_resource_path("orders/1", ("/orders/",)))

    def test_empty_and_none_rejected(self):
        self.assertIsNone(safe_meli_resource_path("", ("/orders/",)))
        self.assertIsNone(safe_meli_resource_path(None, ("/orders/",)))
        self.assertIsNone(safe_meli_resource_path("   ", ("/orders/",)))

    def test_non_string_rejected(self):
        self.assertIsNone(safe_meli_resource_path(12345, ("/orders/",)))
        self.assertIsNone(safe_meli_resource_path(["/orders/1"], ("/orders/",)))


if __name__ == "__main__":
    unittest.main()
