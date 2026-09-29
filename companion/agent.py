"""The Lifestyle Companion agent: Claude Sonnet 5 on Amazon Bedrock running a tool-use loop.

Trust boundaries
* user_id always comes from the application session; tools never accept it as an argument.
* confirm_order is not a model tool. The application calls it only after the customer presses
  Confirm or sends an unambiguous confirmation while a checkout summary is pending.
* The transcript is append-only (system prompt and tools never change mid-conversation), which
  keeps thinking blocks valid; long conversations roll over into a new segment with a summary.
"""
import json
import os
import re
from decimal import Decimal
from pathlib import Path

from anthropic import AnthropicBedrock, APIError

from order_tools import confirm_order
from store import ToolError
from tool_adapter import call_tool
from companion.contacts import ContactLog, new_contact, now_iso
from companion.loyalty import Loyalty, LoyaltyError
from companion.feedback import FeedbackStore
from companion.offers import OfferLog

ROOT = Path(__file__).resolve().parents[1]
REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'
MODEL_ID = os.environ.get('MODEL_ID', 'us.anthropic.claude-sonnet-5')
SUMMARY_MODEL_ID = os.environ.get('SUMMARY_MODEL_ID', 'us.anthropic.claude-haiku-4-5-20251001-v1:0')
EFFORT = os.environ.get('EFFORT', 'low')
MAX_STEPS = 10
ROLLOVER_CHARS = 240_000   # transcript size that triggers a new, summarised segment
ROLLOVER_MESSAGES = 120

MUTATING = {'add_to_basket', 'update_basket', 'remove_from_basket'}
CONFIRM_RE = re.compile(r"^\s*(yes|yep|yeah|yup|sure|ok|okay|confirm|confirmed|i confirm|go ahead|proceed|"
                        r"place (the |my )?order|buy (it|now|them)|complete (the |my )?(order|purchase)|"
                        r"check ?out|do it|please do)\b", re.I)
NOT_CONFIRM_RE = re.compile(r"\b(no|not|don'?t|wait|remove|delete|change|before|instead|cancel|but|without|"
                            r"swap|replace|add|also|hold|use|using|apply|bucks?|points?|loyalty|rewards?|voucher|discount|pay with)\b", re.I)


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


STRING_LIST = {'type': 'array', 'items': {'type': 'string'}}
CUSTOM_TOOLS = [
    {'name': 'get_customer_overview',
     'description': "The customer's profile (region, household, membership tier, device OS, monthly budget, "
                    "preferred quality tier, interests, owned items) and activity across Home, Shop and Rewards: "
                    "top categories and styles, recent purchases, items left in cart, favourites not yet bought, "
                    "rewards redeemed. Use it to personalise suggestions and to answer questions about their history.",
     'input_schema': _schema({})},
    {'name': 'search_products',
     'description': "Search the catalogue. Returns only products this customer can buy (launched, available in "
                    "their region, compatible with their device OS, not already owned when non-repeatable), each "
                    "with its discounted price and a personal 'why' reason, ranked by fit to the customer unless "
                    "another sort is requested. hidden_counts tells you how many matches were excluded and why.",
     'input_schema': _schema({
         'query': {'type': 'string', 'description': 'Free-text need, e.g. "new home", "running", "headphones".'},
         'categories': {**STRING_LIST, 'description': 'Exact catalogue categories, e.g. ["kitchen","furnishings"].'},
         'domain': {'type': 'string', 'description': 'Catalogue domain, e.g. "home" or "electronics".'},
         'max_price': {'type': 'number', 'description': 'Maximum price per item after discount.'},
         'min_price': {'type': 'number'},
         'min_quality': {'type': 'integer', 'description': 'Minimum quality tier (1-5).'},
         'styles': {**STRING_LIST, 'description': 'Any of: active compact creative eco family practical premium social.'},
         'exclude_ids': {**STRING_LIST, 'description': 'Product ids to leave out (rejected or already shown).'},
         'only_discounted': {'type': 'boolean'},
         'include_subscriptions': {'type': 'boolean'},
         'sort': {'type': 'string', 'enum': ['best_match', 'price_low', 'price_high', 'discount', 'quality']},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12}})},
    {'name': 'get_recommendations',
     'description': "This customer's ranked picks from the recommender model, filtered to products they can buy. "
                    "Optionally narrow by categories and a price cap. Blend these with the customer's current request.",
     'input_schema': _schema({
         'categories': STRING_LIST, 'max_price': {'type': 'number'},
         'exclude_ids': STRING_LIST, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12}})},
    {'name': 'get_complementary_products',
     'description': "Products frequently bought together with the given products (cross-sell), filtered to what "
                    "this customer can buy and optionally capped in price.",
     'input_schema': _schema({'product_ids': STRING_LIST, 'max_price': {'type': 'number'},
                              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8}}, ['product_ids'])},
    {'name': 'get_product_details',
     'description': "Full facts for specific products (price, discount, quality tier, styles, subscription, "
                    "repeatability, OS compatibility) and whether this customer can buy each one. Use to compare "
                    "options or to check a product the customer names.",
     'input_schema': _schema({'product_ids': STRING_LIST}, ['product_ids'])},
    {'name': 'remember_preferences',
     'description': "Save what you learn about the customer's current needs so it persists for the rest of the "
                    "conversation: goal, budget, liked styles, quality, rejected products and short notes. Call it "
                    "whenever they state or change a preference or reject an option. Only the fields you pass change.",
     'input_schema': _schema({
         'goal': {'type': 'string'},
         'budget': {'type': 'number', 'description': 'Budget stated by the customer (per item unless noted).'},
         'budget_scope': {'type': 'string', 'enum': ['per_item', 'total']},
         'liked_styles': STRING_LIST,
         'min_quality': {'type': 'integer', 'minimum': 1, 'maximum': 5},
         'rejected_product_ids': {**STRING_LIST, 'description': 'Products the customer declined; they are excluded from later suggestions.'},
         'unreject_product_ids': STRING_LIST,
         'note': {'type': 'string', 'description': 'One short fact worth remembering, e.g. "moving into a 2-bedroom flat".'}})},
    {'name': 'apply_bucks',
     'description': "Apply the customer's Bucks loyalty points (10 Bucks = $1) to the pending checkout summary, "
                    "or remove them. Only when the customer asks or agrees. Bucks are deducted only when the order is "
                    "created. Returns the payment breakdown (Bucks value, amount still to pay).",
     'input_schema': _schema({'points': {'type': 'integer', 'minimum': 0,
                                         'description': 'Bucks to use; omit to use as many as possible.'},
                              'remove': {'type': 'boolean', 'description': 'True to stop using Bucks on this checkout.'}})},
    {'name': 'show_basket',
     'description': "Display the customer's current basket (items, quantities, prices, total) as a card in the chat. "
                    "Call it whenever you talk about what is in the basket, so the customer can see exactly what you "
                    "refer to.",
     'input_schema': _schema({})},
    {'name': 'show_options',
     'description': "Display product cards to the customer, numbered 1..n in the order given (the numbering the "
                    "customer will use, e.g. 'the second one'). Call it whenever you present products, with the "
                    "options in the same order you describe them.",
     'input_schema': _schema({'title': {'type': 'string'},
                              'product_ids': {**STRING_LIST, 'description': '1-6 product ids, in display order.'}},
                             ['product_ids'])},
]
STARTER_TOOLS = [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']}
                 for t in json.loads((ROOT / 'tool_schemas.json').read_text())]
