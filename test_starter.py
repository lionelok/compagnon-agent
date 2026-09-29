import tempfile
import unittest
from pathlib import Path
from store import Store, ToolError
from tool_adapter import call_tool

class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/'test.sqlite'
        self.s = Store(db_path=self.db)
        self.u = 'U000001'
    def tearDown(self):
        self.temp.cleanup()
    def add(self, pid='I0001'):
        return self.s.change(self.u,pid,1,'add')
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ToolError) as ctx:
            function(*args, **kwargs)
        self.assertEqual(code,ctx.exception.code)
    def test_discount_and_persistence(self):
        self.assertEqual(self.add()['total'],'84.02')
        self.assertEqual(Store(db_path=self.db).get_basket(self.u)['total'],'84.02')
    def test_checkout_retry_and_isolation(self):
        self.add()
        q = self.s.prepare_checkout(self.u)['checkout_id']
        self.assert_code('CONFIRMATION_REQUIRED',self.s.confirm_order,self.u,q)
        first = self.s.confirm_order(self.u,q,True)
        self.assertEqual(first,self.s.confirm_order(self.u,q,True))
        self.assertEqual(self.s.get_basket(self.u)['items'],[])
        self.assert_code('ORDER_NOT_FOUND',self.s.get_order,'U000000',first['order_id'])
        self.assert_code('CHECKOUT_NOT_FOUND',self.s.confirm_order,'U000000',q,True)
        self.assert_code('ALREADY_PURCHASED',self.add)
    def test_basket_change_invalidates_quote(self):
        self.add()
        q = self.s.prepare_checkout(self.u)['checkout_id']
        self.add('I0000')
        self.assert_code('CHECKOUT_CHANGED',self.s.confirm_order,self.u,q,True)
        self.assertEqual(len(self.s.get_basket(self.u)['items']),2)
    def test_catalogue_price_change(self):
        self.add()
        q = self.s.prepare_checkout(self.u)['checkout_id']
        self.s.products['I0001']['price']='200'
        self.assert_code('CHECKOUT_CHANGED',self.s.confirm_order,self.u,q,True)
    def test_two_quotes_only_one_order(self):
        self.add()
        a,b = [self.s.prepare_checkout(self.u)['checkout_id'] for _ in range(2)]
        self.s.confirm_order(self.u,a,True)
        self.assert_code('CHECKOUT_CHANGED',self.s.confirm_order,self.u,b,True)
    def test_quantity_remove_and_empty(self):
        self.assert_code('EMPTY_BASKET',self.s.prepare_checkout,self.u)
        for quantity in (True, -1, 0, 1.5, '2',100):
            self.assert_code('INVALID_QUANTITY',self.s.change,self.u,'I0000',quantity,'add')
        self.add()
        self.assert_code('QUANTITY_LIMIT',self.add)
        self.s.change(self.u,'I0001',0,'set')
        self.assertEqual(self.s.get_basket(self.u)['total'],'0.00')
    def test_eligibility_rules(self):
        p = self.s.products['I0001']
        for field,value,code in [('available_from','2027/01/01','NOT_LAUNCHED'),
                                  ('available_regions','west','REGION_UNAVAILABLE'),
                                  ('compatible_os','ios','INCOMPATIBLE_OS')]:
            old=p[field];p[field]=value
            self.assert_code(code,self.add)
            p[field]=old
        self.s.customers[self.u]['owned_items']='I0001'
        self.assert_code('ALREADY_OWNED',self.add)
    def test_subscription_and_repeatable(self):
        p=self.s.products['I0001'];p['is_repeatable']='1';p['is_subscription']='1'
        self.s.change(self.u,'I0001',2,'add')
        self.assertEqual(self.s.get_basket(self.u)['total'],'168.04')
        self.assert_code('SUBSCRIPTION_TERMS_MISSING',self.s.prepare_checkout,self.u)
    def test_adapter_identity_and_errors(self):
        self.assertFalse(call_tool(self.s,self.u,'get_basket',{'user_id':'U000000'})['ok'])
        self.assertFalse(call_tool(self.s,self.u,'confirm_order',{})['ok'])
        self.assertFalse(call_tool(self.s,self.u,'add_to_basket',{})['ok'])
        self.assertEqual(call_tool(self.s,self.u,'add_to_basket',{'product_id':'missing'})['error']['code'],'UNKNOWN_PRODUCT')
        self.add()
        self.assertEqual(self.s.get_basket('U000000')['items'],[])

if __name__=='__main__':
    unittest.main()
