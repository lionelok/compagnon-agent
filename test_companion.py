"""Offline tests for the companion layer (no AWS calls)."""
import tempfile
import unittest
from pathlib import Path

from store import Store
from companion.agent import CONFIRM_RE, NOT_CONFIRM_RE, Companion
from companion.catalog import Catalog
from companion.contacts import ContactLog, new_contact
from companion.loyalty import Loyalty, LoyaltyError
from companion.feedback import FeedbackStore, nps, nps_category
from companion.memory import Memory

ROOT = Path(__file__).resolve().parent
FULL = ROOT / 'data' / 'full'


def is_confirmation(text):
    return bool(CONFIRM_RE.match(text) and not NOT_CONFIRM_RE.search(text))


@unittest.skipUnless((FULL / 'insights.db').exists(), 'full datasets not present')
class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        d = Path(self.temp.name)
        self.store = Store(db_path=d / 'sim.sqlite', products_path=FULL / 'products.csv', customers_path=FULL / 'customers.csv')
        self.cat = Catalog(self.store, FULL / 'insights.db')
        self.mem = Memory(d / 'sessions.sqlite')
        self.companion = Companion(self.store, self.cat, self.mem, d / 'contacts.jsonl', d / 'loyalty.sqlite', d / 'feedback.sqlite')
        self.u = 'U000001'

    def tearDown(self):
        self.temp.cleanup()

    def test_search_returns_only_eligible_products_within_budget(self):
        r = self.cat.search(self.u, 'new home', max_price=100, limit=12)
        self.assertTrue(r['results'])
        for card in r['results']:
            self.assertIsNone(self.cat.eligibility(self.u, card['product_id']))
            self.assertLessEqual(float(card['price']), 100)
            self.assertIn(card['category'], {'furnishings', 'kitchen', 'home_care', 'garden'})

    def test_card_price_matches_basket_price(self):
        pid = self.cat.search(self.u, categories=['audio'], only_discounted=True)['results'][0]['product_id']
        basket = self.store.change(self.u, pid, 1, 'add')
        self.assertEqual(self.cat.card(pid)['price'], basket['items'][0]['unit_price'])

    def test_recommendations_follow_recommender_order(self):
        recs = self.cat.recommendations(self.u, limit=3)['results']
        ranks = [self.cat.insights(self.u)['recs'][c['product_id']][0] for c in recs]
        self.assertEqual(ranks, sorted(ranks))

    def test_rejected_products_are_excluded_and_options_numbered(self):
        state = self.mem.load(self.u)
        first = self.cat.search(self.u, categories=['kitchen'])['results'][0]['product_id']
        self.companion._run_tool(self.u, state, 'remember_preferences', {'rejected_product_ids': [first], 'budget': 100}, lambda e: None)
        r = self.companion._run_tool(self.u, state, 'search_products', {'categories': ['kitchen']}, lambda e: None)
        self.assertNotIn(first, [c['product_id'] for c in r['data']['results']])
        ids = [c['product_id'] for c in r['data']['results'][:3]]
        self.companion._run_tool(self.u, state, 'show_options', {'product_ids': ids}, lambda e: None)
        self.assertEqual([o['product_id'] for o in state['shown']], ids)
        self.assertEqual(state['shown'][0]['n'], 1)

    def test_basket_change_voids_pending_checkout(self):
        state = self.mem.load(self.u)
        pid = self.cat.search(self.u, categories=['kitchen'], include_subscriptions=False)['results'][0]['product_id']
        self.companion._run_tool(self.u, state, 'add_to_basket', {'product_id': pid}, lambda e: None)
        self.companion._run_tool(self.u, state, 'prepare_checkout', {}, lambda e: None)
        self.assertIsNotNone(state['checkout'])
        self.companion._run_tool(self.u, state, 'remove_from_basket', {'product_id': pid}, lambda e: None)
        self.assertIsNone(state['checkout'])

    def test_bucks_applied_and_deducted_once_on_confirmation(self):
        state = self.mem.load(self.u)
        pid = self.cat.search(self.u, categories=['kitchen'], include_subscriptions=False, max_price=90)['results'][0]['product_id']
        self.companion._run_tool(self.u, state, 'add_to_basket', {'product_id': pid}, lambda e: None)
        self.companion._run_tool(self.u, state, 'prepare_checkout', {}, lambda e: None)
        r = self.companion._run_tool(self.u, state, 'apply_bucks', {}, lambda e: None)
        total = float(state['checkout']['summary']['total'])
        points = r['data']['bucks']['points']
        self.assertEqual(points, min(1000, int(total * 10)))
        events = []
        self.companion._confirm(self.u, state, events.append)
        order = next(e['order'] for e in events if e['type'] == 'order')
        self.assertEqual(order['bucks']['points'], points)
        self.assertAlmostEqual(float(order['amount_paid']), total - points / 10, places=2)
        self.assertEqual(self.companion.loyalty.balance(self.u), 1000 - points)
        self.companion.loyalty.redeem(self.u, order['order_id'], points)  # a retried confirmation
        self.assertEqual(self.companion.loyalty.balance(self.u), 1000 - points)

    def test_next_level_is_a_step_up_and_for_u_pairs_with_basket(self):
        pid = self.cat.search(self.u, categories=['kitchen'], max_price=80, include_subscriptions=False)['results'][0]['product_id']
        base = self.cat.products[pid]
        ups = self.cat.upsell(self.u, [pid])
        self.assertTrue(0 < len(ups) <= 3)
        for c in ups:
            self.assertEqual(c['category'], base['category'])
            self.assertGreater(c['quality_tier'], int(float(base['quality_tier'])))
            self.assertFalse(c['subscription'])
        cross = self.cat.complements(self.u, [pid], limit=3, subscriptions=False)['results']
        self.assertTrue(cross)
        self.assertTrue(all(c['pairs_with'] == base['product_name'] and not c['subscription'] for c in cross))

    def test_jnps_asks_each_past_purchase_once_newest_first(self):
        fb = self.companion.feedback
        first = fb.next_jnps(self.u, 'login')
        self.assertEqual(first['survey_type'], 'JNPS')
        self.assertEqual(fb.next_jnps(self.u, 'login')['survey_id'], first['survey_id'])  # unanswered: reused
        answered = fb.answer(self.u, first['survey_id'], 4, 'Broke quickly')
        self.assertEqual(answered['nps_category'], 'detractor')
        second = fb.next_jnps(self.u, 'new_chat')
        self.assertNotEqual(second['product_id'], first['product_id'])
        fb.answer(self.u, second['survey_id'], dismissed=True)
        self.assertEqual(fb.get(second['survey_id'])['status'], 'dismissed')
        self.assertIn('rated 4/10 (detractor)', fb.context_block(self.u))
        with self.assertRaises(KeyError):
            fb.answer('U000000', first['survey_id'], 9)  # another customer's survey

    def test_app_orders_are_surveyed_before_older_history(self):
        pid = self.cat.search(self.u, categories=['kitchen'], include_subscriptions=False)['results'][0]['product_id']
        self.store.change(self.u, pid, 1, 'add')
        q = self.store.prepare_checkout(self.u)['checkout_id']
        order = self.store.confirm_order(self.u, q, True)
        survey = self.companion.feedback.next_jnps(self.u, 'login')
        self.assertEqual((survey['product_id'], survey['order_id'], survey['purchase_source']), (pid, order['order_id'], 'app_order'))

    def test_xnps_and_report(self):
        fb = self.companion.feedback
        for score in (10, 9, 8, 3):
            fb.record_xnps(self.u, 'new_chat', {'id': 'CON-x'}, 3, 0, score)
        fb.record_xnps(self.u, 'switch', None, 1, 0, dismissed=True)
        r = fb.report()['xNPS']
        self.assertEqual((r['answered'], r['dismissed'], r['promoters'], r['neutrals'], r['detractors']), (4, 1, 2, 1, 1))
        self.assertEqual(r['nps'], 25.0)
        self.assertEqual(r['response_rate'], 80.0)
        self.assertIn('xNPS', fb.export_csv())

    def test_customers_are_separated(self):
        a = self.mem.load('U000001'); a['prefs']['budget'] = 50; self.mem.save('U000001', a)
        self.assertEqual(self.mem.load('U000000')['prefs'], {})


