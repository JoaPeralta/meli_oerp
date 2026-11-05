"""Execute production methods with synthetic boundaries; not native ORM proof."""
import ast
import base64
import logging
import io
import zipfile
import importlib.util
from urllib.parse import urlencode
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'SYNTHETIC-p0-token'


def method(path, name, namespace=None, owner=None):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    candidates = [n for n in tree.body if isinstance(n, ast.ClassDef) and (owner is None or n.name == owner)]
    node = next(n for cls in candidates for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    sanitize = {}
    for helper in ('_meli_upload_error', '_meli_upload_payload'):
        source = ast.parse((ROOT/'models/meli_util.py').read_text(encoding='utf-8'))
        part = next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name == helper)
        exec(compile(ast.Module(body=[part],type_ignores=[]),'meli_util.py','exec'),sanitize)
    ns = {'_logger': logging.getLogger('p0'), 'base64': base64, **sanitize, **(namespace or {})}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / path), 'exec'), ns)
    return ns[name]


class Records(list):
    def __getattr__(self, key):
        return getattr(self[0], key)


class TestVulnerableProductionPaths(unittest.TestCase):
    def test_product_api_link_has_no_token(self):
        company = NS(get_ML_LINK_URL=lambda **kw: 'https://example.invalid/')
        client = NS(access_token=TOKEN, need_login=lambda: False,
                    get=lambda *a, **kw: NS(json=lambda: {'permalink': 'https://example.invalid/'}))
        env = NS(company=company, user=NS(company_id=company))
        env = type('Env', (), {'company': company, 'user': env.user,
            '__getitem__': lambda self, key: NS(get_new_instance=lambda *a: client)})()
        row = NS(meli_id='MLA1')
        records = Records([row]); records.env = env
        method('models/product.py', 'product_get_meli_update')(records)
        self.assertNotIn(TOKEN, row.meli_permalink_api)
        self.assertIn('include_attributes=all', row.meli_permalink_api)

    def test_single_label_result_has_no_credentials_or_import(self):
        company = NS()
        client = NS(access_token=TOKEN)
        shipment = NS(env=NS(context={}, user=NS(company_id=company)), update=Mock(),
                      status='pending', shipping_id='1', substatus='',
                      ensure_one=lambda: None,
                      _label_authorize=lambda **kw: NS(mercadolibre_shipment_print_guide_mode='pdf'),
                      _label_print_mode=lambda config: 'pdf',
                      _label_check_mode=lambda mode: None, _label_is_printable=lambda flag: False)
        shipment.env = type('Env', (), {'context': {}, 'company': company, 'user': NS(company_id=company),
            '__getitem__': lambda self, key: NS(get_new_instance=lambda *a: client)})()
        result = method('models/shipment.py', 'shipment_print', owner='mercadolibre_shipment')(shipment, meli=client)
        self.assertNotIn(TOKEN, repr(result))
        shipment.update.assert_not_called()

    def test_unrelated_image_is_denied_before_binary_read(self):
        product = NS(id=1)
        image = NS(id=2)
        env = {'product.product': NS(browse=lambda _: product),
               'product.image': NS(browse=lambda _: image)}
        request = NS(env=env, make_response=lambda body, headers: body,
                     not_found=lambda: 'NOT_FOUND')
        get_image = Mock(return_value=base64.b64encode(b'PRIVATE'))
        result = method('controllers/main.py', 'meli_image', {
            'request': request, 'get_image_full': get_image, 'content_disposition': str,
            'AccessError': PermissionError, 'MissingError': LookupError,
        })(NS(_authorized_image=Mock(side_effect=PermissionError())), 1, 2)
        self.assertEqual(result, 'NOT_FOUND')
        get_image.assert_not_called()


