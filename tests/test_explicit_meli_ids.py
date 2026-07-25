# -*- coding: utf-8 -*-
"""Regression tests: the "MercadoLibre Id's a importar" field must select the
publications the user asked for, and nothing else.

The field was injected into the parameters of
``GET /users/{seller_id}/items/search``:

    post_state_filter.update( { 'meli_id': meli_id } )

That endpoint has no item id filter, so MercadoLibre ignored the parameter and
answered with the whole catalogue. The import then processed those publications
as if they had been requested. Observed in production: asking for
``MLA1651389099`` imported ``MLA1542894081`` (the first item of the catalogue)
and reported it as ``synced``.

The invariant these tests pin down is a safety one, and it matters before any
write towards MercadoLibre is enabled: **with explicit ids, no unrequested
item_id may reach ``results``.**

Note: the ``if (totalmax>1)`` off-by-one in ``product_meli_get_products`` is a
separate defect and is NOT fixed here, so a single explicit id still does not
get processed. Every processing test below therefore uses two or more ids; the
single-id case is only asserted at the parser level.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .test_preserve_existing_meli_id import _FakeMeli, _item

_REQUESTED_1 = "MLA_REQUESTED_1"
_REQUESTED_2 = "MLA_REQUESTED_2"
_WRONG = "MLA_WRONG"

_SKU_1 = "SKU-REQUESTED-1"
_SKU_2 = "SKU-REQUESTED-2"
_SKU_WRONG = "SKU-WRONG"


class _RecordingMeli(_FakeMeli):
    """``_FakeMeli`` that records every path it is asked for.

    Absence of a log line cannot prove an item was never fetched, so the tests
    assert against this list instead of against side effects.
    """

    def __init__(self, items):
        super().__init__(items)
        self.requested_paths = []

    def get(self, path, params=None, extra_headers=None, **kwargs):
        self.requested_paths.append(str(path))
        return super().get(path, params=params, extra_headers=extra_headers, **kwargs)

    def fetched_item_ids(self):
        return [p.rsplit("/", 1)[-1] for p in self.requested_paths if p.startswith("/items/")]


@tagged("post_install", "-at_install")
class TestExplicitMeliIdsParsing(TransactionCase):
    """The parser alone: the label promises a comma separated list."""

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id

    def test_comma_separated_with_surrounding_spaces(self):
        self.assertEqual(
            self.company._parse_explicit_meli_ids(" MLA111 , MLA222,MLA333 "),
            ["MLA111", "MLA222", "MLA333"],
        )

    def test_empty_entries_are_dropped(self):
        self.assertEqual(
            self.company._parse_explicit_meli_ids("MLA111,,  ,MLA222,"),
            ["MLA111", "MLA222"],
        )

    def test_single_id_yields_a_one_element_list(self):
        """Parsing a single id works.

        Processing it does not yet: ``if (totalmax>1)`` skips a one element
        list. That off-by-one is a separate change, so this asserts the parser
        only.
        """
        self.assertEqual(self.company._parse_explicit_meli_ids("  MLA111  "), ["MLA111"])

    def test_blank_input_yields_empty_list(self):
        for raw in (False, None, "", "   ", ",", " , , "):
            with self.subTest(raw=raw):
                self.assertEqual(self.company._parse_explicit_meli_ids(raw), [])


@tagged("post_install", "-at_install")
class TestExplicitMeliIdsDriveTheImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.mercadolibre_import_search_sku = True
        self.product_1 = self.env["product.product"].create({
            "name": "Requested product 1",
            "default_code": _SKU_1,
        })
        self.product_2 = self.env["product.product"].create({
            "name": "Requested product 2",
            "default_code": _SKU_2,
        })
        self.product_wrong = self.env["product.product"].create({
            "name": "Product of an unrequested publication",
            "default_code": _SKU_WRONG,
        })

    def _run_import(self, explicit_ids, search_returns):
        """Run the real import.

        ``search_returns`` is what the general seller search would answer; the
        catalogue always offers the unrequested publication so the test can
        prove it never gets in.
        """
        fake = _RecordingMeli({
            _REQUESTED_1: _item(_REQUESTED_1, _SKU_1, title="Requested 1"),
            _REQUESTED_2: _item(_REQUESTED_2, _SKU_2, title="Requested 2"),
            _WRONG: _item(_WRONG, _SKU_WRONG, title="Never requested"),
        })
        context = {
            "meli_id": explicit_ids,
            "post_state": "all",
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ), patch.object(
            type(self.env["res.company"]), "fetch_list_meli_ids",
            return_value=list(search_returns),
        ) as fetch_mock:
            res = self.company.product_meli_get_products(context=context)
        return res, fake, fetch_mock

    def _synced_ids(self, res):
        return [row["meli_id"] for row in (res or {}).get("json_report", {}).get("synced", [])]

    def test_only_the_requested_publications_are_processed(self):
        """Main regression: the catalogue must not smuggle in another item."""
        res, fake, fetch_mock = self._run_import(
            "%s, %s" % (_REQUESTED_1, _REQUESTED_2),
            [_WRONG, _REQUESTED_1, _REQUESTED_2],
        )

        # The general search is not even consulted when ids are given.
        fetch_mock.assert_not_called()
        # Only the requested publications were fetched from MercadoLibre.
        self.assertEqual(
            fake.fetched_item_ids(), [_REQUESTED_1, _REQUESTED_2],
            "The import fetched publications the user did not ask for",
        )
        self.assertNotIn(
            _WRONG, fake.fetched_item_ids(),
            "An unrequested publication reached GET /items/{id}",
        )
        # ...and only those are reported.
        self.assertEqual(sorted(self._synced_ids(res)), [_REQUESTED_1, _REQUESTED_2])
        self.assertNotIn(_WRONG, self._synced_ids(res))

    def test_requested_order_is_preserved(self):
        """Nothing in the flow reorders the requested ids."""
        _res, fake, _fetch_mock = self._run_import(
            "%s,%s" % (_REQUESTED_2, _REQUESTED_1),
            [_WRONG],
        )

        self.assertEqual(fake.fetched_item_ids(), [_REQUESTED_2, _REQUESTED_1])

    def test_state_filter_does_not_replace_explicit_ids(self):
        """Picking a state must not bring the catalogue back in.

        The explicit ids stay the primary selection; each publication is still
        fetched and processed normally, carrying its real MercadoLibre status.
        """
        fake = _RecordingMeli({
            _REQUESTED_1: _item(_REQUESTED_1, _SKU_1, title="Requested 1"),
            _REQUESTED_2: _item(_REQUESTED_2, _SKU_2, title="Requested 2"),
            _WRONG: _item(_WRONG, _SKU_WRONG, title="Never requested"),
        })
        context = {
            "meli_id": "%s, %s" % (_REQUESTED_1, _REQUESTED_2),
            "post_state": "active",
            "force_dont_create": True,
            "force_meli_pub": False,
            "force_import_images": False,
            "batch_processing_unit": 10,
            "batch_processing_unit_offset": 0,
        }
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ), patch.object(
            type(self.env["res.company"]), "fetch_list_meli_ids",
            return_value=[_WRONG],
        ) as fetch_mock:
            res = self.company.product_meli_get_products(context=context)

        fetch_mock.assert_not_called()
        self.assertEqual(fake.fetched_item_ids(), [_REQUESTED_1, _REQUESTED_2])
        self.assertEqual(sorted(self._synced_ids(res)), [_REQUESTED_1, _REQUESTED_2])

    def test_without_explicit_ids_the_general_search_still_drives_the_import(self):
        """No ids given: previous behaviour is preserved untouched."""
        res, fake, fetch_mock = self._run_import(False, [_WRONG, _REQUESTED_1])

        fetch_mock.assert_called_once()
        self.assertEqual(fake.fetched_item_ids(), [_WRONG, _REQUESTED_1])
        self.assertEqual(sorted(self._synced_ids(res)), sorted([_WRONG, _REQUESTED_1]))

    def test_blank_ids_field_falls_back_to_the_general_search(self):
        """A field holding only separators is not a selection.

        Two ids are returned on purpose: with a single one the untouched
        ``if (totalmax>1)`` off-by-one would skip processing altogether and the
        assertion would be measuring that defect instead of this one.
        """
        _res, fake, fetch_mock = self._run_import("  ,  ", [_WRONG, _REQUESTED_1])

        fetch_mock.assert_called_once()
        self.assertEqual(fake.fetched_item_ids(), [_WRONG, _REQUESTED_1])
