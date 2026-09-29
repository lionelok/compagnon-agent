"""Upsell and cross-sell tracking for the management dashboard.

An *offer* is a product proposed to a customer: upsell (Next Level) or cross-sell (For U panel, cross-sell cards
after an add). Offers are de-duplicated per customer, kind, product and day, so a panel that refreshes after every
message doesn't inflate the count. When an order is created, each order line that was offered to that customer in
the previous 7 days is attributed to the most recent offer's kind; its line total is the offer's converted value.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

ATTRIBUTION_DAYS = 7


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class OfferLog:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS offers(customer_id TEXT, kind TEXT, source TEXT, product_id TEXT, price REAL,
                offered_at TEXT, day TEXT, UNIQUE(customer_id, kind, product_id, day));
            CREATE TABLE IF NOT EXISTS conversions(order_id TEXT, product_id TEXT, customer_id TEXT, kind TEXT,
                source TEXT, value REAL, converted_at TEXT, PRIMARY KEY(order_id, product_id));
            CREATE INDEX IF NOT EXISTS ix_offers_lookup ON offers(customer_id, product_id, offered_at);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, customer_id, kind, source, cards):
        """kind: 'upsell' or 'cross_sell'; cards: product cards with product_id and price."""
        ts = now_iso()
        rows = [(customer_id, kind, source, c['product_id'], float(c['price']), ts, ts[:10]) for c in cards]
        if rows:
            with self._lock, self._db() as db:
                db.executemany('INSERT OR IGNORE INTO offers VALUES (?,?,?,?,?,?,?)', rows)

    def attribute(self, customer_id, order):
        """Credit order lines to the offers that preceded them (idempotent per order line)."""
        created = order.get('created_at') or now_iso()
        since = (datetime.fromisoformat(created) - timedelta(days=ATTRIBUTION_DAYS)).isoformat(timespec='seconds')
        with self._lock, self._db() as db:
            for item in order['items']:
                offer = db.execute('SELECT kind, source FROM offers WHERE customer_id=? AND product_id=? AND '
                                   'offered_at BETWEEN ? AND ? ORDER BY offered_at DESC LIMIT 1',
                                   (customer_id, item['product_id'], since, created)).fetchone()
                if offer:
                    db.execute('INSERT OR IGNORE INTO conversions VALUES (?,?,?,?,?,?,?)',
                               (order['order_id'], item['product_id'], customer_id, offer['kind'], offer['source'],
                                float(item['line_total']), created))

    def summary(self, start=None, end=None):
        """Offers made, customers reached, conversions and converted value per kind within [start, end)."""
        start, end = start or '0000', end or '9999'
        out = {}
        with self._db() as db:
            for kind in ('upsell', 'cross_sell'):
                o = db.execute('SELECT COUNT(*) n, COUNT(DISTINCT customer_id) c, COALESCE(SUM(price),0) v FROM offers '
                               'WHERE kind=? AND offered_at>=? AND offered_at<?', (kind, start, end)).fetchone()
                c = db.execute('SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM conversions '
                               'WHERE kind=? AND converted_at>=? AND converted_at<?', (kind, start, end)).fetchone()
                out[kind] = {'offers': o['n'], 'customers_reached': o['c'], 'offered_value': round(o['v'], 2),
                             'converted': c['n'], 'converted_value': round(c['v'], 2),
                             'conversion_rate': round(100 * c['n'] / o['n'], 1) if o['n'] else None}
        return out
