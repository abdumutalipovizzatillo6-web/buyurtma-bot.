import sqlite3
import random
import string
from typing import Optional, List, Dict, Any

class DB:
    def __init__(self, path: str):
        self.path = path
        self.init_db()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                full_name TEXT,
                current_shop_id INTEGER,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                category_id INTEGER,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                available INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER,
                product_id INTEGER,
                qty INTEGER,
                PRIMARY KEY(user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shop_id INTEGER NOT NULL,
                customer_name TEXT,
                phone TEXT,
                address TEXT,
                delivery_type TEXT,
                delivery_time TEXT,
                total INTEGER,
                status TEXT DEFAULT 'new',
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER,
                product_name TEXT,
                price INTEGER,
                qty INTEGER
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                message TEXT,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)

    def set_setting(self, key: str, value: str):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def upsert_user(self, uid: int, full_name: str):
        with self.connect() as conn:
            conn.execute("""
            INSERT INTO users (id, full_name, created_at) VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(id) DO UPDATE SET full_name = excluded.full_name
            """, (uid, full_name))

    def get_user_shop(self, uid: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            u = conn.execute("SELECT current_shop_id FROM users WHERE id = ?", (uid,)).fetchone()
            if u and u["current_shop_id"]:
                return conn.execute("SELECT * FROM shops WHERE id = ?", (u["current_shop_id"],)).fetchone()
            return None

    def set_user_shop(self, uid: int, shop_id: int):
        with self.connect() as conn:
            conn.execute("UPDATE users SET current_shop_id = ? WHERE id = ?", (shop_id, uid))

    def create_shop(self, owner_id: int, name: str, phone: str) -> Dict[str, Any]:
        code = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO shops (owner_id, name, phone, code, created_at) VALUES (?, ?, ?, ?, strftime('%s', 'now'))",
                (owner_id, name, phone, code)
            )
            return {"id": cursor.lastrowid, "owner_id": owner_id, "name": name, "phone": phone, "code": code}

    def get_shop_by_owner(self, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM shops WHERE owner_id = ?", (owner_id,)).fetchone()

    def get_shop_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM shops WHERE code = ?", (code,)).fetchone()

    def get_shop(self, shop_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()

    def rename_shop(self, shop_id: int, new_name: str):
        with self.connect() as conn:
            conn.execute("UPDATE shops SET name = ? WHERE id = ?", (new_name, shop_id))

    # Categories
    def add_category(self, shop_id: int, name: str):
        with self.connect() as conn:
            conn.execute("INSERT INTO categories (shop_id, name) VALUES (?, ?)", (shop_id, name))

    def list_categories(self, shop_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM categories WHERE shop_id = ?", (shop_id,)).fetchall()

    # Products
    def add_product(self, shop_id: int, name: str, price: int, photo: str = None, category_id: int = None):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO products (shop_id, category_id, name, price, photo) VALUES (?, ?, ?, ?, ?)",
                (shop_id, category_id, name, price, photo)
            )

    def list_products(self, shop_id: int, category_id: int = None, only_available: bool = False) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT * FROM products WHERE shop_id = ?"
            params = [shop_id]
            if category_id:
                query += " AND category_id = ?"
                params.append(category_id)
            if only_available:
                query += " AND available = 1"
            return conn.execute(query, params).fetchall()

    def get_product(self, pid: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def set_available(self, pid: int, available: bool):
        with self.connect() as conn:
            conn.execute("UPDATE products SET available = ? WHERE id = ?", (1 if available else 0, pid))

    def set_price(self, pid: int, price: int):
        with self.connect() as conn:
            conn.execute("UPDATE products SET price = ? WHERE id = ?", (price, pid))

    def delete_product(self, pid: int):
        with self.connect() as conn:
            conn.execute("DELETE FROM products WHERE id = ?", (pid,))

    def count_products(self, shop_id: int) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) as c FROM products WHERE shop_id = ?", (shop_id,)).fetchone()["c"]

    # Cart
    def cart_add(self, uid: int, pid: int):
        with self.connect() as conn:
            p = conn.execute("SELECT shop_id FROM products WHERE id = ?", (pid,)).fetchone()
            cleared = False
            if p:
                c_item = conn.execute(
                    "SELECT p.shop_id FROM cart c JOIN products p ON c.product_id = p.id WHERE c.user_id = ? LIMIT 1",
                    (uid,)
                ).fetchone()
                if c_item and c_item["shop_id"] != p["shop_id"]:
                    conn.execute("DELETE FROM cart WHERE user_id = ?", (uid,))
                    cleared = True
            
            row = conn.execute("SELECT qty FROM cart WHERE user_id = ? AND product_id = ?", (uid, pid)).fetchone()
            if row:
                nqty = row["qty"] + 1
                conn.execute("UPDATE cart SET qty = ? WHERE user_id = ? AND product_id = ?", (nqty, uid, pid))
            else:
                nqty = 1
                conn.execute("INSERT INTO cart (user_id, product_id, qty) VALUES (?, ?, 1)", (uid, pid))
            return nqty, cleared

    def cart_change(self, uid: int, pid: int, delta: int):
        with self.connect() as conn:
            row = conn.execute("SELECT qty FROM cart WHERE user_id = ? AND product_id = ?", (uid, pid)).fetchone()
            if not row:
                return
            nqty = row["qty"] + delta
            if nqty <= 0:
                conn.execute("DELETE FROM cart WHERE user_id = ? AND product_id = ?", (uid, pid))
            else:
                conn.execute("UPDATE cart SET qty = ? WHERE user_id = ? AND product_id = ?", (nqty, uid, pid))

    def cart_items(self, uid: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("""
            SELECT c.product_id as pid, c.qty, p.name, p.price, p.available, p.shop_id
            FROM cart c JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
            """, (uid,)).fetchall()

    def cart_clear(self, uid: int):
        with self.connect() as conn:
            conn.execute("DELETE FROM cart WHERE user_id = ?", (uid,))

    # Orders
    def create_order(self, uid: int, name: str, phone: str, address: str, del_type: str, del_time: str) -> Optional[int]:
        items = self.cart_items(uid)
        if not items:
            return None
        valid_items = [i for i in items if i["available"]]
        if not valid_items:
            return None
        shop_id = valid_items[0]["shop_id"]
        total = sum(i["price"] * i["qty"] for i in valid_items)
        with self.connect() as conn:
            c = conn.execute("""
            INSERT INTO orders (user_id, shop_id, customer_name, phone, address, delivery_type, delivery_time, total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """, (uid, shop_id, name, phone, address, del_type, del_time, total))
            oid = c.lastrowid
            for i in valid_items:
                conn.execute("""
                INSERT INTO order_items (order_id, product_name, price, qty)
                VALUES (?, ?, ?, ?)
                """, (oid, i["name"], i["price"], i["qty"]))
            conn.execute("DELETE FROM cart WHERE user_id = ?", (uid,))
            return oid

    def get_order(self, oid: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()

    def order_items(self, oid: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM order_items WHERE order_id = ?", (oid,)).fetchall()

    def active_orders(self, shop_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE shop_id = ? AND status != 'delivered' AND status != 'canceled'", (shop_id,)).fetchall()

    def set_status(self, oid: int, status: str):
        with self.connect() as conn:
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, oid))

    def user_orders(self, uid: int, limit: int = 5) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON o.shop_id = s.id
            WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?
            """, (uid, limit)).fetchall()

    def day_stats(self, shop_id: int, day_start_ts: int):
        with self.connect() as conn:
            r = conn.execute("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(total), 0) as summ
            FROM orders WHERE shop_id = ? AND created_at >= ? AND status != 'canceled'
            """, (shop_id, day_start_ts)).fetchone()
            return r["cnt"], r["summ"]

    # Feedback
    def add_feedback(self, uid: int, name: str, message: str):
        with self.connect() as conn:
            conn.execute("""
            INSERT INTO feedback (user_id, user_name, message, created_at)
            VALUES (?, ?, ?, strftime('%s', 'now'))
            """, (uid, name, message))

    def totals(self) -> Dict[str, Any]:
        with self.connect() as conn:
            users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            shops = conn.execute("SELECT COUNT(*) as c FROM shops").fetchone()["c"]
            products = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
            orders = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
            summ = conn.execute("SELECT COALESCE(SUM(total), 0) as s FROM orders WHERE status != 'canceled'").fetchone()["s"]
            return {"users": users, "shops": shops, "products": products, "orders": orders, "sum": summ}