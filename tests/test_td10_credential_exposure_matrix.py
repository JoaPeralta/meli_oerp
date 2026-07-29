# -*- coding: utf-8 -*-
"""TD10 Phase 0: measure who can actually reach the MercadoLibre secrets.

WHAT THIS FILE IS
-----------------
The behavioural half of the TD10 threat model. It states the contract the
connector is supposed to satisfy and, run against the current head, it reports
exactly which parts of that contract are not satisfied yet. Every failure here
is a reproduced vulnerability, not a style complaint.

THE CONTRACT
------------
Secrets, on both models:

    res.company        mercadolibre_access_token, mercadolibre_refresh_token,
                       mercadolibre_code, mercadolibre_secret_key
    mercadolibre.auth  access_token, refresh_token, code

may be read or written directly only by ``base.group_system`` and the
superuser. Not by portal, not by a plain internal user, not by MercadoLibre
Reader or Manager, not by a Sales Manager.

"Directly" is the whole point, and it is why this file pokes at so many
surfaces. Hiding a field in a form proves nothing: the web client reaches the
same data through ``read``, ``search_read``, ``web_read``, ``export_data`` and
``fields_get``, and a search domain leaks a value one comparison at a time
without ever returning it.

WHAT IS NOT ACCEPTABLE AS A FIX
-------------------------------
Returning ``False`` while the field stays readable elsewhere; masking in the
view; filtering in JavaScript; allowing a silent write. The property is that an
unauthorised user cannot obtain, modify **or infer** the value -- while
authorised internal code can still consume it as a capability.

CANARIES
--------
Every value here is obviously fake. No production credential is ever written,
read or printed by this suite.
"""

import logging

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_logger = logging.getLogger(__name__)

_ACCESS_CANARY = "TD10_ACCESS_CANARY"
_REFRESH_CANARY = "TD10_REFRESH_CANARY"
_CODE_CANARY = "TD10_CODE_CANARY"
_SECRET_CANARY = "TD10_CLIENT_SECRET_CANARY"

# res.company facade -> the canary it holds
_COMPANY_SECRETS = {
    "mercadolibre_access_token": _ACCESS_CANARY,
    "mercadolibre_refresh_token": _REFRESH_CANARY,
    "mercadolibre_code": _CODE_CANARY,
    "mercadolibre_secret_key": _SECRET_CANARY,
}
_AUTH_SECRETS = ("access_token", "refresh_token", "code")

# Deliberately NOT secrets in TD10. Restricting these is a separate decision.
_NOT_SECRETS = ("mercadolibre_client_id", "mercadolibre_seller_id",
                "mercadolibre_redirect_uri", "mercadolibre_token_expires_in",
                "mercadolibre_token_refreshed_at",
                "mercadolibre_token_expires_at")

_UNAUTHORISED = ("portal", "internal", "meli_reader", "meli_manager",
                 "sales_manager")


