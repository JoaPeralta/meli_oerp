# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2004-2009 Tiny SPRL (<http://tiny.be>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from odoo import fields, models, api
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.translate import _
import hashlib
import hmac
import json
import logging
import re

#from ..melisdk.meli import Meli

import logging
_logger = logging.getLogger(__name__)

from .meli_oerp_config import *
from dateutil.parser import *
from datetime import *

from . import versions
from .versions import *
from .meli_posting_metadata import (
    CORE_FIELDS, ITEM_ID, METADATA_FIELDS, metadata_evidence, numeric_id, read_item_metadata,
)


class mercadolibre_posting_update(models.TransientModel):
    _name = "mercadolibre.posting.update"
    _description = "Refresh Posting Metadata"

    def posting_update(self):
        self.ensure_one()
        self.check_access("write")
        if self.env.context.get("active_model") != "mercadolibre.posting":
            raise ValidationError(_("Select explicit Mercado Libre postings."))
        result = self.env["mercadolibre.posting"]._meli_refresh_metadata(
            self.env.context.get("active_ids"),
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Posting metadata refresh"),
                "message": _("Updated: %(updated)s. Unchanged: %(unchanged)s. Skipped: %(skipped)s.",
                             **result),
                "type": "warning" if result["skipped"] else "success",
                "sticky": bool(result["skipped"]),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }


class mercadolibre_posting(models.Model):
    _name = "mercadolibre.posting"
    _description = "Posting en MercadoLibre"

    @api.model
    def _meli_binding_error(self):
        return ValidationError(_("Mercado Libre product binding is ambiguous or unavailable."))

    @api.model
    def _meli_variation_key(self, variation_id):
        return str(variation_id) if variation_id else False

    @api.model
    def _meli_check_binding_product(self, product, company):
        if not product:
            return product
        if len(product) != 1:
            raise self._meli_binding_error()
        product = product.with_env(self.env)
        try:
            product.check_access("read")
            if not product.active or (product.company_id and product.company_id != company):
                raise self._meli_binding_error()
        except AccessError:
            raise self._meli_binding_error() from None
        return product

    @api.model
    def _meli_find_exact_posting(self, item_id, variation_id, company):
        """Find the visible exact row, including a row awaiting its product."""
        if company not in self.env.companies:
            raise self._meli_binding_error()
        try:
            result = self.search([
                ("meli_id", "=", item_id),
                ("meli_variation_id", "=", self._meli_variation_key(variation_id)),
            ], limit=2)
            if len(result) > 1:
                raise self._meli_binding_error()
            if result:
                self._meli_check_binding_product(result.product_id, company)
        except AccessError:
            raise self._meli_binding_error() from None
        return result

    @api.model
    def _meli_find_catalog_product(self, company, domain):
        """Include archived candidates so an obsolete identity cannot be reused."""
        if company not in self.env.companies:
            raise self._meli_binding_error()
        try:
            result = self.env["product.product"].with_context(active_test=False).search(
                [("company_id", "in", [False, company.id])] + domain, limit=2,
            )
        except AccessError:
            raise self._meli_binding_error() from None
        return self._meli_check_binding_product(result, company)

    @api.model
    def _meli_find_direct_product(self, item_id, variation_id, company):
        product = self._meli_find_catalog_product(company, [
            ("meli_id", "=", item_id),
            ("meli_id_variation", "=", self._meli_variation_key(variation_id)),
        ])
        return self._meli_check_fallback_variant(product, item_id, variation_id)

    @api.model
    def _meli_find_reference_product(self, reference, company, barcode=False):
        if not isinstance(reference, str) or not reference:
            return self.env["product.product"]
        # The external SKU is a literal, not a SQL LIKE pattern.
        reference = reference.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        domain = [("default_code", "=ilike", reference)]
        if barcode:
            domain = ["|"] + domain + [("barcode", "=ilike", reference)]
        return self._meli_find_catalog_product(company, domain)

    @api.model
    def _meli_check_fallback_variant(self, product, item_id, variation_id):
        if not product:
            return product
        key = self._meli_variation_key(variation_id)
        if ((product.meli_id == item_id and (product.meli_id_variation or False) != key)
                or self.search_count([
                    ("meli_id", "=", item_id), ("product_id", "=", product.id),
                    ("meli_variation_id", "!=", key),
                ], limit=1)):
            raise self._meli_binding_error()
        return product

    @api.model
    def _meli_can_import_variant(self, product, item_id, variation_id, company, fallback_match):
        key = self._meli_variation_key(variation_id)
        if not key:
            return False
        try:
            posting = self._meli_find_exact_posting(item_id, key, company)
            if posting.product_id:
                return posting.product_id == product
            if not fallback_match or (product.meli_id and product.meli_id != item_id):
                return False
            if product.meli_id_variation and product.meli_id_variation != key:
                return False
            direct = self._meli_find_direct_product(item_id, key, company)
            return (not direct or direct == product) and not self.search_count([
                ("meli_id", "=", item_id), ("product_id", "=", product.id),
                ("meli_variation_id", "not in", [False, key]),
            ], limit=1)
        except ValidationError:
            return False

    def _meli_bind_product(self, product, company):
        """Fill an existing blank posting without overwriting a concurrent binding."""
        self.ensure_one()
        self._meli_check_binding_product(product, company)
        try:
            self.check_access("write")
            self.env.cr.execute("SELECT id FROM mercadolibre_posting WHERE id = %s FOR UPDATE", [self.id])
            if not self.env.cr.fetchone():
                raise self._meli_binding_error()
            self.invalidate_recordset(["product_id"])
            if self.product_id and self.product_id != product:
                raise self._meli_binding_error()
            if not self.product_id:
                self.product_id = product
        except AccessError:
            raise self._meli_binding_error() from None

    @api.depends("meli_status", "meli_permalink", "meli_price")
    def _posting_update(self):
        for posting in self:
            posting.posting_update = (
                "stored" if posting.meli_status and posting.meli_permalink and posting.meli_price
                else "incomplete"
            )

    def _meli_fill_imported_metadata(self, metadata):
        """Complete an existing link without replacing known values."""
        self.ensure_one()
        changes = {name: value for name, value in metadata.items() if not self[name]}
        if changes:
            self.write(changes)

    @api.model
    def _meli_metadata_selection(self, posting_ids):
        if not self.env.user.has_group("meli_oerp.group_mercadolibre_manager"):
            raise AccessError(_("Mercado Libre manager access is required."))
        if (not isinstance(posting_ids, (list, tuple)) or not 1 <= len(posting_ids) <= 50
                or any(type(row_id) is not int or row_id < 1 for row_id in posting_ids)
                or len(set(posting_ids)) != len(posting_ids)):
            raise ValidationError(_("Select between 1 and 50 unique posting records."))
        rows = self.browse(sorted(posting_ids)).exists()
        if len(rows) != len(posting_ids):
            raise self._meli_binding_error()
        rows.check_access("read")
        rows.check_access("write")
        return rows

    def _meli_metadata_binding(self, company, identity=None):
        """Require company-scoped and independently corroborated product identity."""
        self.ensure_one()
        product = self._meli_check_binding_product(self.product_id, company)
        if not product or product.company_id != company or not ITEM_ID.fullmatch(self.meli_id or ""):
            raise self._meli_binding_error()
        if self._meli_find_exact_posting(self.meli_id, self.meli_variation_id, company) != self:
            raise self._meli_binding_error()
        if identity is None:
            return product
        if identity["listing_with_variants"] or identity["sku_state"] == "invalid":
            raise self._meli_binding_error()
        self._meli_check_fallback_variant(product, self.meli_id, self.meli_variation_id)
        direct = self._meli_find_direct_product(self.meli_id, self.meli_variation_id, company)
        reference = (self._meli_find_reference_product(identity["sku"], company)
                     if identity["sku_kind"] in ("attribute", "sku") else None)
        remote_up = identity["user_product_id"]
        local_up = product.meli_user_product_id
        if ((direct and direct != product) or (reference and reference != product)
                or (local_up and remote_up and local_up != remote_up)):
            raise self._meli_binding_error()
        if direct == product or reference == product:
            return product
        if remote_up and local_up == remote_up:
            same_up = self._meli_find_catalog_product(company, [("meli_user_product_id", "=", remote_up)])
            if same_up == product:
                return product
        raise self._meli_binding_error()

    @api.model
    def _meli_metadata_reader(self, company):
        """Build a read capability only; never refresh or return credentials."""
        if not numeric_id(company.mercadolibre_seller_id):
            raise UserError(_("The active company does not have a verified seller configuration."))
        util = self.env["meli.util"]
        if util._meli_is_neutralized():
            raise UserError(_("Posting metadata requests are disabled in a neutralized database."))
        # _build_client only loads the restricted current-company capability.
        # Its network methods are deliberately not used here.
        token = util._build_client(company).access_token
        return lambda item_id: read_item_metadata(item_id, token)

    def _meli_metadata_lock(self, product):
        self.ensure_one()
        # Use the same order in metadata actions: posting, then product. A
        # concurrent committed modification cannot silently change this plan.
        self.env.cr.execute("SELECT id FROM mercadolibre_posting WHERE id = %s FOR UPDATE NOWAIT", [self.id])
        if not self.env.cr.fetchone():
            raise self._meli_binding_error()
        self.env.cr.execute("SELECT id FROM product_product WHERE id = %s FOR UPDATE NOWAIT", [product.id])
        if not self.env.cr.fetchone():
            raise self._meli_binding_error()
        product.invalidate_recordset()
        template = product.product_tmpl_id
        self.env.cr.execute("SELECT id FROM product_template WHERE id = %s FOR UPDATE NOWAIT", [template.id])
        if not self.env.cr.fetchone():
            raise self._meli_binding_error()
        self.invalidate_recordset()
        template.invalidate_recordset()

    @api.model
    def _meli_metadata_lock_company(self, company):
        self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE NOWAIT", [company.id])
        if not self.env.cr.fetchone():
            raise self._meli_binding_error()
        company.invalidate_recordset(["mercadolibre_seller_id"])

    @api.model
    def _meli_refresh_metadata(self, posting_ids):
        """Ordinary refresh of complete offers, not a historical backfill."""
        from psycopg2 import errors

        rows = self._meli_metadata_selection(posting_ids)
        company = self.env.company
        counts = {"updated": 0, "unchanged": 0, "skipped": 0}
        read_item = None
        evidence = {}
        pending = []
        for posting in rows:
            try:
                # Historical core gaps require a separately reviewed repair.
                if not all(posting[name] for name in CORE_FIELDS):
                    counts["skipped"] += 1
                    continue
                product = posting._meli_metadata_binding(company)
                read_item = read_item or self._meli_metadata_reader(company)
                key = (posting.meli_id, posting.meli_variation_id, company.mercadolibre_seller_id)
                if key[0] not in evidence:
                    evidence[key[0]] = read_item(key[0])
                item, failure = evidence[key[0]]
                if failure:
                    counts["skipped"] += 1
                    continue
                _values, states, _identity, failure = metadata_evidence(
                    item, *key,
                )
                if failure or any(states[name] != "valid" for name in CORE_FIELDS):
                    counts["skipped"] += 1
                    continue
                pending.append((posting, product, key, item))
            except (AccessError, ValidationError):
                counts["skipped"] += 1
        # Never wait for remote I/O while holding a database row lock.
        for posting, product, key, item in pending:
            try:
                with self.env.cr.savepoint():
                    self._meli_metadata_lock_company(company)
                    posting._meli_metadata_lock(product)
                    if ((posting.meli_id, posting.meli_variation_id, company.mercadolibre_seller_id) != key
                            or not all(posting[name] for name in CORE_FIELDS)):
                        raise self._meli_binding_error()
                    # Read identity again after locking so a changed variation
                    # cannot receive a previous variant's price or SKU.
                    values, _states, identity, _failure = metadata_evidence(item, *key)
                    if posting._meli_metadata_binding(company, identity) != product:
                        raise self._meli_binding_error()
                    changes = {name: value for name, value in values.items() if posting[name] != value}
                    if changes:
                        posting.write(changes)
                counts["updated" if changes else "unchanged"] += 1
            except (AccessError, ValidationError, errors.LockNotAvailable, errors.SerializationFailure):
                counts["skipped"] += 1
        return counts

    def _meli_repair_entry(self, company, item, fetch_failure):
        """Produce a sanitized report and an internal snapshot for one row."""
        self.ensure_one()
        report = {"id": self.id, "result": "skipped"}
        if ITEM_ID.fullmatch(self.meli_id or ""):
            report["item"] = self.meli_id
        variation = numeric_id(self.meli_variation_id)
        if variation:
            report["variation"] = variation
        snapshot = {
            "item": self.meli_id, "variation": self.meli_variation_id,
            "product": self.product_id.id,
            "write_date": str(self.write_date or ""),
            "fields": {name: self[name] for name in METADATA_FIELDS},
        }
        try:
            product = self._meli_metadata_binding(company)
        except (AccessError, ValidationError):
            report["reason"] = "binding_unproven"
            return report, snapshot, {}, None
        if all(snapshot["fields"].values()):
            report["result"] = "unchanged"
            report["reason"] = "no_missing_fields"
            return report, snapshot, {}, product
        if fetch_failure:
            report["reason"] = fetch_failure
            return report, snapshot, {}, product
        values, states, identity, failure = metadata_evidence(
            item, self.meli_id, self.meli_variation_id, company.mercadolibre_seller_id,
        )
        if failure:
            report["reason"] = failure
            return report, snapshot, {}, product
        try:
            if self._meli_metadata_binding(company, identity) != product:
                raise self._meli_binding_error()
        except (AccessError, ValidationError):
            report["reason"] = "binding_unproven"
            return report, snapshot, {}, product
        snapshot["proof"] = {
            "remote": identity, "direct_item": product.meli_id, "direct_variation": product.meli_id_variation,
            "sku": product.default_code, "user_product_id": product.meli_user_product_id,
            "template": product.product_tmpl_id.id,
        }
        changes = {name: value for name, value in values.items() if not snapshot["fields"][name]}
        report["field_states"] = states
        if changes:
            report["result"] = "proposed"
            report["changes"] = {
                name: {"before": None, "proposed": value if name in CORE_FIELDS else "<redacted>"}
                for name, value in changes.items()
            }
        else:
            report["result"] = "unchanged"
            report["reason"] = "no_valid_missing_values"
        return report, snapshot, changes, product

    @api.model
    def _meli_repair_metadata(self, posting_ids, *, dry_run=True, reviewed_plan=None):
        """Server-only, fill-only repair; no implicit commit or OAuth refresh.

        Apply requires a maintenance window excluding catalogue/binding writers.
        The row locks protect selected records and their existing owners; they
        cannot exclude a new duplicate key inserted by another transaction.
        """
        from psycopg2 import errors

        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("System administrator access is required for historical repair."))
        if (type(dry_run) is not bool
                or (dry_run and reviewed_plan is not None)
                or (not dry_run and (not isinstance(reviewed_plan, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", reviewed_plan)))):
            raise ValidationError(_("Apply requires the exact reviewed metadata preview."))
        rows = self._meli_metadata_selection(posting_ids)
        company = self.env.company
        read_item = None
        evidence = {}
        for posting in rows:
            try:
                posting._meli_metadata_binding(company)
            except (AccessError, ValidationError):
                continue
            if all(posting[name] for name in METADATA_FIELDS) or posting.meli_id in evidence:
                continue
            read_item = read_item or self._meli_metadata_reader(company)
            evidence[posting.meli_id] = read_item(posting.meli_id)

        def collect():
            entries = []
            for posting in rows:
                item, failure = evidence.get(posting.meli_id, (None, "snapshot_unavailable"))
                entries.append(posting._meli_repair_entry(company, item, failure))
            # Include exact internal values in the digest without returning them.
            identity = [
                "posting-metadata-v1", self.env.cr.dbname, self.env.uid, company.id,
                company.mercadolibre_seller_id,
                [(report, snapshot, changes) for report, snapshot, changes, _product in entries],
            ]
            plan = hashlib.sha256(json.dumps(
                identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            ).encode()).hexdigest()
            return plan, entries

        plan, entries = collect()
        if not dry_run:
            stale = _("The metadata repair plan changed or is busy. Create a new preview.")
            if not hmac.compare_digest(plan, reviewed_plan):
                raise ValidationError(stale)
            try:
                with self.env.cr.savepoint():
                    self._meli_metadata_lock_company(company)
                    for posting, (_report, _snapshot, _changes, product) in zip(rows, entries):
                        if product:
                            posting._meli_metadata_lock(product)
                        else:
                            self.env.cr.execute(
                                "SELECT id FROM mercadolibre_posting WHERE id = %s FOR UPDATE NOWAIT",
                                [posting.id],
                            )
                            if not self.env.cr.fetchone():
                                raise ValidationError(stale)
                            posting.invalidate_recordset()
                    rows.check_access("read")
                    rows.check_access("write")
                    locked_plan, entries = collect()
                    if not hmac.compare_digest(plan, locked_plan):
                        raise ValidationError(stale)
                    for posting, (report, _snapshot, changes, _product) in zip(rows, entries):
                        if changes:
                            posting.write(changes)
                            report["result"] = "applied"
            except (errors.LockNotAvailable, errors.SerializationFailure):
                raise ValidationError(stale) from None
        return {
            "dry_run": dry_run, "plan": plan,
            "rows": [report for report, _snapshot, _changes, _product in entries],
        }

    def posting_query_questions(self):
        """Legacy questions entry point; item metadata is a separate operation."""
        company = self.env.user.company_id
        if not company.mercadolibre_cron_get_questions:
            return {}
        for posting in self:
            if not posting.meli_id:
                continue
            meli = self.env['meli.util'].get_new_instance(company)
            response = meli.get("/questions/search?item_id="+posting.meli_id, {'access_token':meli.access_token})
            questions_json = response.json()
            questions_obj = self.env['mercadolibre.questions']

            if 'questions' in questions_json:
                questions = questions_json['questions']
                #_logger.info( questions )
                cn = 0
                for Question in questions:
                    cn = cn + 1

                    Question["posting_id"] = posting.id

                    self.env['mercadolibre.questions'].process_question(Question=Question,meli=meli,config=company)


        return {}


    def posting_query_all_questions( self, cr, uid, ids, context=None ):

        return {}

    posting_date = fields.Date('Fecha del posting')
    name = fields.Char('Name')
    meli_id = fields.Char('Meli Id', size=256, index=True)
    meli_variation_id = fields.Char('Meli Variante Id', index=True)
    product_id = fields.Many2one('product.product','product_id')
    product_id_active = fields.Boolean(related='product_id.active',string="Active product")
    meli_status = fields.Char( string="Estado del producto en MLA", size=256 )
    meli_permalink = fields.Char( string="Permalink en MercadoLibre", size=512 )
    meli_price = fields.Char(string='Precio de venta', size=128)
    posting_questions = fields.One2many( 'mercadolibre.questions','posting_id','Questions' )
    posting_update = fields.Char(compute=_posting_update, string="Stored Metadata", store=False)
    meli_seller_custom_field = fields.Char('Sellect Custom Field or SKU',size=256)