class TestPredictor(unittest.TestCase):
    def test_query_parameters_stay_server_side_and_reflections_are_redacted(self):
        get=Mock(return_value=NS(json=lambda: {'error': TOKEN}))
        client=NS(access_token=TOKEN,get=get)
        company=NS(_get_ML_sites=lambda: 'MLA')
        category=NS(sudo=lambda: 'categories')
        env=type('Env',(),{'user': NS(company_id=company),
            '__getitem__':lambda self,key: NS(get_new_instance=lambda:client) if key=='meli.util' else category})()
        record=NS(env=env,ensure_one=lambda:None,get_title_for_category_predictor=lambda:'title & accents',
                  get_price_for_category_predictor=lambda:123)
        log=Mock()
        _, result=method('models/product.py','_get_meli_category_from_predictor',{'_logger':log})(record)
        path, params=get.call_args.args
        self.assertEqual(path,'/sites/MLA/domain_discovery/search')
        self.assertEqual(params['q'],'title & accents')
        self.assertNotIn(TOKEN,path)
        self.assertNotIn(TOKEN,repr(result)); self.assertNotIn(TOKEN,repr(log.mock_calls))


class TestImageAuthorization(unittest.TestCase):
    def fixture(self, internal=True):
        class Row(NS):
            def exists(self): return self
            def check_access(self,mode):
                if getattr(self,'denied',False): raise PermissionError('denied')
        company=object()
        template=Row(company_id=company,is_published=True,sale_ok=True,website_id=False)
        product=Row(id=1,product_tmpl_id=template)
        image=Row(product_tmpl_id=template,product_variant_id=False)
        website=NS(company_id=company)
        env=type('Env',(dict,),{})(
            {'product.product':NS(browse=lambda id:product),'product.image':NS(browse=lambda id:image),
             'website':NS(get_current_website=lambda:website)})
        env.user=NS(_is_internal=lambda:internal); env.companies=[company]
        action=method('controllers/main.py','_authorized_image',
                      {'request':NS(env=env),'AccessError':PermissionError})
        return action,product,template,image,website

    def test_matching_template_and_variant_are_accepted(self):
        action,product,template,image,_=self.fixture()
        self.assertIs(action(None,1,2),image)
        image.product_variant_id=product
        self.assertIs(action(None,1,2),image)

    def test_sibling_variant_or_template_mismatch_denied(self):
        for kind in ('sibling','template','orphan'):
            action,product,template,image,_=self.fixture()
            if kind=='sibling': image.product_variant_id=object()
            if kind=='template': image.product_tmpl_id=object()
            if kind=='orphan': image.product_tmpl_id=False
            with self.subTest(kind=kind),self.assertRaises(PermissionError):action(None,1,2)

    def test_public_requires_publication_and_native_permissions(self):
        action,product,template,image,website=self.fixture(internal=False)
        self.assertIs(action(None,1,2),image)
        for field,value in (('is_published',False),('sale_ok',False),('website_id',object()),('company_id',object())):
            original=getattr(template,field); setattr(template,field,value)
            with self.subTest(field=field),self.assertRaises(PermissionError):action(None,1,2)
            setattr(template,field,original)
        image.denied=True
        with self.assertRaises(PermissionError):action(None,1,2)


