# -*- coding: utf-8 -*-
"""Unit tests for ``MeliApiNoSDK._parse_response``.

The method used to read an HTTP error with an EMPTY body as success: ML
sometimes answers 429 (and occasionally 5xx) with no body at all, `resp.json()`
raised, and the old fallback returned `resp.text` unconditionally -- i.e. `''`.
Every caller decides success with `if rjson and "error" in rjson`, and `''` is
falsy, so the connector reported the push as done without ever having written
anything to ML, and without a single log line. The fix (ported from
ctmil/meli_oerp a24b8179) turns a >=400 status with an empty body into an
explicit error dict and logs a warning. It shipped with no test; this is it.

``models/meli_util.py`` imports Odoo, psycopg2, pytz and requests at module
level (unlike ``meli_webhook_security.py`` or ``meli_shipment_format.py``, which
are Odoo-free), so it cannot be plain-imported without a full Odoo install.
Under the Odoo test runner this file imports the real ``MeliApiNoSDK`` class
normally. Standalone, it pulls the *exact* source of ``_parse_response`` out of
the real file with ``ast`` and execs only that -- never a hand-copied
re-implementation, so a regression in the real method still fails this suite.

Runs both under the Odoo test runner and as a plain ``python -m unittest`` /
direct execution (no Odoo, no network, no database, no credentials).
"""

import json
import logging
import os
import textwrap
import unittest

try:  # under the Odoo test runner: import the real class directly
    from odoo.addons.meli_oerp.models.meli_util import MeliApiNoSDK
    from odoo.tests.common import TransactionCase as _TestBase
    from odoo.tests import tagged

    def _load_parse_response():
        return MeliApiNoSDK._parse_response, None

except Exception:  # standalone (no Odoo available): extract the method by ast
    import ast

    _MODELS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
    )
    _MELI_UTIL_PATH = os.path.join(_MODELS_DIR, "meli_util.py")

    def _load_parse_response(source_path=_MELI_UTIL_PATH):
        """Pull ``MeliApiNoSDK._parse_response`` out of the real file with ast.

        Does NOT re-implement the method: it parses ``models/meli_util.py``,
        finds the ``_parse_response`` def inside the ``MeliApiNoSDK`` class,
        and execs that exact source slice. If the real implementation changes
        -- fixed further, or regressed back to the old behaviour -- this picks
        up the change on the next run, because it reads the file every time.

        Returns ``(function, source_text)`` so a test can also assert the
        extracted text really came from the real file (see
        ``test_loaded_source_is_the_real_method_not_a_stand_in`` below).
        """
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=source_path)

        class_node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "MeliApiNoSDK"),
            None,
        )
        if class_node is None:
            raise AssertionError(
                "MeliApiNoSDK class not found in %s -- has it moved or been "
                "renamed?" % source_path
            )

        method_node = next(
            (n for n in class_node.body
             if isinstance(n, ast.FunctionDef) and n.name == "_parse_response"),
            None,
        )
        if method_node is None:
            raise AssertionError(
                "_parse_response not found on MeliApiNoSDK in %s -- has it "
                "moved or been renamed?" % source_path
            )

        method_source = textwrap.dedent(ast.get_source_segment(source, method_node))

        # Only `_logger` is needed: the method references nothing else from
        # meli_util.py's module scope (no odoo/psycopg2/pytz/requests calls
        # inside `_parse_response` itself).
        namespace = {"_logger": logging.getLogger("meli_oerp.models.meli_util")}
        exec(compile(method_source, source_path, "exec"), namespace)
        return namespace["_parse_response"], method_source

    # No Odoo: fall back to a plain TestCase and a no-op tag decorator so the
    # file still runs with `python tests/test_meli_parse_response.py`.
    _TestBase = unittest.TestCase

    def tagged(*args, **kwargs):
        return lambda cls: cls


class _FakeResponse:
    """Minimal ``requests``-like response: only what ``_parse_response`` reads."""

    def __init__(self, status_code, text, url="https://api.mercadolibre.com/fake"):
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        # Mirrors requests.Response.json(): parses .text, raises (a ValueError
        # subclass) when it isn't valid JSON -- including the empty string,
        # which is exactly the shape ML sends on an empty-body 429/5xx.
        return json.loads(self.text)


