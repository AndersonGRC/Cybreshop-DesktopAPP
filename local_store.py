import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path


APP_DIR_NAME = "CyberShopNative"
DB_FILE_NAME = "cybershop_offline.db"


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except OSError:
        fallback = Path.cwd() / "desktop_native_data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def default_db_path() -> Path:
    return app_data_dir() / DB_FILE_NAME


class LocalStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or default_db_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed_first_run()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def authenticate(self, email: str, password: str) -> User | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, email, name, role, password_hash FROM users WHERE lower(email) = lower(?) AND active = 1",
                (email,),
            ).fetchone()

        if not row or not verify_password(password, row["password_hash"]):
            return None

        return User(id=row["id"], email=row["email"], name=row["name"], role=row["role"])

    def dashboard_metrics(self):
        with self.connect() as conn:
            products = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]
            low_stock = conn.execute(
                "SELECT COUNT(*) FROM products WHERE active = 1 AND stock <= min_stock"
            ).fetchone()[0]
            pending_sync = conn.execute("SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL").fetchone()[0]
            today_sales = conn.execute(
                "SELECT COALESCE(SUM(total), 0) FROM sales WHERE date(created_at) = date('now', 'localtime')"
            ).fetchone()[0]

        return {
            "products": products,
            "low_stock": low_stock,
            "pending_sync": pending_sync,
            "today_sales": float(today_sales or 0),
        }

    def products(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sku, barcode, name, category, stock, min_stock, price
                FROM products
                WHERE active = 1
                ORDER BY name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def product_options(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sku, barcode, name, stock, price
                FROM products
                WHERE active = 1
                ORDER BY name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def find_product_by_barcode(self, code: str):
        code = (code or "").strip()
        if not code:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, sku, barcode, name, stock, price
                FROM products
                WHERE active = 1
                  AND (
                        UPPER(TRIM(barcode)) = UPPER(TRIM(?))
                     OR UPPER(TRIM(sku))     = UPPER(TRIM(?))
                  )
                LIMIT 1
                """,
                (code, code),
            ).fetchone()
        return dict(row) if row else None

    def save_product(self, product_id, sku, name, category, stock, min_stock, price, barcode=""):
        sku = (sku or "").strip()
        barcode = (barcode or "").strip() or None
        payload = {
            "sku": sku,
            "barcode": barcode,
            "name": name,
            "category": category,
            "stock": int(stock),
            "min_stock": int(min_stock),
            "price": float(price),
        }

        with self.connect() as conn:
            dup_sku = conn.execute(
                "SELECT id FROM products WHERE sku = ? AND id != COALESCE(?, -1)",
                (sku, product_id),
            ).fetchone()
            if dup_sku:
                raise ValueError(f"El SKU '{sku}' ya esta en uso por otro producto.")

            if barcode:
                dup_barcode = conn.execute(
                    "SELECT id, name FROM products WHERE barcode = ? AND active = 1 AND id != COALESCE(?, -1)",
                    (barcode, product_id),
                ).fetchone()
                if dup_barcode:
                    raise ValueError(
                        f"El codigo de barras '{barcode}' ya esta asignado a '{dup_barcode['name']}'."
                    )

            if product_id:
                conn.execute(
                    """
                    UPDATE products
                    SET sku = ?, barcode = ?, name = ?, category = ?, stock = ?, min_stock = ?, price = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (sku, barcode, name, category, int(stock), int(min_stock), float(price), int(product_id)),
                )
                entity_id = str(product_id)
                action = "update"
            else:
                cur = conn.execute(
                    """
                    INSERT INTO products (sku, barcode, name, category, stock, min_stock, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sku, barcode, name, category, int(stock), int(min_stock), float(price)),
                )
                entity_id = str(cur.lastrowid)
                action = "create"

            self._queue_outbox(conn, "product", entity_id, action, payload)
        return entity_id

    def delete_product(self, product_id):
        with self.connect() as conn:
            conn.execute(
                "UPDATE products SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(product_id),),
            )
            self._queue_outbox(conn, "product", str(product_id), "delete", {"id": int(product_id)})

    def adjust_stock(self, product_id, quantity_delta, reason):
        product_id = int(product_id)
        quantity_delta = int(quantity_delta)
        reason = reason.strip() or "Ajuste manual"

        with self.connect() as conn:
            row = conn.execute(
                "SELECT stock FROM products WHERE id = ? AND active = 1",
                (product_id,),
            ).fetchone()
            if not row:
                raise ValueError("Producto no encontrado.")

            new_stock = int(row["stock"]) + quantity_delta
            if new_stock < 0:
                raise ValueError("El ajuste deja el inventario en negativo.")

            conn.execute(
                "UPDATE products SET stock = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_stock, product_id),
            )
            cur = conn.execute(
                """
                INSERT INTO inventory_movements (product_id, quantity_delta, reason)
                VALUES (?, ?, ?)
                """,
                (product_id, quantity_delta, reason),
            )
            self._queue_outbox(
                conn,
                "inventory_movement",
                str(cur.lastrowid),
                "create",
                {"product_id": product_id, "quantity_delta": quantity_delta, "reason": reason},
            )
        return new_stock

    def inventory_movements(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT im.created_at, p.sku, p.name, im.quantity_delta, im.reason
                FROM inventory_movements im
                JOIN products p ON p.id = im.product_id
                ORDER BY im.id DESC
                LIMIT 100
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_sale(self, items):
        if not items:
            raise ValueError("Agrega productos al POS antes de vender.")

        with self.connect() as conn:
            normalized = []
            total = 0.0
            for item in items:
                product = conn.execute(
                    "SELECT id, sku, name, stock, price FROM products WHERE id = ? AND active = 1",
                    (int(item["product_id"]),),
                ).fetchone()
                if not product:
                    raise ValueError("Uno de los productos ya no existe.")

                quantity = int(item["quantity"])
                if quantity <= 0:
                    raise ValueError("La cantidad debe ser mayor a cero.")
                if int(product["stock"]) < quantity:
                    raise ValueError(f"Stock insuficiente para {product['name']}.")

                line_total = float(product["price"]) * quantity
                total += line_total
                normalized.append(
                    {
                        "product_id": int(product["id"]),
                        "sku": product["sku"],
                        "name": product["name"],
                        "quantity": quantity,
                        "unit_price": float(product["price"]),
                        "line_total": line_total,
                    }
                )

            receipt = self._next_receipt(conn)
            cur = conn.execute(
                "INSERT INTO sales (receipt_number, total) VALUES (?, ?)",
                (receipt, total),
            )
            sale_id = cur.lastrowid

            for item in normalized:
                conn.execute(
                    """
                    INSERT INTO sale_items (sale_id, product_id, quantity, unit_price, line_total)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sale_id, item["product_id"], item["quantity"], item["unit_price"], item["line_total"]),
                )
                conn.execute(
                    "UPDATE products SET stock = stock - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item["quantity"], item["product_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO inventory_movements (product_id, quantity_delta, reason)
                    VALUES (?, ?, ?)
                    """,
                    (item["product_id"], -item["quantity"], f"Venta {receipt}"),
                )

            self._queue_outbox(conn, "sale", str(sale_id), "create", {"receipt": receipt, "total": total, "items": normalized})
        return {"receipt": receipt, "total": total}

    def sales(self, limit=200):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.receipt_number, s.total, s.created_at,
                       COALESCE(SUM(si.quantity), 0) AS total_items
                FROM sales s
                LEFT JOIN sale_items si ON si.sale_id = s.id
                GROUP BY s.id
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def sale_detail(self, sale_id):
        with self.connect() as conn:
            sale = conn.execute(
                "SELECT id, receipt_number, total, created_at FROM sales WHERE id = ?",
                (int(sale_id),),
            ).fetchone()
            if not sale:
                return None
            items = conn.execute(
                """
                SELECT si.quantity, si.unit_price, si.line_total,
                       p.sku, p.name
                FROM sale_items si
                JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = ?
                ORDER BY si.id
                """,
                (int(sale_id),),
            ).fetchall()
        return {
            "sale": dict(sale),
            "items": [dict(item) for item in items],
        }

    def sales_summary(self):
        with self.connect() as conn:
            today = conn.execute(
                """
                SELECT COALESCE(SUM(total),0) AS total, COUNT(*) AS cnt
                FROM sales WHERE date(created_at) = date('now','localtime')
                """
            ).fetchone()
            month = conn.execute(
                """
                SELECT COALESCE(SUM(total),0) AS total, COUNT(*) AS cnt
                FROM sales WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now','localtime')
                """
            ).fetchone()
            total_all = conn.execute(
                "SELECT COALESCE(SUM(total),0) AS total, COUNT(*) AS cnt FROM sales"
            ).fetchone()
        return {
            "today": dict(today),
            "month": dict(month),
            "all": dict(total_all),
        }

    def users(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, email, name, role, active, created_at FROM users ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_user(self, user_id, email, name, role, password=None, active=True):
        email = (email or "").strip().lower()
        name = (name or "").strip()
        role = (role or "Cajero").strip()
        if not email or not name:
            raise ValueError("Correo y nombre son obligatorios.")
        if "@" not in email:
            raise ValueError("Correo invalido.")

        with self.connect() as conn:
            dup = conn.execute(
                "SELECT id FROM users WHERE lower(email) = lower(?) AND id != COALESCE(?, -1)",
                (email, user_id),
            ).fetchone()
            if dup:
                raise ValueError(f"El correo '{email}' ya esta en uso.")

            if user_id:
                if password:
                    conn.execute(
                        "UPDATE users SET email=?, name=?, role=?, active=?, password_hash=? WHERE id=?",
                        (email, name, role, 1 if active else 0, hash_password(password), int(user_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET email=?, name=?, role=?, active=? WHERE id=?",
                        (email, name, role, 1 if active else 0, int(user_id)),
                    )
                return int(user_id)
            else:
                if not password:
                    raise ValueError("La contrasena es obligatoria al crear un usuario.")
                cur = conn.execute(
                    "INSERT INTO users (email, name, role, password_hash, active) VALUES (?, ?, ?, ?, ?)",
                    (email, name, role, hash_password(password), 1 if active else 0),
                )
                return cur.lastrowid

    def deactivate_user(self, user_id):
        with self.connect() as conn:
            count_active = conn.execute(
                "SELECT COUNT(*) FROM users WHERE active = 1 AND id != ?", (int(user_id),)
            ).fetchone()[0]
            if count_active == 0:
                raise ValueError("No puedes desactivar el ultimo usuario activo.")
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (int(user_id),))

    def reset_outbox(self):
        with self.connect() as conn:
            conn.execute("UPDATE outbox SET synced_at = CURRENT_TIMESTAMP WHERE synced_at IS NULL")

    def clear_demo_data(self):
        """Borra productos, ventas, movimientos y outbox demo. Mantiene usuarios."""
        with self.connect() as conn:
            conn.executescript(
                """
                DELETE FROM sale_items;
                DELETE FROM sales;
                DELETE FROM inventory_movements;
                DELETE FROM products;
                DELETE FROM outbox;
                DELETE FROM sqlite_sequence WHERE name IN ('sales','sale_items','inventory_movements','products','outbox');
                """
            )

    def db_info(self):
        with self.connect() as conn:
            counts = {
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "products": conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],
                "sales": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
                "movements": conn.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0],
                "outbox_pending": conn.execute("SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL").fetchone()[0],
                "outbox_total": conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            }
        return {
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "counts": counts,
        }

    def outbox_items(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, entity, entity_id, action, created_at, synced_at
                FROM outbox
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL UNIQUE,
                    barcode TEXT,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'General',
                    stock INTEGER NOT NULL DEFAULT 0,
                    min_stock INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
                """
            )

            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
            if "barcode" not in existing_cols:
                conn.execute("ALTER TABLE products ADD COLUMN barcode TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")

            conn.executescript(
                """

                CREATE TABLE IF NOT EXISTS sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_number TEXT NOT NULL UNIQUE,
                    total REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sale_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity INTEGER NOT NULL,
                    unit_price REAL NOT NULL,
                    line_total REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inventory_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity_delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    synced_at TEXT
                );
                """
            )

    def _queue_outbox(self, conn, entity, entity_id, action, payload):
        conn.execute(
            """
            INSERT INTO outbox (entity, entity_id, action, payload)
            VALUES (?, ?, ?, ?)
            """,
            (entity, entity_id, action, json.dumps(payload, ensure_ascii=True)),
        )

    def _next_receipt(self, conn):
        next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM sales").fetchone()[0]
        return f"LOCAL-{int(next_id):04d}"

    def _seed_first_run(self):
        with self.connect() as conn:
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if user_count == 0:
                conn.execute(
                    """
                    INSERT INTO users (email, name, role, password_hash)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "admin@cybershop.local",
                        "Administrador Local",
                        "Administrador",
                        hash_password("admin123"),
                    ),
                )

            product_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            if product_count == 0:
                conn.executemany(
                    """
                    INSERT INTO products (sku, barcode, name, category, stock, min_stock, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("DEMO-001", "7701234567890", "Producto demo POS", "General", 18, 5, 25000),
                        ("DEMO-002", "7701234567891", "Inventario bajo", "General", 2, 5, 48000),
                        ("DEMO-003", "7701234567892", "Servicio local", "Servicios", 99, 10, 120000),
                    ],
                )



def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt, expected = stored_hash.split("$", 2)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return secrets.compare_digest(digest.hex(), expected)