class TestFinalPaymentLookup(unittest.TestCase):
    def test_final_lookup_uses_bearer_not_query_and_safe_failure(self):
        get = Mock(side_effect=RuntimeError('Authorization: Bearer ' + TOKEN))
        log = Mock()
        pay = NS(status='approved', shipping_amount=0, payment_id=987)
        client = NS(access_token=TOKEN, need_login=lambda: False)
        record = NS(payments=[pay], company_id=object())
        records = Records([record])
        records.env = NS(user=NS(company_id=object()))
        method('models/orders.py', '_ensure_payment_shipping_amounts',
               {'requests': NS(get=get), 'urlencode': urlencode, '_logger': log})(records, meli=client)
        args, kw = get.call_args
        self.assertNotIn(TOKEN, args[0])
        self.assertNotIn('params', kw)
        self.assertEqual(kw['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, repr(log.mock_calls))


def top_function(path, name):
    node = next(n for n in ast.parse((ROOT / path).read_text(encoding='utf-8-sig')).body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {'io': io, 'zipfile': zipfile}
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), ns)
    return ns[name]


class Rows:
    """Tiny recordset double; authorization outcomes still need native tests."""
    def __init__(self, records=(), env=None):
        self.rows, self.env = list(records), env

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return (type(self)([row], self.env) for row in self.rows)

    def __getattr__(self, name):
        self.ensure_one()
        return getattr(self.rows[0], name)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return type(self)(self.rows[key], self.env)
        return getattr(self.rows[0], key)

    def __or__(self, other):
        return type(self)(self.rows + [r for r in other.rows if r not in self.rows], self.env)

    def __sub__(self, other):
        return type(self)([r for r in self.rows if r not in other.rows], self.env)

    def ensure_one(self):
        assert len(self.rows) == 1

    def exists(self):
        return type(self)([r for r in self.rows if not getattr(r, 'missing', False)], self.env)

    def check_access(self, mode):
        if any(getattr(row, 'denied', False) for row in self.rows):
            raise PermissionError('denied')

    def mapped(self, key):
        return [getattr(row, key) for row in self.rows]

    def filtered(self, predicate):
        return type(self)([row.rows[0] for row in self if predicate(row)], self.env)

    def browse(self, ids):
        return type(self)([r for r in self.rows if r.id in ids], self.env)

    def write(self, values):
        for row in self.rows:
            row.writes.append(values)
            vars(row).update(values)


def label_model(get=None):
    namespace = {'AccessError': PermissionError, 'MissingError': LookupError,
                 'ValidationError': ValueError, 'ast': ast, 'urlencode': urlencode,
                 'requests': NS(get=get), '_label_content': top_function('models/shipment.py', '_label_content'),
                 'PDF2IMAGE_AVAILABLE': False}
    namespace['mercadolibre_shipment_print_guide_mode'] = next(
        ast.literal_eval(node.value) for node in ast.parse((ROOT / 'models/versions.py').read_text()).body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
            target.id == 'mercadolibre_shipment_print_guide_mode' for target in node.targets))
    names = ('_label_authorize', '_label_print_mode', '_label_check_mode', '_label_is_printable', '_label_download_url',
             '_label_binary', 'shipment_print')
    return type('Shipment', (Rows,), {name: method('models/shipment.py', name, namespace,
                                                  owner='mercadolibre_shipment') for name in names})


