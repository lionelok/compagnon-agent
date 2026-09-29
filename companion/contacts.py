"""Contact history: one record per customer session with the companion.

Stored as JSON Lines (state/contact_history.jsonl, one contact per line, append-only). Each record holds:
customer ID, start/end timestamps, duration, channel, reason for chatting, sentiment, a context summary,
outcome (orders, basket actions), the preferences captured, and the verbatim transcript with per-message
timestamps. Reason, sentiment and summary come from Claude Haiku reading the transcript; a heuristic
fallback keeps the record complete if the model call fails.

The companion reads a customer's recent contacts at the start of every new conversation to personalise it.
"""
import csv
import io
import json
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REASONS = ['product_discovery', 'home_planning', 'device_and_connectivity', 'purchase', 'basket_change',
           'order_inquiry', 'price_or_budget', 'complaint_or_issue', 'rewards', 'browsing_or_greeting', 'other']
SENTIMENTS = ['positive', 'neutral', 'negative', 'mixed']

ANALYSIS_PROMPT = """You analyse one customer's chat session with a shopping companion in a telco lifestyle app.
Prices are in US dollars (USD): write amounts like $12.34, never £, € or "units".
Return only a JSON object with these keys:
"reason": the customer's main reason for chatting, max 12 words, in plain English
"reason_category": one of {reasons}
"sentiment": the customer's overall sentiment, one of {sentiments}
"sentiment_score": number from -1 (very negative) to 1 (very positive)
"sentiment_rationale": max 20 words, based on what the customer said
"summary": max 90 words for a future agent: goal, budget and preferences stated, products discussed, rejected or bought, outcome, anything unresolved
"resolved": true if the customer's need was met in this session, else false
"follow_up": one short suggestion for the next contact, or null

Facts recorded by the app (authoritative): {facts}

Transcript:
{transcript}"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _parse_ts(ts):
    return datetime.fromisoformat(ts)


class ContactLog:
    def __init__(self, path, client, model_id):
        self.path = Path(path)
        self.client, self.model_id = client, model_id
        self._lock = threading.Lock()
        self._by_user = defaultdict(list)
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._by_user[rec['customer_id']].append(rec)

    # ---------- read ----------
    def history(self, user_id, limit=None):
        """Newest first."""
        recs = sorted(self._by_user.get(user_id, []), key=lambda r: r['started_at'], reverse=True)
        return recs[:limit] if limit else recs

    def context_block(self, user_id, limit=5):
        """Compact history for the agent's first message of a conversation."""
        recs = self.history(user_id, limit)
        if not recs:
            return ''
        lines = []
        for r in recs:
            lines.append(json.dumps({
                'date': r['started_at'][:16].replace('T', ' '), 'channel': r['channel'],
                'duration_min': round(r['duration_seconds'] / 60, 1), 'reason': r['reason'],
                'sentiment': r['sentiment'], 'resolved': r['resolved'], 'summary': r['summary'],
                'orders': [f"{o['order_id']} ({', '.join(o['items'])}; {o['total']})" for o in r['outcome']['orders']] or None,
                'rejected_products': r['preferences'].get('rejected_products') or None,
                'follow_up': r.get('follow_up'),
                'session_rating': f"{r['xnps']['score']}/10 {r['xnps']['category']}" if r.get('xnps') else None},
                ensure_ascii=False))
        total = len(self._by_user.get(user_id, []))
        return (f'<contact_history total_contacts="{total}" showing="most recent {len(recs)}, newest first">\n'
                + '\n'.join(lines) + '\n</contact_history>')

    def export_csv(self, user_id=None):
        cols = ['contact_id', 'customer_id', 'started_at', 'ended_at', 'duration_seconds', 'channel', 'turns',
                'reason', 'reason_category', 'sentiment', 'sentiment_score', 'resolved', 'summary', 'follow_up',
                'orders', 'verbatim']
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=';')
        w.writerow(cols)
        users = [user_id] if user_id else sorted(self._by_user)
        for u in users:
            for r in sorted(self._by_user.get(u, []), key=lambda r: r['started_at']):
                w.writerow([r['contact_id'], r['customer_id'], r['started_at'], r['ended_at'], r['duration_seconds'],
                            r['channel'], r['turns'], r['reason'], r['reason_category'], r['sentiment'],
                            r['sentiment_score'], r['resolved'], r['summary'], r.get('follow_up') or '',
                            ' | '.join(o['order_id'] for o in r['outcome']['orders']),
                            '\n'.join(f"[{m['timestamp']}] {m['speaker']}: {m['text']}" for m in r['verbatim'])])
        return buf.getvalue()

    # ---------- write ----------
    def build(self, user_id, contact, ui_entries, prefs_view):
        """Assemble the factual part of a record from the session (no model call)."""
        verbatim = []
        for u in ui_entries:
            if not u.get('text') and not u.get('items'):
                continue
            entry = {'timestamp': u.get('ts') or contact['started_at'],
                     'speaker': 'customer' if u['role'] == 'user' else 'companion',
                     'channel': u.get('channel', 'text') if u['role'] == 'user' else None,
                     'text': u.get('text', '')}
            shown = [i for i in u.get('items', []) if i['type'] == 'products']
            if shown:
                entry['products_shown'] = [c['name'] for i in shown for c in i['items']]
            verbatim.append({k: v for k, v in entry.items() if v is not None})
        customer_msgs = [m for m in verbatim if m['speaker'] == 'customer']
        channels = {m.get('channel', 'text') for m in customer_msgs}
        start = contact['started_at']
        end = verbatim[-1]['timestamp'] if verbatim else start
        return {
            'contact_id': contact['id'], 'customer_id': user_id,
            'started_at': start, 'ended_at': end,
            'duration_seconds': max(0, int((_parse_ts(end) - _parse_ts(start)).total_seconds())),
            'channel': 'mixed' if len(channels) > 1 else (channels.pop() if channels else 'text'),
            'turns': len(customer_msgs),
            'outcome': {'orders': contact.get('orders', []), 'actions': contact.get('actions', [])},
            'xnps': contact.get('xnps'),
            'preferences': prefs_view, 'verbatim': verbatim,
        }

    def analyse(self, record):
        transcript = '\n'.join(f"{m['speaker'].upper()}: {m['text']}" + (f" [cards: {', '.join(m['products_shown'])}]"
                                                                       if m.get('products_shown') else '')
                               for m in record['verbatim'])[-24000:]
        facts = json.dumps({'orders': record['outcome']['orders'], 'actions': record['outcome']['actions'],
                            'preferences': record['preferences']}, ensure_ascii=False)
        try:
            r = self.client.messages.create(
                model=self.model_id, max_tokens=800,
                messages=[{'role': 'user', 'content': ANALYSIS_PROMPT.format(
                    reasons=REASONS, sentiments=SENTIMENTS, facts=facts, transcript=transcript)}])
            text = ''.join(b.text for b in r.content if b.type == 'text')
            data = json.loads(re.search(r'\{.*\}', text, re.S).group(0))
            return {
                'reason': str(data.get('reason', ''))[:160],
                'reason_category': data.get('reason_category') if data.get('reason_category') in REASONS else 'other',
                'sentiment': data.get('sentiment') if data.get('sentiment') in SENTIMENTS else 'neutral',
                'sentiment_score': max(-1.0, min(1.0, float(data.get('sentiment_score', 0)))),
                'sentiment_rationale': str(data.get('sentiment_rationale', ''))[:200],
                'summary': str(data.get('summary', ''))[:900],
                'resolved': bool(data.get('resolved')),
                'follow_up': data.get('follow_up') or None,
                'analysis': 'model',
            }
        except Exception as e:  # keep the record complete even when the model is unavailable
            print('Contact analysis failed:', repr(e)[:300], flush=True)
            first = next((m['text'] for m in record['verbatim'] if m['speaker'] == 'customer'), '')
            return {'reason': first[:120], 'reason_category': 'other', 'sentiment': 'neutral', 'sentiment_score': 0.0,
                    'sentiment_rationale': 'not analysed', 'summary': first[:300],
                    'resolved': bool(record['outcome']['orders']), 'follow_up': None, 'analysis': 'fallback'}

    def append(self, record):
        with self._lock:
            with self.path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
            self._by_user[record['customer_id']].append(record)

    def finalise(self, user_id, contact, ui_entries, prefs_view):
        """Build, analyse and store one contact. Returns the record, or None when the customer never spoke."""
        record = self.build(user_id, contact, ui_entries, prefs_view)
        if not record['turns']:
            return None
        analysis = self.analyse(record)
        # Field order: identity, timing, why, how it felt, what happened, then the verbatim.
        ordered = {k: record[k] for k in ('contact_id', 'customer_id', 'started_at', 'ended_at', 'duration_seconds',
                                          'channel', 'turns')}
        ordered.update({k: analysis[k] for k in ('reason', 'reason_category', 'sentiment', 'sentiment_score',
                                                 'sentiment_rationale', 'summary', 'resolved', 'follow_up')})
        ordered['xnps'] = record['xnps']
        ordered.update(outcome=record['outcome'], preferences=record['preferences'],
                       analysis=analysis['analysis'], verbatim=record['verbatim'])
        self.append(ordered)
        return ordered


def new_contact():
    return {'id': 'CON-' + uuid.uuid4().hex[:12], 'started_at': now_iso(), 'orders': [], 'actions': []}