TOOLS = CUSTOM_TOOLS + STARTER_TOOLS

STATUS = {'get_customer_overview': 'Looking at your profile and history', 'search_products': 'Searching the catalogue',
          'get_recommendations': 'Checking your personalised picks', 'get_complementary_products': 'Finding items that go well together',
          'get_product_details': 'Checking product details', 'remember_preferences': 'Noting your preferences',
          'show_options': 'Preparing options', 'get_basket': 'Checking your basket', 'add_to_basket': 'Adding to your basket',
          'update_basket': 'Updating your basket', 'remove_from_basket': 'Removing from your basket',
          'prepare_checkout': 'Preparing your checkout summary', 'get_order': 'Looking up your order',
          'apply_bucks': 'Applying your Bucks', 'show_basket': 'Showing your basket'}

SYSTEM_TEMPLATE = """You are the Lifestyle Companion, the AI shopping and planning assistant inside a telco lifestyle app with three sections: Home, Shop and Rewards. Customers talk to you by text or voice. You help them plan for a need, discover relevant products, compare and choose, and complete a simulated purchase.

# How you work
1. Understand the need. Identify the goal, budget and preferences. The customer's profile already tells you a lot (household size, device OS, monthly budget, preferred quality tier, interests, history), so use it rather than asking again. Ask one or two focused follow-up questions only when the answer would change what you recommend (for a broad goal like furnishing a new home: which rooms or priorities come first, and the budget). When you can already offer something useful, offer it and ask at the same time.
2. Recommend. Use get_recommendations (the recommender model's ranked output for this customer) together with search_products for the current request: the request and constraints decide what is relevant, the recommender and history rank within that. Present 2-4 options, each with one short reason drawn from its "why" field and facts (price, discount, quality tier, style). Always call show_options with the products in the order you describe them. For planning goals, propose a short plan (for example the 3-4 categories that matter most) and show the best options for the first step.
3. Compare when asked: use get_product_details and give a compact comparison of the facts that differ.
4. Adapt. When the customer changes budget or preferences, or rejects an option, call remember_preferences, then search again honouring the new constraints and excluding rejected products. Resolve references like "the first option" or "that one" with the numbered on-screen options in <app_context>. If nothing fits, say so and offer the closest alternatives (for example slightly above budget, or another category).
5. Engage proactively and cross-sell. When the customer states a goal, keep your suggestions on that goal; bring up unrelated history (such as other cart items) only when there is no active goal. Start sessions with something genuinely useful from the customer's data (an item they saved in their cart on a past visit that is now discounted, a favourite they have not bought, a recommender pick matching their interests). Always cross-sell after an add, like an experienced sales associate: every time add_to_basket succeeds, in that same reply confirm the add in one short sentence, then recommend 1-2 complementary products that complete the purchase. Take them from cross_sell_hint in the add_to_basket result (or call get_complementary_products), call show_options with them, and give each one concrete, factual reason it goes with what they just added: how the categories are used together, a matching style tag, a quality match, a discount, or that it fits the remaining budget. Base every reason only on catalogue facts and the customer's data; never invent features or benefits. One sentence per product, confident and friendly, never pushy; if the customer declines, drop it. If the customer has marketing_opt_in = false, keep suggestions tied to what they are currently shopping for.
6. Purchase. The basket is exactly the "basket:" line in <app_context> (or the latest basket tool result), nothing else. Never say a product is in the basket unless it is listed there, and quote only the names, quantities and prices listed. Items under saved_in_cart_on_past_visits in the profile are from earlier visits and are NOT in the basket; call them "saved on a past visit". Whenever you talk about what is in the basket, call show_basket so the customer sees it. Add, update or remove basket items only when the customer asks or agrees. If adding an item would take the basket over the budget they gave you, say so and ask before adding it. If a request is ambiguous (for example "the cheapest of those" when nothing matched), ask which item they mean instead of guessing. When they want to check out, call prepare_checkout and summarise the items and total in one or two lines; the app shows the summary with a Confirm button. Ask them to press Confirm or say "confirm". Never say an order is placed unless <app_context> reports that the app created it. If the basket changes after a summary, the old summary is void: prepare a new one when they are ready.
7. Bucks. Every customer has Bucks loyalty points (balance in <app_context>; 10 Bucks = $1). When you present a checkout summary, mention their balance and what it is worth and offer to apply it; call apply_bucks when they agree (all by default, or the amount they name). The checkout panel also has a "Use Bucks" switch. Bucks can't exceed the order total, and they are only spent when the order is created.

# Facts and rules
- Every product fact, price and availability must come from a tool result in this conversation. Never invent products, features, brands, specs or prices. Prices are in US dollars (USD): write them like $43.39; quote the discounted price and mention the discount when there is one. Never use other currencies or the word "units" for money.
- Search tools only return products this customer is eligible for. When the customer names a product (e.g. "add Kitchen 04"), act on it directly (add_to_basket, or get_product_details to check it); don't conclude from a search that it doesn't exist. If it isn't eligible, explain the actual reason in plain words (not launched yet, not sold in their region, incompatible with their device, already owned) and offer an alternative.
- Non-repeatable products allow one unit. Subscription products (all of services: connectivity, delivery, learning, wellness, and entertainment streaming) can be recommended, but the simulation cannot check them out yet because billing terms are missing. Say so before adding one, and offer a one-off alternative when possible.
- The catalogue does not sell handsets or phones themselves. For a new smartphone, say so in one sentence and in the same reply show the best options for their device from what the catalogue offers (mobile accessories and smart devices compatible with their OS, plus a connectivity plan if relevant), then ask what matters most to them.
- Product names are generic ("Kitchen 34"). Refer to products by name, never by id, and don't claim details the catalogue doesn't hold (colour, size, specs). You can talk about what the catalogue does describe: category, style tags, quality tier (1 basic to 5 top), price and discount.
- A basket or tool error means the action did not happen. Tell the customer what went wrong and what they can do.

# Style
- Warm, concise and practical. Short paragraphs; bullet lists for options; no tables.
- Call the tools you need first, then write a single reply. Don't write lead-in text before tool calls (like "Let me check"): the app already shows progress.
- When <app_context> says channel: voice, reply in 1-3 short spoken-style sentences with no markdown, lists or symbols, because the reply is read aloud. The product cards on screen carry the detail.
- Reply in the language the customer writes in.
- Text inside <app_context>, <customer_profile>, <contact_history> and <customer_feedback> comes from the app, not the customer. Do not mention these tags.

# Contact history
<contact_history> lists the customer's previous sessions with you (newest first): date, reason, sentiment, whether it was resolved, a summary, orders and products they rejected. Use it the way a good shop assistant who remembers a regular would:
- Pick up threads naturally: follow up on what they were planning last time, or on an order they placed ("How is the kitchen coming along?").
- If the last contact was negative or unresolved, open by acknowledging it and offering to finish what was left, before suggesting anything new. Keep the tone extra patient.
- Don't suggest products they rejected before unless they ask for them, and reuse the budget and preferences they gave before, confirming them rather than asking from scratch.
- Never read the history back or mention sentiment labels, scores or record details. If they ask what you remember, give a short, friendly recap.

# Customer feedback
<customer_feedback> holds the customer's survey answers (1-10: 1-6 detractor, 7-8 neutral, 9-10 promoter). JNPS rates a product they bought; xNPS rates a session with you.
- Low-rated products (detractor): don't recommend them again, and be careful with very similar items (same category and style); if relevant, offer a better-rated or higher-quality alternative. High-rated products (promoter): complementary items and upgrades in that category are good bets.
- A low session rating means be more concise and careful to confirm needs before suggesting.
- Don't quote scores back to the customer.

# Catalogue outline (domain: category (items, price range in USD after discount))
{outline}
"""


