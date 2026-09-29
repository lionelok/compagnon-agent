"""Per-customer session memory, persisted in SQLite.

Each customer has one state document:
* messages   - the model transcript for the current conversation segment (append-only)
* summary    - a digest of earlier segments, carried into a fresh segment when the transcript grows long
* prefs      - structured memory the agent maintains: goal, budget, liked styles, rejected products, notes
* shown      - the numbered options most recently shown, so "the first option" resolves reliably
* checkout   - the pending checkout awaiting the customer's confirmation
* events     - things the customer did outside the chat (button clicks) not yet seen by the agent
* ui         - the rendered chat log (timestamped), so reloading the page restores the conversation
* contact    - the open contact (session) being recorded for the contact history
Customers never share state: every read and write is keyed by the session's user_id.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


def empty_state():
    return {'conversation_id': uuid.uuid4().hex[:12], 'messages': [], 'summary': '', 'prefs': {},
            'shown': [], 'checkout': None, 'events': [], 'ui': [], 'contact': None, 'updated': time.time()}


class Memory:
    def __init__(self, path):
        self.path = str(path)
        self._locks = {}
        self._guard = threading.Lock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions(user_id TEXT PRIMARY KEY, state TEXT NOT NULL)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def lock(self, user_id):
        """One agent turn at a time per customer."""
        with self._guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def load(self, user_id):
        with self._db() as db:
            row = db.execute('SELECT state FROM sessions WHERE user_id=?', (user_id,)).fetchone()
        state = json.loads(row[0]) if row else empty_state()
        for key, value in empty_state().items():
            state.setdefault(key, value)
        return state

    def save(self, user_id, state):
        state['updated'] = time.time()
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (user_id, json.dumps(state)))

    def reset(self, user_id):
        state = empty_state()
        self.save(user_id, state)
        return state

    def fresh_state(self, keep_prefs=None):
        state = empty_state()
        if keep_prefs:
            state['prefs'] = keep_prefs
        return state

    def idle_contacts(self, idle_seconds):
        """Customers whose open contact has seen no activity for idle_seconds."""
        cutoff = time.time() - idle_seconds
        with self._db() as db:
            rows = db.execute('SELECT user_id, state FROM sessions').fetchall()
        out = []
        for user_id, raw in rows:
            state = json.loads(raw)
            if state.get('contact') and state.get('updated', 0) < cutoff:
                out.append(user_id)
        return out

    def add_event(self, user_id, text):
        with self.lock(user_id):
            state = self.load(user_id)
            state['events'].append(text)
            self.save(user_id, state)
