"""Management dashboard metrics.

Definitions (also shown on the dashboard):
* FCR (first contact resolution), last 7 days: share of customers who contacted the companion exactly once in the
  last 7 days, i.e. their need was handled without a repeat contact. Also reports how many of those single
  contacts were marked resolved by the contact analysis.
* Session average: mean duration and customer messages per recorded session (contact) in the period.
* JNPS / xNPS of the month: NPS (% promoters - % detractors, scale 1-10) of surveys answered this calendar month,
  with last month for comparison.
* Sentiment: mean contact sentiment score (-1..1) mapped to a 0-100 index, plus the label split.
* Selling value: simulated orders in the period (value, orders, average order value, Bucks redeemed).
* Upsell / cross-sell: offers made and the order value they converted into (7-day attribution).
* Interactions by membership tier: sessions, customers, average duration, sentiment and sales per tier.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from companion.feedback import nps, nps_category

TIERS = ['basic', 'plus', 'premium']


def _utc(ts):
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _month_bounds(now, back=0):
    y, m = now.year, now.month - back
    while m < 1:
        y, m = y - 1, m + 12
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def period_bounds(period, now):
    if period == '7d':
        return now - timedelta(days=7), now + timedelta(seconds=1), 'Last 7 days'
    if period == '30d':
        return now - timedelta(days=30), now + timedelta(seconds=1), 'Last 30 days'
    if period == 'all':
        return datetime(2000, 1, 1, tzinfo=timezone.utc), now + timedelta(seconds=1), 'All time'
    start, end = _month_bounds(now)
    return start, end, start.strftime('%B %Y')


def _nps_block(rows, kind, now):
    def month(back):
        start, end = _month_bounds(now, back)
        answered = [r for r in rows if r['survey_type'] == kind and r['status'] == 'answered'
                    and start <= _utc(r['responded_at']) < end]
        shown = [r for r in rows if r['survey_type'] == kind and start <= _utc(r['shown_at']) < end]
        scores = [r['score'] for r in answered]
        cats = Counter(nps_category(s) for s in scores)
        return {'month': start.strftime('%B %Y'), 'nps': nps(scores), 'responses': len(scores), 'shown': len(shown),
                'response_rate': round(100 * len(scores) / len(shown), 1) if shown else None,
                'promoters': cats['promoter'], 'neutrals': cats['neutral'], 'detractors': cats['detractor'],
                'avg_score': round(sum(scores) / len(scores), 1) if scores else None}
    cur, prev = month(0), month(1)
    cur['previous_month'] = {'month': prev['month'], 'nps': prev['nps']}
    return cur


def build(period, store, contacts, feedback, offers, loyalty):
    now = datetime.now(timezone.utc)
    start, end, label = period_bounds(period, now)
    tier_of = lambda uid: store.customers.get(uid, {}).get('membership_tier', 'unknown')

    # ---- contacts (sessions) ----
    all_contacts = [r for recs in contacts._by_user.values() for r in recs]
    in_period = [r for r in all_contacts if start <= _utc(r['started_at']) < end]

    week = [r for r in all_contacts if _utc(r['started_at']) >= now - timedelta(days=7)]
    per_customer = Counter(r['customer_id'] for r in week)
    single = [c for c, n in per_customer.items() if n == 1]
    single_resolved = sum(1 for r in week if r['customer_id'] in single and r.get('resolved'))
    fcr = {'window': 'Last 7 days', 'rate': round(100 * len(single) / len(per_customer), 1) if per_customer else None,
           'customers': len(per_customer), 'unique_interactions': len(single), 'contacts': len(week),
           'repeat_customers': len(per_customer) - len(single), 'single_contact_resolved': single_resolved}

    durations = [r['duration_seconds'] for r in in_period]
    turns = [r['turns'] for r in in_period]
    sessions = {'count': len(in_period), 'customers': len({r['customer_id'] for r in in_period}),
                'avg_duration_s': round(sum(durations) / len(durations)) if durations else None,
                'avg_messages': round(sum(turns) / len(turns), 1) if turns else None,
                'voice_share': round(100 * sum(r['channel'] in ('voice', 'mixed') for r in in_period) / len(in_period), 1)
                if in_period else None}

    scores = [r['sentiment_score'] for r in in_period if r.get('analysis') == 'model']
    labels = Counter(r['sentiment'] for r in in_period)
    avg = sum(scores) / len(scores) if scores else None
    sentiment = {'index': round((avg + 1) * 50) if avg is not None else None,
                 'avg_score': round(avg, 2) if avg is not None else None, 'analysed': len(scores),
                 'split': {k: labels.get(k, 0) for k in ('positive', 'neutral', 'mixed', 'negative')}}

    # ---- surveys ----
    rows = feedback.rows()
    jnps, xnps = _nps_block(rows, 'JNPS', now), _nps_block(rows, 'xNPS', now)

    # ---- sales ----
    with store.transaction() as db:
        orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders')]
    orders = [o for o in orders if start <= _utc(o['created_at']) < end]
    value = sum(float(o['total']) for o in orders)
    redeemed = sum(float((loyalty.redemption(o['order_id']) or {}).get('value', 0)) for o in orders)
    by_day = defaultdict(lambda: {'value': 0.0, 'orders': 0})
    for o in orders:
        d = by_day[o['created_at'][:10]]
        d['value'] += float(o['total'])
        d['orders'] += 1
    # Every day of the period (days without orders show as zero), at most the last 31 days.
    first = min(datetime.fromisoformat(d).date() for d in by_day) if period == 'all' and by_day else start.date()
    if period == 'all' and not by_day:
        first = now.date()
    last = now.date()
    span = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)][-31:] if orders or period != 'all' else []
    sales = {'value': round(value, 2), 'orders': len(orders), 'aov': round(value / len(orders), 2) if orders else None,
             'items': sum(i['quantity'] for o in orders for i in o['items']),
             'bucks_redeemed_value': round(redeemed, 2), 'paid_value': round(value - redeemed, 2),
             'by_day': [{'date': d, 'value': round(by_day[d]['value'], 2), 'orders': by_day[d]['orders']} for d in span]}

    offer_stats = offers.summary(start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'))

    # ---- membership ----
    tiers = {t: {'tier': t, 'interactions': 0, 'customers': set(), 'durations': [], 'sentiments': [], 'sales_value': 0.0}
             for t in TIERS}
    for r in in_period:
        t = tiers.setdefault(tier_of(r['customer_id']), {'tier': tier_of(r['customer_id']), 'interactions': 0,
                                                          'customers': set(), 'durations': [], 'sentiments': [],
                                                          'sales_value': 0.0})
        t['interactions'] += 1
        t['customers'].add(r['customer_id'])
        t['durations'].append(r['duration_seconds'])
        if r.get('analysis') == 'model':
            t['sentiments'].append(r['sentiment_score'])
    for o in orders:
        tiers.setdefault(tier_of(o['user_id']), {'tier': tier_of(o['user_id']), 'interactions': 0, 'customers': set(),
                                                 'durations': [], 'sentiments': [], 'sales_value': 0.0})
        tiers[tier_of(o['user_id'])]['sales_value'] += float(o['total'])
    base = Counter(c['membership_tier'] for c in store.customers.values())
    membership = [{'tier': t['tier'], 'interactions': t['interactions'], 'customers': len(t['customers']),
                   'base_customers': base.get(t['tier'], 0),
                   'reach_pct': round(100 * len(t['customers']) / base[t['tier']], 2) if base.get(t['tier']) else None,
                   'avg_duration_s': round(sum(t['durations']) / len(t['durations'])) if t['durations'] else None,
                   'sentiment_index': round((sum(t['sentiments']) / len(t['sentiments']) + 1) * 50) if t['sentiments'] else None,
                   'sales_value': round(t['sales_value'], 2)} for t in tiers.values()]

    return {'generated_at': now.isoformat(timespec='seconds'), 'period': {'key': period, 'label': label,
            'start': start.isoformat(timespec='seconds'), 'end': min(end, now).isoformat(timespec='seconds')},
            'fcr': fcr, 'sessions': sessions, 'jnps': jnps, 'xnps': xnps, 'sentiment': sentiment, 'sales': sales,
            'upsell': offer_stats['upsell'], 'cross_sell': offer_stats['cross_sell'], 'membership': membership}