@tagged("post_install", "-at_install")
class TestParseResponse(_TestBase):

    @classmethod
    def setUpClass(cls):
        super(TestParseResponse, cls).setUpClass()
        fn, cls._source = _load_parse_response()
        # staticmethod: plain attribute access on a function object would
        # otherwise auto-bind it to the TestCase instance (descriptor
        # protocol), turning `self._parse_response(None, resp)` into a 3-arg
        # call. staticmethod keeps it a plain 2-arg (self, resp) callable.
        cls._parse_response = staticmethod(fn)

    def _call(self, resp):
        # The method's own `self` is never read inside `_parse_response`; any
        # placeholder works.
        return self._parse_response(None, resp)

    # --- proof this is the real method, not a stand-in ------------------

    def test_loaded_source_is_the_real_method_not_a_stand_in(self):
        """The distinctive docstring text only exists in models/meli_util.py.

        If this assertion is checking a hand-written re-implementation instead
        of the real file, it fails immediately -- it can only pass against the
        actual source (or a real Odoo import of the actual class).
        """
        doc = type(self)._parse_response.__doc__
        self.assertIsNotNone(doc)
        self.assertIn("Portado de ctmil/meli_oerp", doc)
        self.assertIn("dict de error explicito", doc)

    # --- ordinary success: unaffected by the fix -------------------------

    def test_200_with_valid_json_returns_parsed_dict_unchanged(self):
        resp = _FakeResponse(200, json.dumps({"id": 123, "status": "paid"}))
        self.assertEqual(self._call(resp), {"id": 123, "status": "paid"})

    def test_400_with_json_error_body_returns_that_json_unchanged(self):
        """The API's own error wins -- _parse_response must not overwrite it."""
        body = {"error": "bad_request", "message": "invalid attribute", "status": 400}
        resp = _FakeResponse(400, json.dumps(body))
        self.assertEqual(self._call(resp), body)

    # --- the defect: an HTTP error with an EMPTY body --------------------

    def test_429_with_empty_body_is_reported_as_error_not_empty_string(self):
        resp = _FakeResponse(429, "")
        with self.assertLogs(level="WARNING") as log_ctx:
            result = self._call(resp)

        # Every caller decides success/failure with exactly this idiom:
        #     if rjson and "error" in rjson:
        # '' is falsy, so before the fix a 429-with-empty-body satisfied
        # neither half and was silently read as success. Assert the same
        # predicate the callers use, not just "is a dict".
        self.assertTrue(result and "error" in result)
        self.assertTrue(any("WARNING" in line for line in log_ctx.output))

    def test_500_with_empty_body_is_reported_as_error_not_empty_string(self):
        resp = _FakeResponse(500, "")
        with self.assertLogs(level="WARNING") as log_ctx:
            result = self._call(resp)

        self.assertTrue(result and "error" in result)
        self.assertTrue(any("WARNING" in line for line in log_ctx.output))

    # --- non-JSON body with real text: pin current behaviour -------------

    def test_non_json_text_status_200_returns_text_unchanged(self):
        """No regression: a plain-text 200 body still comes back as text."""
        resp = _FakeResponse(200, "Service temporarily unavailable")
        self.assertEqual(self._call(resp), "Service temporarily unavailable")

    def test_non_json_text_status_500_returns_the_text_not_an_error_dict(self):
        """Pinned, not prescribed.

        The fix only substitutes an error dict when the body is EMPTY. A
        non-JSON body that actually has text is returned as-is even at
        status >= 400, same as before the fix -- the guard is
        `if not texto and status >= 400`, so non-empty `texto` always wins.
        """
        resp = _FakeResponse(500, "<html>Internal Server Error</html>")
        self.assertEqual(self._call(resp), "<html>Internal Server Error</html>")

    # --- an empty body with a non-error status must NOT become an error --

    def test_empty_body_status_200_is_not_turned_into_an_error(self):
        resp = _FakeResponse(200, "")
        result = self._call(resp)
        self.assertEqual(result, "")
        # Same caller idiom as above, this time it must read as "no error".
        self.assertFalse(result and "error" in result)


if __name__ == "__main__":
    unittest.main()