@tagged("post_install", "-at_install")
class TestTd10CredentialExposureMatrix(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.ref("base.main_company")
        cls.company_b = cls.env["res.company"].create({"name": "TD10 Company B"})

        cls.company_a.write({
            "mercadolibre_seller_id": "2288636236",
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": _SECRET_CANARY,
            "mercadolibre_access_token": _ACCESS_CANARY,
            "mercadolibre_refresh_token": _REFRESH_CANARY,
            "mercadolibre_code": _CODE_CANARY,
        })
        cls.env.flush_all()

        cls.actors = {}
        for key, xmlids in (
            ("portal", ["base.group_portal"]),
            ("internal", ["base.group_user"]),
            ("meli_reader", ["base.group_user",
                             "meli_oerp.group_mercadolibre_reader"]),
            ("meli_manager", ["base.group_user",
                              "meli_oerp.group_mercadolibre_manager"]),
            ("sales_manager", ["base.group_user",
                               "sales_team.group_sale_manager"]),
            ("system", ["base.group_user", "base.group_system"]),
        ):
            groups = cls.env["res.groups"]
            for xmlid in xmlids:
                rec = cls.env.ref(xmlid, raise_if_not_found=False)
                if rec:
                    groups |= rec
            if len(groups) != len(xmlids):
                continue
            cls.actors[key] = cls.env["res.users"].create({
                "name": "TD10 %s" % key,
                "login": "td10_%s" % key,
                "company_id": cls.company_a.id,
                "company_ids": [(6, 0, [cls.company_a.id])],
                "group_ids": [(6, 0, groups.ids)],
            })

    # ------------------------------------------------------------------
    def _as(self, actor):
        return self.company_a.with_user(self.actors[actor])

    def _auth_as(self, actor):
        return self.env["mercadolibre.auth"].with_user(self.actors[actor])

    def _leaks(self, value):
        """Did a canary come back in any shape?"""
        return isinstance(value, str) and value in (
            _ACCESS_CANARY, _REFRESH_CANARY, _CODE_CANARY, _SECRET_CANARY)

    # ==================================================================
    # positive controls -- if these fail the suite proves nothing
    # ==================================================================
    def test_superuser_reads_the_exact_canaries(self):
        """Anti-vacuity: the canaries really are in place."""
        data = self.company_a.read(list(_COMPANY_SECRETS))[0]
        for field, canary in _COMPANY_SECRETS.items():
            self.assertEqual(data[field], canary,
                             "the canary is not where the matrix assumes")

    def test_system_administrator_can_still_read_the_secrets(self):
        data = self._as("system").read(list(_COMPANY_SECRETS))[0]
        for field, canary in _COMPANY_SECRETS.items():
            self.assertEqual(
                data[field], canary,
                "System Administrator lost access to %s; the protection is "
                "too broad" % field)

    def test_system_administrator_can_still_write_a_secret(self):
        company = self._as("system")
        company.write({"mercadolibre_access_token": "TD10_ACCESS_CANARY_2"})
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.company_a.mercadolibre_access_token,
                         "TD10_ACCESS_CANARY_2",
                         "System Administrator can no longer configure the "
                         "connector")
        self.company_a.write({"mercadolibre_access_token": _ACCESS_CANARY})
        self.env.flush_all()

    def test_the_non_secret_configuration_stays_readable(self):
        """TD10 restricts four fields, not the whole configuration."""
        data = self._as("meli_manager").read(list(_NOT_SECRETS))[0]
        self.assertEqual(data["mercadolibre_seller_id"], "2288636236",
                         "the seller id was restricted; that is a separate "
                         "decision, not TD10")

    # ==================================================================
    # read surfaces on res.company
    # ==================================================================
    def _assert_field_unreachable(self, actor, field, surface, getter):
        """A surface is safe if it errors, or omits the field entirely.

        Returning False while the value is still reachable elsewhere is not
        protection, so the caller checks the real value separately.
        """
        try:
            with self.env.cr.savepoint():
                value = getter()
        except (AccessError, UserError):
            # Rechazo explicito: resultado aceptable para esta superficie.
            return
        self.assertFalse(
            self._leaks(value),
            "%s obtained %s through %s" % (actor, field, surface))

    # ==================================================================
    # the matrix itself, reported rather than asserted
    # ==================================================================
    def _probe(self, getter):
        """Classify one cell: what does this actor actually get here?

        Distinguishing DENIED from ABSENT from LEAK matters. A test that only
        asserts 'no canary came back' cannot tell protection from an unrelated
        AccessError, and would report a surface as safe for the wrong reason.
        """
        # Cada celda va en su propio savepoint. Buscar por un campo calculado
        # no almacenado llega a PostgreSQL y deja la transaccion abortada: sin
        # esto, la primera celda invalida se lleva puesta toda la corrida y no
        # queda ni resultado que leer.
        try:
            with self.env.cr.savepoint():
                value = getter()
        except AccessError:
            return "DENIED(access)"
        except UserError:
            return "DENIED(user)"
        except Exception as exc:
            return "ERROR(%s)" % type(exc).__name__
        if self._leaks(value):
            return "*** LEAK ***"
        if value is None:
            return "ABSENT"
        if value is False or value == "":
            return "EMPTY"
        return "value"

    def test_report_the_exposure_matrix(self):
        """Phase 0 instrument. Asserts nothing; prints what is true today."""
        Company = self.env["res.company"]
        rows = []
        for actor in list(_UNAUTHORISED) + ["system"]:
            if actor not in self.actors:
                continue
            user = self.actors[actor]
            company = self.company_a.with_user(user)
            model = Company.with_user(user)
            for field in _COMPANY_SECRETS:
                canary = _COMPANY_SECRETS[field]
                cells = {
                    "read": lambda f=field, c=company: c.read([f])[0].get(f),
                    "search_read": lambda f=field, m=model: (
                        m.search_read([("id", "=", self.company_a.id)], [f])
                        or [{}])[0].get(f),
                    "web_read": lambda f=field, c=company: (
                        c.web_read({f: {}}) or [{}])[0].get(f),
                    "export": lambda f=field, c=company: (
                        (c.export_data([f]).get("datas") or [[None]])[0]
                        or [None])[0],
                    "fields_get": lambda f=field, m=model: (
                        canary if f in m.fields_get() else None),
                    "domain=": lambda f=field, m=model, k=canary: (
                        canary if self.company_a in m.search([(f, "=", k)])
                        else None),
                    "srch_count": lambda f=field, m=model, k=canary: (
                        canary if m.search_count([(f, "=", k)]) else None),
                }
                for surface, getter in cells.items():
                    rows.append("  %-14s %-32s %-12s %s"
                                % (actor, field, surface, self._probe(getter)))
        _logger.info("TD10 MATRIX BEGIN\n%s\nTD10 MATRIX END", "\n".join(rows))

    def test_read_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                self._assert_field_unreachable(
                    actor, field, "read()",
                    lambda a=actor, f=field: self._as(a).read([f])[0].get(f))

    def test_search_read_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                self._assert_field_unreachable(
                    actor, field, "search_read()",
                    lambda a=actor, f=field: (
                        self.env["res.company"].with_user(self.actors[a])
                        .search_read([("id", "=", self.company_a.id)], [f])
                        or [{}])[0].get(f))

    def test_web_read_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                self._assert_field_unreachable(
                    actor, field, "web_read()",
                    lambda a=actor, f=field: (
                        self._as(a).web_read({f: {}}) or [{}])[0].get(f))

    def test_web_search_read_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                self._assert_field_unreachable(
                    actor, field, "web_search_read()",
                    lambda a=actor, f=field: (
                        self.env["res.company"].with_user(self.actors[a])
                        .web_search_read([("id", "=", self.company_a.id)],
                                         {f: {}}).get("records") or [{}]
                    )[0].get(f))

    def test_export_data_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                def getter(a=actor, f=field):
                    rows = self._as(a).export_data([f]).get("datas") or [[]]
                    return rows[0][0] if rows and rows[0] else None
                self._assert_field_unreachable(actor, field, "export_data()",
                                               getter)

    def test_fields_get_hides_the_secrets(self):
        """A field an actor may not read must not be advertised to them."""
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            described = self.env["res.company"].with_user(
                self.actors[actor]).fields_get()
            for field in _COMPANY_SECRETS:
                # assertTrue y no assertNotIn: al fallar, assertNotIn formatea
                # el contenedor entero, y fields_get() de res.company son
                # cientos de campos con su help. Ese mensaje mata el proceso.
                self.assertTrue(
                    field not in described,
                    "%s sees %s in fields_get(), so the web client will ask "
                    "for it" % (actor, field))

    def test_default_get_does_not_expose_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                self._assert_field_unreachable(
                    actor, field, "default_get()",
                    lambda a=actor, f=field: self.env["res.company"]
                    .with_user(self.actors[a]).default_get([f]).get(f))

    # ==================================================================
    # write
    # ==================================================================
    def test_unauthorised_actors_cannot_write_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            for field in _COMPANY_SECRETS:
                try:
                    with self.env.cr.savepoint():
                        self._as(actor).write({field: "TD10_OVERWRITTEN"})
                        self.env.flush_all()
                except Exception:
                    self.env.invalidate_all()
                    continue
                self.env.invalidate_all()
                self.assertNotEqual(
                    self.company_a[field], "TD10_OVERWRITTEN",
                    "%s silently overwrote %s" % (actor, field))

    # ==================================================================
    # inference: a domain leaks a value without ever returning it
    # ==================================================================
    def test_domains_cannot_be_used_as_an_oracle(self):
        """Searching by a secret answers 'is it this value?' one guess at a
        time. That is disclosure even though nothing is returned."""
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            Company = self.env["res.company"].with_user(self.actors[actor])
            for field, canary in _COMPANY_SECRETS.items():
                for domain in (
                    [(field, "=", canary)],
                    [(field, "!=", False)],
                    [(field, "ilike", canary[:10])],
                ):
                    try:
                        with self.env.cr.savepoint():
                            found = Company.search(domain)
                    except Exception:
                        continue
                    self.assertNotIn(
                        self.company_a, found,
                        "%s can confirm the value of %s with domain %s"
                        % (actor, field, domain))

    def test_search_count_cannot_be_used_as_an_oracle(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            Company = self.env["res.company"].with_user(self.actors[actor])
            for field, canary in _COMPANY_SECRETS.items():
                try:
                    with self.env.cr.savepoint():
                        count = Company.search_count([(field, "=", canary)])
                except Exception:
                    continue
                self.assertEqual(
                    count, 0,
                    "%s can confirm %s through search_count" % (actor, field))

    # ==================================================================
    # the auth model itself
    # ==================================================================
    def test_the_auth_model_stays_unreachable(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            try:
                with self.env.cr.savepoint():
                    rows = self._auth_as(actor).search([])
                    values = rows.read(list(_AUTH_SECRETS))
            except Exception:
                continue
            for row in values:
                for field in _AUTH_SECRETS:
                    self.assertFalse(
                        self._leaks(row.get(field)),
                        "%s read mercadolibre.auth.%s directly"
                        % (actor, field))

    def test_the_auth_model_fields_get_hides_the_secrets(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            try:
                described = self._auth_as(actor).fields_get()
            except AccessError:
                continue
            for field in _AUTH_SECRETS:
                self.assertTrue(
                    field not in described,
                    "%s sees mercadolibre.auth.%s in fields_get()"
                    % (actor, field))

    # ==================================================================
    # the settings screen is a second door onto the same values
    # ==================================================================
    def test_the_settings_screen_does_not_expose_the_secrets(self):
        """res.config.settings mirrors three of the four as related fields."""
        mirrored = ("mercadolibre_secret_key", "mercadolibre_access_token",
                    "mercadolibre_refresh_token")
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            Settings = self.env["res.config.settings"].with_user(
                self.actors[actor])
            try:
                described = Settings.fields_get()
            except AccessError:
                continue
            for field in mirrored:
                self.assertTrue(
                    field not in described,
                    "%s sees res.config.settings.%s, a related mirror of a "
                    "secret" % (actor, field))

    def test_the_settings_screen_default_get_does_not_expose_the_secrets(self):
        """Asking Settings for the default of a restricted field.

        A non-system user gets a KeyError: the field is not in their field set
        at all, which is the strongest form of "absent" the contract allows.
        The positive control below proves that is the restriction talking and
        not a pre-existing crash -- for a system user the very same call
        answers normally.
        """
        mirrored = ("mercadolibre_secret_key", "mercadolibre_access_token",
                    "mercadolibre_refresh_token")

        for field in mirrored:
            try:
                self.env["res.config.settings"].with_user(
                    self.actors["system"]).default_get([field])
            except KeyError:
                self.fail("default_get(%s) raises for System Administrator "
                          "too, so the KeyError below is not the restriction "
                          "and this test would prove nothing" % field)

        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            Settings = self.env["res.config.settings"].with_user(
                self.actors[actor])
            for field in mirrored:
                try:
                    with self.env.cr.savepoint():
                        value = Settings.default_get([field]).get(field)
                except (AccessError, UserError, KeyError):
                    continue
                self.assertFalse(
                    self._leaks(value),
                    "%s obtained %s through res.config.settings.default_get()"
                    % (actor, field))

    def test_the_settings_screen_cannot_be_read_into(self):
        """The web client opens Settings with create + web_read."""
        mirrored = ("mercadolibre_secret_key", "mercadolibre_access_token",
                    "mercadolibre_refresh_token")
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            Settings = self.env["res.config.settings"].with_user(
                self.actors[actor])
            for field in mirrored:
                def getter(f=field, s=Settings):
                    record = s.create({})
                    return record.read([f])[0].get(f)
                self._assert_field_unreachable(
                    actor, field, "res.config.settings.read()", getter)

    # ==================================================================
    # the superuser keeps administrative access
    # ==================================================================
    def test_the_superuser_keeps_administrative_access(self):
        """Restricting a field must not lock out the one account that has to
        be able to repair the connector."""
        root = self.env["res.company"].with_user(1).browse(self.company_a.id)
        data = root.read(list(_COMPANY_SECRETS))[0]
        for field, canary in _COMPANY_SECRETS.items():
            self.assertEqual(data[field], canary,
                             "the superuser lost access to %s" % field)

    def test_the_auth_row_stays_reachable_for_system(self):
        rows = self.env["mercadolibre.auth"].with_user(
            self.actors["system"]).search([("company_id", "=",
                                            self.company_a.id)])
        self.assertTrue(rows, "System Administrator cannot see the auth row")
        self.assertEqual(rows[0].access_token, _ACCESS_CANARY,
                         "System Administrator cannot read the auth row")
