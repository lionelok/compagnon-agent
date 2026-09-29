"""HTTP layer: customer identification, streamed chat turns, basket view, recommendations, voice output
and the contact history (one record per customer session).

Run locally:  uvicorn companion.server:app --port 8000   (from the ai_companion_starter folder)
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import boto3
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from store import Store, ToolError
from companion.agent import Companion, REGION
from companion.catalog import Catalog
from companion.memory import Memory
from companion.feedback import XNPS_QUESTION
from companion.portrait import Portraits
from companion import dashboard as dashboards
from companion.agent import SUMMARY_MODEL_ID

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('DATA_DIR', ROOT / 'data' / 'full'))
STATE = Path(os.environ.get('STATE_DIR', ROOT / 'state'))
STATE.mkdir(parents=True, exist_ok=True)

store = Store(db_path=STATE / 'simulation.sqlite', products_path=DATA / 'products.csv',
              customers_path=DATA / 'customers.csv', currency='USD')
catalog = Catalog(store, DATA / 'insights.db')
memory = Memory(STATE / 'sessions.sqlite')
companion = Companion(store, catalog, memory, STATE / 'contact_history.jsonl', STATE / 'loyalty.sqlite',
                      STATE / 'feedback.sqlite', STATE / 'offers.sqlite')
feedback = companion.feedback
portraits = Portraits(companion.client, SUMMARY_MODEL_ID, catalog, feedback, companion.loyalty)
IDLE_SECONDS = int(os.environ.get('CONTACT_IDLE_SECONDS', 600))  # a session ends after 10 minutes without activity
polly = boto3.client('polly', region_name=REGION)

app = FastAPI(title='Lifestyle Companion')
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')
USER_RE = re.compile(r'^U\d{6}$')


def customer_id(raw):
    uid = (raw or '').strip().upper()
    if not USER_RE.match(uid) or uid not in store.customers:
        raise HTTPException(404, 'We could not find that customer ID. Please enter a valid one, for example U000001.')
    return uid


class SessionIn(BaseModel):
    user_id: str


class ChatIn(BaseModel):
    user_id: str
    message: str = ''
    channel: str = 'text'
    action: str | None = None
    survey_id: str | None = None


class BucksIn(BaseModel):
    user_id: str
    use: bool


class TtsIn(BaseModel):
    text: str


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/dashboard')
def dashboard_page():
    return FileResponse(STATIC / 'dashboard.html', headers={'Cache-Control': 'no-store'})


@app.get('/api/dashboard')
def dashboard_data(period: str = 'month'):
    """Management KPIs: FCR, sessions, JNPS/xNPS of the month, sentiment, sales, upsell/cross-sell, membership."""
    if period not in ('month', '7d', '30d', 'all'):
        raise HTTPException(400, 'period must be month, 7d, 30d or all')
    return dashboards.build(period, store, companion.contacts, feedback, companion.offers, companion.loyalty)


@app.get('/health')
def health():
    return {'ok': True, 'customers': len(store.customers), 'products': len(store.products)}


def close_idle_contacts():
    """Background sweeper: a customer who went quiet has ended their contact."""
    while True:
        time.sleep(60)
        try:
            for uid in memory.idle_contacts(IDLE_SECONDS):
                companion.close_contact(uid)
        except Exception as e:
            print('Idle sweep failed:', repr(e)[:300], flush=True)


threading.Thread(target=close_idle_contacts, daemon=True).start()


@app.post('/api/session')
def open_session(body: SessionIn):
    uid = customer_id(body.user_id)
    state = memory.load(uid)
    if state.get('contact') and time.time() - state.get('updated', 0) > IDLE_SECONDS:
        companion.close_contact(uid)  # returning after a break: record the old session, start a new one
        state = memory.load(uid)
    overview = catalog.overview(uid)
    return {'user_id': uid, 'overview': overview, 'basket': store.get_basket(uid),
            'prefs': companion._prefs_view(state['prefs']), 'checkout': companion.checkout_view(uid, state['checkout']),
            'history': state['ui'], 'is_new': not state['messages'] and not state['ui'],
            'contacts': contact_cards(uid)}


def contact_cards(uid, limit=10):
    keys = ('contact_id', 'started_at', 'duration_seconds', 'channel', 'turns', 'reason', 'reason_category',
            'sentiment', 'sentiment_score', 'summary', 'resolved')
    return [{k: r[k] for k in keys} | {'orders': [o['order_id'] for o in r['outcome']['orders']]}
            for r in companion.contacts.history(uid, limit)]


@app.post('/api/session/end')
def end_session(body: SessionIn):
    """The customer left (switched customer). Recorded in the background so the UI isn't kept waiting."""
    uid = customer_id(body.user_id)
    threading.Thread(target=companion.close_contact, args=(uid,), daemon=True).start()
    return {'ok': True}


@app.post('/api/session/reset')
def reset_session(body: SessionIn):
    uid = customer_id(body.user_id)
    record = companion.close_contact(uid, fresh=True)
    return {'ok': True, 'recorded': record['contact_id'] if record else None, 'contacts': contact_cards(uid)}