class _FailingClient:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError('offline')


class ContactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'contacts.jsonl'
        self.log = ContactLog(self.path, _FailingClient(), 'none')

    def tearDown(self):
        self.temp.cleanup()

    def session(self, start='2026-09-29T08:00:00+00:00'):
        contact = new_contact() | {'started_at': start, 'orders': [{'order_id': 'ORD-1', 'total': '43.39', 'items': ['1 x Kitchen 11']}]}
        ui = [{'role': 'user', 'text': 'I need kitchen items', 'channel': 'voice', 'ts': '2026-09-29T08:00:05+00:00'},
              {'role': 'assistant', 'text': 'Here are some', 'ts': '2026-09-29T08:00:12+00:00',
               'items': [{'type': 'products', 'items': [{'name': 'Kitchen 11'}]}]},
              {'role': 'user', 'text': 'Add it', 'channel': 'text', 'ts': '2026-09-29T08:02:00+00:00'},
              {'role': 'assistant', 'text': 'Added.', 'ts': '2026-09-29T08:02:30+00:00'}]
        return contact, ui

    def test_record_fields_and_persistence(self):
        contact, ui = self.session()
        rec = self.log.finalise('U000001', contact, ui, {'budget': 60})
        for key in ('customer_id', 'started_at', 'ended_at', 'duration_seconds', 'reason', 'sentiment', 'summary', 'verbatim'):
            self.assertIn(key, rec)
        self.assertEqual(rec['duration_seconds'], 150)
        self.assertEqual(rec['channel'], 'mixed')
        self.assertEqual(rec['turns'], 2)
        self.assertEqual(rec['analysis'], 'fallback')
        self.assertEqual(rec['verbatim'][1]['products_shown'], ['Kitchen 11'])
        reloaded = ContactLog(self.path, _FailingClient(), 'none')
        self.assertEqual(reloaded.history('U000001')[0]['contact_id'], rec['contact_id'])
        self.assertIn('ORD-1', reloaded.context_block('U000001'))
        self.assertEqual(reloaded.history('U000000'), [])

    def test_session_without_customer_message_is_not_a_contact(self):
        contact, ui = self.session()
        self.assertIsNone(self.log.finalise('U000001', contact, [ui[1]], {}))
        self.assertFalse(self.path.exists())

    def test_csv_export(self):
        contact, ui = self.session()
        self.log.finalise('U000001', contact, ui, {})
        csv_text = self.log.export_csv('U000001')
        self.assertTrue(csv_text.startswith('contact_id;customer_id;started_at'))
        self.assertIn('customer: I need kitchen items', csv_text)


class LoyaltyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.l = Loyalty(Path(self.temp.name) / 'l.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    def test_default_balance_and_rate(self):
        self.assertEqual(self.l.summary('U1'), {'points': 1000, 'value': '100.00', 'points_per_unit': 10})

    def test_plan_is_capped_by_total_and_balance(self):
        self.assertEqual(self.l.plan('U1', '43.39')['points'], 433)
        self.assertEqual(self.l.plan('U1', '43.39')['amount_due'], '0.09')
        self.assertEqual(self.l.plan('U1', '250.00')['points'], 1000)
        self.assertEqual(self.l.plan('U1', '250.00', 200)['amount_due'], '230.00')

    def test_cannot_overspend(self):
        self.l.redeem('U1', 'ORD-1', 900)
        with self.assertRaises(LoyaltyError):
            self.l.redeem('U1', 'ORD-2', 200)
        self.assertEqual(self.l.balance('U1'), 100)


class NpsMathTests(unittest.TestCase):
    def test_categories_and_score(self):
        self.assertEqual([nps_category(s) for s in (1, 6, 7, 8, 9, 10)],
                         ['detractor', 'detractor', 'neutral', 'neutral', 'promoter', 'promoter'])
        self.assertEqual(nps([10, 10, 7, 2]), 25.0)
        self.assertIsNone(nps([]))


class ConfirmationGateTests(unittest.TestCase):
    def test_confirmations(self):
        for text in ('yes', 'Yes please', 'confirm', 'ok go ahead', 'place the order', 'Confirmed.'):
            self.assertTrue(is_confirmation(text), text)

    def test_not_confirmations(self):
        for text in ('Remove that item before placing the order.', 'no', "yes but remove the bag",
                     'wait', 'ok add one more', 'what is the total?', "don't place it yet",
                     'Yes, use my Bucks', 'yes apply my points', 'ok pay with bucks and confirm'):
            self.assertFalse(is_confirmation(text), text)


if __name__ == '__main__':
    unittest.main()
