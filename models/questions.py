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

from urllib.parse import quote, urlencode

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from .meli_posting_metadata import ITEM_ID, import_response_status, numeric_id
import logging
from . import versions
from .versions import *
import logging

_logger = logging.getLogger(__name__)
#https://api.mercadolibre.com/questions/search?item_id=MLA508223205

class mercadolibre_questions(models.Model):
    _name = "mercadolibre.questions"
    _description = "Preguntas en MercadoLibre"

    name = fields.Char(string="Name")
    posting_id = fields.Many2one("mercadolibre.posting","Posting")
    company_id = fields.Many2one("res.company",string="Empresa")
    #product_id = fields.Many2one("product.product","Product")
    question_id = fields.Char('Question Id')
    date_created = fields.Date('Creation date')
    item_id = fields.Char(string="Item ID",size=255)
    seller_id = fields.Char(string="Seller ID",size=255)
    text = fields.Text("Question Text")
    status = fields.Selection( [("UNANSWERED","Question is not answered yet."),
                                ("ANSWERED","Question was answered."),
                                ("CLOSED_UNANSWERED","The item is closed and the question was never answered."),
                                ("UNDER_REVIEW","The item is under review and the question too."),
                                ("BANNED","The item was banned")],
                                string='Question Status')
    answer_date_created = fields.Date('Answer creation date')
    answer_status = fields.Selection( [("ACTIVE","Active"),("DISABLED","Disabled"),("BANNED","Banned")], string='Answer Status')
    answer_text = fields.Text("Answer Text")


    @api.depends("company_id.country_id.code", "company_id.mercadolibre_currency", "item_id", "question_id")
    @api.depends_context("company")
    def compute_answer_link(self):
        for question in self:
            company = question.company_id or self.env.company
            question.answer_link = False
            if question.item_id and question.question_id:
                site = company._meli_site_id_local()
                storefront = company._meli_public_storefront(site)
                question.answer_link = (
                    storefront + "preguntas/vendedor/articulo/" + quote(question.item_id, safe="")
                    + "?" + urlencode({"question_id": question.question_id})
                )

    answer_link = fields.Char(string="Answer Link",compute=compute_answer_link, store=False)

    def prepare_question_fields( self, Question, meli=None, config=None ):
        question_fields = {
            'name': ''+str(Question['item_id']),
            'posting_id': "posting_id" in Question and Question["posting_id"],
            'question_id': Question['id'],
            'date_created': ml_datetime(Question['date_created']),
            'item_id': Question['item_id'],
            'seller_id': Question['seller_id'],
            'text': Question.get('text') or "",
            'status': Question['status'],
        }
        if config:
            question_fields["company_id"] = config.id
        return question_fields

    @api.model
    def _meli_valid_question(self, payload, item_id, company, question_id=None):
        if (not isinstance(payload, dict) or "error" in payload or not isinstance(item_id, str)
                or not ITEM_ID.fullmatch(item_id) or payload.get("item_id") != item_id):
            return False
        seller = numeric_id(company.mercadolibre_seller_id)
        key = numeric_id(payload.get("id"))
        return bool(seller and key and numeric_id(payload.get("seller_id")) == seller
                    and (question_id is None or key == numeric_id(question_id)))

    def fetch_question( self, question_id=None, meli=None, config=None ):
        Question = None
        config = config or self.env.company
        if not numeric_id(question_id):
            return None
        if not meli:
            meli = self.env['meli.util'].get_new_instance(config)
        if meli.need_login():
            self.env['meli.util']._meli_require_reconnect()
        response = meli.get("/questions/" + str(question_id), {
            "access_token": meli.access_token, "api_version": 4,
        })
        if import_response_status(response) == 200:
            payload = response.json()
            if (isinstance(payload, dict) and config
                    and self._meli_valid_question(payload, payload.get("item_id"), config, question_id)):
                Question = payload

        return Question

    def process_question( self, question_id=None, Question=None, meli=None, config=None ):
        questions_obj = self
        question = None
        if question_id and not Question:
            Question = questions_obj.fetch_question( question_id=question_id, meli=meli, config=config )


        if (Question and 'id' in Question):
            question_answer = 'answer' in Question and Question['answer']

            question_fields = self.prepare_question_fields( Question=Question, meli=meli, config=config )

            if (question_answer):
                question_fields['answer_text'] = question_answer['text'].encode("utf-8")
                question_fields['answer_status'] = question_answer['status']
                question_fields['answer_date_created'] = ml_datetime(question_answer['date_created'])

            domain = [
                ('question_id', '=', str(question_fields['question_id'])),
                ('item_id', '=', question_fields['item_id']),
                ('seller_id', '=', str(question_fields['seller_id'])),
            ]
            if config:
                domain.append(('company_id', 'in', [False, config.id]))
            question = questions_obj.search(domain, limit=2)
            if len(question) > 1:
                raise ValidationError("Multiple stored rows match this question.")
            if not question:
                question = questions_obj.create(question_fields)
            else:
                # An item question is not specific to a legacy variation.
                # Keep its established posting attachment, including on refresh.
                if question.posting_id:
                    question_fields.pop("posting_id", None)
                question.write(question_fields)

        return question
