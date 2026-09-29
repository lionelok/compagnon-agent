"""Customer insight and product discovery layer.

Joins the customer profile (train), the product catalogue, interaction history across
Home / Shop / Rewards and the recommender output. Every product returned to the agent
has passed the starter's eligibility checks (store.product) and carries the discounted
price computed exactly as the basket computes it.
"""
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from decimal import Decimal
from functools import lru_cache

from store import Store, ToolError, money

EVENT_WEIGHT = {'view': 1.0, 'favorite': 2.5, 'cart': 3.0, 'purchase': 4.0, 'redeem': 3.0,
                'return': -3.0, 'cancel': -2.0}

# Everyday words customers use, mapped to catalogue categories.
SYNONYMS = {
    'phone': ['mobile_accessories', 'smart_devices', 'connectivity'],
    'smartphone': ['mobile_accessories', 'smart_devices', 'connectivity'],
    'mobile': ['mobile_accessories', 'connectivity'],
    'headphone': ['audio'], 'headphones': ['audio'], 'earbuds': ['audio'], 'speaker': ['audio'], 'music': ['audio', 'streaming'],
    'laptop': ['computing'], 'computer': ['computing'], 'tablet': ['computing', 'smart_devices'],
    'watch': ['smart_devices', 'accessories'], 'smart': ['smart_devices'], 'wearable': ['smart_devices'],
    'data': ['connectivity'], 'internet': ['connectivity'], 'wifi': ['connectivity'], 'fibre': ['connectivity'],
    'fiber': ['connectivity'], 'bundle': ['connectivity'], 'airtime': ['connectivity'],
    'home': ['furnishings', 'kitchen', 'home_care', 'garden'], 'house': ['furnishings', 'kitchen', 'home_care', 'garden'],
    'apartment': ['furnishings', 'kitchen', 'home_care'], 'furniture': ['furnishings'], 'sofa': ['furnishings'],
    'bed': ['furnishings'], 'decor': ['furnishings'], 'cooking': ['kitchen'], 'cook': ['kitchen'],
    'appliance': ['kitchen', 'home_care'], 'cleaning': ['home_care'], 'clean': ['home_care'],
    'plants': ['garden'], 'outdoor': ['outdoor', 'garden'],
    'clothes': ['clothing'], 'shirt': ['clothing'], 'jacket': ['clothing'], 'shoes': ['footwear'],
    'sneakers': ['footwear'], 'bag': ['bags'], 'backpack': ['bags', 'travel_accessories'],
    'book': ['books'], 'reading': ['books'], 'game': ['gaming'], 'games': ['gaming'], 'console': ['gaming'],
    'movies': ['streaming'], 'tv': ['streaming'], 'series': ['streaming'], 'video': ['streaming'],
    'art': ['creative_hobbies'], 'craft': ['creative_hobbies'], 'hobby': ['creative_hobbies'],
    'travel': ['travel_accessories', 'local_experiences'], 'trip': ['travel_accessories', 'local_experiences'],
    'experience': ['local_experiences'], 'gym': ['fitness'], 'sport': ['fitness', 'outdoor'],
    'workout': ['fitness'], 'hiking': ['outdoor'], 'camping': ['outdoor'],
    'course': ['learning'], 'learn': ['learning'], 'study': ['learning'], 'school': ['learning', 'books'],
    'delivery': ['delivery'], 'food': ['delivery', 'kitchen'], 'spa': ['wellness'], 'health': ['wellness', 'fitness'],
    'wellbeing': ['wellness'], 'relax': ['wellness'],
}


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Catalog:
    def __init__(self, store: Store, insights_path):
        self.store = store
        self.insights_path = str(insights_path)
        self._local = threading.local()
        self.products = store.products
        self.domains = defaultdict(set)
        for p in self.products.values():
            self.domains[p['domain']].add(p['category'])
        self.categories = {c for cs in self.domains.values() for c in cs}
        self.by_name = {p['product_name'].lower(): pid for pid, p in self.products.items()}

    # ---------- data access ----------
    @property
    def db(self):
        if getattr(self._local, 'db', None) is None:
            self._local.db = sqlite3.connect(f'file:{self.insights_path}?mode=ro', uri=True, check_same_thread=False)
        return self._local.db

    def final_price(self, p):
        return Decimal(money(Decimal(p['price']) * (1 - Decimal(p['offer_discount'] or '0'))))

    def eligibility(self, user_id, product_id):
        """Returns None when the customer can buy the product, otherwise {code, message}."""
        try:
            self.store.product(user_id, product_id)
            return None
        except ToolError as e:
            return {'code': e.code, 'message': e.message}

    def card(self, product_id, user_id=None, reason=None):
        p = self.products[product_id]
        disc = _num(p['offer_discount'])
        card = {
            'product_id': product_id, 'name': p['product_name'], 'domain': p['domain'],
            'category': p['category'], 'brand': p['brand_id'],
            'price': money(self.final_price(p)), 'list_price': money(p['price']),
            'discount_pct': round(disc * 100), 'quality_tier': int(_num(p['quality_tier'])),
            'styles': p['style_tags'].split(), 'subscription': p['is_subscription'] == '1',
            'repeatable': p['is_repeatable'] == '1', 'description': p['description'],
        }
        if p['compatible_os'] != 'any':
            card['compatible_os'] = p['compatible_os']
        if reason:
            card['why'] = reason
        if user_id:
            issue = self.eligibility(user_id, product_id)
            card['eligible'] = issue is None
            if issue:
                card['not_eligible_reason'] = issue['message']
        return card

    # ---------- customer understanding ----------
    @lru_cache(maxsize=512)
    def insights(self, user_id):
        c = self.store.customer(user_id)
        rows = self.db.execute(
            'SELECT product_id, app_section, event_type, event_date, amount FROM interactions '
            'WHERE user_id=? ORDER BY event_date', (user_id,)).fetchall()
        cat_w, style_w, section = Counter(), Counter(), Counter()
        purchased, carted, favourited, redeemed, returned = [], [], [], [], set()
        for pid, sec, ev, day, amount in rows:
            p = self.products.get(pid)
            if not p:
                continue
            w = EVENT_WEIGHT.get(ev, 0.5)
            cat_w[p['category']] += w
            for s in p['style_tags'].split():
                style_w[s] += w
            section[sec] += 1
            if ev == 'purchase':
                purchased.append((day, pid, sec, amount))
            elif ev == 'cart':
                carted.append((day, pid, sec))
            elif ev == 'favorite':
                favourited.append((day, pid, sec))
            elif ev == 'redeem':
                redeemed.append((day, pid))
            elif ev in ('return', 'cancel'):
                returned.add(pid)
        for interest in c.get('declared_interests', '').split():
            cat_w[interest] += 6.0
        bought = {pid for _, pid, _, _ in purchased} | set(c.get('owned_items', '').split())
        recs = self.db.execute('SELECT product_id, rank, score FROM recommendations WHERE user_id=? ORDER BY rank',
                               (user_id,)).fetchall()
        return {
            'cat_w': cat_w, 'style_w': style_w, 'section': section, 'n_events': len(rows),
            'purchased': purchased, 'bought': bought, 'returned': returned,
            'abandoned': [x for x in reversed(carted) if x[1] not in bought],
            'favourites': [x for x in reversed(favourited) if x[1] not in bought],
            'redeemed': redeemed, 'recs': {pid: (rank, score) for pid, rank, score in recs},
            'rec_order': [pid for pid, _, _ in recs],
            'last_date': rows[-1][3] if rows else None,
        }

    def personal_score(self, user_id, product_id, ins=None):
        ins = ins or self.insights(user_id)
        c = self.store.customer(user_id)
        p = self.products[product_id]
        top_cat = max(ins['cat_w'].values(), default=1) or 1
        top_style = max(ins['style_w'].values(), default=1) or 1
        score = 0.0
        if product_id in ins['recs']:
            score += 1.5 * (1 - (ins['recs'][product_id][0] - 1) / 60)
        score += 1.0 * max(ins['cat_w'].get(p['category'], 0), 0) / top_cat
        score += 0.5 * sum(max(ins['style_w'].get(s, 0), 0) for s in p['style_tags'].split()) / (2 * top_style)
        pref_q = _num(c.get('preferred_quality'), 3)
        score += 0.4 * (1 - min(abs(_num(p['quality_tier']) - pref_q), 4) / 4)
        score += 0.3 * _num(p['offer_discount'])
        if product_id in ins['returned']:
            score -= 1.0
        return score

    def reason(self, user_id, product_id, ins=None):
        """Short, factual explanation of why this product fits the customer."""
        ins = ins or self.insights(user_id)
        c = self.store.customer(user_id)
        p = self.products[product_id]
        bits = []
        if product_id in ins['recs']:
            bits.append(f"ranked #{ins['recs'][product_id][0]} for you by the recommender")
        if any(pid == product_id for _, pid, _ in ins['abandoned'][:20]):
            bits.append('you saved it in your cart on a past visit')
        elif any(pid == product_id for _, pid, _ in ins['favourites'][:20]):
            bits.append('on your favourites list')
        if p['category'] in c.get('declared_interests', '').split():
            bits.append(f"matches your interest in {p['category'].replace('_', ' ')}")
        elif ins['cat_w'].get(p['category'], 0) > 0 and p['category'] in [k for k, _ in ins['cat_w'].most_common(4)]:
            bits.append(f"{p['category'].replace('_', ' ')} is one of your most-browsed categories")
        liked = [s for s in p['style_tags'].split() if s in [k for k, _ in ins['style_w'].most_common(3)]]
        if liked:
            bits.append(f"{' & '.join(liked)} style you tend to choose")
        if _num(p['quality_tier']) == _num(c.get('preferred_quality'), -1):
            bits.append('matches your preferred quality tier')
        if _num(p['offer_discount']) >= 0.1:
            bits.append(f"{round(_num(p['offer_discount']) * 100)}% off right now")
        return '; '.join(bits[:3]) or 'fits your request'

    def overview(self, user_id):
        c = self.store.customer(user_id)
        ins = self.insights(user_id)
        name = lambda pid: self.products[pid]['product_name'] if pid in self.products else pid

        def still_buyable(items, limit):
            out, seen = [], set()
            for row in items:
                pid = row[1]
                if pid in seen or self.eligibility(user_id, pid):
                    continue
                seen.add(pid)
                out.append({'product_id': pid, 'name': name(pid), 'price': money(self.final_price(self.products[pid])),
                            'section': row[2], 'date': row[0]})
                if len(out) == limit:
                    break
            return out

        recent = sorted(ins['purchased'], reverse=True)[:6]
        spend = defaultdict(float)
        for day, pid, sec, amount in ins['purchased']:
            spend[day[:7]] += _num(amount)
        return {
            'user_id': user_id,
            'profile': {
                'region': c['region'], 'age_band': c['age_band'], 'household_size': c['household_size'],
                'membership_tier': c['membership_tier'], 'device': f"{c['device_type']} {c['device_os']}",
                'device_os': c['device_os'], 'tenure_months': c['tenure_months'],
                'monthly_budget': c['monthly_budget'], 'preferred_quality_tier': c['preferred_quality'],
                'language': c['language'], 'marketing_opt_in': c['marketing_opt_in'] == '1',
                'declared_interests': c.get('declared_interests', '').split(),
                'owned_items': [name(i) for i in c.get('owned_items', '').split()],
            },
            'activity': {
                'events': ins['n_events'], 'last_active': ins['last_date'],
                'by_section': dict(ins['section']),
                'top_categories': [k for k, v in ins['cat_w'].most_common(6) if v > 0],
                'top_styles': [k for k, v in ins['style_w'].most_common(3) if v > 0],
                'recent_purchases': [{'name': name(pid), 'date': d, 'section': s, 'paid': money(_num(a))}
                                     for d, pid, s, a in recent],
                'avg_monthly_spend': money(sum(spend.values()) / max(len(spend), 1)) if spend else '0.00',
                'saved_in_cart_on_past_visits': still_buyable(ins['abandoned'], 4),
                'favourites_not_bought': still_buyable(ins['favourites'], 4),
                'rewards_redeemed': [name(pid) for _, pid in ins['redeemed'][-4:]],
            },
        }

    # ---------- discovery ----------
    def resolve_categories(self, text):
        cats = set()
        for tok in re.findall(r'[a-z_]+', (text or '').lower()):
            if tok in self.categories:
                cats.add(tok)
            elif tok in self.domains:
                cats |= self.domains[tok]
            elif tok in SYNONYMS:
                cats |= set(SYNONYMS[tok])
            elif tok.endswith('s') and tok[:-1] in SYNONYMS:
                cats |= set(SYNONYMS[tok[:-1]])
        return cats

    def search(self, user_id, query=None, categories=None, domain=None, max_price=None, min_price=None,
               min_quality=None, styles=None, exclude_ids=(), only_discounted=False, include_subscriptions=True,
               sort='best_match', limit=6):
        self.store.customer(user_id)
        ins = self.insights(user_id)
        cats = set(categories or [])
        bad = cats - self.categories
        if bad:
            cats = (cats - bad) | self.resolve_categories(' '.join(bad))
        if query and not cats and not domain:
            cats = self.resolve_categories(query)
        words = [w for w in re.findall(r'[a-z0-9]+', (query or '').lower()) if len(w) > 2]
        styles = set(styles or [])
        exclude = set(exclude_ids or ())
        hidden = Counter()
        pool = []
        for pid, p in self.products.items():
            if pid in exclude:
                continue
            if cats and p['category'] not in cats:
                continue
            if domain and p['domain'] != domain:
                continue
            if not cats and not domain and words:
                hay = f"{p['product_name']} {p['category']} {p['domain']} {p['style_tags']} {p['description']}".lower()
                if not any(w in hay for w in words):
                    continue
            price = float(self.final_price(p))
            if max_price is not None and price > float(max_price):
                hidden['over_budget'] += 1
                continue
            if min_price is not None and price < float(min_price):
                continue
            if min_quality is not None and _num(p['quality_tier']) < float(min_quality):
                continue
            if styles and not styles & set(p['style_tags'].split()):
                continue
            if only_discounted and _num(p['offer_discount']) <= 0:
                continue
            if not include_subscriptions and p['is_subscription'] == '1':
                continue
            issue = self.eligibility(user_id, pid)
            if issue:
                hidden[issue['code']] += 1
                continue
            pool.append((pid, price))
        key = {
            'price_low': lambda x: x[1], 'price_high': lambda x: -x[1],
            'discount': lambda x: -_num(self.products[x[0]]['offer_discount']),
            'quality': lambda x: -_num(self.products[x[0]]['quality_tier']),
        }.get(sort, lambda x: -self.personal_score(user_id, x[0], ins))
        pool.sort(key=key)
        results = [self.card(pid, reason=self.reason(user_id, pid, ins)) for pid, _ in pool[:max(1, min(limit, 12))]]
        return {'matches': len(pool), 'results': results,
                'searched_categories': sorted(cats) if cats else None,
                'hidden_counts': dict(hidden) or None}

    def recommendations(self, user_id, categories=None, max_price=None, exclude_ids=(), limit=6):
        ins = self.insights(user_id)
        cats = set(categories or [])
        if cats - self.categories:
            cats = (cats & self.categories) | self.resolve_categories(' '.join(cats - self.categories))
        out = []
        for pid in ins['rec_order']:
            p = self.products.get(pid)
            if not p or pid in exclude_ids or (cats and p['category'] not in cats):
                continue
            if max_price is not None and float(self.final_price(p)) > float(max_price):
                continue
            if self.eligibility(user_id, pid):
                continue
            out.append(self.card(pid, reason=self.reason(user_id, pid, ins)))
            if len(out) >= limit:
                break
        source = 'recommender model output'
        if len(out) < limit:  # recommender list is exhausted for this filter: fall back to personal scoring
            extra = self.search(user_id, categories=list(cats) or None, max_price=max_price,
                                exclude_ids=set(exclude_ids) | {c['product_id'] for c in out}, limit=limit - len(out))
            out += extra['results']
            if extra['results']:
                source += ' + personalised catalogue ranking'
        return {'source': source, 'results': out}

    def complements(self, user_id, product_ids, max_price=None, exclude_ids=(), limit=4, subscriptions=True):
        ins = self.insights(user_id)
        exclude = set(exclude_ids) | set(product_ids) | ins['bought']
        scored, source = Counter(), {}

        def credit(pid, points, base):
            scored[pid] += points
            if points > source.get(pid, (0, None))[0]:
                source[pid] = (points, base)

        for base in product_ids:
            for comp, lift in self.db.execute(
                    'SELECT complement_id, lift FROM item_complements WHERE product_id=? ORDER BY lift DESC LIMIT 40', (base,)):
                credit(comp, lift, base)
        for base in product_ids:
            cat = self.products.get(base, {}).get('category')
            for comp_cat, lift in self.db.execute(
                    'SELECT complement_category, lift FROM category_complements WHERE category=? ORDER BY lift DESC LIMIT 4', (cat,)):
                for pid, p in self.products.items():
                    if p['category'] == comp_cat:
                        credit(pid, 0.3 * lift * (1 + self.personal_score(user_id, pid, ins)) / 3, base)
        out = []
        for pid, _ in scored.most_common():
            p = self.products.get(pid)
            if not p or pid in exclude or (not subscriptions and p['is_subscription'] == '1'):
                continue
            if max_price is not None and float(self.final_price(p)) > float(max_price):
                continue
            if self.eligibility(user_id, pid):
                continue
            card = self.card(pid, reason=self.reason(user_id, pid, ins))
            card['pairs_with'] = self.products[source[pid][1]]['product_name']
            out.append(card)
            if len(out) >= limit:
                break
        return {'results': out}

    def upsell(self, user_id, base_ids, exclude_ids=(), limit=3, subscriptions=False):
        """Next Level: a real step up (higher quality tier) from products the customer has chosen or bought,
        in the same category and at a sensible price jump. Falls back to premium picks in favourite categories."""
        ins = self.insights(user_id)
        seen = set(exclude_ids) | set(base_ids)
        queues = []
        for base in base_ids:
            b = self.products.get(base)
            if not b:
                continue
            bq, bp = _num(b['quality_tier']), float(self.final_price(b))
            cands = []
            for pid, p in self.products.items():
                q, price = _num(p['quality_tier']), float(self.final_price(p))
                if pid in seen or p['category'] != b['category'] or q <= bq or price > bp * 1.8 + 10:
                    continue
                if not subscriptions and p['is_subscription'] == '1':
                    continue
                if self.eligibility(user_id, pid):
                    continue
                cands.append((self.personal_score(user_id, pid, ins) + 0.3 * (q - bq), pid, base))
            queues.append(sorted(cands, reverse=True))
        out = []
        while len(out) < limit and any(queues):
            for queue in queues:
                while queue and queue[0][1] in seen:
                    queue.pop(0)
                if queue and len(out) < limit:
                    _, pid, base = queue.pop(0)
                    seen.add(pid)
                    card = self.card(pid)
                    b = self.products[base]
                    card['why'] = f"Step up from {b['product_name']}: quality {card['quality_tier']} vs {int(_num(b['quality_tier']))}"
                    out.append(card)
        if len(out) < limit:
            top = [c for c, v in ins['cat_w'].most_common(4) if v > 0]
            pool = [pid for pid, p in self.products.items()
                    if pid not in seen and p['category'] in top and _num(p['quality_tier']) >= 4
                    and (subscriptions or p['is_subscription'] != '1') and not self.eligibility(user_id, pid)]
            pool.sort(key=lambda pid: -self.personal_score(user_id, pid, ins))
            for pid in pool[:limit - len(out)]:
                card = self.card(pid)
                card['why'] = f"Premium pick in {card['category'].replace('_', ' ')}"
                out.append(card)
        return out

    def find_ids(self, text):
        """Map free-text product names ('Audio 02') to ids; used to repair model slips."""
        pid = (text or '').strip()
        if pid in self.products:
            return pid
        return self.by_name.get(pid.lower())

    def catalogue_outline(self):
        lines = []
        for domain in sorted(self.domains):
            parts = []
            for cat in sorted(self.domains[domain]):
                prices = [float(self.final_price(p)) for p in self.products.values() if p['category'] == cat]
                subs = sum(p['is_subscription'] == '1' for p in self.products.values() if p['category'] == cat)
                parts.append(f"{cat} ({len(prices)} items, {min(prices):.0f}-{max(prices):.0f}"
                             + (f", {subs} subscriptions" if subs else '') + ')')
            lines.append(f"- {domain}: " + '; '.join(parts))
        return '\n'.join(lines)