@app.get('/api/contacts/{user_id}')
def contacts(user_id: str, verbatim: bool = False):
    uid = customer_id(user_id)
    if verbatim:
        return {'contacts': companion.contacts.history(uid)}
    return {'contacts': contact_cards(uid, limit=50)}


@app.get('/api/contacts/{user_id}/export.csv')
def contacts_csv(user_id: str):
    uid = customer_id(user_id)
    return PlainTextResponse(companion.contacts.export_csv(uid), media_type='text/csv',
                             headers={'Content-Disposition': f'attachment; filename="contact_history_{uid}.csv"'})


@app.post('/api/chat')
def chat(body: ChatIn):
    uid = customer_id(body.user_id)
    if body.action not in (None, 'session_start', 'confirm', 'cancel_checkout', 'jnps_followup'):
        raise HTTPException(400, 'Unknown action.')
    survey = None
    if body.action == 'jnps_followup':
        survey = feedback.get(body.survey_id or '')
        if not survey or survey['customer_id'] != uid or survey['status'] != 'answered':
            raise HTTPException(400, 'No answered survey to follow up.')
    if not body.message.strip() and not body.action:
        raise HTTPException(400, 'Message is empty.')
    channel = 'voice' if body.channel == 'voice' else 'text'

    def stream():
        for event in companion.turn(uid, body.message[:2000], channel, body.action, survey):
            yield json.dumps(event, default=str) + '\n'

    return StreamingResponse(stream(), media_type='application/x-ndjson',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@app.get('/api/basket/{user_id}')
def basket(user_id: str):
    return store.get_basket(customer_id(user_id))


@app.get('/api/for-you/{user_id}')
def for_you(user_id: str):
    uid = customer_id(user_id)
    state = memory.load(uid)
    rejected = set(state['prefs'].get('rejected', []))
    ins = catalog.insights(uid)
    rails = []
    picks = catalog.recommendations(uid, exclude_ids=rejected, limit=8)['results']
    rails.append({'title': 'Picked for you', 'section': 'Home', 'items': picks})
    carted = [catalog.card(p, reason='Saved on a past visit') for _, p, _ in ins['abandoned']
              if not catalog.eligibility(uid, p) and p not in rejected]
    fav = [catalog.card(p, reason='From your favourites') for _, p, _ in ins['favourites']
           if not catalog.eligibility(uid, p) and p not in rejected]
    seen, pick_up = set(), []
    for c in carted + fav:
        if c['product_id'] not in seen:
            seen.add(c['product_id'])
            pick_up.append(c)
    if pick_up:
        rails.append({'title': 'Pick up where you left off', 'section': 'Shop', 'items': pick_up[:8]})
    deals = catalog.search(uid, only_discounted=True, sort='best_match', exclude_ids=rejected, limit=8)['results']
    rails.append({'title': 'Deals that suit you', 'section': 'Rewards', 'items': deals})
    return {'rails': rails}


def _mini(card, why):
    return {k: card[k] for k in ('product_id', 'name', 'price', 'list_price', 'discount_pct', 'category')} | {'why': why}


@app.get('/api/just-for-u/{user_id}')
def just_for_u(user_id: str):
    """Side-panel block: Bucks balance, Next Level (upsell) and For U (cross-sell), three products each."""
    uid = customer_id(user_id)
    rejected = set(memory.load(uid)['prefs'].get('rejected', []))
    in_basket = [i['product_id'] for i in store.get_basket(uid)['items']]
    bought = []  # newest first: orders placed in the companion, then purchases from the activity history
    with store.transaction() as db:
        orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders WHERE user_id=?', (uid,))]
    for order in sorted(orders, key=lambda o: o['created_at'], reverse=True):
        bought += [i['product_id'] for i in order['items'] if i['product_id'] not in bought]
    for _, pid, _, _ in sorted(catalog.insights(uid)['purchased'], reverse=True):
        if pid not in bought and pid in catalog.products:
            bought.append(pid)
    rejected |= set(bought)  # never propose something the customer has just bought
    # Next Level: step up from what is in the basket, or from recent purchases when the basket is empty.
    upsell = catalog.upsell(uid, in_basket or bought[:3], exclude_ids=rejected | set(in_basket), limit=3)
    # For U: items that go with the basket first, then with past purchases; affordable for this customer.
    prefs = memory.load(uid)['prefs']
    cap = float(prefs['budget']) if prefs.get('budget') and prefs.get('budget_scope') != 'total' \
        else max(30.0, 1.5 * float(store.customer(uid)['monthly_budget'] or 0))
    cross, taken = [], rejected | set(in_basket) | {c['product_id'] for c in upsell}
    for base in (in_basket, [p for p in bought if p not in in_basket][:5]):
        if base and len(cross) < 3:
            found = catalog.complements(uid, base, cap, taken, 3 - len(cross), subscriptions=False)['results']
            cross += found
            taken |= {c['product_id'] for c in found}
    companion.offers.record(uid, 'upsell', 'just_for_u', upsell)
    companion.offers.record(uid, 'cross_sell', 'just_for_u', cross)
    return {'bucks': companion.loyalty.summary(uid),
            'next_level': [_mini(c, c['why']) for c in upsell],
            'for_u': [_mini(c, f"Goes with {c['pairs_with']}") for c in cross],
            'based_on': 'basket' if in_basket else ('purchases' if bought else 'profile')}


@app.get('/api/portrait/{user_id}')
def portrait(user_id: str):
    """A short, warm affirmation of the customer for the "What I remember" panel."""
    uid = customer_id(user_id)
    with store.transaction() as db:
        orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders WHERE user_id=?', (uid,))]
    bought = [i['product_name'] for o in sorted(orders, key=lambda o: o['created_at'], reverse=True) for i in o['items']]
    return {'text': portraits.get(uid, memory.load(uid)['prefs'], bought)}


@app.post('/api/checkout/bucks')
def checkout_bucks(body: BucksIn):
    uid = customer_id(body.user_id)
    view = companion.set_bucks(uid, body.use)
    if view is None:
        raise HTTPException(409, 'There is no checkout summary to apply Bucks to. Ask the companion to check out first.')
    return {'checkout': view, 'bucks': companion.loyalty.summary(uid)}


# ---------- feedback: JNPS (journey, per purchased product) and xNPS (session) ----------
class JnpsIn(BaseModel):
    user_id: str
    survey_id: str
    score: int | None = None
    comment: str | None = None
    dismissed: bool = False


class XnpsIn(BaseModel):
    user_id: str
    trigger: str
    score: int | None = None
    comment: str | None = None
    dismissed: bool = False


def _survey_view(s):
    return s and {k: s[k] for k in ('survey_id', 'survey_type', 'question', 'product_id', 'product_name',
                                    'product_category', 'purchased_at', 'purchase_source', 'status', 'score')}


@app.get('/api/feedback/jnps/{user_id}')
def next_jnps(user_id: str, trigger: str = 'login'):
    uid = customer_id(user_id)
    return {'survey': _survey_view(feedback.next_jnps(uid, 'new_chat' if trigger == 'new_chat' else 'login'))}


@app.post('/api/feedback/jnps')
def answer_jnps(body: JnpsIn):
    uid = customer_id(body.user_id)
    if not body.dismissed and body.score is None:
        raise HTTPException(400, 'Choose a score from 1 to 10.')
    try:
        s = feedback.answer(uid, body.survey_id, body.score, body.comment, body.dismissed)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'survey': _survey_view(s) | {'nps_category': s['nps_category']}}