class TestLabelSecurity(unittest.TestCase):
    def fixture(self, count=1, body=b'%PDF-1.4 synthetic\n%%EOF'):
        response = Mock(status_code=200)
        response.iter_content.return_value = [body]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        transport = Mock(return_value=response)
        cls = label_model(transport)
        actor = object()
        company = NS(id=1, mercadolibre_seller_id='7', mercadolibre_seller_user=actor,
                     mercadolibre_shipment_print_guide_mode='pdf',
                     _fields={'mercadolibre_shipment_print_guide_mode': object()}, check_access=Mock())
        client = NS(access_token=TOKEN, seller_id='7', need_login=lambda: False,
                    _meli_stock_seller_snapshot={'company_id': 1, 'seller_id': '7', 'status': 200})
        util = NS(get_new_instance=Mock(return_value=client))
        env = type('Env', (), {'company': company, 'companies': [company],
            '__getitem__': lambda self, key: util if key == 'meli.util' else self.shipments})()
        records = []
        for index in range(count):
            order = Rows([NS(company_id=company, seller_id=actor, seller="{'id': 7}",
                             shipping_id=str(index+10))], env)
            records.append(NS(id=index+1, order=order, orders=Rows([],env), company_id=company,
                seller_id=actor, sender_id='7', shipping_id=str(index+10), mode='me2',
                logistic_type='cross_docking', status='ready_to_ship', substatus='ready_to_print',
                pdf_file=False, pdf_link=False, pdf_filename=False, writes=[], update=Mock()))
        env.shipments = cls(records,env)
        return env.shipments, util, transport, response

    def test_base_company_without_optional_print_field_supports_batch(self):
        rows, util, get, _ = self.fixture()
        del rows.env.company.mercadolibre_shipment_print_guide_mode
        rows.env.company._fields = {}
        wizard = NS(env=rows.env)
        result = method('models/shipment.py', '_get_shipment_labels_data')(
            wizard, [1], include_ready_to_print=True)
        self.assertEqual(result['print_mode'], 'pdf')
        self.assertEqual(result['batches'][0]['url'], '/meli/labels/1?mode=pdf')
        get.assert_not_called(); util.get_new_instance.assert_not_called()

    def test_base_company_without_optional_print_field_supports_individual(self):
        rows, _, get, _ = self.fixture()
        del rows.env.company.mercadolibre_shipment_print_guide_mode
        rows.env.company._fields = {}
        result = rows.shipment_print(include_ready_to_print=True)
        self.assertEqual(result['url'], '/meli/labels/1?mode=pdf')
        self.assertEqual(rows.pdf_filename, 'Shipment_10.pdf')
        get.assert_called_once()

    def test_fixed_endpoint_bearer_binary_pdf(self):
        rows, util, get, _ = self.fixture()
        data, filename, mime = rows._label_binary('pdf')
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertEqual((filename,mime), ('Shipment_10.pdf','application/pdf'))
        args, kw = get.call_args
        self.assertEqual(args, ('https://api.mercadolibre.com/shipment_labels',))
        self.assertEqual(kw['headers'], {'Authorization': 'Bearer ' + TOKEN})
        self.assertNotIn(TOKEN, repr(kw['params']))
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['timeout'], (5,30))
        self.assertEqual(rows.writes, [])

    def test_all_ownership_denials_precede_oauth_and_transport(self):
        for changed in ('denied','missing','company','seller','sender','order_seller','order_company','shipping'):
            rows, util, get, _ = self.fixture()
            row = rows.rows[0]
            if changed in ('denied','missing'):
                setattr(row, changed, True)
            elif changed == 'company': row.company_id = object()
            elif changed == 'seller': row.seller_id = object()
            elif changed == 'sender': row.sender_id = '8'
            elif changed == 'order_seller': row.order.rows[0].seller = "{'id': 8}"
            elif changed == 'order_company': row.order.rows[0].company_id = object()
            else: row.order.rows[0].shipping_id = '99'
            with self.subTest(changed=changed), self.assertRaisesRegex(PermissionError, '^Shipping labels are not available$'):
                rows._label_binary('pdf')
            util.get_new_instance.assert_not_called()
            get.assert_not_called()

    def test_mixed_selection_cannot_partially_fetch(self):
        rows, util, get, _ = self.fixture(2)
        rows.rows[1].sender_id = '8'
        with self.assertRaises(PermissionError): rows._label_binary('pdf')
        get.assert_not_called(); util.get_new_instance.assert_not_called()

    def test_identity_failure_prevents_label_transport(self):
        rows, util, get, _ = self.fixture()
        util.get_new_instance.return_value._meli_stock_seller_snapshot = {}
        with self.assertRaises(PermissionError): rows._label_binary('pdf')
        get.assert_not_called()

    def test_state_mode_and_fifty_id_limit_precede_network(self):
        for case in ('status','substatus','mode','logistic','limit','format'):
            rows, util, get, _ = self.fixture(51 if case == 'limit' else 1)
            if case == 'status': rows.rows[0].status = 'shipped'
            if case == 'substatus': rows.rows[0].substatus = 'pending'
            if case == 'mode': rows.rows[0].mode = 'me1'
            if case == 'logistic': rows.rows[0].logistic_type = 'fulfillment'
            with self.subTest(case=case), self.assertRaises((PermissionError,ValueError)):
                rows._label_binary('arbitrary' if case == 'format' else 'pdf')
            get.assert_not_called(); util.get_new_instance.assert_not_called()

    def test_batch_and_auto_split_token_free_local_links(self):
        rows, util, get, _ = self.fixture(101)
        wizard = NS(env=rows.env)
        wizard._get_shipment_labels_data = lambda *args, **kw: method(
            'models/shipment.py','_get_shipment_labels_data')(wizard,*args,**kw)
        result = method('models/shipment.py','_get_labels_urls_auto_print')(
            wizard, list(range(1,102)), include_ready_to_print=True)
        self.assertEqual(result['count'],3)
        self.assertEqual([len(url.split('?')[0].split('/')[-1].split(',')) for url in result['urls']], [50,50,1])
        self.assertTrue(all(url.startswith('/meli/labels/') for url in result['urls']))
        self.assertNotIn(TOKEN, repr(result))
        get.assert_not_called(); util.get_new_instance.assert_not_called()

    def test_single_binary_storage_is_idempotent_without_commercial_update(self):
        rows, _, _, _ = self.fixture()
        first = rows.shipment_print(include_ready_to_print=True)
        second = rows.shipment_print(include_ready_to_print=True)
        self.assertEqual(first,second)
        self.assertEqual(len(rows.writes),1)
        self.assertNotIn(TOKEN, repr(rows.writes))
        rows.update.assert_not_called()

    def test_zpl_zip_plain_and_text_modes(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('label.zpl',b'^XA\nsynthetic\n^XZ')
        for mode, body, extension in (('zpl',stream.getvalue(),'.zip'),('zpl_txt',stream.getvalue(),'.zpl'),
                                       ('zpl',b'^XA synthetic ^XZ','.zpl')):
            rows, _, _, _ = self.fixture(body=body)
            data, filename, _ = rows._label_binary(mode)
            self.assertTrue(filename.endswith(extension))
            if mode == 'zpl_txt': self.assertEqual(data,b'^XA\nsynthetic\n^XZ')

    def test_provider_errors_and_incomplete_content_are_safe(self):
        for status, body in ((401,b'{}'),(302,b'%PDF-1.4 %%EOF'),(200,b'{}'),(200,b'%PDF-truncated')):
            rows, _, _, response = self.fixture(body=body)
            response.status_code = status
            with self.subTest(status=status,body=body), self.assertRaisesRegex(ValueError,'^Shipping label download failed; retry later$'):
                rows._label_binary('pdf')
            self.assertEqual(rows.writes,[])
        rows, _, get, _ = self.fixture()
        get.side_effect = RuntimeError('Authorization: Bearer '+TOKEN)
        with self.assertRaises(ValueError) as failure: rows._label_binary('pdf')
        self.assertNotIn(TOKEN,str(failure.exception))


class TestOAuthReuse(unittest.TestCase):
    def test_existing_refresh_rejects_foreign_or_incomplete_and_preserves_inputs(self):
        validate=top_function('models/meli_util.py','meli_validate_refresh_response')
        original={'access_token':TOKEN,'refresh_token':'synthetic-refresh','user_id':7}
        self.assertEqual(validate(original,'7'),(True,None))
        for body in ({**original,'user_id':8},{**original,'refresh_token':''},None):
            self.assertFalse(validate(body,'7')[0])
        self.assertEqual(original,{'access_token':TOKEN,'refresh_token':'synthetic-refresh','user_id':7})

    def test_existing_refresh_due_decision_is_unchanged(self):
        due=method('models/meli_util.py','_meli_refresh_is_due',
                   {'fields':NS(Datetime=NS(now=lambda:100))})
        self.assertIsNone(due(None,NS(mercadolibre_token_expires_at=101)))
        self.assertIsNone(due(None,NS(mercadolibre_token_expires_at=False)))
        self.assertEqual(due(None,NS(mercadolibre_token_expires_at=99)),'reported expiry already past')
        self.assertEqual(due(None,NS(mercadolibre_token_expires_at=101),True),'refresh_force')


class TestTargetedCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('cleanup',ROOT/'models/meli_link_cleanup.py')
        cls.cleanup=importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.cleanup)

    def test_only_known_api_links_are_cleaned_and_idempotent(self):
        original='https://api.mercadolibre.com/shipment_labels?shipment_ids=1&access_token='+TOKEN+'&response_type=pdf'
        clean=self.cleanup.clean_api_links(original)
        self.assertNotIn(TOKEN,clean)
        self.assertIn('shipment_ids=1',clean); self.assertIn('response_type=pdf',clean)
        self.assertEqual(clean,self.cleanup.clean_api_links(clean))
        portal='/web/content/1?access_token=legitimate-odoo-capability'
        self.assertEqual(portal,self.cleanup.clean_api_links(portal))
        foreign='https://other.invalid/shipment_labels?access_token=unrelated'
        self.assertEqual(foreign,self.cleanup.clean_api_links(foreign))

    def test_legacy_json_token_keys_and_html_credentials_removed(self):
        import json
        link='https://api.mercadolibre.com/shipment_labels?shipment_ids=1&access_token='+TOKEN
        original=json.dumps({TOKEN:{'full_ids':'1','full_link':link},'note':'keep unrelated data'})
        clean=self.cleanup.clean_print_links(original)
        self.assertNotIn(TOKEN,clean)
        self.assertEqual(clean,self.cleanup.clean_print_links(clean))
        original='<a href="https://api.mercadolibre.com/items/MLA1?include_attributes=all&amp;access_token='+TOKEN+'">item</a>'
        clean=self.cleanup.clean_api_links(original)
        self.assertNotIn(TOKEN,clean); self.assertIn('include_attributes=all',clean)


if __name__ == '__main__':
    unittest.main()
