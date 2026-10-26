"""Run the actual company item-read methods with synthetic transport only.

Like the existing standalone response/C6 tests, AST extraction executes source
methods, not a second implementation. This is not ORM or live API evidence.
"""
import ast
import copy
import importlib.util
from pathlib import Path
from types import MethodType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


class ContractError(Exception):
    pass


class Client:
    access_token = "SYNTHETIC-ONLY"
    refresh_token = "UNCHANGED-SYNTHETIC"
    seller_id = "123"
    logged_out = False

    def __init__(self, ids=()):
        self.ids = list(ids)
        self.calls = []
        self.status = 200
        self.reply = lambda batch: [self.row(id_) for id_ in batch]

    @staticmethod
    def row(id_, store=99):
        return {"id": id_, "status_code": 200,
                "body": {"id": id_, "official_store_id": store}}

    def need_login(self):
        return self.logged_out

    def get(self, path, params=None, **kwargs):
        self.last_status_code = self.status
        query = parse_qs(urlsplit(path).query)
        query.update(params or {})
        self.calls.append((path, copy.deepcopy(params or {})))
        if path.startswith("/users/123/items/search"):
            body = {"results": list(self.ids), "paging": {"total": len(self.ids)}}
        elif urlsplit(path).path in ("/items", "/items/bulk"):
            ids = query.get("ids", "")
            ids = ids[0] if isinstance(ids, list) else ids
            body = self.reply(ids.split(",") if ids else [])
        else:
            raise AssertionError("Unbound synthetic route: " + path)
        return SimpleNamespace(status_code=self.status, headers={}, rjson=body,
                               json=lambda: copy.deepcopy(body))

    def bulk_calls(self):
        return [(path, params) for path, params in self.calls if path.startswith("/items")]


def fixture(ids=()):
    path = ROOT / "models/company.py"
    tree = ast.parse(path.read_bytes())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "res_company")
    names = {"filter_meli_ids", "get_all_items_from_existing_sku", "get_all_items_from_ML",
             "_meli_items_bulk", "fetch_list_meli_ids", "_meli_ids_auth_failed", "_meli_ids_auth_stop"}
    methods = [copy.deepcopy(node) for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    for method in methods:
        method.decorator_list = []
    spec = importlib.util.spec_from_file_location("actual_bulk_snapshot", ROOT / "models/meli_pack_admission.py")
    admission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admission)
    scope = {"UserError": ContractError, "_": lambda value: value, "RATE_LIMIT": 0,
             "time": SimpleNamespace(sleep=lambda _: None), "snapshot_response": admission.snapshot_response,
             "_logger": SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), scope)
    auth_messages = next(node for node in owner.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "_MELI_IDS_AUTH_MESSAGES"
                                 for target in node.targets))
    company = SimpleNamespace(id=7, mercadolibre_seller_id="123", mercadolibre_official_store_id=False,
                              _MELI_IDS_AUTH_MESSAGES=ast.literal_eval(auth_messages.value))
    client = Client(ids)
    reconnect = Mock(side_effect=ContractError("Reconnect explicitly"))
    util = SimpleNamespace(get_new_instance=Mock(return_value=client), _meli_require_reconnect=reconnect)
    parameters = {"meli_local_products_search_range": "50", "meli_local_products_search_last_offset": "0",
                  "meli_local_products_search_total": "0"}
    writes = []
    config = SimpleNamespace(get_param=lambda key: parameters[key],
                             set_param=lambda key, value: writes.append((key, value)))
    config.sudo = lambda: config
    class Env:
        user = SimpleNamespace(company_id=company)
        def __getitem__(self, name):
            return {"meli.util": util, "ir.config_parameter": config}[name]
    company.env = Env()
    company.env.company = company
    for name in names:
        if name in scope:
            setattr(company, name, MethodType(scope[name], company))
    return company, client, writes


class ExistingItemReadBaseline(unittest.TestCase):
    def test_no_store_filter_preserves_selection_and_no_item_request(self):
        company, client, _ = fixture()
        ids = ["MLA1", "MLA2"]
        self.assertEqual(company.filter_meli_ids(ids), ids)
        self.assertEqual(client.calls, [])


    def test_empty_store_selection_does_not_request_items(self):
        company, client, _ = fixture()
        self.assertEqual(company.filter_meli_ids([], 99), [])
        self.assertEqual(client.calls, [])

    def test_sku_without_matches_preserves_product_and_single_search(self):
        company, client, _ = fixture()
        product = SimpleNamespace(default_code="SKU-1", meli_id=False, product_meli_get_product=Mock())
        company.get_all_items_from_existing_sku([product])
        self.assertFalse(product.meli_id)
        product.product_meli_get_product.assert_not_called()
        self.assertEqual(client.calls[0][0], "/users/123/items/search?seller_sku=SKU-1")
        self.assertEqual(len(client.calls), 1)

    def test_reconnect_does_not_modify_tokens_or_issue_item_reads(self):
        company, client, _ = fixture()
        client.logged_out = True
        before = (client.access_token, client.refresh_token)
        with self.assertRaises(ContractError):
            company.filter_meli_ids(["MLA1"], 99)
        self.assertEqual((client.access_token, client.refresh_token), before)
        self.assertEqual(client.calls, [])

