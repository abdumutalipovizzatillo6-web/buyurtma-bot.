import sqlite3

class DB:
    def __init__(self, db_path="shop.db"):
        self.db_path = db_path
        self.init_db()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_conn() as conn:
            c = conn.cursor()
            # Users table
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    shop_id INTEGER
                )
            """)
            # Shops table
            c.execute("""
                CREATE TABLE IF NOT EXISTS shops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER UNIQUE,
                    name TEXT,
                    phone TEXT,
                    code TEXT UNIQUE
                )
            """)
            # Categories table
            c.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER,
                    name TEXT
                )
            """)
            # Products table
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
            # Cart table
            c.execute("""
                CREATE TABLE IF NOT EXISTS cart (
                    user_id INTEGER,
                    product_id INTEGER,
                    qty INTEGER,
                    PRIMARY KEY (user_id, product_id)
                )
            """)
            # Orders table
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
            # Order items table
            c.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    product_name TEXT,
                    price INTEGER,
                    qty INTEGER
                )
            """)
            # System Settings table (Admin uchun)
            c.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # System Feedback table (Shikoyat va takliflar)
            c.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    text TEXT,
                    created_at INTEGER
                )
            """)
            conn.commit()

    # --- User operations ---
    def upsert_user(self, uid, full_name):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO users (id, full_name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET full_name=?",
                (uid, full_name, full_name)
            )

    def set_user_shop(self, uid, shop_id):
        with self.get_conn() as conn:
            conn.execute("UPDATE users SET shop_id=? WHERE id=?", (shop_id, uid))

    def get_user_shop(self, uid):
        with self.get_conn() as conn:
            u = conn.execute("SELECT shop_id FROM users WHERE id=?", (uid,)).fetchone()
            if u and u["shop_id"]:
                return conn.execute("SELECT * FROM shops WHERE id=?", (u["shop_id"],)).fetchone()
        return None

    # --- Shop operations ---
    def create_shop(self, owner_id, name, phone):
        import uuid
        code = str(uuid.uuid4())[:8]
        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO shops (owner_id, name, phone, code) VALUES (?, ?, ?, ?)",
                (owner_id, name, phone, code)
            )
            sid = c.lastrowid
            return conn.execute("SELECT * FROM shops WHERE id=?", (sid,)).fetchone()

    def get_shop_by_owner(self, owner_id):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM shops WHERE owner_id=?", (owner_id,)).fetchone()

    def get_shop_by_code(self, code):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM shops WHERE code=?", (code,)).fetchone()

    def get_shop(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()

    def rename_shop(self, shop_id, name):
        with self.get_conn() as conn:
            conn.execute("UPDATE shops SET name=? WHERE id=?", (name, shop_id))

    # --- Category operations ---
    def add_category(self, shop_id, name):
        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO categories (shop_id, name) VALUES (?, ?)", (shop_id, name))
            return c.lastrowid

    def get_categories(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM categories WHERE shop_id=?", (shop_id,)).fetchall()

    # --- Product operations ---
    def add_product(self, shop_id, name, price, photo, category_id=None):
        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO products (shop_id, category_id, name, price, photo) VALUES (?, ?, ?, ?, ?)",
                (shop_id, category_id, name, price, photo)
            )
            return c.lastrowid

    def list_products(self, shop_id, category_id=None, only_available=False):
        with self.get_conn() as conn:
            sql = "SELECT * FROM products WHERE shop_id=?"
            params = [shop_id]
            if category_id:
                sql += " AND category_id=?"
                params.append(category_id)
            if only_available:
                sql += " AND available=1"
            return conn.execute(sql, params).fetchall()

    def get_product(self, pid):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

    def set_available(self, pid, available):
        with self.get_conn() as conn:
            conn.execute("UPDATE products SET available=? WHERE id=?", (1 if available else 0, pid))

    def set_price(self, pid, price):
        with self.get_conn() as conn:
            conn.execute("UPDATE products SET price=? WHERE id=?", (price, pid))

    def delete_product(self, pid):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM products WHERE id=?", (pid,))

    def count_products(self, shop_id):
        with self.get_conn() as conn:
            r = conn.execute("SELECT COUNT(*) as c FROM products WHERE shop_id=?", (shop_id,)).fetchone()
            return r["c"] if r else 0

    # --- Cart operations ---
    def cart_add(self, uid, pid):
        p = self.get_product(pid)
        if not p:
            return 0, False
        with self.get_conn() as conn:
            # Tekshiramiz: agar savatda boshqa do'kon mahsuloti bo'lsa, tozalaymiz
            cur_shop = conn.execute(
                "SELECT p.shop_id FROM cart c JOIN products p ON c.product_id = p.id WHERE c.user_id=? LIMIT 1",
                (uid,)
            ).fetchone()
            cleared = False
            if cur_shop and cur_shop["shop_id"] != p["shop_id"]:
                conn.execute("DELETE FROM cart WHERE user_id=?", (uid,))
                cleared = True
            
            c = conn.execute("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (uid, pid)).fetchone()
            if c:
                new_qty = c["qty"] + 1
                conn.execute("UPDATE cart SET qty=? WHERE user_id=? AND product_id=?", (new_qty, uid, pid))
            else:
                new_qty = 1
                conn.execute("INSERT INTO cart (user_id, product_id, qty) VALUES (?, ?, ?)", (uid, pid, 1))
            return new_qty, cleared

    def cart_items(self, uid):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT c.qty, p.id as pid, p.name, p.price, p.available, p.shop_id "
                "FROM cart c JOIN products p ON c.product_id = p.id WHERE c.user_id=?",
                (uid,)
            ).fetchall()

    def cart_change(self, uid, pid, delta):
        with self.get_conn() as conn:
            c = conn.execute("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (uid, pid)).fetchone()
            if not c:
                return
            new_qty = c["qty"] + delta
            if new_qty <= 0:
                conn.execute("DELETE FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
            else:
                conn.execute("UPDATE cart SET qty=? WHERE user_id=? AND product_id=?", (new_qty, uid, pid))

    def cart_clear(self, uid):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM cart WHERE user_id=?", (uid,))

    # --- Order operations ---
    def create_order(self, uid, name, phone, address):
        import time
        items = self.cart_items(uid)
        valid_items = [i for i in items if i["available"]]
        if not valid_items:
            return None
        shop_id = valid_items[0]["shop_id"]
        total = sum(i["price"] * i["qty"] for i in valid_items)
        now = int(time.time())
        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO orders (shop_id, customer_id, customer_name, phone, address, total, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (shop_id, uid, name, phone, address, total, now)
            )
            oid = c.lastrowid
            for i in valid_items:
                c.execute(
                    "INSERT INTO order_items (order_id, product_name, price, qty) VALUES (?, ?, ?, ?)",
                    (oid, i["name"], i["price"], i["qty"])
                )
            c.execute("DELETE FROM cart WHERE user_id=?", (uid,))
            return oid

    def get_order(self, oid):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    def order_items(self, oid):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()

    def active_orders(self, shop_id):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM orders WHERE shop_id=? AND status NOT IN ('delivered', 'canceled') ORDER BY id DESC",
                (shop_id,)
            ).fetchall()

    def set_status(self, oid, status):
        with self.get_conn() as conn:
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))

    def user_orders(self, uid, limit=5):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT o.*, s.name as shop_name FROM orders o JOIN shops s ON o.shop_id = s.id "
                "WHERE o.customer_id=? ORDER BY o.id DESC LIMIT ?",
                (uid, limit)
            ).fetchall()

    def day_stats(self, shop_id, ts):
        with self.get_conn() as conn:
            r = conn.execute(
                "SELECT COUNT(*) as cnt, SUM(total) as summ FROM orders "
                "WHERE shop_id=? AND status != 'canceled' AND created_at >= ?",
                (shop_id, ts)
            ).fetchone()
            return (r["cnt"] or 0, r["summ"] or 0)

    # --- Settings & Feedback (Admin) ---
    def get_setting(self, key, default=""):
        with self.get_conn() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key, value):
        with self.get_conn() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?", (key, str(value), str(value)))

    def add_feedback(self, uid, user_name, text):
        import time
        with self.get_conn() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO feedback (user_id, user_name, text, created_at) VALUES (?, ?, ?, ?)", (uid, user_name, text, int(time.time())))
            return c.lastrowid

    def totals(self):
        with self.get_conn() as conn:
            u = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            s = conn.execute("SELECT COUNT(*) as c FROM shops").fetchone()["c"]
            p = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
            o = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
            sm = conn.execute("SELECT SUM(total) as s FROM orders WHERE status != 'canceled'").fetchone()["s"] or 0
            return {"users": u, "shops": s, "products": p, "orders": o, "sum": sm}

    def all_users(self):
        with self.get_conn() as conn:
            return conn.execute("SELECT id FROM users").fetchall()
