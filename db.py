"""Ma'lumotlar bazasi (SQLite). Faqat standart kutubxona kerak."""
import secrets
import sqlite3
import time


class DB:
    def __init__(self, path: str = "shop.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                shop_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS shops(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER UNIQUE NOT NULL,
                name TEXT NOT NULL,
                phone TEXT,
                code TEXT UNIQUE NOT NULL,
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS carts(
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                qty INTEGER NOT NULL,
                PRIMARY KEY(user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                customer_name TEXT,
                phone TEXT,
                address TEXT,
                total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items(
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                qty INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    # ---------- foydalanuvchi ----------
    def upsert_user(self, user_id: int, name: str):
        self.conn.execute(
            "INSERT INTO users(user_id, name) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
            (user_id, name),
        )
        self.conn.commit()

    def set_user_shop(self, user_id: int, shop_id: int):
        self.conn.execute("UPDATE users SET shop_id=? WHERE user_id=?", (shop_id, user_id))
        self.conn.commit()

    def get_user_shop(self, user_id: int):
        r = self.conn.execute(
            "SELECT s.* FROM users u JOIN shops s ON s.id=u.shop_id WHERE u.user_id=?",
            (user_id,),
        ).fetchone()
        return r

    # ---------- do'kon ----------
    def create_shop(self, owner_id: int, name: str, phone: str):
        code = secrets.token_hex(4)
        cur = self.conn.execute(
            "INSERT INTO shops(owner_id, name, phone, code, created) VALUES(?,?,?,?,?)",
            (owner_id, name, phone, code, int(time.time())),
        )
        self.conn.commit()
        return self.get_shop(cur.lastrowid)

    def get_shop(self, shop_id: int):
        return self.conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()

    def get_shop_by_owner(self, owner_id: int):
        return self.conn.execute("SELECT * FROM shops WHERE owner_id=?", (owner_id,)).fetchone()

    def get_shop_by_code(self, code: str):
        return self.conn.execute("SELECT * FROM shops WHERE code=?", (code,)).fetchone()

    def rename_shop(self, shop_id: int, name: str):
        self.conn.execute("UPDATE shops SET name=? WHERE id=?", (name, shop_id))
        self.conn.commit()

    # ---------- mahsulot ----------
    def add_product(self, shop_id: int, name: str, price: int, photo=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO products(shop_id, name, price, photo) VALUES(?,?,?,?)",
            (shop_id, name, price, photo),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_product(self, pid: int):
        return self.conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

    def list_products(self, shop_id: int, only_available: bool = False):
        q = "SELECT * FROM products WHERE shop_id=?"
        if only_available:
            q += " AND available=1"
        q += " ORDER BY id"
        return self.conn.execute(q, (shop_id,)).fetchall()

    def count_products(self, shop_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE shop_id=?", (shop_id,)
        ).fetchone()[0]

    def set_available(self, pid: int, flag: bool):
        self.conn.execute("UPDATE products SET available=? WHERE id=?", (1 if flag else 0, pid))
        self.conn.commit()

    def set_price(self, pid: int, price: int):
        self.conn.execute("UPDATE products SET price=? WHERE id=?", (price, pid))
        self.conn.commit()

    def delete_product(self, pid: int):
        self.conn.execute("DELETE FROM products WHERE id=?", (pid,))
        self.conn.commit()

    # ---------- savatcha ----------
    def cart_add(self, user_id: int, pid: int):
        """Savatga 1 ta qo'shadi. Boshqa do'kon mahsulotlari bo'lsa, ularni olib tashlaydi.
        Qaytaradi: (savatdagi soni, boshqa do'kondan tozalandimi)"""
        p = self.get_product(pid)
        if p is None:
            return 0, False
        cur = self.conn.execute(
            "DELETE FROM carts WHERE user_id=? AND product_id IN "
            "(SELECT c.product_id FROM carts c JOIN products x ON x.id=c.product_id "
            " WHERE c.user_id=? AND x.shop_id<>?)",
            (user_id, user_id, p["shop_id"]),
        )
        cleared = cur.rowcount > 0
        self.conn.execute(
            "INSERT INTO carts(user_id, product_id, qty) VALUES(?,?,1) "
            "ON CONFLICT(user_id, product_id) DO UPDATE SET qty=MIN(qty+1, 99)",
            (user_id, pid),
        )
        self.conn.commit()
        qty = self.conn.execute(
            "SELECT qty FROM carts WHERE user_id=? AND product_id=?", (user_id, pid)
        ).fetchone()[0]
        return qty, cleared

    def cart_change(self, user_id: int, pid: int, delta: int):
        r = self.conn.execute(
            "SELECT qty FROM carts WHERE user_id=? AND product_id=?", (user_id, pid)
        ).fetchone()
        if r is None:
            return
        q = r[0] + delta
        if q <= 0:
            self.conn.execute("DELETE FROM carts WHERE user_id=? AND product_id=?", (user_id, pid))
        else:
            self.conn.execute(
                "UPDATE carts SET qty=? WHERE user_id=? AND product_id=?",
                (min(q, 99), user_id, pid),
            )
        self.conn.commit()

    def cart_items(self, user_id: int):
        return self.conn.execute(
            "SELECT p.id AS pid, p.shop_id, p.name, p.price, p.available, c.qty "
            "FROM carts c JOIN products p ON p.id=c.product_id "
            "WHERE c.user_id=? ORDER BY p.id",
            (user_id,),
        ).fetchall()

    def cart_clear(self, user_id: int):
        self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        self.conn.commit()

    # ---------- buyurtma ----------
    def create_order(self, user_id: int, name: str, phone: str, address: str):
        items = [i for i in self.cart_items(user_id) if i["available"]]
        if not items:
            return None
        shop_id = items[0]["shop_id"]
        total = sum(i["price"] * i["qty"] for i in items)
        cur = self.conn.execute(
            "INSERT INTO orders(shop_id, customer_id, customer_name, phone, address, total, created) "
            "VALUES(?,?,?,?,?,?,?)",
            (shop_id, user_id, name, phone, address, total, int(time.time())),
        )
        oid = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO order_items(order_id, name, price, qty) VALUES(?,?,?,?)",
            [(oid, i["name"], i["price"], i["qty"]) for i in items],
        )
        self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        self.conn.commit()
        return oid

    def get_order(self, oid: int):
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    def order_items(self, oid: int):
        return self.conn.execute(
            "SELECT name, price, qty FROM order_items WHERE order_id=?", (oid,)
        ).fetchall()

    def set_status(self, oid: int, status: str):
        self.conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        self.conn.commit()

    def active_orders(self, shop_id: int, limit: int = 15):
        return self.conn.execute(
            "SELECT * FROM orders WHERE shop_id=? AND status IN ('new','accepted','ready') "
            "ORDER BY id DESC LIMIT ?",
            (shop_id, limit),
        ).fetchall()

    def user_orders(self, user_id: int, limit: int = 5):
        return self.conn.execute(
            "SELECT o.*, s.name AS shop_name FROM orders o JOIN shops s ON s.id=o.shop_id "
            "WHERE o.customer_id=? ORDER BY o.id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def day_stats(self, shop_id: int, since_ts: int):
        r = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders "
            "WHERE shop_id=? AND created>=? AND status<>'canceled'",
            (shop_id, since_ts),
        ).fetchone()
        return r[0], r[1]

    def totals(self):
        c = self.conn
        return {
            "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "shops": c.execute("SELECT COUNT(*) FROM shops").fetchone()[0],
            "products": c.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "orders": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "sum": c.execute(
                "SELECT COALESCE(SUM(total),0) FROM orders WHERE status<>'canceled'"
            ).fetchone()[0],
        }