def _session_facts(state):
    contact = state.get('contact')
    if not contact:
        return None, 0
    turns = sum(1 for u in state['ui'] if u['role'] == 'user' and u.get('ts', '') >= contact['started_at'])
    return contact, turns


@app.get('/api/feedback/xnps/eligible/{user_id}')
def xnps_eligible(user_id: str):
    """xNPS is asked only when the customer actually talked to the companion in this session."""
    uid = customer_id(user_id)
    contact, turns = _session_facts(memory.load(uid))
    return {'eligible': bool(contact and turns and not contact.get('xnps')), 'question': XNPS_QUESTION}


@app.post('/api/feedback/xnps')
def answer_xnps(body: XnpsIn):
    uid = customer_id(body.user_id)
    if body.trigger not in ('new_chat', 'switch'):
        raise HTTPException(400, 'Unknown trigger.')
    if not body.dismissed and body.score is None:
        raise HTTPException(400, 'Choose a score from 1 to 10.')
    with memory.lock(uid):
        state = memory.load(uid)
        contact, turns = _session_facts(state)
        try:
            s = feedback.record_xnps(uid, body.trigger, contact, turns, len((contact or {}).get('orders', [])),
                                     body.score, body.comment, body.dismissed)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if contact and s['status'] == 'answered':
            contact['xnps'] = {'score': s['score'], 'category': s['nps_category'], 'comment': s['comment'],
                               'survey_id': s['survey_id']}
            memory.save(uid, state)
    return {'survey_id': s['survey_id'], 'status': s['status'], 'nps_category': s['nps_category']}


@app.get('/api/feedback/report')
def feedback_report():
    return feedback.report()


@app.get('/api/feedback/export.csv')
def feedback_csv():
    return PlainTextResponse(feedback.export_csv(), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename="feedback_surveys.csv"'})


@app.post('/api/tts')
def tts(body: TtsIn):
    text = re.sub(r'[*_#`>\[\]]', '', body.text)[:2500].strip()
    if not text:
        raise HTTPException(400, 'Nothing to say.')
    try:
        audio = polly.synthesize_speech(Text=text, OutputFormat='mp3', VoiceId=os.environ.get('VOICE_ID', 'Joanna'),
                                        Engine='neural')['AudioStream'].read()
    except Exception as e:
        print('Polly error:', repr(e)[:300], flush=True)
        raise HTTPException(503, 'Voice output unavailable.')
    return Response(audio, media_type='audio/mpeg', headers={'Cache-Control': 'no-store'})


@app.exception_handler(ToolError)
def tool_error(_: Request, exc: ToolError):
    return Response(json.dumps({'detail': exc.message, 'code': exc.code}), status_code=400,
                    media_type='application/json')