class BulkItemContracts(unittest.TestCase):
    def test_actual_fetch_list_search_and_official_store_caller_remain_compatible(self):
        company, client, _ = fixture(["MLA1", "MLA2"])
        company.mercadolibre_official_store_id = 99
        self.assertEqual(company.fetch_list_meli_ids(meli=client), ["MLA1", "MLA2"])
        self.assertEqual(client.calls[0][0], "/users/123/items/search")
        self.assertEqual(client.calls[0][1]["access_token"], client.access_token)

    def test_actual_fetch_list_http_auth_rejection_keeps_credentials_and_stops(self):
        company, client, _ = fixture(["MLA1"])
        client.status = 401
        before = (client.access_token, client.refresh_token)
        with self.assertRaises(ContractError):
            company.fetch_list_meli_ids(meli=client)
        self.assertEqual((client.access_token, client.refresh_token), before)
        self.assertEqual(client.bulk_calls(), [])

    def test_actual_individual_item_get_call_still_uses_the_single_item_route(self):
        tree = ast.parse((ROOT / "models/company.py").read_bytes())
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "product_meli_get_products")
        call = next(node for node in ast.walk(method)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.BinOp)
                    and isinstance(node.args[0].left, ast.Constant) and node.args[0].left.value == "/items/")
        client = SimpleNamespace(access_token="SYNTHETIC-ONLY", get=Mock())
        eval(compile(ast.Expression(body=call), "actual-single-item-route", "eval"),
             {"meli": client, "item_id": "MLA1"})
        client.get.assert_called_once_with("/items/MLA1", {"access_token": "SYNTHETIC-ONLY"})

    def test_empty_or_duplicate_selection_never_requests_a_bulk_page(self):
        company, client, _ = fixture()
        self.assertEqual(company._meli_items_bulk(client, []), [])
        for ids in (["MLA1", "MLA1"], ["MLA1", ""], ["MLA1", None]):
            with self.subTest(ids=ids), self.assertRaises(ContractError):
                company._meli_items_bulk(client, ids)
        self.assertEqual(client.calls, [])

    def test_store_filter_uses_bulk_body_attributes_and_official_store(self):
        company, client, _ = fixture()
        client.reply = lambda ids: [Client.row(ids[1], 98), Client.row(ids[0], 99)]
        self.assertEqual(company.filter_meli_ids(["MLA1", "MLA2"], 99), ["MLA1"])
        path, params = client.bulk_calls()[0]
        self.assertEqual(path, "/items/bulk")
        self.assertEqual(params, {"access_token": client.access_token, "ids": "MLA1,MLA2",
                                  "attributes": "body.id,body.official_store_id"})

    def test_twenty_and_twenty_one_ids_are_bounded_and_ordered(self):
        for count in (20, 21):
            with self.subTest(count=count):
                ids = ["MLA" + str(i) for i in range(count)]
                company, client, _ = fixture()
                client.reply = lambda batch: [Client.row(id_) for id_ in reversed(batch)]
                self.assertEqual(company.filter_meli_ids(ids, 99), ids)
                calls = client.bulk_calls()
                self.assertEqual(len(calls), (count + 19) // 20)
                self.assertTrue(all(len(params["ids"].split(",")) <= 20 for _, params in calls))

    def test_sku_import_correlates_first_search_id_not_first_response_position(self):
        company, client, _ = fixture(["MLA1", "MLA2"])
        client.reply = lambda batch: [Client.row(id_) for id_ in reversed(batch)]
        product = SimpleNamespace(default_code="SKU-1", meli_id=False, product_meli_get_product=Mock())
        company.get_all_items_from_existing_sku([product])
        self.assertEqual(product.meli_id, "MLA1")
        self.assertEqual(product.product_meli_get_product.call_args.kwargs["meli_rjson"]["id"], "MLA1")
        self.assertEqual(client.bulk_calls()[0][0], "/items/bulk")

    def test_sku_twenty_one_results_are_chunked_and_do_not_guess_include_attributes(self):
        company, client, _ = fixture(["MLA" + str(i) for i in range(21)])
        product = SimpleNamespace(default_code="SKU-1", meli_id=False, product_meli_get_product=Mock())
        company.get_all_items_from_existing_sku([product])
        calls = client.bulk_calls()
        self.assertEqual([path for path, _ in calls], ["/items/bulk", "/items/bulk"])
        self.assertEqual([len(params["ids"].split(",")) for _, params in calls], [20, 1])
        self.assertTrue(all("include_attributes" not in params for _, params in calls))

    def test_full_fetch_returns_complete_id_correlated_envelopes_and_updates_cursor_last(self):
        ids = ["MLA" + str(i) for i in range(21)]
        company, client, writes = fixture(ids)
        client.reply = lambda batch: [Client.row(id_) for id_ in reversed(batch)]
        rows = company.get_all_items_from_ML()
        self.assertEqual([row["id"] for row in rows], ids)
        self.assertEqual([row["body"]["id"] for row in rows], ids)
        self.assertEqual([len(params["ids"].split(",")) for _, params in client.bulk_calls()], [20, 1])
        self.assertTrue(all("include_attributes" not in params for _, params in client.bulk_calls()))
        self.assertEqual(writes, [("meli_local_products_search_last_offset", 0),
                                  ("meli_local_products_search_total", 21)])

    def test_missing_duplicate_or_unrequested_root_ids_cannot_be_complete(self):
        variants = [[], [Client.row("MLA1")], [Client.row("MLA1"), Client.row("MLA1")],
                    [Client.row("MLA1"), Client.row("MLA999")]]
        for rows in variants:
            with self.subTest(rows=rows):
                company, client, writes = fixture(["MLA1", "MLA2"])
                client.reply = lambda batch: rows
                with self.assertRaises(ContractError):
                    company.get_all_items_from_ML()
                self.assertEqual(writes, [], "Failed bulk reads must not advance the search cursor")

    def test_status_body_identity_and_error_envelopes_are_not_success(self):
        variants = [dict(Client.row("MLA1"), status_code=404),
                    dict(Client.row("MLA1"), status_code="200"),
                    dict(Client.row("MLA1"), status_code=True),
                    {"id": "MLA1", "code": 200, "body": {"id": "MLA1"}},
                    dict(Client.row("MLA1"), body={"id": "MLA999"}),
                    dict(Client.row("MLA1"), body={"id": "MLA1", "error": "unavailable"}),
                    dict(Client.row("MLA1"), body=None),
                    {"error": "unavailable"}, "unparseable", {}]
        for row in variants:
            with self.subTest(row=row):
                company, client, _ = fixture(["MLA1"])
                client.reply = lambda batch: [row]
                product = SimpleNamespace(default_code="SKU-1", meli_id=False, product_meli_get_product=Mock())
                try:
                    company.get_all_items_from_existing_sku([product])
                except ContractError:
                    pass
                except Exception as error:
                    self.fail("Malformed API data escaped as uncontrolled " + type(error).__name__)
                else:
                    self.fail("Invalid item response was accepted")
                self.assertFalse(product.meli_id)
                product.product_meli_get_product.assert_not_called()

    def test_outer_http_error_or_malformed_collection_stops_without_cursor_write(self):
        for status, payload in ((206, [Client.row("MLA1")]), (404, [Client.row("MLA1")]),
                                (None, [Client.row("MLA1")]), (200, {"error": "unavailable"}),
                                (200, "invalid")):
            with self.subTest(status=status, payload=payload):
                company, client, writes = fixture(["MLA1"])
                client.status, client.reply = status, lambda batch: payload
                with self.assertRaises(ContractError):
                    company.get_all_items_from_ML()
                self.assertEqual(writes, [])

    def test_second_batch_failure_returns_no_partial_list_and_does_not_advance_cursor(self):
        company, client, writes = fixture(["MLA" + str(i) for i in range(21)])
        client.reply = lambda batch: [Client.row(id_) for id_ in batch] if len(batch) == 20 else []
        with self.assertRaises(ContractError):
            company.get_all_items_from_ML()
        self.assertEqual(writes, [])

    def test_store_attribute_must_be_present_even_when_null_is_a_valid_nonstore(self):
        company, client, _ = fixture()
        client.reply = lambda ids: [Client.row(ids[0], None)]
        self.assertEqual(company.filter_meli_ids(["MLA1"], 99), [])
        client.reply = lambda ids: [{"id": ids[0], "status_code": 200, "body": {"id": ids[0]}}]
        with self.assertRaises(ContractError):
            company.filter_meli_ids(["MLA1"], 99)
