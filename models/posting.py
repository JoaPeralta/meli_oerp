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
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.translate import _
import logging

#from ..melisdk.meli import Meli

import logging
_logger = logging.getLogger(__name__)

from .meli_oerp_config import *
from dateutil.parser import *
from datetime import *

from . import versions
from .versions import *


class mercadolibre_posting_update(models.TransientModel):
    _name = "mercadolibre.posting.update"
    _description = "Update Posting Questions"

    def posting_update(self, context=None ):
        context = context or self.env.context
        posting_ids = False
        _logger.info("context:")
        _logger.info(context)
        posting_ids = ('active_ids' in context and context['active_ids']) or []
        #_logger.info("ids %s", ''.join(ids))
        #posting_ids = ids
        posting_obj = self.env['mercadolibre.posting']

        if (posting_ids):
            for posting_id in posting_ids:

        #    _logger.info("posting_update: %s " % (posting_id) )

                posting = posting_obj.browse(posting_id)
                posting.posting_query_questions()

        return {}


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

    def _posting_update( self ):

        company = self.env.user.company_id
        posting_obj = self.env['mercadolibre.posting']

        for posting in self:
            update_status = "ok"
            posting.posting_update = update_status
            posting.posting_query_questions()
            #res = {}
            #res[posting.id] = update_status
            #return res

    def posting_query_questions( self ):

        #get with an item id
        company = self.env.user.company_id
        posting_obj = self.env['mercadolibre.posting']

        for posting in self:

            log_msg = 'posting_query_questions: %s' % (posting.meli_id)
            #_logger.info(log_msg)
            meli = self.env['meli.util'].get_new_instance(company)
            if (posting.meli_id):
                pass;
            else:
                continue;
            response = meli.get("/items/"+posting.meli_id, {'access_token':meli.access_token})
            product_json = response.json()
            #_logger.info( product_json )

            # ML no siempre responde un item. Un 401 no trae 'error' ni 'status',
            # y un body no-JSON llega como str (_parse_response devuelve
            # resp.text), con lo cual `"error" in product_json` pasa a ser un
            # match de substring y el indexado tira TypeError. Antes eso rompia
            # la lectura del posting: posting_update es un campo computed, asi
            # que cualquier vista que muestre postings reventaba.
            # 'status' es el unico campo que este bloque indexa sin guarda propia
            # ('error', 'permalink' y 'price' ya traen la suya).
            if not isinstance(product_json, dict):
                _logger.error(
                    "posting_query_questions > respuesta no procesable para item %s: tipo=%s len=%d",
                    str(posting.meli_id), type(product_json).__name__, len(str(product_json)))
            elif "error" in product_json:
                ML_status = product_json["error"]
            elif "status" not in product_json:
                _logger.error(
                    "posting_query_questions > respuesta sin 'status' para item %s: claves=%s",
                    str(posting.meli_id), sorted(product_json.keys())[:10])
            else:
                ML_status = product_json["status"]
                post = { 'meli_status': ML_status }
                if "permalink" in product_json:
                    ML_permalink = product_json["permalink"]
                    post["meli_permalink"] = ML_permalink
                if "price" in product_json:
                    ML_price = product_json["price"]
                    post["meli_price"] = ML_price
                #ML_sku = product_json["seller_custom_field"]
                posting.write( post )

            if (not company.mercadolibre_cron_get_questions):
                return {}

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
    posting_update = fields.Char( compute=_posting_update, string="Posting Update", store=False )
    meli_seller_custom_field = fields.Char('Sellect Custom Field or SKU',size=256)
