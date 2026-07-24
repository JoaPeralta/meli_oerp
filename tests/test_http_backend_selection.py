# -*- coding: utf-8 -*-
"""Tests for the deterministic HTTP backend selection.

The backend MeliApi uses must be chosen explicitly (default: NoSDK) and must NOT
depend on whether the optional ``meli`` package is importable. These tests pin
that contract down, plus the requirements.txt commit pin.

No network, no credentials.
"""

import os

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models import versions, meli_util

# The exact SDK commit pinned in requirements.txt for reproducibility.
_SDK_PIN = "70fc5c0252c4414580e6dd42610acb606b193b09"


@tagged("post_install", "-at_install")
class TestHttpBackendSelection(TransactionCase):

    # ---- 1 & 2: default is NoSDK, independent of SDK availability --------

    def test_default_backend_is_nosdk(self):
        """With no explicit configuration, MeliApi resolves to MeliApiNoSDK."""
        self.assertIs(
            meli_util.MeliApi, meli_util.MeliApiNoSDK,
            "default MeliApi must be the NoSDK backend",
        )
        self.assertFalse(
            versions.USE_MELI_SDK,
            "USE_MELI_SDK must be False by default",
        )

    def test_default_does_not_depend_on_sdk_availability(self):
        """Even when the `meli` package IS importable (as in CI, which installs
        it), the default backend is still NoSDK — presence must not flip it."""
        self.assertTrue(
            versions.MELI_SDK_AVAILABLE,
            "this test assumes the SDK package is installed in the CI image",
        )
        # Availability is True, yet the resolved backend is still NoSDK.
        self.assertIs(meli_util.MeliApi, meli_util.MeliApiNoSDK)

    # ---- normalize_http_backend -----------------------------------------

    def test_normalize_backend_values(self):
        self.assertEqual(versions.normalize_http_backend("sdk"), "sdk")
        self.assertEqual(versions.normalize_http_backend(" SDK "), "sdk")
        self.assertEqual(versions.normalize_http_backend("NoSDK"), "nosdk")
        self.assertEqual(versions.normalize_http_backend("nosdk"), "nosdk")

    def test_normalize_backend_invalid_defaults_to_nosdk(self):
        for bad in ("garbage", "", None, "true", "1", "requests"):
            self.assertEqual(
                versions.normalize_http_backend(bad), "nosdk",
                "invalid backend %r must normalize to 'nosdk'" % (bad,),
            )

    # ---- 3 & 4 & 5: resolve_use_sdk truth table -------------------------

    def test_resolve_use_sdk_explicit_nosdk(self):
        # explicit nosdk -> never SDK, regardless of availability
        self.assertFalse(versions.resolve_use_sdk("nosdk", True))
        self.assertFalse(versions.resolve_use_sdk("nosdk", False))

    def test_resolve_use_sdk_explicit_sdk_available(self):
        # explicit sdk + package available -> SDK
        self.assertTrue(versions.resolve_use_sdk("sdk", True))

    def test_resolve_use_sdk_requested_but_unavailable_falls_back(self):
        # explicit sdk but package NOT available -> deterministic NoSDK (fallback)
        self.assertFalse(versions.resolve_use_sdk("sdk", False))

    # ---- 6: invalid value is safe/deterministic -------------------------

    def test_invalid_value_resolves_to_nosdk(self):
        backend = versions.normalize_http_backend("something-weird")
        self.assertEqual(backend, "nosdk")
        self.assertFalse(versions.resolve_use_sdk(backend, True))

    # ---- 8: requirements.txt commit pin ---------------------------------

    def test_requirements_pins_sdk_commit(self):
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "requirements.txt",
        )
        with open(req_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "python-sdk-2025.git@%s" % _SDK_PIN, content,
            "requirements.txt must pin python-sdk-2025 to the audited commit",
        )
        # The unpinned form must no longer be present.
        self.assertNotIn(
            "python-sdk-2025.git\n", content,
            "requirements.txt must not contain the unpinned SDK line",
        )
