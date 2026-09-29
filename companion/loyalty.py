"""Bucks: the loyalty points customers can spend at checkout.

Every customer starts with 1,000 Bucks; 10 Bucks = 1 US dollar. Bucks are applied to a pending
checkout summary and deducted only when the application creates the order, once per order (idempotent on
order_id), so retrying a confirmation never charges points twice.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

DEFAULT_BALANCE = 1000
POINTS_PER_UNIT = 10


class LoyaltyError(Exception):
    pass


def points_value(points):
    """Currency value of a number of Bucks, as a 2-decimal string."""
    return str((Decimal(points) / POINTS_PER_UNIT).quantize(Decimal('0.01')))


def max_points_for(total):
    """Most Bucks that can go towards a total (never more than the total itself)."""
    return int((Decimal(str(total)) * POINTS_PER_UNIT).to_integral_value(rounding=ROUND_DOWN))


class Loyalty:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS balances(user_id TEXT PRIMARY KEY, points INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS redemptions(order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                points INTEGER NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def balance(self, user_id):
        with self._db() as db:
            row = db.execute('SELECT points FROM balances WHERE user_id=?', (user_id,)).fetchone()
        return row[0] if row else DEFAULT_BALANCE

    def summary(self, user_id):
        points = self.balance(user_id)
        return {'points': points, 'value': points_value(points), 'points_per_unit': POINTS_PER_UNIT}

    def plan(self, user_id, total, requested=None):
        """Bucks to apply to a total: the requested amount (or as much as possible), capped by balance and total."""
        cap = min(self.balance(user_id), max_points_for(total))
        points = cap if requested is None else max(0, min(int(requested), cap))
        return {'points': points, 'value': points_value(points),
                'amount_due': str((Decimal(str(total)) - Decimal(points_value(points))).quantize(Decimal('0.01'))),
                'max_points': cap}

    def redeem(self, user_id, order_id, points):
        """Deduct Bucks for a created order. Returns the redemption; repeating it for the same order is a no-op."""
        with self._lock, self._db() as db:
            done = db.execute('SELECT points, value FROM redemptions WHERE order_id=?', (order_id,)).fetchone()
            if done:
                return {'points': done[0], 'value': done[1]}
            if points <= 0:
                return {'points': 0, 'value': '0.00'}
            row = db.execute('SELECT points FROM balances WHERE user_id=?', (user_id,)).fetchone()
            balance = row[0] if row else DEFAULT_BALANCE
            if points > balance:
                raise LoyaltyError('Not enough Bucks.')
            db.execute('INSERT OR REPLACE INTO balances VALUES (?,?)', (user_id, balance - points))
            db.execute('INSERT INTO redemptions VALUES (?,?,?,?,?)',
                       (order_id, user_id, points, points_value(points), datetime.now(timezone.utc).isoformat()))
            return {'points': points, 'value': points_value(points)}

    def redemption(self, order_id):
        with self._db() as db:
            row = db.execute('SELECT points, value FROM redemptions WHERE order_id=?', (order_id,)).fetchone()
        return {'points': row[0], 'value': row[1]} if row else None
