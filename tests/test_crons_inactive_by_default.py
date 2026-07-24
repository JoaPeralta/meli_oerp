# -*- coding: utf-8 -*-
"""Regression tests: every ir.cron shipped by meli_oerp must be inactive on a
fresh install.

Enabling a MercadoLibre synchronisation must be an explicit decision of the
administrator, never a side effect of installing the module. Several of these
crons push data *to* MercadoLibre (stock, prices, publications), so an
active-by-default state is not an acceptable starting point.

The crons are discovered through ``ir.model.data`` (module = meli_oerp,
model = ir.cron) instead of a hand-maintained list, so a newly added cron that
ships active will fail these tests automatically.

Note on XML: the ``active`` flag must be declared as ``eval="False"``, never as
element text. Odoo's ``_eval_xml`` returns the raw text for a plain ``<field>``
and ``Boolean.convert_to_column`` does ``bool(value)`` — so
``<field name="active">False</field>`` yields the string ``"False"``, and
``bool("False")`` is ``True``. The XML test below guards against that pitfall.
"""

import os
import xml.etree.ElementTree as ET

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Crons that can write to MercadoLibre. They get an explicit, named assertion on
# top of the generic "everything is inactive" check.
_WRITER_CRON_XML_IDS = (
    "meli_oerp.ir_cron_module_cron_meli_process_post_products",
    "meli_oerp.ir_cron_module_cron_meli_process_post_stock",
    "meli_oerp.ir_cron_module_cron_meli_process_post_stock_rt",
    "meli_oerp.ir_cron_module_cron_meli_process_post_price",
)


@tagged("post_install", "-at_install")
class TestCronsInactiveByDefault(TransactionCase):

    def _module_crons(self):
        """All ir.cron records owned by meli_oerp, ignoring the active filter."""
        data = self.env["ir.model.data"].search(
            [("module", "=", "meli_oerp"), ("model", "=", "ir.cron")]
        )
        return self.env["ir.cron"].with_context(active_test=False).browse(data.mapped("res_id"))

    def test_module_ships_crons(self):
        """Sanity: the module does define crons (otherwise the checks are vacuous)."""
        crons = self._module_crons()
        self.assertTrue(
            crons, "No ir.cron records owned by meli_oerp were found; the "
            "inactive-by-default checks would be meaningless."
        )

    def test_all_module_crons_are_inactive(self):
        """Every cron shipped by meli_oerp must be inactive on a fresh install."""
        crons = self._module_crons()
        active = crons.filtered(lambda c: c.active)
        self.assertFalse(
            active,
            "These meli_oerp crons are active on a fresh install: %s. Enabling a "
            "MercadoLibre synchronisation must be an explicit admin decision."
            % active.mapped("name"),
        )

    def test_writer_crons_are_inactive(self):
        """Crons that push data to MercadoLibre must be inactive."""
        for xml_id in _WRITER_CRON_XML_IDS:
            cron = self.env.ref(xml_id, raise_if_not_found=False)
            self.assertTrue(cron, "Missing expected cron %s" % xml_id)
            self.assertFalse(
                cron.active,
                "%s is active on a fresh install; it can write stock/prices/"
                "publications to MercadoLibre." % xml_id,
            )

    def test_cron_xml_declares_active_with_eval(self):
        """Every ir.cron in the module XML must use eval="False" for active.

        A plain ``<field name="active">False</field>`` is passed to the ORM as the
        string "False", and ``bool("False")`` is True — i.e. it would silently
        ship the cron ACTIVE. This guards against that regression.
        """
        offenders = []
        data_dir = os.path.join(_MODULE_DIR, "data")
        for filename in sorted(os.listdir(data_dir)):
            if not filename.endswith(".xml"):
                continue
            path = os.path.join(data_dir, filename)
            root = ET.parse(path).getroot()
            for record in root.iter("record"):
                if record.get("model") != "ir.cron":
                    continue
                fields = [f for f in record.findall("field") if f.get("name") == "active"]
                if not fields or fields[0].get("eval") != "False":
                    offenders.append("%s:%s" % (filename, record.get("id")))
        self.assertFalse(
            offenders,
            'These ir.cron records do not declare \'<field name="active" '
            "eval=\"False\"/>\": %s" % offenders,
        )
