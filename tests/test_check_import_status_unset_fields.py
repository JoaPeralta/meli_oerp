# -*- coding: utf-8 -*-
""""Check import status" must work before any import has run.

``mercadolibre.product.template.import.check_import_status`` builds its summary
by concatenating three ``Char`` fields straight into a string:

    messhtml+= "<br/>Actives to sync: "+self.actives_to_sync
    messhtml+= "<br/>Paused to sync: "+self.paused_to_sync
    messhtml+= "<br/>Closed to sync: "+self.closed_to_sync

Those fields carry no default, and an unset ``Char`` in Odoo reads as ``False``.
Concatenating a ``str`` with ``False`` raises

    TypeError: can only concatenate str (not "bool") to str

so the button fails on a freshly opened wizard — the most common case, since
the three fields are only populated after a status check has already run.

Out of scope: what the summary should contain, the wizard layout, and the
import flow itself. This only stops the button from raising.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestCheckImportStatusUnsetFields(TransactionCase):

    def setUp(self):
        super().setUp()
        self.wizard = self.env["mercadolibre.product.template.import"].create({
            "title": "Importar",
        })
        self.captured = {}

        warning_model = type(self.env["meli.warning"])
        original_info = warning_model.info

        def _capture(inner_self, *args, **kwargs):
            self.captured.update(kwargs)
            return original_info(inner_self, *args, **kwargs)

        self._patcher = patch.object(warning_model, "info", _capture)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_unset_fields_do_not_raise(self):
        """A freshly created wizard has the three fields unset."""
        self.assertFalse(self.wizard.actives_to_sync)
        self.assertFalse(self.wizard.paused_to_sync)
        self.assertFalse(self.wizard.closed_to_sync)

        res = self.wizard.check_import_status()

        self.assertTrue(res, "check_import_status did not return an action")

    def test_partially_set_fields_do_not_raise(self):
        """Only one of the three populated is still enough to break it."""
        self.wizard.actives_to_sync = "5"

        res = self.wizard.check_import_status()

        self.assertTrue(res)

    def test_values_are_still_reported(self):
        """The summary must keep showing the counts when they exist."""
        self.wizard.write({
            "actives_to_sync": "7",
            "paused_to_sync": "3",
            "closed_to_sync": "1",
        })

        self.wizard.check_import_status()

        html = self.captured.get("message_html", "")
        self.assertIn("Actives to sync: 7", html)
        self.assertIn("Paused to sync: 3", html)
        self.assertIn("Closed to sync: 1", html)
