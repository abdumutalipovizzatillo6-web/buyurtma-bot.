import sqlite3
import time
import uuid
import os


class DB:
    def __init__(self, db_path="shop.db"):
        self.db_path = db_path

        folder = os.path.dirname(
            os.path.abspath(db_path)
        )
        os.makedirs(folder, exist_ok=True)

        self.init_db()

    def get_conn(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=30
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_db(self):
        with self.get_conn() as conn:
            c = conn.cursor()

            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    shop_id INTEGER
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS shops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER UNIQUE,
                    name TEXT,
                    phone TEXT,
                    code TEXT UNIQUE,
                    is_active INTEGER DEFAULT 1
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER,
                    name TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER,
                    category_id INTEGER,
                    name TEXT,
                    price INTEGER,
                    photo TEXT,
                    available INTEGER DEFAULT 1
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS cart (
                    user_id INTEGER,
                    product_id INTEGER,
                    qty INTEGER,
                    PRIMARY KEY(user_id, product_id)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER,
                    customer_id INTEGER,
                    customer_name TEXT,
                    phone TEXT,
                    address TEXT,
                    total INTEGER,
                    status TEXT DEFAULT 'new',
                    created_at INTEGER
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    product_name TEXT,
                    price INTEGER,
                    qty INTEGER
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    text TEXT,
                    created_at INTEGER
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    shop_name TEXT,
                    receipt TEXT,
                    amount INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at INTEGER
                )
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_products_shop
                ON products(shop_id)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_shop
                ON orders(shop_id, status)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_customer
                ON orders(customer_id)
            """)

            # Eski bazaga yangi ustun qo'shish
            columns = [
                r["name"] for r in
                c.execute("PRAGMA table_info(shops)").fetchall()
            ]

            if "is_active" not in columns:
                c.execute("""
                    ALTER TABLE shops
                    ADD COLUMN is_active INTEGER DEFAULT 1
                """)

            conn.commit()

    # USERS

    def upsert_user(self, uid, full_name):
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO users(id, full_name)
                VALUES (?, ?)
                ON CONFLICT(id)
                DO UPDATE SET full_name=excluded.full_name
            """, (uid, full_name))

    def set_user_shop(self, uid, shop_id):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE users SET shop_id=? WHERE id=?",
                (shop_id, uid)
            )

    def get_user_shop(self, uid):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT s.*
                FROM users u
                JOIN shops s ON s.id=u.shop_id
                WHERE u.id=? AND s.is_active=1
            """, (uid,)).fetchone()

    def all_users(self):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT id FROM users"
            ).fetchall()

    # SHOPS

    def create_shop(self, owner_id, name, phone):
        code = uuid.uuid4().hex[:10]

        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO shops(owner_id, name, phone, code)
                VALUES (?, ?, ?, ?)
            """, (owner_id, name, phone, code))

            sid = c.lastrowid

            return conn.execute(
                "SELECT * FROM shops WHERE id=?",
                (sid,)
            ).fetchone()

    def get_shop(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM shops WHERE id=?",
                (shop_id,)
            ).fetchone()

    def get_shop_by_owner(self, owner_id):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM shops WHERE owner_id=?",
                (owner_id,)
            ).fetchone()

    def get_shop_by_code(self, code):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM shops
                WHERE code=? AND is_active=1
            """, (code,)).fetchone()

    def rename_shop(self, shop_id, name):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE shops SET name=? WHERE id=?",
                (name, shop_id)
            )

    def set_shop_active(self, shop_id, active):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE shops SET is_active=? WHERE id=?",
                (int(active), shop_id)
            )    # CATEGORIES

    def add_category(self, shop_id, name):
        with self.get_conn() as conn:
            c = conn.execute("""
                INSERT INTO categories(shop_id, name)
                VALUES (?, ?)
            """, (shop_id, name))
            return c.lastrowid

    def get_categories(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM categories
                WHERE shop_id=? ORDER BY id DESC
            """, (shop_id,)).fetchall()

    def delete_category(self, cid, shop_id):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE products SET category_id=NULL
                WHERE category_id=? AND shop_id=?
            """, (cid, shop_id))
            conn.execute("""
                DELETE FROM categories
                WHERE id=? AND shop_id=?
            """, (cid, shop_id))

    # PRODUCTS

    def add_product(self, shop_id, name, price,
                    photo=None, category_id=None):
        with self.get_conn() as conn:
            c = conn.execute("""
                INSERT INTO products
                (shop_id, category_id, name, price, photo)
                VALUES (?, ?, ?, ?, ?)
            """, (shop_id, category_id, name, price, photo))
            return c.lastrowid

    def get_product(self, pid):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM products WHERE id=?",
                (pid,)
            ).fetchone()

    def list_products(self, shop_id, category_id=None,
                      only_available=False):
        with self.get_conn() as conn:
            sql = "SELECT * FROM products WHERE shop_id=?"
            params = [shop_id]

            if category_id and category_id != "all":
                sql += " AND category_id=?"
                params.append(category_id)

            if only_available:
                sql += " AND available=1"

            sql += " ORDER BY id DESC"

            return conn.execute(sql, params).fetchall()

    def search_products(self, shop_id, query):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM products
                WHERE shop_id=? AND available=1
                AND name LIKE ?
                ORDER BY id DESC LIMIT 30
            """, (shop_id, f"%{query}%")).fetchall()

    def update_product(self, pid, shop_id, name,
                       price, photo, category_id=None):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE products
                SET name=?, price=?, photo=?, category_id=?
                WHERE id=? AND shop_id=?
            """, (name, price, photo, category_id,
                  pid, shop_id))

    def set_available(self, pid, available):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE products SET available=?
                WHERE id=?
            """, (int(bool(available)), pid))

    def set_price(self, pid, price):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE products SET price=?
                WHERE id=?
            """, (price, pid))

    def delete_product(self, pid):
        with self.get_conn() as conn:
            conn.execute(
                "DELETE FROM cart WHERE product_id=?", (pid,)
            )
            conn.execute(
                "DELETE FROM products WHERE id=?", (pid,)
            )

    def count_products(self, shop_id):
        with self.get_conn() as conn:
            r = conn.execute("""
                SELECT COUNT(*) AS c FROM products
                WHERE shop_id=?
            """, (shop_id,)).fetchone()
            return r["c"]

    # CART

    def cart_add(self, uid, pid):
        with self.get_conn() as conn:
            p = conn.execute("""
                SELECT * FROM products
                WHERE id=? AND available=1
            """, (pid,)).fetchone()

            if not p:
                return 0, False

            current = conn.execute("""
                SELECT p.shop_id FROM cart c
                JOIN products p ON p.id=c.product_id
                WHERE c.user_id=? LIMIT 1
            """, (uid,)).fetchone()

            cleared = False

            if current and current["shop_id"] != p["shop_id"]:
                conn.execute(
                    "DELETE FROM cart WHERE user_id=?", (uid,)
                )
                cleared = True

            conn.execute("""
                INSERT INTO cart(user_id, product_id, qty)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, product_id)
                DO UPDATE SET qty=qty+1
            """, (uid, pid))

            r = conn.execute("""
                SELECT qty FROM cart
                WHERE user_id=? AND product_id=?
            """, (uid, pid)).fetchone()

            return r["qty"], cleared

    def cart_items(self, uid):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT c.qty, p.id AS pid, p.name,
                p.price, p.available, p.shop_id
                FROM cart c
                JOIN products p ON p.id=c.product_id
                WHERE c.user_id=?
            """, (uid,)).fetchall()

    def cart_change(self, uid, pid, delta):
        with self.get_conn() as conn:
            r = conn.execute("""
                SELECT qty FROM cart
                WHERE user_id=? AND product_id=?
            """, (uid, pid)).fetchone()

            if not r:
                return

            qty = r["qty"] + delta

            if qty <= 0:
                conn.execute("""
                    DELETE FROM cart
                    WHERE user_id=? AND product_id=?
                """, (uid, pid))
            else:
                conn.execute("""
                    UPDATE cart SET qty=?
                    WHERE user_id=? AND product_id=?
                """, (qty, uid, pid))

    def cart_clear(self, uid):
        with self.get_conn() as conn:
            conn.execute(
                "DELETE FROM cart WHERE user_id=?", (uid,)
            )

    # ORDERS

    def create_order(self, uid, name, phone, address):
        with self.get_conn() as conn:
            items = conn.execute("""
                SELECT c.qty, p.id AS pid, p.name,
                p.price, p.available, p.shop_id
                FROM cart c
                JOIN products p ON p.id=c.product_id
                WHERE c.user_id=?
            """, (uid,)).fetchall()

            if not items or any(not i["available"] for i in items):
                return None

            shop_ids = {i["shop_id"] for i in items}

            if len(shop_ids) != 1:
                return None

            shop_id = next(iter(shop_ids))
            total = sum(i["price"] * i["qty"] for i in items)

            c = conn.execute("""
                INSERT INTO orders
                (shop_id, customer_id, customer_name,
                phone, address, total, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                shop_id, uid, name, phone,
                address, total, int(time.time())
            ))

            oid = c.lastrowid

            for i in items:
                conn.execute("""
                    INSERT INTO order_items
                    (order_id, product_name, price, qty)
                    VALUES (?, ?, ?, ?)
                """, (
                    oid, i["name"], i["price"], i["qty"]
                ))

            conn.execute(
                "DELETE FROM cart WHERE user_id=?", (uid,)
            )

            return oid

    def get_order(self, oid):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM orders WHERE id=?", (oid,)
            ).fetchone()

    def order_items(self, oid):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM order_items WHERE order_id=?
            """, (oid,)).fetchall()

    def active_orders(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM orders
                WHERE shop_id=?
                AND status NOT IN ('delivered', 'canceled')
                ORDER BY id DESC
            """, (shop_id,)).fetchall()

    def set_status(self, oid, status):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE orders SET status=? WHERE id=?
            """, (status, oid))

    def user_orders(self, uid, limit=5):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT o.*, s.name AS shop_name
                FROM orders o
                JOIN shops s ON s.id=o.shop_id
                WHERE o.customer_id=?
                ORDER BY o.id DESC LIMIT ?
            """, (uid, limit)).fetchall()

    def cancel_order(self, oid, uid):
        with self.get_conn() as conn:
            c = conn.execute("""
                UPDATE orders SET status='canceled'
                WHERE id=? AND customer_id=?
                AND status='new'
            """, (oid, uid))
            return c.rowcount > 0

    # STATISTICS

    def day_stats(self, shop_id, ts):
        with self.get_conn() as conn:
            r = conn.execute("""
                SELECT COUNT(*) AS cnt,
                COALESCE(SUM(total), 0) AS summ
                FROM orders
                WHERE shop_id=? AND status!='canceled'
                AND created_at>=?
            """, (shop_id, ts)).fetchone()

            return r["cnt"], r["summ"]

    def monthly_stats(self, shop_id, ts):
        return self.day_stats(shop_id, ts)

    def totals(self):
        with self.get_conn() as conn:
            u = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            s = conn.execute(
                "SELECT COUNT(*) FROM shops"
            ).fetchone()[0]

            p = conn.execute(
                "SELECT COUNT(*) FROM products"
            ).fetchone()[0]

            o = conn.execute(
                "SELECT COUNT(*) FROM orders"
            ).fetchone()[0]

            sm = conn.execute("""
                SELECT COALESCE(SUM(total), 0)
                FROM orders WHERE status!='canceled'
            """).fetchone()[0]

            return {
                "users": u,
                "shops": s,
                "products": p,
                "orders": o,
                "sum": sm
            }

    # SETTINGS

    def get_setting(self, key, default=""):
        with self.get_conn() as conn:
            r = conn.execute("""
                SELECT value FROM settings WHERE key=?
            """, (key,)).fetchone()

            return r["value"] if r else default

    def set_setting(self, key, value):
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO settings(key, value)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
            """, (key, str(value)))

    # FEEDBACK

    def add_feedback(self, uid, user_name, text):
        with self.get_conn() as conn:
            c = conn.execute("""
                INSERT INTO feedback
                (user_id, user_name, text, created_at)
                VALUES (?, ?, ?, ?)
            """, (uid, user_name, text, int(time.time())))

            return c.lastrowid

    def get_feedback(self, limit=20):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM feedback
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()

    # PAYMENTS

    def add_payment(self, uid, shop_name, receipt, amount):
        with self.get_conn() as conn:
            c = conn.execute("""
                INSERT INTO payments
                (user_id, shop_name, receipt, amount, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                uid, shop_name, receipt,
                amount, int(time.time())
            ))
            return c.lastrowid

    def get_payment(self, payment_id):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM payments WHERE id=?
            """, (payment_id,)).fetchone()

    def set_payment_status(self, payment_id, status):
        with self.get_conn() as conn:
            c = conn.execute("""
                UPDATE payments SET status=?
                WHERE id=? AND status='pending'
            """, (status, payment_id))
            return c.rowcount > 0

    def pending_payments(self):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT * FROM payments
                WHERE status='pending'
                ORDER BY id DESC
            """).fetchall()