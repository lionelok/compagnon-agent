"""Offline simulation. Python 3.10+, standard library only."""
import csv
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent

class ToolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

def money(value):
    return str(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def read_csv(path, key):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f, delimiter=';'))
    if not rows or key not in rows[0]:
        raise ValueError(f'{path}: expected nonempty semicolon-delimited CSV with {key}')
    result = {}
    for row in rows:
        ident = row[key].strip()
        if not ident or ident in result:
            raise ValueError(f'{path}: missing or duplicate {key}')
        result[ident] = row
    return result

class Store:
    def __init__(self, db_path=ROOT/'simulation.sqlite', products_path=ROOT/'data/products.csv',
                 customers_path=ROOT/'data/customers.csv', demo_date='2026-04-01', currency='DEMO_UNITS'):
        self.db_path = str(db_path)
        self.products = read_csv(products_path, 'product_id')
        self.customers = read_csv(customers_path, 'user_id')
        self.demo_date = date.fromisoformat(demo_date)
        self.currency = currency
        with self.transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS baskets(user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS items(user_id TEXT, product_id TEXT, quantity INTEGER NOT NULL,
                PRIMARY KEY(user_id,product_id));
            CREATE TABLE IF NOT EXISTS quotes(checkout_id TEXT PRIMARY KEY, user_id TEXT, revision INTEGER,
                snapshot TEXT NOT NULL, order_id TEXT);
            CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, user_id TEXT, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def customer(self, user_id):
        if user_id not in self.customers:
            raise ToolError('UNKNOWN_CUSTOMER', 'Customer was not found.')
        return self.customers[user_id]

    def product(self, user_id, product_id):
        customer = self.customer(user_id)
        if product_id not in self.products:
            raise ToolError('UNKNOWN_PRODUCT', 'Product was not found.')
        p = self.products[product_id]
        if date.fromisoformat(p['available_from'].replace('/', '-')) > self.demo_date:
            raise ToolError('NOT_LAUNCHED', 'Product is not available on the demo date.')
        if customer['region'] not in p['available_regions'].split():
            raise ToolError('REGION_UNAVAILABLE', 'Product is unavailable in this customer region.')
        if p['compatible_os'] != 'any' and customer['device_os'] not in p['compatible_os'].split():
            raise ToolError('INCOMPATIBLE_OS', 'Product is incompatible with the customer device OS.')
        if p['is_repeatable'] != '1':
            if product_id in customer.get('owned_items', '').split():
                raise ToolError('ALREADY_OWNED', 'Non-repeatable product is already owned.')
        price, discount = Decimal(p['price']), Decimal(p['offer_discount'] or '0')
        if not price.is_finite() or price < 0 or not discount.is_finite() or not 0 <= discount <= 1:
            raise ToolError('INVALID_CATALOGUE', 'Price or discount is invalid.')
        return p

    def revision(self, db, user_id):
        db.execute('INSERT OR IGNORE INTO baskets VALUES (?,0)', (user_id,))
        return db.execute('SELECT revision FROM baskets WHERE user_id=?', (user_id,)).fetchone()[0]

    def basket_snapshot(self, db, user_id):
        self.customer(user_id)
        revision = self.revision(db, user_id)
        lines, total = [], Decimal(0)
        for row in db.execute('SELECT product_id,quantity FROM items WHERE user_id=? ORDER BY product_id', (user_id,)):
            p = self.product(user_id, row['product_id'])
            unit = Decimal(money(Decimal(p['price']) * (1-Decimal(p['offer_discount'] or '0'))))
            line_total = unit * row['quantity']
            total += line_total
            lines.append(dict(product_id=row['product_id'], product_name=p['product_name'],
                quantity=row['quantity'], list_price=money(p['price']), discount_rate=p['offer_discount'] or '0',
                unit_price=money(unit), line_total=money(line_total),
                is_subscription=p['is_subscription']=='1'))
        return dict(user_id=user_id, revision=revision, items=lines, total=money(total),
                    currency=self.currency, simulated=True)

    def get_basket(self, user_id):
        with self.transaction() as db:
            return self.basket_snapshot(db, user_id)

    def change(self, user_id, product_id, quantity, mode):
        self.customer(user_id)
        if type(quantity) is not int or not 0 <= quantity <= 99 or (mode=='add' and quantity==0):
            raise ToolError('INVALID_QUANTITY', 'Quantity must be an integer: add 1–99, update 0–99.')
        with self.transaction() as db:
            self.revision(db, user_id)
            old = db.execute('SELECT quantity FROM items WHERE user_id=? AND product_id=?', (user_id,product_id)).fetchone()
            if mode=='set' and old is None:
                raise ToolError('ITEM_NOT_IN_BASKET', 'Product is not in the basket.')
            new = quantity + (old[0] if old else 0) if mode=='add' else quantity
            if new:
                p = self.product(user_id, product_id)
                if new > 99 or (p['is_repeatable']!='1' and new > 1):
                    raise ToolError('QUANTITY_LIMIT', 'Non-repeatable products allow one unit; others allow up to 99.')
                if p['is_repeatable']!='1':
                    for order in db.execute('SELECT payload FROM orders WHERE user_id=?', (user_id,)):
                        if any(i['product_id']==product_id for i in json.loads(order[0])['items']):
                            raise ToolError('ALREADY_PURCHASED', 'Non-repeatable product was already purchased in this simulation.')
                db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?)', (user_id, product_id, new))
            else:
                db.execute('DELETE FROM items WHERE user_id=? AND product_id=?', (user_id,product_id))
            db.execute('UPDATE baskets SET revision=revision+1 WHERE user_id=?', (user_id,))
            return self.basket_snapshot(db, user_id)

    def prepare_checkout(self, user_id):
        with self.transaction() as db:
            snapshot = self.basket_snapshot(db, user_id)
            if not snapshot['items']:
                raise ToolError('EMPTY_BASKET', 'Add a product before checkout.')
            if any(i['is_subscription'] for i in snapshot['items']):
                raise ToolError('SUBSCRIPTION_TERMS_MISSING', 'Subscription checkout is disabled until the organizer supplies billing terms. Remove subscriptions to check out other items.')
            checkout_id = 'CHK-' + uuid.uuid4().hex
            db.execute('INSERT INTO quotes VALUES (?,?,?,?,NULL)',
                       (checkout_id, user_id, snapshot['revision'], json.dumps(snapshot)))
            return dict(checkout_id=checkout_id, summary=snapshot, requires_confirmation=True,
                        message='Show this summary and obtain explicit customer confirmation.')

    def confirm_order(self, user_id, checkout_id, customer_confirmed=False):
        """Trusted application calls this only after customer confirmation; not a model tool."""
        self.customer(user_id)
        if customer_confirmed is not True:
            raise ToolError('CONFIRMATION_REQUIRED', 'Explicit customer confirmation is required.')
        with self.transaction() as db:
            quote = db.execute('SELECT * FROM quotes WHERE checkout_id=? AND user_id=?', (checkout_id,user_id)).fetchone()
            if quote is None:
                raise ToolError('CHECKOUT_NOT_FOUND', 'Checkout was not found for this customer.')
            if quote['order_id']:
                return json.loads(db.execute('SELECT payload FROM orders WHERE order_id=?', (quote['order_id'],)).fetchone()[0])
            current = self.basket_snapshot(db, user_id)
            if current != json.loads(quote['snapshot']):
                raise ToolError('CHECKOUT_CHANGED', 'Basket or prices changed. Prepare checkout again and obtain fresh confirmation.')
            order_id = 'ORD-' + uuid.uuid4().hex
            order = dict(current, order_id=order_id, checkout_id=checkout_id,
                         status='SIMULATED_PURCHASE_COMPLETED', payment_processed=False,
                         created_at=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO orders VALUES (?,?,?)', (order_id,user_id,json.dumps(order)))
            db.execute('UPDATE quotes SET order_id=? WHERE checkout_id=?', (order_id,checkout_id))
            db.execute('DELETE FROM items WHERE user_id=?', (user_id,))
            db.execute('UPDATE baskets SET revision=revision+1 WHERE user_id=?', (user_id,))
            return order

    def get_order(self, user_id, order_id):
        self.customer(user_id)
        with self.transaction() as db:
            row = db.execute('SELECT payload FROM orders WHERE order_id=? AND user_id=?', (order_id,user_id)).fetchone()
            if row is None:
                raise ToolError('ORDER_NOT_FOUND', 'Order was not found for this customer.')
            return json.loads(row[0])
