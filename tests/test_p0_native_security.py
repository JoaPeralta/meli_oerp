"""Native Odoo ORM/HTTP acceptance; synthetic data and intercepted transport only."""
import base64
import io
import re
import zipfile
from html import unescape
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..models import shipment as shipment_module

_PNG = base64.b64encode(base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
_TOKEN = 'SYNTHETIC-NATIVE-P0-NOT-A-CREDENTIAL'


@tagged('post_install', '-at_install')
class TestP0NativeSecurity(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'P0 isolated synthetic company'})
        cls.operator = cls.env['res.users'].create({
            'name': 'P0 existing-role operator', 'login': 'p0_operator', 'password': 'synthetic-only',
            'company_id': cls.company.id, 'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                    cls.env.ref('meli_oerp.group_mercadolibre_reader').id])],
        })
        cls.unprivileged = cls.env['res.users'].create({
            'name': 'P0 no connector permissions', 'login': 'p0_unprivileged', 'password': 'synthetic-only',
            'company_id': cls.company.id, 'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.sales_manager = cls.env['res.users'].create({
            'name': 'P0 normal Sales Manager', 'login': 'p0_sales_manager', 'password': 'synthetic-only',
            'company_id': cls.company.id, 'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                    cls.env.ref('sales_team.group_sale_manager').id])],
        })
        cls.catalog_manager = cls.sales_manager.copy({
            'name': 'P0 existing-role catalog manager', 'login': 'p0_catalog_manager',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                    cls.env.ref('sales_team.group_sale_manager').id,
                                    cls.env.ref('meli_oerp.group_mercadolibre_manager').id])],
        })
        cls.company.write({'mercadolibre_seller_id': '700',
                           'mercadolibre_seller_user': cls.env.user.id})
        order = cls.env['mercadolibre.orders'].create({
            'order_id': '900', 'company_id': cls.company.id, 'seller_id': cls.env.user.id,
            'seller': "{'id': 700}", 'shipping_id': '800'})
        cls.shipment = cls.env['mercadolibre.shipment'].create({
            'shipping_id': '800', 'order': order.id, 'seller_id': cls.env.user.id,
            'sender_id': '700', 'mode': 'me2', 'logistic_type': 'cross_docking',
            'status': 'ready_to_ship', 'substatus': 'ready_to_print'})
        attribute = cls.env['product.attribute'].create({'name': 'P0 synthetic variant'})
        values = cls.env['product.attribute.value'].create([
            {'name': 'A', 'attribute_id': attribute.id}, {'name': 'B', 'attribute_id': attribute.id}])
        template = cls.env['product.template'].create({
            'name': 'P0 published synthetic', 'is_published': True, 'sale_ok': True,
            'company_id': cls.company.id, 'image_1920': _PNG,
            'attribute_line_ids': [(0, 0, {'attribute_id': attribute.id, 'value_ids': [(6, 0, values.ids)]})]})
        cls.product, cls.sibling = template.product_variant_ids[:2]
        cls.private = cls.env['product.product'].create({
            'name': 'P0 private synthetic', 'is_published': False, 'image_1920': _PNG})
        cls.foreign = cls.env['product.product'].create({
            'name': 'P0 other company', 'company_id': cls.other_company.id,
            'is_published': True, 'image_1920': _PNG})
        cls.template_image = cls.env['product.image'].create({
            'name': 'P0 template image', 'product_tmpl_id': template.id, 'image_1920': _PNG})
        cls.sibling_image = cls.env['product.image'].create({
            'name': 'P0 sibling image', 'product_variant_id': cls.sibling.id,
            'product_tmpl_id': template.id, 'image_1920': _PNG})
        cls.private_image = cls.env['product.image'].create({
            'name': 'P0 private image', 'product_tmpl_id': cls.private.product_tmpl_id.id, 'image_1920': _PNG})

    @contextmanager
    def no_external(self):
        original = requests.sessions.Session.request
        def local_only(session, method, url, *args, **kwargs):
            if urlsplit(url).hostname not in ('localhost', '127.0.0.1', '::1'):
                raise AssertionError('external HTTP forbidden')
            return original(session, method, url, *args, **kwargs)
        with patch('requests.sessions.Session.request', new=local_only):
            yield

    def test_public_image_positive_and_private_missing_negative_http(self):
        self.authenticate(None, None)
        with self.no_external():
            for path in ('/meli/image/%s' % self.product.id,
                         '/meli/image/%s/%s' % (self.product.id, self.template_image.id)):
                self.assertEqual(self.url_open(path).status_code, 200)
            for path in ('/meli/image/%s' % self.private.id, '/meli/image/0',
                         '/meli/image/%s' % self.foreign.id,
                         '/meli/image/%s/0' % self.product.id,
                         '/meli/image/%s/%s' % (self.product.id, self.private_image.id),
                         '/meli/image/%s/%s' % (self.product.id, self.sibling_image.id)):
                self.assertEqual(self.url_open(path).status_code, 404)

    def test_normal_user_variant_template_company_and_private_http(self):
        self.authenticate('p0_operator', 'synthetic-only')
        with self.no_external():
            self.assertEqual(self.url_open('/meli/image/%s' % self.private.id).status_code, 200)
            for image in (self.private_image, self.sibling_image):
                self.assertEqual(self.url_open('/meli/image/%s/%s' % (self.product.id,image.id)).status_code,404)
            self.assertEqual(self.url_open('/meli/image/%s' % self.foreign.id).status_code,404)
            self.assertEqual(self.url_open('/meli/image/%s/999999999' % self.product.id).status_code,404)

    def test_reader_label_http_bearer_binary_and_no_writes(self):
        self.authenticate('p0_operator', 'synthetic-only')
        client = SimpleNamespace(access_token=_TOKEN, seller_id='700', need_login=lambda: False,
            _meli_stock_seller_snapshot={'status': 200, 'seller_id': '700', 'company_id': self.company.id})
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'%PDF-1.4 synthetic\n%%EOF']
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        before = self.shipment.write_date
        with patch.object(type(self.env['meli.util']), 'get_new_instance', return_value=client), \
                patch.object(shipment_module.requests, 'get', return_value=response) as get:
            result = self.url_open('/meli/labels/%s?mode=pdf' % self.shipment.id)
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.content.startswith(b'%PDF-'))
        self.assertNotIn(_TOKEN,result.text)
        self.assertEqual(get.call_args.kwargs['headers'],{'Authorization':'Bearer '+_TOKEN})
        self.shipment.invalidate_recordset()
        self.assertEqual(self.shipment.write_date,before)
        self.assertFalse(self.shipment.pdf_link)

    def test_native_label_denial_before_oauth_for_no_access_foreign_and_unknown(self):
        for user, change in ((self.unprivileged, {}), (self.operator, {'sender_id': '701'}),
                             (self.operator, {'order': False})):
            shipment = self.shipment.with_user(user).with_context(allowed_company_ids=[self.company.id])
            with self.env.cr.savepoint():
                self.shipment.write(change)
                with patch.object(type(self.env['meli.util']), 'get_new_instance') as get:
                    with self.assertRaises(AccessError): shipment._label_binary('pdf')
                    get.assert_not_called()
                # Restore only synthetic fixture within its test transaction.
                self.shipment.write({'sender_id': '700', 'order': self.env['mercadolibre.orders'].search([('order_id','=','900')]).id})

    def test_native_foreign_company_denial_and_no_credential_facade_access(self):
        self.shipment.order.company_id = self.other_company
        shipment = self.shipment.with_user(self.operator).with_context(allowed_company_ids=[self.company.id])
        with patch.object(type(self.env['meli.util']), 'get_new_instance') as get:
            with self.assertRaises(AccessError): shipment._label_binary('pdf')
            get.assert_not_called()
        with self.assertRaises(AccessError):
            self.company.with_user(self.operator).read(['mercadolibre_access_token'])

    def test_native_cleanup_admin_dry_run_apply_and_idempotence(self):
        self.shipment.pdf_link = 'https://api.mercadolibre.com/shipment_labels?shipment_ids=800&access_token='+_TOKEN
        model = self.env['mercadolibre.shipment']
        first = model.cleanup_legacy_label_links()
        self.assertTrue(first['dry_run']); self.assertEqual(first['records']['mercadolibre.shipment'],1)
        self.assertIn(_TOKEN,self.shipment.pdf_link)
        model.cleanup_legacy_label_links(dry_run=False)
        self.assertNotIn(_TOKEN,self.shipment.pdf_link)
        self.assertEqual(model.cleanup_legacy_label_links()['records']['mercadolibre.shipment'],0)
        with self.assertRaises(AccessError):
            model.with_user(self.operator).cleanup_legacy_label_links(False)

    def test_native_individual_batch_auto_pdf_zpl_modes_keep_existing_fields(self):
        import io
        import zipfile
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('label.zpl',b'^XA synthetic ^XZ')
        client=SimpleNamespace(access_token=_TOKEN,seller_id='700',need_login=lambda:False,
            _meli_stock_seller_snapshot={'status':200,'seller_id':'700','company_id':self.company.id})
        wizard=self.env['mercadolibre.shipment.print'].create({})
        for mode, body in (('pdf',b'%PDF-1.4 synthetic\n%%EOF'),('zpl',stream.getvalue()),('zpl_txt',stream.getvalue())):
            response=Mock(status_code=200)
            response.iter_content.return_value=[body]
            response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
            with patch.object(type(self.env['meli.util']),'get_new_instance',return_value=client), \
                    patch.object(shipment_module.requests,'get',return_value=response), \
                    patch.object(shipment_module,'PDF2IMAGE_AVAILABLE',False), \
                    patch.object(shipment_module,'mercadolibre_shipment_print_guide_mode',mode), \
                    patch.object(type(self.shipment),'update',side_effect=AssertionError('commercial update forbidden')):
                result=self.shipment.shipment_print(include_ready_to_print=True)
                first=self.shipment.write_date
                self.shipment.shipment_print(include_ready_to_print=True)
                self.assertEqual(self.shipment.write_date,first)
                self.assertTrue(self.shipment.pdf_file)
                self.assertNotIn(_TOKEN,self.shipment.pdf_link)
                self.assertNotIn(_TOKEN,repr(result))
                batch=wizard._get_shipment_labels_data([self.shipment.id],include_ready_to_print=True)
                auto=wizard._get_labels_urls_auto_print([self.shipment.id],include_ready_to_print=True)
                self.assertEqual(auto['urls'],[entry['url'] for entry in batch['batches']])
                self.assertEqual(auto['count'],1)
                self.assertNotIn(_TOKEN,repr(batch))

    @contextmanager
    def label_transport(self, mode='pdf'):
        """Intercept only provider transport; local HttpCase requests remain real."""
        plain = b'^XA synthetic compatibility ^XZ'
        if mode == 'pdf':
            body = b'%PDF-1.4 synthetic compatibility\n%%EOF'
        else:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('label.zpl', plain)
            body = stream.getvalue()
        client = SimpleNamespace(access_token=_TOKEN, seller_id='700', need_login=lambda: False,
            _meli_stock_seller_snapshot={'status': 200, 'seller_id': '700', 'company_id': self.company.id})
        response = Mock(status_code=200)
        response.iter_content.return_value = [body]
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with patch.object(type(self.env['meli.util']), 'get_new_instance', return_value=client) as capability, \
                patch.object(shipment_module.requests, 'get', return_value=response) as get, \
                patch.object(shipment_module, 'PDF2IMAGE_AVAILABLE', False), \
                patch.object(shipment_module, 'mercadolibre_shipment_print_guide_mode', mode), \
                patch.object(type(self.shipment), 'update', side_effect=AssertionError('commercial update forbidden')), \
                self.no_external():
            yield get, capability, plain if mode == 'zpl_txt' else body

    def test_native_flex_labels_allowed_and_fulfillment_denied(self):
        self.shipment.logistic_type = 'self_service'
        record = self.shipment.with_user(self.operator).with_context(allowed_company_ids=[self.company.id])
        with self.label_transport() as (get, capability, expected):
            for state in ('ready_to_print', 'printed'):
                self.shipment.substatus = state
                self.assertTrue(record._label_is_printable())
                self.assertEqual(record._label_binary('pdf')[0], expected)
            get.reset_mock(); capability.reset_mock()
            self.shipment.logistic_type = 'fulfillment'
            self.assertFalse(record._label_is_printable())
            with self.assertRaises(AccessError): record._label_binary('pdf')
            get.assert_not_called(); capability.assert_not_called()

    def test_native_default_sales_manager_wizard_button_to_http_first_and_reprint(self):
        self.authenticate('p0_sales_manager', 'synthetic-only')
        self.shipment.logistic_type = 'self_service'
        model = self.env['mercadolibre.shipment.print'].with_user(self.sales_manager).with_context(
            allowed_company_ids=[self.company.id], active_model='mercadolibre.shipment',
            active_ids=[self.shipment.id])
        self.assertFalse(model.env.su)
        self.assertFalse(self.sales_manager.has_group('base.group_system'))
        with self.label_transport() as (get, capability, expected):
            for state in ('ready_to_print', 'printed'):
                self.shipment.substatus = state
                wizard = model.create({})
                self.assertFalse(wizard.include_ready_to_print)
                action = wizard.shipment_print()
                warning = self.env['meli.warning'].with_user(self.sales_manager).browse(action['res_id'])
                urls = [unescape(url) for url in re.findall(r'href="([^"]+)"', warning.message_html)]
                self.assertEqual(len(urls), 1)
                self.assertTrue(urls[0].startswith('/meli/labels/'))
                self.assertNotIn(_TOKEN, urls[0])
                result = self.url_open(urls[0], allow_redirects=False)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.content, expected)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.kwargs['headers'], {'Authorization': 'Bearer ' + _TOKEN})

    def test_native_auto_result_urls_download_with_session_not_anonymous_or_denied(self):
        self.authenticate('p0_sales_manager', 'synthetic-only')
        self.shipment.logistic_type = 'self_service'
        wizard = self.env['mercadolibre.shipment.print'].with_user(self.sales_manager).with_context(
            allowed_company_ids=[self.company.id]).create({})
        for mode in ('pdf', 'zpl'):
            self.shipment.substatus = 'ready_to_print'
            with self.label_transport(mode) as (get, capability, expected):
                data = wizard._get_labels_urls_auto_print([self.shipment.id])
                self.assertEqual(data['count'], 1)
                url = data['urls'][0]
                result = self.url_open(url, allow_redirects=False)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.content, expected)
                self.assertNotIn(_TOKEN, repr(data))
                self.shipment.substatus = 'printed'
                self.assertEqual(wizard._get_labels_urls_auto_print([self.shipment.id])['count'], 0)
                reprint = wizard._get_labels_urls_auto_print([self.shipment.id], include_ready_to_print=True)
                self.assertEqual(reprint['count'], 1)
                self.assertEqual(self.url_open(reprint['urls'][0]).content, expected)
        # Reuse a produced URL, not a separately constructed syntax-only surrogate.
        with self.label_transport(mode) as (get, capability, expected):
            self.authenticate('p0_unprivileged', 'synthetic-only')
            denied = self.url_open(url, allow_redirects=False)
            self.assertEqual(denied.status_code, 404)
            self.assertNotEqual(denied.content, expected)
            self.authenticate(None, None)
            anonymous = self.url_open(url, allow_redirects=False)
            self.assertIn(anonymous.status_code, (302, 303, 401, 403))
            self.assertNotEqual(anonymous.content, expected)
            get.assert_not_called(); capability.assert_not_called()

    def test_native_effective_product_views_keep_public_link_without_api_widget(self):
        self.product.meli_id = 'MLA123456789'
        with self.no_external(), patch.object(type(self.env['meli.util']), 'get_new_instance',
                side_effect=AssertionError('public permalink/view must not use OAuth')):
            for model_name, view_id in (('product.template', 'product.product_template_only_form_view'),
                                         ('product.product', 'product.product_normal_form_view')):
                model = self.env[model_name].with_user(self.catalog_manager)
                result = model.get_view(view_id=self.env.ref(view_id).id, view_type='form')
                arch = ET.fromstring(result['arch'])
                self.assertIsNotNone(arch.find(".//field[@name='meli_permalink'][@widget='url']"))
                self.assertEqual(arch.findall(".//field[@name='meli_permalink_api'][@widget='url']"), [])
            public_url = self.product.with_user(self.catalog_manager).meli_permalink
            self.assertEqual(public_url, 'https://articulo.mercadolibre.com.ar/MLA-123456789')
            self.assertNotIn(_TOKEN, public_url)
