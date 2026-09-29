"""Customer portrait: a short, warm affirmation shown at the top of "What I remember".

Written by Claude Haiku from what the customer has shown us (favourite categories and styles, quality
preference, loyalty, recent purchases, current goal, products they loved). It celebrates their taste and gently
invites the next purchase, without pressure. It never mentions age, household, budget figures or anything
sensitive, and never invents facts. Cached per customer and regenerated only when the underlying signals change.
"""
import hashlib
import json
import threading

PROMPT = """Write a short, warm portrait of a shopper, addressed to them as "you", for the "What I remember" panel of their shopping companion app.

Goal: make them feel recognised and good about their choices, and gently motivate their next purchase.
Rules:
- 2 sentences, 30-45 words in total. Plain text, no emojis, no quotation marks, no lists.
- Sentence 1: affirm their taste or way of shopping, using the signals below (favourite categories, styles, quality, loyalty, what they are working on).
- Sentence 2: a light, encouraging nudge towards a next step that fits them (e.g. completing a plan, treating themselves in a category they love, using their Bucks). Inviting, never pushy, no urgency or scarcity.
- Only use the facts given. Never mention age, household, income, budget amounts, device, region or scores. Don't name product IDs.
- Categories use underscores in the data; write them naturally ("home care", not "home_care").

Signals:
{signals}"""


class Portraits:
    def __init__(self, client, model_id, catalog, feedback, loyalty):
        self.client, self.model_id = client, model_id
        self.catalog, self.feedback, self.loyalty = catalog, feedback, loyalty
        self._cache = {}
        self._lock = threading.Lock()

    def signals(self, user_id, prefs, app_orders):
        o = self.catalog.overview(user_id)
        p, a = o['profile'], o['activity']
        loved = [r['product_category'] for r in self.feedback.rows('JNPS')
                 if r['customer_id'] == user_id and r['nps_category'] == 'promoter']
        return {
            'favourite_categories': a['top_categories'][:4],
            'favourite_styles': a['top_styles'],
            'declared_interests': p['declared_interests'],
            'prefers_quality_tier': p['preferred_quality_tier'],
            'membership_tier': p['membership_tier'],
            'member_for_months': p['tenure_months'],
            'recent_purchases': [x['name'] for x in a['recent_purchases'][:3]] + app_orders[:3],
            'still_in_cart_or_favourites': len(a['saved_in_cart_on_past_visits']) + len(a['favourites_not_bought']),
            'current_goal': prefs.get('goal'),
            'liked_styles_this_session': prefs.get('liked_styles'),
            'categories_they_loved': sorted(set(loved)) or None,
            'bucks_available': self.loyalty.balance(user_id) > 0,
        }

    def get(self, user_id, prefs, app_orders):
        signals = self.signals(user_id, prefs, app_orders)
        key = hashlib.sha1(json.dumps(signals, sort_keys=True, default=str).encode()).hexdigest()
        cached = self._cache.get(user_id)
        if cached and cached[0] == key:
            return cached[1]
        text = self._write(signals)
        with self._lock:
            self._cache[user_id] = (key, text)
        return text

    def _write(self, s):
        try:
            r = self.client.messages.create(model=self.model_id, max_tokens=200, messages=[{
                'role': 'user', 'content': PROMPT.format(signals=json.dumps(s, ensure_ascii=False, default=str))}])
            text = ' '.join(''.join(b.text for b in r.content if b.type == 'text').split()).strip('"')
            if 10 < len(text) < 400:
                return text
        except Exception as e:
            print('Portrait failed:', repr(e)[:200], flush=True)
        return self._fallback(s)

    @staticmethod
    def _fallback(s):
        cats = [c.replace('_', ' ') for c in s['favourite_categories'][:2]]
        styles = ' and '.join(s['favourite_styles'][:2])
        first = (f"You have a real eye for {' and '.join(cats)}" if cats else 'You know what you like') + \
                (f", with a {styles} touch that is unmistakably yours." if styles else '.')
        second = (f"Let's keep your {s['current_goal']} moving, one great find at a time." if s['current_goal']
                  else 'Your next favourite is probably just a few taps away.')
        return f'{first} {second}'
