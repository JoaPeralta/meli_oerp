# -*- coding: utf-8 -*-
"""Regression tests: installing meli_oerp must not change the global
``decimal.precision`` for "Product Price".

The module used to declare a ``post_init_hook`` that raised that precision to 6
digits for the whole database. On Odoo <= 17 that had a purpose, because the
relevant core price fields were declared with ``digits='Product Price'`` and
therefore rounded on write. On Odoo 18/19 those fields use
``min_display_digits='Product Price'``, which only sets a minimum number of
decimals shown in the UI — it does not affect storage or rounding. The hook was
therefore removed on this branch: it no longer buys precision and it mutates a
global, user-visible setting on every install.

These tests fail if the hook (or any equivalent write to that precision) comes
back. They do not attempt to test Odoo core's ``min_display_digits`` behaviour.
"""

import ast
import importlib
import os

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PRECISION_NAME = "Product Price"


def _load_manifest():
    with open(os.path.join(_MODULE_DIR, "__manifest__.py"), encoding="utf-8") as fh:
        return ast.literal_eval(fh.read())


@tagged("post_install", "-at_install")
class TestNoGlobalPricePrecision(TransactionCase):

    def test_manifest_registers_no_init_hooks(self):
        """The manifest must not register install hooks any more."""
        manifest = _load_manifest()
        for key in ("post_init_hook", "pre_init_hook"):
            self.assertNotIn(
                key, manifest,
                "%s is registered again in __manifest__.py; installing the module "
                "must not run install-time hooks that mutate global settings." % key,
            )

    def test_module_defines_no_init_hooks(self):
        """The module package must not expose install hook callables."""
        mod = importlib.import_module("odoo.addons.meli_oerp")
        for name in ("post_init_hook", "pre_init_hook"):
            self.assertFalse(
                hasattr(mod, name),
                "meli_oerp still defines %s(); it was removed so installing the "
                "module cannot change the global Product Price precision." % name,
            )

    def test_no_source_file_references_product_price_precision(self):
        """No module source file may target the "Product Price" precision.

        This is the actual regression guard: it fails if anyone reintroduces a
        write/create against that decimal.precision record anywhere in the addon.
        """
        offenders = []
        for root, dirs, files in os.walk(_MODULE_DIR):
            dirs[:] = [d for d in dirs if d not in ("tests", ".git", "__pycache__")]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(root, filename)
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
                if _PRECISION_NAME in content and "decimal.precision" in content:
                    offenders.append(os.path.relpath(path, _MODULE_DIR))
        self.assertFalse(
            offenders,
            "These files write/read the global '%s' decimal.precision: %s. The "
            "module must not mutate that global setting." % (_PRECISION_NAME, offenders),
        )

    def test_existing_product_price_precision_is_left_untouched(self):
        """An existing Product Price precision must survive the module untouched.

        Sets the precision to 2 and runs the module's install hook if one exists
        (there should be none). The value must still be 2 afterwards.
        """
        precision = self.env["decimal.precision"].search(
            [("name", "=", _PRECISION_NAME)], limit=1
        )
        if precision:
            precision.write({"digits": 2})
        else:
            precision = self.env["decimal.precision"].create(
                {"name": _PRECISION_NAME, "digits": 2}
            )
        self.env.flush_all()

        # If a hook is ever reintroduced, run it here so this test catches the
        # mutation instead of silently passing.
        mod = importlib.import_module("odoo.addons.meli_oerp")
        hook = getattr(mod, "post_init_hook", None)
        if hook is not None:
            hook(self.env)

        precision.invalidate_recordset()
        self.assertEqual(
            precision.digits, 2,
            "The module changed the global '%s' precision (expected it to stay at 2)."
            % _PRECISION_NAME,
        )