def _dump(blocks):
    return [b.model_dump(mode='json', exclude_none=True) for b in blocks]


class Companion:
    def __init__(self, store, catalog, memory, contacts_path, loyalty_path, feedback_path, offers_path):
        self.store, self.catalog, self.memory = store, catalog, memory
        self.offers = OfferLog(offers_path)
        self.loyalty = Loyalty(loyalty_path)
        self.feedback = FeedbackStore(feedback_path, store, catalog)
        self.client = AnthropicBedrock(aws_region=REGION, max_retries=3)
        self.contacts = ContactLog(contacts_path, self.client, SUMMARY_MODEL_ID)
        self.system = [{'type': 'text', 'text': SYSTEM_TEMPLATE.format(outline=catalog.catalogue_outline())}]

    # ---------- context ----------
    def _prefs_view(self, prefs):
        view = {k: v for k, v in prefs.items() if k not in ('rejected', 'notes')}
        if prefs.get('rejected'):
            view['rejected_products'] = [f"{self.catalog.products[p]['product_name']} ({p})"
                                         for p in prefs['rejected'] if p in self.catalog.products]
        if prefs.get('notes'):
            view['notes'] = prefs['notes'][-8:]
        return view

    def _context(self, user_id, state, channel, events):
        basket = self.store.get_basket(user_id)
        lines = [f'channel: {channel}', f'demo date: {self.store.demo_date.isoformat()}']
        lines.append('remembered preferences: ' + (json.dumps(self._prefs_view(state['prefs'])) if state['prefs'] else 'none yet'))
        if state['shown']:
            lines.append('options on screen: ' + '; '.join(
                f"{o['n']}. {o['name']} ({o['product_id']}) ${o['price']}" for o in state['shown']))
        if basket['items']:
            lines.append('basket: ' + '; '.join(f"{i['quantity']} x {i['product_name']} ({i['product_id']}) ${i['line_total']}"
                                                for i in basket['items']) + f" | total ${basket['total']}")
        else:
            lines.append('basket: empty')
        lines.append('checkout summary awaiting confirmation: ' + ('yes' if state['checkout'] else 'no'))
        bucks = self.loyalty.summary(user_id)
        applied = (state['checkout'] or {}).get('bucks')
        lines.append(f"Bucks balance: {bucks['points']} (worth ${bucks['value']})"
                     + (f"; {applied['points']} applied to the pending checkout, ${applied['amount_due']} left to pay"
                        if applied and applied['points'] else ''))
        for e in events:
            lines.append(f'app event: {e}')
        return '<app_context>\n' + '\n'.join(lines) + '\n</app_context>'

    def _profile_block(self, user_id, state):
        text = '<customer_profile>\n' + json.dumps(self.catalog.overview(user_id)) + '\n</customer_profile>'
        history = self.contacts.context_block(user_id)
        if history:
            text += '\n' + history
        rated = self.feedback.context_block(user_id)
        if rated:
            text += '\n' + rated
        if state['summary']:
            text += '\n<earlier_conversation_summary>\n' + state['summary'] + '\n</earlier_conversation_summary>'
        return text

    # ---------- long conversations ----------
    def _maybe_rollover(self, state):
        size = len(json.dumps(state['messages']))
        if size < ROLLOVER_CHARS and len(state['messages']) < ROLLOVER_MESSAGES:
            return
        transcript = [f"{u['role']}: {u.get('text', '')}" for u in state['ui'][-60:] if u.get('text')]
        prompt = ('Summarise this shopping conversation for the assistant who continues it. Keep: the goal, budget, '
                  'preferences and changes to them, products shown/rejected/added (with names), the basket and orders, '
                  'open questions. Max 200 words.\n\n' + '\n'.join(transcript) +
                  (f"\n\nEarlier summary: {state['summary']}" if state['summary'] else ''))
        try:
            r = self.client.messages.create(model=SUMMARY_MODEL_ID, max_tokens=600,
                                            messages=[{'role': 'user', 'content': prompt}])
            state['summary'] = ''.join(b.text for b in r.content if b.type == 'text')
        except APIError:
            state['summary'] = (state['summary'] + '\n' + '\n'.join(transcript[-12:]))[-3000:]
        state['messages'] = []  # a new segment: nothing earlier is edited, the old one is simply closed

    # ---------- tools ----------
    def _fix_ids(self, ids):
        out = []
        for pid in ids or []:
            fixed = self.catalog.find_ids(pid)
            if fixed:
                out.append(fixed)
        return out

    def _run_tool(self, user_id, state, name, args, emit):
        cat, prefs = self.catalog, state['prefs']
        rejected = set(prefs.get('rejected', []))
        if name == 'get_customer_overview':
            return {'ok': True, 'data': cat.overview(user_id)}
        if name == 'search_products':
            args = dict(args)
            args['exclude_ids'] = list(set(self._fix_ids(args.get('exclude_ids'))) | rejected)
            return {'ok': True, 'data': cat.search(user_id, **args)}
        if name == 'get_recommendations':
            return {'ok': True, 'data': cat.recommendations(
                user_id, args.get('categories'), args.get('max_price'),
                set(self._fix_ids(args.get('exclude_ids'))) | rejected, args.get('limit', 6))}
        if name == 'get_complementary_products':
            basket_ids = {i['product_id'] for i in self.store.get_basket(user_id)['items']}
            return {'ok': True, 'data': cat.complements(user_id, self._fix_ids(args['product_ids']),
                                                        args.get('max_price'), rejected | basket_ids, args.get('limit', 4))}
        if name == 'get_product_details':
            ids = self._fix_ids(args['product_ids'])
            missing = [p for p in args['product_ids'] if not cat.find_ids(p)]
            return {'ok': True, 'data': {'products': [cat.card(p, user_id, cat.reason(user_id, p)) for p in ids],
                                         'not_found': missing or None}}
        if name == 'remember_preferences':
            for key in ('goal', 'budget', 'budget_scope', 'liked_styles', 'min_quality'):
                if key in args:
                    prefs[key] = args[key]
            if args.get('rejected_product_ids'):
                prefs['rejected'] = sorted(rejected | set(self._fix_ids(args['rejected_product_ids'])))
            if args.get('unreject_product_ids'):
                prefs['rejected'] = sorted(set(prefs.get('rejected', [])) - set(self._fix_ids(args['unreject_product_ids'])))
            if args.get('note'):
                prefs.setdefault('notes', []).append(args['note'])
            emit({'type': 'prefs', 'prefs': self._prefs_view(prefs)})
            return {'ok': True, 'data': {'saved': self._prefs_view(prefs)}}
        if name == 'show_options':
            ids = [p for p in dict.fromkeys(self._fix_ids(args['product_ids']))][:6]
            cards = [cat.card(p, user_id, cat.reason(user_id, p)) for p in ids]
            state['shown'] = [{'n': i + 1, 'product_id': c['product_id'], 'name': c['name'], 'price': c['price']}
                              for i, c in enumerate(cards)]
            emit({'type': 'products', 'title': args.get('title') or 'Options for you', 'items': cards})
            return {'ok': True, 'data': {'displayed': [f"{o['n']}. {o['name']} {o['price']}" for o in state['shown']],
                                         'ineligible': [c['name'] for c in cards if not c.get('eligible')] or None}}

        if name == 'show_basket':
            basket = self.store.get_basket(user_id)
            emit({'type': 'basket_card', 'basket': basket})
            return {'ok': True, 'data': basket}
        if name == 'apply_bucks':
            if not state['checkout']:
                return {'ok': False, 'error': {'code': 'NO_PENDING_CHECKOUT',
                                               'message': 'Prepare a checkout summary before applying Bucks.'}}
            self._set_bucks(user_id, state, None if args.get('remove') else args.get('points', -1), emit)
            return {'ok': True, 'data': self.checkout_view(user_id, state['checkout'])}

        # Starter tools, dispatched through the supplied adapter with the session's user_id.
        if 'product_id' in args:
            args = dict(args, product_id=cat.find_ids(args['product_id']) or args['product_id'])
        result = call_tool(self.store, user_id, name, args)
        if result['ok'] and name in MUTATING and state.get('contact'):
            label = cat.products.get(args.get('product_id'), {}).get('product_name', args.get('product_id'))
            verb = {'add_to_basket': 'added', 'update_basket': 'set quantity of', 'remove_from_basket': 'removed'}[name]
            state['contact']['actions'].append(f"{verb} {label}" + (f" (qty {args['quantity']})" if 'quantity' in args else ''))
        if result['ok'] and name in MUTATING:
            state['checkout'] = None  # any basket change voids an earlier summary
            emit({'type': 'basket', 'basket': result['data']})
            emit({'type': 'checkout_cleared'})
            if name == 'add_to_basket':
                in_basket = {i['product_id'] for i in result['data']['items']}
                budget = prefs.get('budget')
                if budget is not None and prefs.get('budget_scope') == 'total':
                    budget = round(float(budget) - float(result['data']['total']), 2)
                    result['remaining_budget'] = budget
                cap = self._cross_sell_cap(user_id, args['product_id'], budget)
                hint = cat.complements(user_id, [args['product_id']], cap, rejected | in_basket, 2,
                                       subscriptions=False)['results'] \
                    if budget is None or budget > 0 else []
                result['cross_sell_hint'] = [{k: h[k] for k in ('product_id', 'name', 'price', 'category', 'styles',
                                                                'quality_tier', 'discount_pct', 'why', 'pairs_with')}
                                             for h in hint]
                self.offers.record(user_id, 'cross_sell', 'chat', hint)
                if hint:
                    result['next_step'] = ('Cross-sell now, in this same reply: confirm the add in one sentence, then '
                                           'call show_options with these cross_sell_hint products and give each one a '
                                           'factual reason it goes with the item just added.')
        elif result['ok'] and name == 'prepare_checkout':
            state['checkout'] = {'checkout_id': result['data']['checkout_id'], 'summary': result['data']['summary'],
                                 'bucks': None}
            emit({'type': 'checkout', 'checkout': self.checkout_view(user_id, state['checkout'])})
            plan = self.loyalty.plan(user_id, result['data']['summary']['total'])
            result['data'] = {'summary': result['data']['summary'], 'requires_confirmation': True,
                              'bucks_available': {'balance': self.loyalty.balance(user_id), 'usable_now': plan['max_points'],
                                                  'worth': plan['value'], 'amount_due_if_used': plan['amount_due']},
                              'note': 'The app now shows this summary with Confirm and Cancel buttons and a Use Bucks '
                                      'switch. The order is created only after the customer confirms.'}
        elif not result['ok'] and name == 'prepare_checkout':
            state['checkout'] = None
            emit({'type': 'checkout_cleared'})
        return result

    # ---------- confirmation handled by the application ----------
    def _confirm(self, user_id, state, emit):
        pending = state['checkout']
        if not pending:
            return 'The customer asked to confirm, but no checkout summary is pending. Nothing was ordered.'
        bucks = pending.get('bucks') or {}
        plan = self.loyalty.plan(user_id, pending['summary']['total'], bucks.get('points', 0))  # re-checked now
        try:
            order = confirm_order(self.store, user_id, checkout_id=pending['checkout_id'], customer_confirmed=True)
            self.offers.attribute(user_id, order)
            try:
                used = self.loyalty.redeem(user_id, order['order_id'], plan['points'])
            except LoyaltyError:
                used = {'points': 0, 'value': '0.00'}
            order = self.order_view(order, used)
        except ToolError as e:
            state['checkout'] = None
            emit({'type': 'checkout_cleared'})
            return f'The customer confirmed, but the order could not be created: {e.code} ({e.message}). Nothing was ordered.'
        state['checkout'] = None
        if state.get('contact'):
            state['contact']['orders'].append({'order_id': order['order_id'], 'total': order['total'],
                                               'bucks_used': order['bucks']['points'], 'amount_paid': order['amount_paid'],
                                               'items': [f"{i['quantity']} x {i['product_name']}" for i in order['items']]})
        emit({'type': 'order', 'order': order})
        emit({'type': 'basket', 'basket': self.store.get_basket(user_id)})
        items = ', '.join(f"{i['quantity']} x {i['product_name']}" for i in order['items'])
        paid = (f"; {order['bucks']['points']} Bucks used (worth ${order['bucks']['value']}), ${order['amount_paid']} paid, "
                f"{self.loyalty.balance(user_id)} Bucks left" if order['bucks']['points'] else '')
        return (f"The customer explicitly confirmed the checkout summary and the app created simulated order "
                f"{order['order_id']} ({items}; total ${order['total']}{paid}). Thank them, give the order reference, "
                f"and optionally suggest one complementary product.")

    def _cross_sell_cap(self, user_id, product_id, budget=None):
        """Add-ons stay in proportion to the purchase, like a good sales associate's: at most 1.2x the price of
        the item just added (never below $15), and within any budget the customer gave."""
        price = float(self.catalog.final_price(self.catalog.products[product_id]))
        cap = max(1.2 * price, 15.0)
        return min(cap, float(budget)) if budget is not None else cap

    def _auto_cross_sell(self, user_id, state, added, reply):
        """Fallback when the model added an item without recommending anything to go with it."""
        prefs = state['prefs']
        basket = self.store.get_basket(user_id)
        budget = prefs.get('budget')
        if budget is not None and prefs.get('budget_scope') == 'total':
            budget = float(budget) - float(basket['total'])
            if budget <= 0:
                return
        exclude = set(prefs.get('rejected', [])) | {i['product_id'] for i in basket['items']}
        cap = self._cross_sell_cap(user_id, added[-1], budget)
        picks = self.catalog.complements(user_id, added[::-1], cap, exclude, 2, subscriptions=False)['results']
        if not picks:
            return
        self.offers.record(user_id, 'cross_sell', 'chat', picks)
        base = self.catalog.products[added[-1]]['product_name']
        state['shown'] = [{'n': i + 1, 'product_id': c['product_id'], 'name': c['name'], 'price': c['price']}
                          for i, c in enumerate(picks)]
        state['events'].append('After the add, the app showed complementary options: '
                               + '; '.join(f"{o['n']}. {o['name']} ${o['price']}" for o in state['shown']))
        line = f"\n\nTo complete it, customers who choose {base} often add these:"
        reply.append(line)
        yield {'type': 'text', 'delta': line}
        yield {'type': 'products', 'title': f'Goes well with {base}', 'items': picks}

    # ---------- Bucks ----------
    def checkout_view(self, user_id, checkout):
        """Pending checkout as the UI shows it: summary plus Bucks breakdown."""
        if not checkout:
            return None
        total = checkout['summary']['total']
        bucks = checkout.get('bucks') or {'points': 0, 'value': '0.00', 'amount_due': total}
        return {'checkout_id': checkout['checkout_id'], 'summary': checkout['summary'], 'bucks': bucks,
                'bucks_balance': self.loyalty.balance(user_id),
                'bucks_usable': self.loyalty.plan(user_id, total)['max_points']}

    def order_view(self, order, used):
        paid = (Decimal(order['total']) - Decimal(used['value'])).quantize(Decimal('0.01'))
        return dict(order, bucks=used, amount_paid=str(paid))

    def _set_bucks(self, user_id, state, points, emit):
        """points: None removes Bucks, -1 applies as many as possible, otherwise that many (capped)."""
        checkout = state['checkout']
        if points is None:
            checkout['bucks'] = None
        else:
            checkout['bucks'] = self.loyalty.plan(user_id, checkout['summary']['total'], None if points < 0 else points)
        emit({'type': 'checkout', 'checkout': self.checkout_view(user_id, checkout)})

    def set_bucks(self, user_id, use):
        """The customer flipped the Use Bucks switch in the checkout panel."""
        with self.memory.lock(user_id):
            state = self.memory.load(user_id)
            if not state['checkout']:
                return None
            self._set_bucks(user_id, state, -1 if use else None, lambda e: None)
            b = state['checkout']['bucks']
            state['events'].append(f"The customer applied {b['points']} Bucks (worth ${b['value']}) to the checkout; "
                                   f"${b['amount_due']} left to pay." if b else 'The customer removed Bucks from the checkout.')
            self.memory.save(user_id, state)
            return self.checkout_view(user_id, state['checkout'])

    # ---------- contact lifecycle ----------
    def close_contact(self, user_id, fresh=False):
        """End the customer's open contact: write it to the contact history and start the next session clean.

        fresh=True also forgets remembered preferences (the customer asked for a new chat). Otherwise
        preferences carry over; the conversation itself is summarised into the contact record.
        """
        with self.memory.lock(user_id):
            state = self.memory.load(user_id)
            contact = state.get('contact')
            entries = [u for u in state['ui'] if contact and u.get('ts', '') >= contact['started_at']]
            prefs = self._prefs_view(state['prefs'])
            nxt = self.memory.fresh_state(keep_prefs=None if fresh else state['prefs'])
            self.memory.save(user_id, nxt)
        if not contact:
            return None
        return self.contacts.finalise(user_id, contact, entries, prefs)

    # ---------- one conversational turn ----------
    def turn(self, user_id, text, channel='text', action=None, survey=None):
        """Generator of UI events for one customer turn."""
        with self.memory.lock(user_id):
            state = self.memory.load(user_id)
            if not state.get('contact'):
                state['contact'] = new_contact()
            out, ui_items = [], []
            emit = out.append

            def flush():
                while out:
                    ev = out.pop(0)
                    if ev['type'] in ('products', 'order', 'checkout', 'basket_card'):
                        ui_items.append(ev)
                    yield ev

            events = list(state['events'])
            state['events'] = []
            text = (text or '').strip()
            if action == 'confirm' or (state['checkout'] and CONFIRM_RE.match(text) and not NOT_CONFIRM_RE.search(text)):
                events.append(self._confirm(user_id, state, emit))
                text = text or 'Confirm my order.'
            elif action == 'cancel_checkout' and state['checkout']:
                state['checkout'] = None
                emit({'type': 'checkout_cleared'})
                events.append('The customer cancelled the checkout summary; the basket is unchanged and no order was placed.')
                text = text or 'Cancel the checkout for now.'
            yield from flush()

            if action == 'jnps_followup' and survey:
                user_text = (f"[The customer just answered the journey survey about their purchase of {survey['product_name']}: "
                             f"{survey['score']}/10 ({survey['nps_category']})"
                             + (f', comment: "{survey["comment"]}"' if survey.get('comment') else '') +
                             '. Reply in 1-2 short sentences. Promoter: thank them warmly and, if it fits, suggest one '
                             'complementary product (show_options). Neutral: thank them and ask briefly what would have '
                             'made it better. Detractor: apologise sincerely, acknowledge their comment, and offer concrete '
                             'help such as a better-rated or higher-quality alternative (show_options). Do not quote the score.]')
            elif action == 'session_start':
                user_text = ('[The customer just opened the companion. Greet them briefly. If there is contact history, '
                             'first pick up the thread from their last contact (and address it first if it was negative '
                             'or unresolved). Then offer one or two helpful, personalised suggestions from their profile, '
                             'activity and past contacts (for example an item they saved in their cart on a past visit and can '
                             'still buy (say "saved on your last visit", never "in your basket"), '
                             'a favourite on discount, a top recommender pick that fits their budget, or the next step of '
                             'a plan they started). Show them with show_options. End with a short question about what '
                             'they want to plan or buy today.]')
            else:
                user_text = text
                state['ui'].append({'role': 'user', 'text': text, 'channel': channel, 'ts': now_iso()})

            self._maybe_rollover(state)
            parts = []
            if not state['messages']:
                parts.append(self._profile_block(user_id, state))
            parts.append(self._context(user_id, state, channel, events))
            parts.append(user_text)
            state['messages'].append({'role': 'user', 'content': '\n\n'.join(parts)})

            reply = []
            added, cross_sold, basket_shown, changed = [], True, False, False
            try:
                for step in range(MAX_STEPS):
                    with self.client.messages.stream(
                            model=MODEL_ID, max_tokens=8000, system=self.system, tools=TOOLS,
                            messages=state['messages'], output_config={'effort': EFFORT}) as stream:
                        for event in stream:
                            if event.type == 'content_block_delta' and event.delta.type == 'text_delta':
                                reply.append(event.delta.text)
                                yield {'type': 'text', 'delta': event.delta.text}
                        final = stream.get_final_message()
                    if final.stop_reason == 'refusal':
                        msg = "Sorry, I can't help with that. Is there something else I can help you plan or find?"
                        reply.append(msg)
                        yield {'type': 'text', 'delta': msg}
                        break
                    state['messages'].append({'role': 'assistant', 'content': _dump(final.content)})
                    calls = [b for b in final.content if b.type == 'tool_use']
                    if not calls:
                        break
                    results = []
                    for call in calls:
                        yield {'type': 'status', 'text': STATUS.get(call.name, 'Working')}
                        try:
                            result = self._run_tool(user_id, state, call.name, call.input or {}, emit)
                            if result['ok'] and call.name == 'add_to_basket':
                                added.append(self.catalog.find_ids((call.input or {}).get('product_id')))
                                cross_sold = False
                            elif result['ok'] and call.name == 'show_options' and added:
                                cross_sold = True
                            elif result['ok'] and call.name == 'show_basket':
                                basket_shown = True
                            changed |= result['ok'] and call.name in MUTATING
                        except ToolError as e:
                            result = {'ok': False, 'error': {'code': e.code, 'message': e.message}}
                        except Exception as e:  # never leave a tool_use without its result
                            result = {'ok': False, 'error': {'code': 'TOOL_FAILED', 'message': str(e)[:200]}}
                        yield from flush()
                        results.append({'type': 'tool_result', 'tool_use_id': call.id,
                                        'content': json.dumps(result, default=str), 'is_error': not result['ok']})
                    state['messages'].append({'role': 'user', 'content': results})
                    if reply and not reply[-1].endswith('\n'):
                        reply.append('\n\n')
                        yield {'type': 'text', 'delta': '\n\n'}
                else:
                    yield {'type': 'text', 'delta': "\n\nI've done several steps. What would you like next?"}
                # Guarantees, whatever the model wrote: an add is always followed by a cross-sell, and any talk about
                # the basket comes with the real basket on screen.
                if added and not cross_sold:
                    for ev in self._auto_cross_sell(user_id, state, [a for a in added if a], reply):
                        if ev['type'] == 'products':
                            ui_items.append(ev)
                        yield ev
                text_now = ''.join(reply)
                basket = self.store.get_basket(user_id)
                if not basket_shown and basket['items'] and (changed or re.search(r'\b(basket|cart)\b', text_now, re.I)):
                    ui_items.append({'type': 'basket_card', 'basket': basket})
                    yield {'type': 'basket_card', 'basket': basket}
            except APIError as e:
                yield {'type': 'error', 'message': 'The assistant is temporarily unavailable. Please try again.'}
                print('Bedrock error:', getattr(e, 'status_code', ''), str(e)[:500], flush=True)
                # Drop the unanswered user turn so the transcript stays valid.
                if state['messages'] and state['messages'][-1]['role'] == 'user' and isinstance(state['messages'][-1]['content'], str):
                    state['messages'].pop()
            except Exception as e:
                print('Turn failed:', repr(e)[:500], flush=True)
                yield {'type': 'error', 'message': 'Something went wrong on my side. Please try again.'}
                msgs = state['messages']
                if msgs and msgs[-1]['role'] == 'assistant' and any(b.get('type') == 'tool_use' for b in msgs[-1]['content']):
                    msgs.pop()  # a tool call without its result would make the transcript invalid
            finally:
                yield from flush()
                text_out = ''.join(reply).strip()
                if text_out or ui_items:
                    state['ui'].append({'role': 'assistant', 'text': text_out, 'items': ui_items, 'ts': now_iso()})
                state['ui'] = state['ui'][-200:]
                self.memory.save(user_id, state)
                yield {'type': 'done'}
