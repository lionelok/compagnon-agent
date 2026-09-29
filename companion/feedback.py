"""Feedback framework: JNPS (journey NPS, per purchased product) and xNPS (overall session experience).

Scale 1-10: 1-6 detractor, 7-8 neutral, 9-10 promoter. NPS = % promoters - % detractors.

* JNPS is pushed when the customer logs in or starts a new chat, about one past purchase (an order placed in
  the app first, then purchases from the activity history) that has not been rated or dismissed before.
* xNPS is asked when the customer ends a session with New chat or Switch, if they talked to the companion.

Every survey shown is kept (answered, dismissed or still pending) in state/feedback.sqlite with the customer
attributes needed to slice a reporting dashboard (region, membership tier, age band, product category...).
"""
import csv
import io
import json
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

COLUMNS = ['survey_id', 'survey_type', 'customer_id', 'status', 'score', 'nps_category', 'comment', 'question',
           'shown_at', 'responded_at', 'trigger',
           'product_id', 'product_name', 'product_category', 'product_domain', 'order_id', 'purchase_source', 'purchased_at',
           'contact_id', 'session_turns', 'session_orders',
           'region', 'membership_tier', 'age_band', 'device_os']

JNPS_QUESTION = 'How likely are you to recommend {product} to a friend or family member?'
XNPS_QUESTION = 'Based on this conversation, how likely are you to recommend the Lifestyle Companion to a friend?'


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def nps_category(score):
    return 'promoter' if score >= 9 else 'neutral' if score >= 7 else 'detractor'


def nps(scores):
    if not scores:
        return None
    cats = Counter(nps_category(s) for s in scores)
    return round(100 * (cats['promoter'] - cats['detractor']) / len(scores), 1)


