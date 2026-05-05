import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


APP_DIR_NAME = "CyberShopNative"
DB_FILE_NAME = "cybershop_offline.db"

# Iteraciones PBKDF2-SHA256. OWASP 2023 recomienda >=600_000 para SHA-256.
PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 120_000  # Hashes antiguos: re-hashear al login.

DEFAULT_ADMIN_EMAIL = "admin@cybershop.local"
DEFAULT_ADMIN_PASSWORD = "admin123"


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    must_change_password: bool = False


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

    @contextmanager
    def connect(self):
        """Yield una conexion SQLite con FK activadas y commit/rollback automaticos.

        - PRAGMA foreign_keys = ON enforce los REFERENCES del esquema.
        - context manager garantiza que la conexion se cierra (no solo el cursor).
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def authenticate(self, email: str, password: str) -> User | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, email, name, role, password_hash, must_change_password
                FROM users WHERE lower(email) = lower(?) AND active = 1
                """,
                (email,),
            ).fetchone()

            if not row or not verify_password(password, row["password_hash"]):
                return None

            # Migracion automatica: si el hash usa iteraciones antiguas, re-hashear.
            stored_iters = _hash_iterations(row["password_hash"])
            if stored_iters and stored_iters < PBKDF2_ITERATIONS:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), row["id"]),
                )

        return User(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            role=row["role"],
            must_change_password=bool(row["must_change_password"]),
        )

    def change_password(self, user_id: int, new_password: str) -> None:
        if not new_password or len(new_password) < 6:
            raise ValueError("La nueva contrasena debe tener al menos 6 caracteres.")
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (hash_password(new_password), int(user_id)),
            )

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
            # SKU duplicado: si el match es un producto inactivo y estamos creando,
            # reactivamos en vez de fallar con UNIQUE constraint.
            dup_sku = conn.execute(
                "SELECT id, active FROM products WHERE sku = ? AND id != COALESCE(?, -1)",
                (sku, product_id),
            ).fetchone()
            if dup_sku:
                if dup_sku["active"]:
                    raise ValueError(f"El SKU '{sku}' ya esta en uso por otro producto activo.")
                if product_id:
                    raise ValueError(
                        f"El SKU '{sku}' pertenece a un producto desactivado. Desactiva o renombra ese antes."
                    )
                # Reactivamos el desactivado con los nuevos datos en vez de crear.
                product_id = dup_sku["id"]

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
                        active = 1,
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
            local_mov_id = cur.lastrowid
            sku_row = conn.execute("SELECT sku FROM products WHERE id = ?", (product_id,)).fetchone()
            sku = sku_row["sku"] if sku_row else None
            self._queue_outbox(
                conn,
                "inventory_movement",
                str(local_mov_id),
                "create",
                {
                    "client_movement_id": f"desktop-{local_mov_id}",
                    "product_id": product_id,
                    "sku": sku,
                    "quantity_delta": quantity_delta,
                    "reason": reason,
                },
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

            sale_row = conn.execute(
                "SELECT created_at FROM sales WHERE id = ?", (sale_id,)
            ).fetchone()
            created_at_local = sale_row["created_at"] if sale_row else None
            self._queue_outbox(
                conn, "sale", str(sale_id), "create",
                {
                    "receipt": receipt,
                    "total": total,
                    "created_at_local": created_at_local,
                    "items": normalized,
                },
            )
        return {"sale_id": int(sale_id), "receipt": receipt, "total": total}

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

    def clear_demo_data(self, acting_user: User | None = None):
        """Borra productos, ventas, movimientos y outbox demo. Mantiene usuarios.

        Solo permite la operacion si acting_user tiene rol Administrador.
        Marca seed_done=1 para evitar que el seed inicial vuelva a poblar.
        """
        if acting_user is None or acting_user.role != "Administrador":
            raise PermissionError("Solo un Administrador puede limpiar los datos demo.")
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
            self._set_meta(conn, "seed_done", "1")

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

    def pending_outbox(self, limit: int = 50, entities: tuple[str, ...] = ("sale", "inventory_movement")):
        """Devuelve items pendientes de enviar al servidor (max `limit`).

        Filtra por entidades soportadas por el server (sale, inventory_movement).
        Otras entidades quedan encoladas pero no se intentan empujar.
        """
        placeholders = ",".join("?" * len(entities))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, entity, entity_id, action, payload, created_at
                FROM outbox
                WHERE synced_at IS NULL
                  AND entity IN ({placeholders})
                ORDER BY id ASC
                LIMIT ?
                """,
                (*entities, int(limit)),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "entity": row["entity"],
                "entity_id": row["entity_id"],
                "action": row["action"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mark_outbox_synced(self, ids):
        """Marca los items de outbox como sincronizados (synced_at = ahora)."""
        ids = [int(i) for i in ids if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE outbox SET synced_at = CURRENT_TIMESTAMP "
                f"WHERE id IN ({placeholders}) AND synced_at IS NULL",
                ids,
            )
            return cur.rowcount

    def upsert_product_from_remote(self, remote_product):
        """Aplica un producto del servidor al SQLite local.

        Match por SKU (lo que el server llama 'sku' viene de productos.referencia).
        Si no existe, lo crea. Si existe, actualiza precio/nombre/stock/categoria.
        El campo barcode no se toca (la web no lo maneja, lo gestiona el desktop).
        Esta operacion NO encola outbox (no es un cambio local).
        """
        sku = (remote_product.get("sku") or "").strip()
        if not sku:
            raise ValueError("Producto remoto sin sku")

        name = (remote_product.get("name") or "").strip() or sku
        category = (remote_product.get("category") or "General").strip() or "General"
        try:
            price = float(remote_product.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            stock = int(remote_product.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM products WHERE sku = ?", (sku,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE products
                    SET name = ?, category = ?, price = ?, stock = ?,
                        active = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (name, category, price, stock, existing["id"]),
                )
                return {"action": "updated", "local_id": int(existing["id"])}
            cur = conn.execute(
                """
                INSERT INTO products (sku, name, category, price, stock, min_stock, active)
                VALUES (?, ?, ?, ?, ?, 0, 1)
                """,
                (sku, name, category, price, stock),
            )
            return {"action": "created", "local_id": int(cur.lastrowid)}

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
                    must_change_password INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            # Migraciones idempotentes (ALTER TABLE no es opcional en SQLite si la columna ya existe).
            self._ensure_column(conn, "products", "barcode", "TEXT")
            self._ensure_column(conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
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
                    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
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

    def _ensure_column(self, conn, table, column, ddl):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _get_meta(self, conn, key, default=None):
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, conn, key, value):
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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
        """Crea admin y productos demo SOLO en el primer arranque.

        Usa metadata.seed_done para no re-sembrar despues de Limpiar datos.
        El admin queda marcado con must_change_password = 1 para forzar
        cambio antes de operar.
        """
        with self.connect() as conn:
            # Admin: si no hay ningun usuario, sembrar.
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if user_count == 0:
                conn.execute(
                    """
                    INSERT INTO users (email, name, role, password_hash, must_change_password)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        DEFAULT_ADMIN_EMAIL,
                        "Administrador Local",
                        "Administrador",
                        hash_password(DEFAULT_ADMIN_PASSWORD),
                    ),
                )

            # Productos demo: solo si NUNCA se hizo el seed (no si user limpio datos).
            seed_done = self._get_meta(conn, "seed_done") == "1"
            if not seed_done:
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
                self._set_meta(conn, "seed_done", "1")



def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Genera hash PBKDF2-SHA256 con salt aleatorio y conteo de iteraciones embebido.

    Formato: pbkdf2_sha256$<iterations>$<salt_hex>$<digest_hex>
    El conteo se incluye para que verify_password sepa que iteraciones usar y
    detectar hashes antiguos que necesitan re-hashing.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _parse_hash(stored_hash: str):
    """Parsea ambos formatos: nuevo (con iters) y legacy (sin iters)."""
    parts = stored_hash.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            return parts[0], int(parts[1]), parts[2], parts[3]
        except ValueError:
            return None
    if len(parts) == 3 and parts[0] == "pbkdf2_sha256":
        return parts[0], LEGACY_PBKDF2_ITERATIONS, parts[1], parts[2]
    return None


def _hash_iterations(stored_hash: str) -> int:
    parsed = _parse_hash(stored_hash)
    return parsed[1] if parsed else 0


def verify_password(password: str, stored_hash: str) -> bool:
    parsed = _parse_hash(stored_hash)
    if not parsed:
        return False
    _algorithm, iterations, salt, expected = parsed
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return secrets.compare_digest(digest.hex(), expected)