class FeedbackStore:
    def __init__(self, path, store, catalog):
        self.path, self.store, self.catalog = str(path), store, catalog
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS surveys({', '.join(c + (' TEXT PRIMARY KEY' if c == 'survey_id' else '') for c in COLUMNS)})")
            db.execute('CREATE INDEX IF NOT EXISTS ix_surveys_customer ON surveys(customer_id, survey_type)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _customer_fields(self, user_id):
        c = self.store.customer(user_id)
        return {'region': c['region'], 'membership_tier': c['membership_tier'], 'age_band': c['age_band'],
                'device_os': c['device_os']}

    def _insert(self, row):
        row = {c: row.get(c) for c in COLUMNS}
        with self._lock, self._db() as db:
            db.execute(f"INSERT INTO surveys VALUES ({','.join('?' * len(COLUMNS))})", [row[c] for c in COLUMNS])
        return row

    def get(self, survey_id):
        with self._db() as db:
            r = db.execute('SELECT * FROM surveys WHERE survey_id=?', (survey_id,)).fetchone()
        return dict(r) if r else None

    # ---------- JNPS ----------
    def _past_purchases(self, user_id):
        """Newest first: (product_id, order_id, source, purchased_at)."""
        out = []
        with self.store.transaction() as db:
            orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders WHERE user_id=?', (user_id,))]
        for o in sorted(orders, key=lambda o: o['created_at'], reverse=True):
            for i in o['items']:
                out.append((i['product_id'], o['order_id'], 'app_order', o['created_at']))
        for day, pid, _, _ in sorted(self.catalog.insights(user_id)['purchased'], reverse=True):
            out.append((pid, None, 'purchase_history', day))
        return out

    def next_jnps(self, user_id, trigger):
        """The JNPS survey to push now (reusing an unanswered one), or None when every past purchase is rated."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM surveys WHERE customer_id=? AND survey_type='JNPS'", (user_id,)).fetchall()
        closed = {r['product_id'] for r in rows if r['status'] in ('answered', 'dismissed')}
        pending = {r['product_id']: dict(r) for r in rows if r['status'] == 'pending'}
        for pid, order_id, source, when in self._past_purchases(user_id):
            if pid in closed or pid not in self.catalog.products:
                continue
            if pid in pending:
                return pending[pid]
            p = self.catalog.products[pid]
            return self._insert({
                'survey_id': 'JNPS-' + uuid.uuid4().hex[:12], 'survey_type': 'JNPS', 'customer_id': user_id,
                'status': 'pending', 'question': JNPS_QUESTION.format(product=p['product_name']),
                'shown_at': now_iso(), 'trigger': trigger, 'product_id': pid, 'product_name': p['product_name'],
                'product_category': p['category'], 'product_domain': p['domain'], 'order_id': order_id,
                'purchase_source': source, 'purchased_at': when, **self._customer_fields(user_id)})
        return None

    def answer(self, user_id, survey_id, score=None, comment=None, dismissed=False):
        survey = self.get(survey_id)
        if not survey or survey['customer_id'] != user_id:
            raise KeyError('Survey not found for this customer.')
        if survey['status'] != 'pending':
            return survey  # already answered: keep the first response
        if dismissed:
            fields = {'status': 'dismissed', 'responded_at': now_iso()}
        else:
            score = int(score)
            if not 1 <= score <= 10:
                raise ValueError('Score must be between 1 and 10.')
            fields = {'status': 'answered', 'score': score, 'nps_category': nps_category(score),
                      'comment': (comment or '').strip()[:1000] or None, 'responded_at': now_iso()}
        with self._lock, self._db() as db:
            db.execute(f"UPDATE surveys SET {', '.join(k + '=?' for k in fields)} WHERE survey_id=?",
                       [*fields.values(), survey_id])
        return self.get(survey_id)

    # ---------- xNPS ----------
    def record_xnps(self, user_id, trigger, contact, turns, orders, score=None, comment=None, dismissed=False):
        base = {'survey_id': 'XNPS-' + uuid.uuid4().hex[:12], 'survey_type': 'xNPS', 'customer_id': user_id,
                'question': XNPS_QUESTION, 'shown_at': now_iso(), 'responded_at': now_iso(), 'trigger': trigger,
                'contact_id': contact.get('id') if contact else None, 'session_turns': turns, 'session_orders': orders,
                **self._customer_fields(user_id)}
        if dismissed:
            return self._insert(base | {'status': 'dismissed'})
        score = int(score)
        if not 1 <= score <= 10:
            raise ValueError('Score must be between 1 and 10.')
        return self._insert(base | {'status': 'answered', 'score': score, 'nps_category': nps_category(score),
                                    'comment': (comment or '').strip()[:1000] or None})

    # ---------- personalisation ----------
    def context_block(self, user_id, limit=6):
        with self._db() as db:
            rows = db.execute("SELECT * FROM surveys WHERE customer_id=? AND status='answered' ORDER BY responded_at DESC LIMIT ?",
                              (user_id, limit)).fetchall()
        if not rows:
            return ''
        lines = []
        for r in rows:
            subject = f"product {r['product_name']} ({r['product_category']})" if r['survey_type'] == 'JNPS' else 'a session with you'
            lines.append(f"- {r['responded_at'][:10]} {r['survey_type']}: {subject} rated {r['score']}/10 ({r['nps_category']})"
                         + (f', comment: "{r["comment"]}"' if r['comment'] else ''))
        return '<customer_feedback>\n' + '\n'.join(lines) + '\n</customer_feedback>'

    # ---------- reporting ----------
    def rows(self, survey_type=None):
        q, args = 'SELECT * FROM surveys', []
        if survey_type:
            q, args = q + ' WHERE survey_type=?', [survey_type]
        with self._db() as db:
            return [dict(r) for r in db.execute(q + ' ORDER BY shown_at', args)]

    def export_csv(self):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, delimiter=';')
        w.writeheader()
        for r in self.rows():
            w.writerow(r)
        return buf.getvalue()

    def report(self):
        out = {'generated_at': now_iso(), 'scale': '1-10: 1-6 detractor, 7-8 neutral, 9-10 promoter'}
        for kind in ('JNPS', 'xNPS'):
            rows = self.rows(kind)
            answered = [r for r in rows if r['status'] == 'answered']
            scores = [r['score'] for r in answered]
            cats = Counter(r['nps_category'] for r in answered)

            def breakdown(key):
                groups = defaultdict(list)
                for r in answered:
                    groups[r[key] or 'unknown'].append(r['score'])
                return {k: {'responses': len(v), 'nps': nps(v), 'avg_score': round(sum(v) / len(v), 2)}
                        for k, v in sorted(groups.items())}

            by_day = defaultdict(list)
            for r in answered:
                by_day[r['responded_at'][:10]].append(r['score'])
            section = {
                'shown': len(rows), 'answered': len(answered),
                'dismissed': sum(r['status'] == 'dismissed' for r in rows),
                'pending': sum(r['status'] == 'pending' for r in rows),
                'response_rate': round(100 * len(answered) / len(rows), 1) if rows else None,
                'nps': nps(scores), 'avg_score': round(sum(scores) / len(scores), 2) if scores else None,
                'promoters': cats['promoter'], 'neutrals': cats['neutral'], 'detractors': cats['detractor'],
                'score_distribution': {str(s): scores.count(s) for s in range(1, 11)},
                'by_day': {d: {'responses': len(v), 'nps': nps(v)} for d, v in sorted(by_day.items())},
                'by_membership_tier': breakdown('membership_tier'), 'by_region': breakdown('region'),
                'recent_comments': [{k: r[k] for k in ('responded_at', 'customer_id', 'score', 'nps_category', 'comment',
                                                       'product_name')} for r in reversed(answered) if r['comment']][:20],
            }
            if kind == 'JNPS':
                section['by_product_category'] = breakdown('product_category')
                section['by_product'] = breakdown('product_name')
            else:
                section['by_trigger'] = breakdown('trigger')
            out[kind] = section
        return out
