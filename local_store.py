import hashlib
import json
import os
import secrets
import sqlite3
import uuid as _uuid
from contextlib import contextmanager
from datetime import date, timedelta
from dataclasses import dataclass
from pathlib import Path


APP_DIR_NAME = "CyberShopNative"
DB_FILE_NAME = "cybershop_offline.db"

# Iteraciones PBKDF2-SHA256. OWASP 2023 recomienda >=600_000 para SHA-256.
PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 120_000  # Hashes antiguos: re-hashear al login.

DEFAULT_ADMIN_EMAIL = "admin@cybershop.local"
DEFAULT_ADMIN_PASSWORD = "admin123"

# Roles de producción (tabla `roles`, espejo de security.py del backend):
#   1 admin · 2 usuario/propietario · 3 cliente · 4 empleado · 5 contador
#   6 Mesero · 7 Cajero.  Se prioriza rol_id (autoritativo) y se cae al nombre.
_ROLE_BY_ID = {
    1: "Administrador", 2: "Administrador", 3: "Cliente",
    4: "Empleado", 5: "Contador", 6: "Mesero", 7: "Cajero",
}


def map_role(rol_id, rol_nombre=None) -> str:
    """Traduce el rol de producción a un rol canónico del desktop."""
    try:
        rid = int(rol_id) if rol_id is not None else None
    except (TypeError, ValueError):
        rid = None
    if rid in _ROLE_BY_ID:
        return _ROLE_BY_ID[rid]
    name = (rol_nombre or "").strip().lower()
    if name in ("admin", "administrador", "super_admin", "propietario", "usuario", "owner", "dueño"):
        return "Administrador"
    if name == "mesero":
        return "Mesero"
    if name == "cajero":
        return "Cajero"
    if name in ("empleado", "vendedor"):
        return "Empleado"
    if name == "contador":
        return "Contador"
    if name == "cliente":
        return "Cliente"
    return "Cajero"


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

    def cache_remote_login(self, remote_user: dict, password: str) -> "User":
        """Persiste el resultado de un login remoto exitoso.

        Crea o actualiza el registro local en `users` usando los datos del
        VPS (email, nombre, rol, remote_id) y guarda el hash PBKDF2 del
        password recién verificado por el servidor. Esto permite que el
        siguiente login del mismo usuario funcione offline contra el cache.
        """
        email = (remote_user.get("email") or "").strip().lower()
        if not email:
            raise ValueError("Respuesta remota sin email.")
        nombre = (remote_user.get("nombre") or remote_user.get("name") or email).strip()
        role = map_role(remote_user.get("rol_id"), remote_user.get("rol_nombre"))
        remote_id = remote_user.get("remote_id")
        estado = (remote_user.get("estado") or "habilitado").strip().lower()
        active = 0 if estado in ("deshabilitado", "eliminado") else 1
        pwd_hash = hash_password(password)

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE LOWER(email) = LOWER(?)",
                (email,),
            ).fetchone()
            if existing:
                user_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE users
                       SET name = ?, role = ?, active = ?, remote_id = ?,
                           password_hash = ?, must_change_password = 0,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                    """,
                    (nombre, role, active, remote_id, pwd_hash, user_id),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO users (email, name, role, password_hash, active,
                                       must_change_password, remote_id)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (email, nombre, role, pwd_hash, active, remote_id),
                )
                user_id = int(cur.lastrowid)

        return User(
            id=user_id,
            email=email,
            name=nombre,
            role=role,
            must_change_password=False,
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

    def dashboard_extras(self):
        """Series para los mini-gráficos del dashboard: ventas de los últimos
        7 días, top de productos vendidos (30 días) y salud del stock."""
        hoy = date.today()
        dias = [hoy - timedelta(days=i) for i in range(6, -1, -1)]
        with self.connect() as conn:
            ventas = {r["d"]: (float(r["t"]), int(r["c"])) for r in conn.execute(
                """SELECT date(created_at) d, COALESCE(SUM(total),0) t, COUNT(*) c
                   FROM sales WHERE date(created_at) >= date('now', 'localtime', '-6 days')
                   GROUP BY date(created_at)""")}
            top = [dict(r) for r in conn.execute(
                """SELECT p.name AS name, COALESCE(SUM(si.quantity),0) AS unidades,
                          COALESCE(SUM(si.line_total),0) AS total
                   FROM sale_items si
                   JOIN sales s ON s.id = si.sale_id
                   JOIN products p ON p.id = si.product_id
                   WHERE date(s.created_at) >= date('now', 'localtime', '-29 days')
                   GROUP BY si.product_id ORDER BY unidades DESC LIMIT 4""")]
            stock_ok = conn.execute(
                "SELECT COUNT(*) FROM products WHERE active = 1 AND stock > min_stock").fetchone()[0]
            stock_low = conn.execute(
                "SELECT COUNT(*) FROM products WHERE active = 1 AND stock <= min_stock").fetchone()[0]
        return {
            "ventas_7d": [(d.isoformat(), *ventas.get(d.isoformat(), (0.0, 0))) for d in dias],
            "top_products": top,
            "stock_ok": int(stock_ok),
            "stock_low": int(stock_low),
        }

    def products(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sku, barcode, name, category, stock, min_stock, price,
                       image_path, genero_id, remote_id, updated_at
                FROM products
                WHERE active = 1
                ORDER BY name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def count_products(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])

    def count_generos(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM generos").fetchone()[0])

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

    def save_product(self, product_id, sku, name, category, stock, min_stock, price,
                     barcode="", image_path="", genero_id=None):
        sku = (sku or "").strip()
        barcode = (barcode or "").strip() or None
        image_path = (image_path or "").strip() or None
        payload = {
            "sku": sku,
            "barcode": barcode or "",
            "name": name,
            "category": category,
            "stock": int(stock),
            "min_stock": int(min_stock),
            "price": float(price),
            "image": image_path or "",
            "genero_id": int(genero_id) if genero_id else None,
            "active": True,
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
                    SET sku = ?, barcode = ?, name = ?, category = ?, stock = ?, min_stock = ?,
                        price = ?, image_path = ?, genero_id = ?, active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (sku, barcode, name, category, int(stock), int(min_stock), float(price),
                     image_path, genero_id, int(product_id)),
                )
                entity_id = str(product_id)
                action = "update"
            else:
                cur = conn.execute(
                    """
                    INSERT INTO products (sku, barcode, name, category, stock, min_stock,
                                          price, image_path, genero_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sku, barcode, name, category, int(stock), int(min_stock), float(price),
                     image_path, genero_id),
                )
                entity_id = str(cur.lastrowid)
                action = "create"

            # Releer updated_at para que el server pueda hacer LWW
            row = conn.execute("SELECT updated_at FROM products WHERE id = ?", (entity_id,)).fetchone()
            payload["updated_at"] = row["updated_at"] if row else None
            self._queue_outbox(conn, "product", entity_id, action, payload)
        return entity_id

    def delete_product(self, product_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sku FROM products WHERE id = ?", (int(product_id),)
            ).fetchone()
            sku = row["sku"] if row else None
            conn.execute(
                "UPDATE products SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(product_id),),
            )
            ts = conn.execute(
                "SELECT updated_at FROM products WHERE id = ?", (int(product_id),)
            ).fetchone()
            self._queue_outbox(conn, "product", str(product_id), "delete", {
                "sku": sku,
                "active": False,
                "updated_at": ts["updated_at"] if ts else None,
            })

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
                        "UPDATE users SET email=?, name=?, role=?, active=?, password_hash=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (email, name, role, 1 if active else 0, hash_password(password), int(user_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET email=?, name=?, role=?, active=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (email, name, role, 1 if active else 0, int(user_id)),
                    )
                final_id = int(user_id)
                action = "update"
            else:
                if not password:
                    raise ValueError("La contrasena es obligatoria al crear un usuario.")
                cur = conn.execute(
                    "INSERT INTO users (email, name, role, password_hash, active) VALUES (?, ?, ?, ?, ?)",
                    (email, name, role, hash_password(password), 1 if active else 0),
                )
                final_id = int(cur.lastrowid)
                action = "create"

            ts = conn.execute("SELECT updated_at FROM users WHERE id = ?", (final_id,)).fetchone()
            self._queue_outbox(conn, "user", str(final_id), action, {
                "email": email,
                "nombre": name,
                "rol_nombre": role,
                "estado": "habilitado" if active else "deshabilitado",
                "updated_at": ts["updated_at"] if ts else None,
            })
            return final_id

    def deactivate_user(self, user_id):
        with self.connect() as conn:
            count_active = conn.execute(
                "SELECT COUNT(*) FROM users WHERE active = 1 AND id != ?", (int(user_id),)
            ).fetchone()[0]
            if count_active == 0:
                raise ValueError("No puedes desactivar el ultimo usuario activo.")
            row = conn.execute("SELECT email FROM users WHERE id = ?", (int(user_id),)).fetchone()
            email = row["email"] if row else None
            conn.execute(
                "UPDATE users SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(user_id),),
            )
            ts = conn.execute("SELECT updated_at FROM users WHERE id = ?", (int(user_id),)).fetchone()
            self._queue_outbox(conn, "user", str(user_id), "delete", {
                "email": email,
                "estado": "deshabilitado",
                "updated_at": ts["updated_at"] if ts else None,
            })

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

    def pending_outbox(self, limit: int = 50, entities: tuple[str, ...] = ("sale", "inventory_movement", "product", "user", "category", "order", "restaurant_op", "contabilidad_op")):
        """Devuelve items pendientes de enviar al servidor (max `limit`).

        Filtra por entidades soportadas por el server. Otras entidades quedan
        encoladas pero no se intentan empujar.
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

        Match por SKU. Respeta active (si server.active=False, marca local
        como inactivo). NO encola outbox (no es un cambio local). Tambien
        guarda remote_id, barcode, genero_id e image_path.
        """
        sku = (remote_product.get("sku") or "").strip()
        if not sku:
            raise ValueError("Producto remoto sin sku")

        name = (remote_product.get("name") or "").strip() or sku
        category = (remote_product.get("category") or "General").strip() or "General"
        barcode = (remote_product.get("barcode") or "").strip() or None
        image = (remote_product.get("image") or "").strip() or None
        remote_id = remote_product.get("remote_id")
        genero_id = remote_product.get("genero_id")
        active = 1 if remote_product.get("active", True) else 0
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
                        barcode = ?, image_path = ?, genero_id = ?,
                        remote_id = ?, active = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (name, category, price, stock, barcode, image, genero_id,
                     remote_id, active, existing["id"]),
                )
                return {"action": "updated", "local_id": int(existing["id"])}
            cur = conn.execute(
                """
                INSERT INTO products (sku, name, category, price, stock, min_stock,
                                      barcode, image_path, genero_id, remote_id, active)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (sku, name, category, price, stock, barcode, image, genero_id, remote_id, active),
            )
            return {"action": "created", "local_id": int(cur.lastrowid)}

    def upsert_genero_from_remote(self, remote):
        """UPSERT por remote_id. Devuelve {action, local_id}."""
        rid = remote.get("remote_id")
        nombre = (remote.get("nombre") or "").strip()
        if not rid or not nombre:
            raise ValueError("genero remoto sin remote_id o nombre")
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM generos WHERE remote_id = ?", (int(rid),)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE generos SET nombre = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (nombre, existing["id"]),
                )
                return {"action": "updated", "local_id": int(existing["id"])}
            cur = conn.execute(
                "INSERT INTO generos (remote_id, nombre) VALUES (?, ?)",
                (int(rid), nombre),
            )
            return {"action": "created", "local_id": int(cur.lastrowid)}

    def list_generos(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, remote_id, nombre FROM generos ORDER BY nombre"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_generos_with_product_count(self):
        """Como list_generos pero con count(productos) por categoría.

        Local: products.category es TEXT (no FK), así que cuenta por nombre.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.id, g.remote_id, g.nombre,
                       (SELECT COUNT(*) FROM products p WHERE LOWER(p.category) = LOWER(g.nombre)) AS productos_count
                FROM generos g
                ORDER BY g.nombre
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Outbox: categorías (PUSH al VPS vía /sync/outbox entity='category') ───

    def enqueue_category_create(self, nombre: str) -> int:
        """Inserta categoría local y encola create. Devuelve el local_id."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("nombre obligatorio")
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM generos WHERE LOWER(nombre) = LOWER(?)", (nombre,)
            ).fetchone()
            if existing:
                raise ValueError("Ya existe una categoría con ese nombre")
            cur = conn.execute(
                "INSERT INTO generos (nombre) VALUES (?)", (nombre,)
            )
            local_id = int(cur.lastrowid)
            self._queue_outbox(conn, "category", str(local_id), "create",
                               {"nombre": nombre, "local_id": local_id})
            return local_id

    def enqueue_category_update(self, local_id: int, nombre: str):
        """Renombra categoría local y encola update con remote_id."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("nombre obligatorio")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT remote_id, nombre FROM generos WHERE id = ?", (int(local_id),)
            ).fetchone()
            if not row:
                raise ValueError("categoría no existe")
            collision = conn.execute(
                "SELECT id FROM generos WHERE LOWER(nombre) = LOWER(?) AND id != ?",
                (nombre, int(local_id)),
            ).fetchone()
            if collision:
                raise ValueError("Ya existe otra categoría con ese nombre")
            conn.execute(
                "UPDATE generos SET nombre = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (nombre, int(local_id)),
            )
            payload = {"nombre": nombre, "local_id": int(local_id)}
            if row["remote_id"]:
                payload["remote_id"] = int(row["remote_id"])
            self._queue_outbox(conn, "category", str(local_id), "update", payload)

    def enqueue_category_delete(self, local_id: int):
        """Elimina categoría local (si no tiene productos) y encola delete.

        Lanza ValueError si tiene productos asociados (consistente con el
        comportamiento del servidor). Match por nombre (products.category es TEXT).
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT remote_id, nombre FROM generos WHERE id = ?", (int(local_id),)
            ).fetchone()
            if not row:
                raise ValueError("categoría no existe")
            cnt = conn.execute(
                "SELECT COUNT(*) FROM products WHERE LOWER(category) = LOWER(?)",
                (row["nombre"],),
            ).fetchone()[0]
            if cnt:
                raise ValueError(f"Tiene {cnt} producto(s) asociados; reasígnalos primero")
            conn.execute("DELETE FROM generos WHERE id = ?", (int(local_id),))
            payload = {"local_id": int(local_id)}
            if row["remote_id"]:
                payload["remote_id"] = int(row["remote_id"])
            self._queue_outbox(conn, "category", str(local_id), "delete", payload)

    # ─── Outbox: pedidos web (PUSH al VPS vía /sync/outbox entity='order') ───

    def enqueue_order_status_update(self, remote_id: int, estado_pago: str | None,
                                     estado_envio: str | None,
                                     updated_at_iso: str | None = None) -> int:
        """Encola un cambio de estado de pedido web. También actualiza
        remote_sales_cache de forma optimista para reflejar el cambio en UI.

        Retorna el id de la fila insertada en outbox.
        """
        if not remote_id:
            raise ValueError("remote_id obligatorio")
        payload = {"remote_id": int(remote_id)}
        if estado_pago is not None and estado_pago != "":
            payload["estado_pago"] = str(estado_pago)
        if estado_envio is not None and estado_envio != "":
            payload["estado_envio"] = str(estado_envio)
        if not (set(payload.keys()) - {"remote_id"}):
            raise ValueError("nada que actualizar")
        if updated_at_iso:
            payload["updated_at"] = updated_at_iso

        with self.connect() as conn:
            self._queue_outbox(conn, "order", str(int(remote_id)), "update", payload)
            outbox_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            # UI optimista: refleja el cambio en cache local
            sets, params = [], []
            if "estado_pago" in payload:
                sets.append("status_payment = ?"); params.append(payload["estado_pago"])
            if "estado_envio" in payload:
                sets.append("status_shipping = ?"); params.append(payload["estado_envio"])
            if sets:
                params.append(int(remote_id))
                conn.execute(
                    f"UPDATE remote_sales_cache SET {', '.join(sets)} WHERE remote_id = ?",
                    tuple(params),
                )
            return outbox_id

    # ════════════════════════════════════════════════════════════════
    # Modulo Restaurante (espejo local + outbox)
    # ════════════════════════════════════════════════════════════════
    # Modelo de consistencia offline-first:
    #   - synced=1  -> fila confiada al servidor (la sobreescribe el pull).
    #   - synced=0  -> cambio local pendiente; el pull la preserva.
    #   - replace_restaurant_snapshot() borra las filas synced=1 y reconstruye
    #     desde el servidor; conserva las synced=0 (creadas/cambiadas offline).
    #   - mark_restaurant_pushed() (tras push exitoso) pone synced=1, para que
    #     el siguiente pull adopte la verdad del servidor sin duplicar.

    RT_TABLE_STATES = ("disponible", "ocupada", "reservada", "cuenta_solicitada")
    RT_CONSUMPTION_STATES = ("pendiente", "preparando", "servido")
    RT_PAYMENT_METHODS = ("EFECTIVO", "TARJETA", "TRANSFERENCIA", "MIXTO")

    def replace_restaurant_snapshot(self, data: dict):
        """Reconstruye el espejo local desde el snapshot del servidor.

        Conserva las filas con cambios locales pendientes (synced=0).
        """
        tables = data.get("tables") or []
        open_orders = data.get("open_orders") or []
        consumptions = data.get("consumptions") or []
        with self.connect() as conn:
            # ── Mesas ──────────────────────────────────────────────
            server_ids = set()
            for t in tables:
                tid = int(t["id"])
                server_ids.add(tid)
                row = conn.execute("SELECT synced FROM rt_tables WHERE remote_id = ?", (tid,)).fetchone()
                if row and row["synced"] == 0:
                    # preserva estado local pendiente; actualiza solo el layout
                    conn.execute(
                        """UPDATE rt_tables SET codigo=?, nombre=?, area=?, capacidad=?, forma=?,
                               pos_x=?, pos_y=?, ancho=?, alto=?, rotacion=?, updated_at=?
                           WHERE remote_id=?""",
                        (t.get("codigo"), t.get("nombre"), t.get("area") or "Salon principal",
                         int(t.get("capacidad") or 0), t.get("forma") or "square",
                         float(t.get("pos_x") or 0), float(t.get("pos_y") or 0),
                         float(t.get("ancho") or 16), float(t.get("alto") or 16),
                         int(t.get("rotacion") or 0), t.get("updated_at"), tid),
                    )
                else:
                    conn.execute(
                        """INSERT INTO rt_tables
                               (remote_id, codigo, nombre, area, capacidad, forma, estado,
                                pos_x, pos_y, ancho, alto, rotacion, synced, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                           ON CONFLICT(remote_id) DO UPDATE SET
                               codigo=excluded.codigo, nombre=excluded.nombre, area=excluded.area,
                               capacidad=excluded.capacidad, forma=excluded.forma, estado=excluded.estado,
                               pos_x=excluded.pos_x, pos_y=excluded.pos_y, ancho=excluded.ancho,
                               alto=excluded.alto, rotacion=excluded.rotacion, synced=1,
                               updated_at=excluded.updated_at""",
                        (tid, t.get("codigo"), t.get("nombre"), t.get("area") or "Salon principal",
                         int(t.get("capacidad") or 0), t.get("forma") or "square", t.get("estado") or "disponible",
                         float(t.get("pos_x") or 0), float(t.get("pos_y") or 0),
                         float(t.get("ancho") or 16), float(t.get("alto") or 16),
                         int(t.get("rotacion") or 0), t.get("updated_at")),
                    )
            # mesas borradas en el server (solo las ya sincronizadas)
            if server_ids:
                placeholders = ",".join("?" * len(server_ids))
                conn.execute(
                    f"DELETE FROM rt_tables WHERE synced=1 AND remote_id NOT IN ({placeholders})",
                    tuple(server_ids),
                )

            # ── Ordenes abiertas ──────────────────────────────────
            # Borra las confiadas; conserva las pendientes (synced=0).
            conn.execute("DELETE FROM rt_consumptions WHERE synced=1")
            conn.execute("DELETE FROM rt_orders WHERE synced=1")
            remote_to_local_order = {}
            for o in open_orders:
                conn.execute(
                    """INSERT INTO rt_orders
                           (remote_id, table_id, estado, cliente_nombre, comensales, notas,
                            total_acumulado, opened_at, last_activity_at, synced, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET
                           table_id=excluded.table_id, estado=excluded.estado,
                           cliente_nombre=excluded.cliente_nombre, comensales=excluded.comensales,
                           notas=excluded.notas, total_acumulado=excluded.total_acumulado,
                           opened_at=excluded.opened_at, last_activity_at=excluded.last_activity_at,
                           synced=1, updated_at=excluded.updated_at""",
                    (int(o["id"]), int(o["table_id"]), o.get("estado") or "abierta",
                     o.get("cliente_nombre"), int(o.get("comensales") or 1), o.get("notas"),
                     float(o.get("total_acumulado") or 0), o.get("opened_at"),
                     o.get("last_activity_at"), o.get("updated_at")),
                )
                lid = conn.execute("SELECT id FROM rt_orders WHERE remote_id = ?", (int(o["id"]),)).fetchone()["id"]
                remote_to_local_order[int(o["id"])] = lid

            for c in consumptions:
                local_order = remote_to_local_order.get(int(c["order_id"]))
                if local_order is None:
                    continue
                conn.execute(
                    """INSERT INTO rt_consumptions
                           (remote_id, order_local_id, table_id, producto_id, descripcion, cantidad,
                            precio_unitario, subtotal, estado, notas, ordered_at, synced, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET
                           order_local_id=excluded.order_local_id, table_id=excluded.table_id,
                           producto_id=excluded.producto_id, descripcion=excluded.descripcion,
                           cantidad=excluded.cantidad, precio_unitario=excluded.precio_unitario,
                           subtotal=excluded.subtotal, estado=excluded.estado, notas=excluded.notas,
                           ordered_at=excluded.ordered_at, synced=1, updated_at=excluded.updated_at""",
                    (int(c["id"]), local_order, int(c["table_id"]),
                     c.get("producto_id"), c.get("descripcion") or "", int(c.get("cantidad") or 1),
                     float(c.get("precio_unitario") or 0), float(c.get("subtotal") or 0),
                     c.get("estado") or "pendiente", c.get("notas"), c.get("ordered_at"), c.get("updated_at")),
                )

    def mark_restaurant_pushed(self):
        """Tras un push exitoso: confia todas las filas locales (synced=1) para
        que el siguiente pull adopte la verdad del servidor sin duplicar."""
        with self.connect() as conn:
            conn.execute("UPDATE rt_tables SET synced=1 WHERE synced=0")
            conn.execute("UPDATE rt_orders SET synced=1 WHERE synced=0")
            conn.execute("UPDATE rt_consumptions SET synced=1 WHERE synced=0")

    # ─── Lecturas para la UI ───────────────────────────────────────

    def rt_list_areas(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT DISTINCT area FROM rt_tables ORDER BY area").fetchall()
        return [r["area"] for r in rows]

    def rt_list_tables(self, area: str | None = None):
        """Mesas con metricas de su orden abierta (para la grilla del salon)."""
        sql = """
            SELECT t.remote_id AS id, t.codigo, t.nombre, t.area, t.capacidad,
                   t.forma, t.estado, t.synced,
                   o.id AS order_local_id, o.remote_id AS order_remote_id,
                   o.cliente_nombre, o.comensales, o.total_acumulado, o.opened_at,
                   (SELECT COUNT(*) FROM rt_consumptions c
                      WHERE c.order_local_id = o.id AND c.estado = 'pendiente') AS pendientes,
                   (SELECT COUNT(*) FROM rt_consumptions c
                      WHERE c.order_local_id = o.id AND c.estado = 'servido') AS servidos,
                   (SELECT COUNT(*) FROM rt_consumptions c
                      WHERE c.order_local_id = o.id) AS items
            FROM rt_tables t
            LEFT JOIN rt_orders o ON o.table_id = t.remote_id AND o.estado = 'abierta'
        """
        params = []
        if area:
            sql += " WHERE t.area = ?"
            params.append(area)
        sql += " ORDER BY t.nombre"
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def rt_table_detail(self, table_id: int):
        """Detalle de una mesa: su orden abierta + consumos."""
        with self.connect() as conn:
            table = conn.execute(
                "SELECT remote_id AS id, codigo, nombre, area, capacidad, estado FROM rt_tables WHERE remote_id = ?",
                (int(table_id),),
            ).fetchone()
            if not table:
                return None
            order = conn.execute(
                """SELECT id AS order_local_id, remote_id AS order_remote_id, estado,
                          cliente_nombre, comensales, notas, total_acumulado, opened_at, synced
                   FROM rt_orders WHERE table_id = ? AND estado = 'abierta'
                   ORDER BY id DESC LIMIT 1""",
                (int(table_id),),
            ).fetchone()
            consumptions = []
            if order:
                consumptions = conn.execute(
                    """SELECT id AS local_id, remote_id, producto_id, descripcion, cantidad,
                              precio_unitario, subtotal, estado, notas, ordered_at, synced
                       FROM rt_consumptions WHERE order_local_id = ?
                       ORDER BY id""",
                    (order["order_local_id"],),
                ).fetchall()
        return {
            "table": dict(table),
            "order": dict(order) if order else None,
            "consumptions": [dict(c) for c in consumptions],
        }

    # ─── Operaciones (optimista local + encola outbox) ─────────────

    def _rt_user_fields(self, user) -> dict:
        """Extrae user_remote_id / user_email del objeto User (o dict) para el payload."""
        if user is None:
            return {}
        remote_id = getattr(user, "remote_id", None) if not isinstance(user, dict) else user.get("remote_id")
        email = getattr(user, "email", None) if not isinstance(user, dict) else user.get("email")
        out = {}
        if remote_id:
            out["user_remote_id"] = int(remote_id)
        if email:
            out["user_email"] = email
        return out

    def _rt_local_open_order(self, conn, table_id: int):
        return conn.execute(
            "SELECT id, remote_id FROM rt_orders WHERE table_id = ? AND estado = 'abierta' ORDER BY id DESC LIMIT 1",
            (int(table_id),),
        ).fetchone()

    def rt_open_table(self, table_id: int, user=None, cliente: str = "", comensales: int = 1):
        """Abre (o asegura) la cuenta de una mesa."""
        table_id = int(table_id)
        with self.connect() as conn:
            existing = self._rt_local_open_order(conn, table_id)
            if existing:
                return existing["id"]
            conn.execute(
                """INSERT INTO rt_orders (table_id, estado, cliente_nombre, comensales, synced,
                                          opened_at, last_activity_at)
                   VALUES (?, 'abierta', ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (table_id, (cliente or "").strip() or None, max(1, int(comensales or 1))),
            )
            order_local_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute("UPDATE rt_tables SET estado='ocupada', synced=0 WHERE remote_id=?", (table_id,))
            payload = {"op": "open_table", "table_id": table_id,
                       "cliente_nombre": (cliente or "").strip(), "comensales": max(1, int(comensales or 1))}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", _uuid.uuid4().hex, "create", payload)
            return order_local_id

    def rt_add_consumption(self, table_id: int, user=None, producto_id=None, descripcion: str = "",
                           precio_unitario: float = 0, cantidad: int = 1, notas: str = ""):
        """Agrega un consumo a la mesa (abre la cuenta si hace falta)."""
        table_id = int(table_id)
        cantidad = max(1, int(cantidad or 1))
        descripcion = (descripcion or "").strip()
        notas = (notas or "").strip()
        if producto_id:
            with self.connect() as conn:
                prod = conn.execute("SELECT name, price FROM products WHERE remote_id = ?", (int(producto_id),)).fetchone()
            if prod:
                descripcion = prod["name"]
                precio_unitario = float(prod["price"] or 0)
        precio_unitario = float(precio_unitario or 0)
        if not descripcion:
            raise ValueError("Indica un producto o una descripcion.")
        if precio_unitario <= 0:
            raise ValueError("El precio debe ser mayor a cero.")
        subtotal = round(precio_unitario * cantidad, 2)
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            existing = self._rt_local_open_order(conn, table_id)
            if existing:
                order_local_id = existing["id"]
                conn.execute("UPDATE rt_orders SET synced=0, last_activity_at=CURRENT_TIMESTAMP WHERE id=?", (order_local_id,))
            else:
                conn.execute(
                    """INSERT INTO rt_orders (table_id, estado, comensales, synced, opened_at, last_activity_at)
                       VALUES (?, 'abierta', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (table_id,),
                )
                order_local_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """INSERT INTO rt_consumptions
                       (order_local_id, table_id, producto_id, descripcion, cantidad,
                        precio_unitario, subtotal, estado, notas, synced, ordered_at)
                   VALUES (?,?,?,?,?,?,?, 'pendiente', ?, 0, CURRENT_TIMESTAMP)""",
                (order_local_id, table_id, int(producto_id) if producto_id else None,
                 descripcion, cantidad, precio_unitario, subtotal, notas or None),
            )
            # recalcula total local
            conn.execute(
                """UPDATE rt_orders SET total_acumulado =
                       (SELECT COALESCE(SUM(subtotal),0) FROM rt_consumptions WHERE order_local_id=?)
                   WHERE id=?""",
                (order_local_id, order_local_id),
            )
            conn.execute("UPDATE rt_tables SET estado='ocupada', synced=0 WHERE remote_id=?", (table_id,))
            payload = {"op": "add_consumption", "table_id": table_id, "client_op_uuid": op_uuid,
                       "cantidad": cantidad, "notas": notas}
            if producto_id:
                payload["producto_id"] = int(producto_id)
            else:
                payload["descripcion"] = descripcion
                payload["precio_unitario"] = precio_unitario
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", op_uuid, "create", payload)

    def rt_set_consumption_state(self, consumption_local_id: int, new_state: str, user=None):
        if new_state not in self.RT_CONSUMPTION_STATES:
            raise ValueError("Estado de consumo invalido.")
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM rt_consumptions WHERE id=?", (int(consumption_local_id),)).fetchone()
            if not row:
                raise ValueError("Consumo no encontrado.")
            conn.execute("UPDATE rt_consumptions SET estado=?, synced=0 WHERE id=?", (new_state, int(consumption_local_id)))
            if row["remote_id"] is None:
                # creado offline y aun sin id remoto: el cambio viaja con el add (pendiente)
                return
            payload = {"op": "set_consumption_state", "consumption_id": int(row["remote_id"]), "estado": new_state}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", _uuid.uuid4().hex, "create", payload)

    def rt_set_table_state(self, table_id: int, new_state: str, user=None):
        if new_state not in self.RT_TABLE_STATES:
            raise ValueError("Estado de mesa invalido.")
        with self.connect() as conn:
            conn.execute("UPDATE rt_tables SET estado=?, synced=0 WHERE remote_id=?", (new_state, int(table_id)))
            payload = {"op": "set_table_state", "table_id": int(table_id), "estado": new_state}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", _uuid.uuid4().hex, "create", payload)

    def rt_close_table(self, table_id: int, payment_method: str = "EFECTIVO", user=None):
        payment_method = (payment_method or "EFECTIVO").upper()
        if payment_method not in self.RT_PAYMENT_METHODS:
            raise ValueError("Metodo de pago invalido.")
        table_id = int(table_id)
        with self.connect() as conn:
            order = self._rt_local_open_order(conn, table_id)
            if not order:
                raise ValueError("La mesa no tiene una cuenta abierta.")
            items = conn.execute("SELECT COUNT(*) FROM rt_consumptions WHERE order_local_id=?", (order["id"],)).fetchone()[0]
            if int(items or 0) <= 0:
                raise ValueError("No puedes cerrar una cuenta sin consumos.")
            conn.execute("UPDATE rt_orders SET estado='cerrada', synced=0 WHERE id=?", (order["id"],))
            conn.execute("UPDATE rt_tables SET estado='disponible', synced=0 WHERE remote_id=?", (table_id,))
            payload = {"op": "close_table", "table_id": table_id, "payment_method": payment_method}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", _uuid.uuid4().hex, "create", payload)

    def rt_cancel_order(self, table_id: int, reason: str = "", user=None):
        table_id = int(table_id)
        with self.connect() as conn:
            order = self._rt_local_open_order(conn, table_id)
            if not order:
                raise ValueError("La mesa no tiene una cuenta abierta.")
            conn.execute("UPDATE rt_orders SET estado='cancelada', synced=0 WHERE id=?", (order["id"],))
            conn.execute("UPDATE rt_tables SET estado='disponible', synced=0 WHERE remote_id=?", (table_id,))
            payload = {"op": "cancel_order", "table_id": table_id, "cancel_reason": (reason or "").strip()}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "restaurant_op", _uuid.uuid4().hex, "create", payload)

    def rt_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='restaurant_op'"
            ).fetchone()[0])

    # ════════════════════════════════════════════════════════════════
    # Modulo Contabilidad (espejo local + outbox)  — patrón rt_*
    # ════════════════════════════════════════════════════════════════
    CB_CATEGORIAS = [
        "venta_pos", "venta_restaurante", "venta_online", "servicio", "otro_ingreso",
        "arriendo", "nomina", "servicios_publicos", "proveedores", "impuestos",
        "mantenimiento", "transporte", "otro_egreso",
    ]

    @staticmethod
    def cb_calcular_impuestos(bruto, rtefte_pct=0, iva_pct=0, reteiva_pct=0, rteica_pct=0):
        """Réplica de routes.contabilidad._calcular_impuestos (preview local)."""
        def pct(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0
        bruto = float(bruto or 0)
        rtefte = round(bruto * pct(rtefte_pct) / 100, 2)
        iva = round(bruto * pct(iva_pct) / 100, 2)
        reteiva = round(iva * pct(reteiva_pct) / 100, 2)
        rteica = round(bruto * pct(rteica_pct) / 100, 2)
        total_ret = rtefte + reteiva + rteica
        return {
            "retefuente_monto": rtefte, "iva_monto": iva, "reteiva_monto": reteiva,
            "reteica_monto": rteica, "total_retenciones": total_ret,
            "monto_neto": round(bruto - total_ret, 2),
        }

    def replace_contabilidad_snapshot(self, data: dict):
        """Reconstruye el espejo de contabilidad; conserva filas synced=0."""
        with self.connect() as conn:
            conn.execute("DELETE FROM cb_movimientos WHERE synced=1")
            for m in (data.get("movimientos") or []):
                conn.execute(
                    """INSERT INTO cb_movimientos
                        (remote_id, tipo, categoria, descripcion, monto_bruto, monto,
                         retefuente_pct, retefuente_monto, iva_pct, iva_monto,
                         reteiva_pct, reteiva_monto, reteica_pct, reteica_monto,
                         total_retenciones, fecha, referencia_tipo, referencia_id,
                         notas, usuario_nombre, auto_generado, synced, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET
                         tipo=excluded.tipo, categoria=excluded.categoria, descripcion=excluded.descripcion,
                         monto_bruto=excluded.monto_bruto, monto=excluded.monto,
                         retefuente_pct=excluded.retefuente_pct, retefuente_monto=excluded.retefuente_monto,
                         iva_pct=excluded.iva_pct, iva_monto=excluded.iva_monto,
                         reteiva_pct=excluded.reteiva_pct, reteiva_monto=excluded.reteiva_monto,
                         reteica_pct=excluded.reteica_pct, reteica_monto=excluded.reteica_monto,
                         total_retenciones=excluded.total_retenciones, fecha=excluded.fecha,
                         referencia_tipo=excluded.referencia_tipo, referencia_id=excluded.referencia_id,
                         notas=excluded.notas, usuario_nombre=excluded.usuario_nombre,
                         auto_generado=excluded.auto_generado, synced=1, created_at=excluded.created_at""",
                    (m.get("id"), m.get("tipo"), m.get("categoria"), m.get("descripcion"),
                     float(m.get("monto_bruto") or 0), float(m.get("monto") or 0),
                     float(m.get("retefuente_pct") or 0), float(m.get("retefuente_monto") or 0),
                     float(m.get("iva_pct") or 0), float(m.get("iva_monto") or 0),
                     float(m.get("reteiva_pct") or 0), float(m.get("reteiva_monto") or 0),
                     float(m.get("reteica_pct") or 0), float(m.get("reteica_monto") or 0),
                     float(m.get("total_retenciones") or 0), m.get("fecha"),
                     m.get("referencia_tipo"), m.get("referencia_id"), m.get("notas"),
                     m.get("usuario_nombre"), 1 if m.get("auto_generado") else 0, m.get("created_at")),
                )
            conn.execute("DELETE FROM cb_plantillas WHERE synced=1")
            for p in (data.get("plantillas") or []):
                conn.execute(
                    """INSERT INTO cb_plantillas (remote_id, tipo, categoria, descripcion, monto_bruto, notas, activo, synced, created_at)
                       VALUES (?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET tipo=excluded.tipo, categoria=excluded.categoria,
                         descripcion=excluded.descripcion, monto_bruto=excluded.monto_bruto, notas=excluded.notas,
                         activo=excluded.activo, synced=1""",
                    (p.get("id"), p.get("tipo"), p.get("categoria"), p.get("descripcion"),
                     float(p.get("monto_bruto") or 0), p.get("notas"), 1 if p.get("activo") else 0, p.get("created_at")),
                )
            conn.execute("DELETE FROM cb_cierres WHERE synced=1")
            for c in (data.get("cierres") or []):
                conn.execute(
                    """INSERT INTO cb_cierres (remote_id, nombre, fecha_inicio, fecha_fin, total_ingresos,
                         total_egresos, total_retenciones, saldo, notas, usuario_nombre, synced, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET nombre=excluded.nombre, fecha_inicio=excluded.fecha_inicio,
                         fecha_fin=excluded.fecha_fin, total_ingresos=excluded.total_ingresos,
                         total_egresos=excluded.total_egresos, total_retenciones=excluded.total_retenciones,
                         saldo=excluded.saldo, notas=excluded.notas, usuario_nombre=excluded.usuario_nombre, synced=1""",
                    (c.get("id"), c.get("nombre"), c.get("fecha_inicio"), c.get("fecha_fin"),
                     float(c.get("total_ingresos") or 0), float(c.get("total_egresos") or 0),
                     float(c.get("total_retenciones") or 0), float(c.get("saldo") or 0),
                     c.get("notas"), c.get("usuario_nombre"), c.get("created_at")),
                )

    def mark_contabilidad_pushed(self):
        with self.connect() as conn:
            conn.execute("UPDATE cb_movimientos SET synced=1 WHERE synced=0")
            conn.execute("UPDATE cb_plantillas SET synced=1 WHERE synced=0")
            conn.execute("UPDATE cb_cierres SET synced=1 WHERE synced=0")

    def cb_categorias(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT DISTINCT categoria FROM cb_movimientos WHERE categoria IS NOT NULL ORDER BY categoria").fetchall()
        usadas = [r["categoria"] for r in rows if r["categoria"]]
        # une las usadas con la lista base, sin duplicar
        out = list(dict.fromkeys(self.CB_CATEGORIAS + usadas))
        return out

    @staticmethod
    def _cb_periodo_rango(periodo: str):
        hoy = date.today()
        if periodo == "semana":
            ini = hoy - timedelta(days=hoy.weekday()); fin = hoy
        elif periodo == "mes_ant":
            primero = hoy.replace(day=1); fin = primero - timedelta(days=1); ini = fin.replace(day=1)
        elif periodo == "anio":
            ini = hoy.replace(month=1, day=1); fin = hoy
        else:  # mes
            ini = hoy.replace(day=1); fin = hoy
        return ini.isoformat(), fin.isoformat()

    def cb_dashboard(self, periodo: str = "mes"):
        ini, fin = self._cb_periodo_rango(periodo)
        out = {
            "ingresos": 0.0, "ingresos_bruto": 0.0, "egresos": 0.0, "saldo": 0.0,
            "retenciones": 0.0, "retefuente": 0.0, "iva": 0.0, "reteiva": 0.0, "reteica": 0.0,
            "num_ingresos": 0, "num_egresos": 0, "por_categoria": [], "ultimos": [], "rango": (ini, fin),
        }
        with self.connect() as conn:
            for r in conn.execute(
                """SELECT tipo, COALESCE(SUM(monto),0) neto, COALESCE(SUM(monto_bruto),0) bruto,
                          COALESCE(SUM(total_retenciones),0) ret, COALESCE(SUM(retefuente_monto),0) rf,
                          COALESCE(SUM(iva_monto),0) iva, COALESCE(SUM(reteiva_monto),0) ri,
                          COALESCE(SUM(reteica_monto),0) ric, COUNT(*) cnt
                   FROM cb_movimientos WHERE fecha BETWEEN ? AND ? GROUP BY tipo""", (ini, fin)):
                if r["tipo"] == "ingreso":
                    out["ingresos"] = float(r["neto"]); out["ingresos_bruto"] = float(r["bruto"])
                    out["retenciones"] = float(r["ret"]); out["retefuente"] = float(r["rf"])
                    out["iva"] = float(r["iva"]); out["reteiva"] = float(r["ri"]); out["reteica"] = float(r["ric"])
                    out["num_ingresos"] = r["cnt"]
                else:
                    out["egresos"] = float(r["neto"]); out["num_egresos"] = r["cnt"]
            out["saldo"] = out["ingresos"] - out["egresos"]
            out["por_categoria"] = [dict(r) for r in conn.execute(
                """SELECT categoria, tipo, COALESCE(SUM(monto),0) total, COUNT(*) cnt
                   FROM cb_movimientos WHERE fecha BETWEEN ? AND ?
                   GROUP BY categoria, tipo ORDER BY total DESC LIMIT 8""", (ini, fin))]
            out["ultimos"] = [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, tipo, categoria, descripcion, monto, fecha,
                          auto_generado, synced
                   FROM cb_movimientos ORDER BY fecha DESC, id DESC LIMIT 10""")]
        return out

    def cb_list_movimientos(self, tipo=None, categoria=None, fecha_ini=None, fecha_fin=None, limit=500):
        sql = """SELECT id AS local_id, remote_id, tipo, categoria, descripcion, monto_bruto, monto,
                        total_retenciones, fecha, notas, usuario_nombre, auto_generado, synced
                 FROM cb_movimientos WHERE 1=1"""
        params = []
        if tipo in ("ingreso", "egreso"):
            sql += " AND tipo=?"; params.append(tipo)
        if categoria:
            sql += " AND categoria=?"; params.append(categoria)
        if fecha_ini:
            sql += " AND fecha>=?"; params.append(fecha_ini)
        if fecha_fin:
            sql += " AND fecha<=?"; params.append(fecha_fin)
        sql += " ORDER BY fecha DESC, id DESC LIMIT ?"; params.append(int(limit))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params))]

    def cb_list_plantillas(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id AS local_id, remote_id, tipo, categoria, descripcion, monto_bruto, notas, activo, synced FROM cb_plantillas ORDER BY id")]

    def cb_list_cierres(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id AS local_id, remote_id, nombre, fecha_inicio, fecha_fin, total_ingresos, total_egresos, total_retenciones, saldo, notas, usuario_nombre, synced FROM cb_cierres ORDER BY fecha_fin DESC, id DESC")]

    def cb_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='contabilidad_op'").fetchone()[0])

    # ─── Operaciones (optimista local + outbox) ───────────────────

    def cb_crear_movimiento(self, tipo, categoria, descripcion, monto_bruto, fecha=None,
                            notas="", retefuente_pct=0, iva_pct=0, reteiva_pct=0, reteica_pct=0, user=None):
        tipo = (tipo or "").strip()
        if tipo not in ("ingreso", "egreso"):
            raise ValueError("Tipo inválido.")
        descripcion = (descripcion or "").strip()
        if not descripcion:
            raise ValueError("La descripción es obligatoria.")
        bruto = float(monto_bruto or 0)
        if bruto <= 0:
            raise ValueError("El monto debe ser mayor a cero.")
        if tipo == "egreso":
            retefuente_pct = iva_pct = reteiva_pct = reteica_pct = 0
        calc = self.cb_calcular_impuestos(bruto, retefuente_pct, iva_pct, reteiva_pct, reteica_pct)
        neto = calc["monto_neto"] if tipo == "ingreso" else bruto
        fecha = fecha or date.today().isoformat()
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cb_movimientos
                    (tipo, categoria, descripcion, monto_bruto, monto, retefuente_pct, retefuente_monto,
                     iva_pct, iva_monto, reteiva_pct, reteiva_monto, reteica_pct, reteica_monto,
                     total_retenciones, fecha, notas, auto_generado, synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,CURRENT_TIMESTAMP)""",
                (tipo, (categoria or "otro").strip() or "otro", descripcion, bruto, neto,
                 float(retefuente_pct or 0), calc["retefuente_monto"], float(iva_pct or 0), calc["iva_monto"],
                 float(reteiva_pct or 0), calc["reteiva_monto"], float(reteica_pct or 0), calc["reteica_monto"],
                 calc["total_retenciones"], fecha, (notas or "").strip() or None),
            )
            payload = {"op": "create_movimiento", "client_op_uuid": op_uuid, "tipo": tipo,
                       "categoria": (categoria or "otro").strip() or "otro", "descripcion": descripcion,
                       "monto_bruto": bruto, "fecha": fecha, "notas": (notas or "").strip(),
                       "retefuente_pct": float(retefuente_pct or 0), "iva_pct": float(iva_pct or 0),
                       "reteiva_pct": float(reteiva_pct or 0), "reteica_pct": float(reteica_pct or 0)}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "contabilidad_op", op_uuid, "create", payload)

    def cb_eliminar_movimiento(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id, auto_generado FROM cb_movimientos WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Movimiento no encontrado.")
            if row["auto_generado"]:
                raise ValueError("Los movimientos automáticos (ventas POS/restaurante) no se pueden eliminar.")
            conn.execute("DELETE FROM cb_movimientos WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_movimiento", "movimiento_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_crear_plantilla(self, tipo, categoria, descripcion, monto_bruto, notas="", user=None):
        if tipo not in ("ingreso", "egreso"):
            raise ValueError("Tipo inválido.")
        bruto = float(monto_bruto or 0)
        if bruto <= 0:
            raise ValueError("El monto debe ser mayor a cero.")
        descripcion = (descripcion or "").strip()
        if not descripcion:
            raise ValueError("La descripción es obligatoria.")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cb_plantillas (tipo, categoria, descripcion, monto_bruto, notas, activo, synced) VALUES (?,?,?,?,?,1,0)",
                (tipo, (categoria or "otro").strip() or "otro", descripcion, bruto, (notas or "").strip() or None),
            )
            payload = {"op": "create_plantilla", "tipo": tipo, "categoria": (categoria or "otro").strip() or "otro",
                       "descripcion": descripcion, "monto_bruto": bruto, "notas": (notas or "").strip()}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_toggle_plantilla(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id, activo FROM cb_plantillas WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Plantilla no encontrada.")
            conn.execute("UPDATE cb_plantillas SET activo = 1-activo, synced=0 WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "toggle_plantilla", "plantilla_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_eliminar_plantilla(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM cb_plantillas WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Plantilla no encontrada.")
            conn.execute("DELETE FROM cb_plantillas WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_plantilla", "plantilla_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_generar_plantillas(self, user=None):
        """Encola la generación server-side (anti-dup). El resultado baja en el próximo pull."""
        with self.connect() as conn:
            payload = {"op": "generar_plantillas"}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_crear_cierre(self, nombre, fecha_inicio, fecha_fin, notas="", user=None):
        nombre = (nombre or "").strip()
        if not nombre or not fecha_inicio or not fecha_fin:
            raise ValueError("Nombre y rango de fechas son obligatorios.")
        with self.connect() as conn:
            payload = {"op": "create_cierre", "nombre": nombre, "fecha_inicio": fecha_inicio,
                       "fecha_fin": fecha_fin, "notas": (notas or "").strip()}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def cb_eliminar_cierre(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM cb_cierres WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Cierre no encontrado.")
            conn.execute("DELETE FROM cb_cierres WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_cierre", "cierre_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "contabilidad_op", _uuid.uuid4().hex, "create", payload)

    def upsert_user_from_remote(self, remote):
        """Sincroniza perfil de usuario (sin password).

        Match por email (case-insensitive). Si no existe en local, lo crea con
        un password_hash placeholder (must_change_password=1) para que el
        operador haga reset al primer login en este lado.
        """
        email = (remote.get("email") or "").strip().lower()
        if not email:
            raise ValueError("usuario remoto sin email")
        nombre = (remote.get("nombre") or remote.get("name") or email).strip()
        rol = (remote.get("rol_nombre") or remote.get("role") or "Cajero").strip()
        # Mapeo de rol web -> rol desktop
        if rol.lower() in ("admin", "administrador"):
            rol = "Administrador"
        elif rol.lower() in ("cajero", "vendedor"):
            rol = "Cajero"
        else:
            rol = "Cajero"
        estado = (remote.get("estado") or "habilitado").strip().lower()
        active = 0 if estado in ("deshabilitado", "eliminado") else 1
        remote_id = remote.get("remote_id")

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE users SET name = ?, role = ?, active = ?, remote_id = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (nombre, rol, active, remote_id, existing["id"]),
                )
                return {"action": "updated", "local_id": int(existing["id"])}
            placeholder = "PLACEHOLDER_RESET_REQUIRED_" + secrets.token_hex(8)
            cur = conn.execute(
                """
                INSERT INTO users (email, name, role, password_hash, active,
                                   must_change_password, remote_id)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (email, nombre, rol, placeholder, active, remote_id),
            )
            return {"action": "created", "local_id": int(cur.lastrowid)}

    def cache_remote_sale(self, remote):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_sales_cache
                  (remote_id, reference, customer_name, customer_email,
                   status_payment, status_shipping, total, payment_method,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(remote_id) DO UPDATE SET
                   reference = excluded.reference,
                   customer_name = excluded.customer_name,
                   customer_email = excluded.customer_email,
                   status_payment = excluded.status_payment,
                   status_shipping = excluded.status_shipping,
                   total = excluded.total,
                   payment_method = excluded.payment_method,
                   updated_at = excluded.updated_at,
                   cached_at = CURRENT_TIMESTAMP
                """,
                (
                    int(remote["remote_id"]),
                    remote.get("reference"),
                    remote.get("customer_name"),
                    remote.get("customer_email"),
                    remote.get("status_payment"),
                    remote.get("status_shipping"),
                    float(remote.get("total") or 0),
                    remote.get("payment_method"),
                    remote.get("created_at"),
                    remote.get("updated_at"),
                ),
            )

    def cache_remote_inventory(self, remote):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_inventory_cache
                  (remote_id, sku, product_name, tipo, quantity_delta,
                   stock_anterior, stock_nuevo, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(remote_id) DO UPDATE SET
                   sku = excluded.sku,
                   product_name = excluded.product_name,
                   tipo = excluded.tipo,
                   quantity_delta = excluded.quantity_delta,
                   stock_anterior = excluded.stock_anterior,
                   stock_nuevo = excluded.stock_nuevo,
                   reason = excluded.reason,
                   created_at = excluded.created_at,
                   cached_at = CURRENT_TIMESTAMP
                """,
                (
                    int(remote["remote_id"]),
                    remote.get("sku"),
                    remote.get("product_name"),
                    remote.get("tipo"),
                    int(remote.get("quantity_delta") or 0),
                    remote.get("stock_anterior"),
                    remote.get("stock_nuevo"),
                    remote.get("reason"),
                    remote.get("created_at"),
                ),
            )

    def list_remote_sales(self, limit=200):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_sales_cache ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_remote_inventory(self, limit=200):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_inventory_cache ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

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

                CREATE TABLE IF NOT EXISTS generos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    nombre TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS remote_sales_cache (
                    remote_id INTEGER PRIMARY KEY,
                    reference TEXT,
                    customer_name TEXT,
                    customer_email TEXT,
                    status_payment TEXT,
                    status_shipping TEXT,
                    total REAL,
                    payment_method TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS remote_inventory_cache (
                    remote_id INTEGER PRIMARY KEY,
                    sku TEXT,
                    product_name TEXT,
                    tipo TEXT,
                    quantity_delta INTEGER,
                    stock_anterior INTEGER,
                    stock_nuevo INTEGER,
                    reason TEXT,
                    created_at TEXT,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Migraciones idempotentes (ALTER TABLE no es opcional en SQLite si la columna ya existe).
            self._ensure_column(conn, "products", "barcode", "TEXT")
            self._ensure_column(conn, "products", "image_path", "TEXT")
            self._ensure_column(conn, "products", "genero_id", "INTEGER")
            self._ensure_column(conn, "products", "remote_id", "INTEGER")
            self._ensure_column(conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "users", "remote_id", "INTEGER")
            self._ensure_column(conn, "users", "updated_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_remote_id ON products(remote_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_remote_id ON users(remote_id)")

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

            # ── Modulo Restaurante: espejo local de las tablas de produccion ──
            # remote_id es el id del servidor (None mientras no se haya sincronizado).
            # synced=0 marca filas con cambios locales aun no confirmados por el VPS.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rt_tables (
                    remote_id INTEGER PRIMARY KEY,
                    codigo TEXT,
                    nombre TEXT NOT NULL,
                    area TEXT NOT NULL DEFAULT 'Salon principal',
                    capacidad INTEGER NOT NULL DEFAULT 4,
                    forma TEXT NOT NULL DEFAULT 'square',
                    estado TEXT NOT NULL DEFAULT 'disponible',
                    pos_x REAL NOT NULL DEFAULT 0,
                    pos_y REAL NOT NULL DEFAULT 0,
                    ancho REAL NOT NULL DEFAULT 16,
                    alto REAL NOT NULL DEFAULT 16,
                    rotacion INTEGER NOT NULL DEFAULT 0,
                    synced INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS rt_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    table_id INTEGER NOT NULL,
                    estado TEXT NOT NULL DEFAULT 'abierta',
                    cliente_nombre TEXT,
                    comensales INTEGER NOT NULL DEFAULT 1,
                    notas TEXT,
                    total_acumulado REAL NOT NULL DEFAULT 0,
                    opened_at TEXT,
                    last_activity_at TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS rt_consumptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    order_local_id INTEGER NOT NULL,
                    table_id INTEGER NOT NULL,
                    producto_id INTEGER,
                    descripcion TEXT NOT NULL,
                    cantidad INTEGER NOT NULL DEFAULT 1,
                    precio_unitario REAL NOT NULL DEFAULT 0,
                    subtotal REAL NOT NULL DEFAULT 0,
                    estado TEXT NOT NULL DEFAULT 'pendiente',
                    notas TEXT,
                    ordered_at TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_rt_orders_table ON rt_orders(table_id, estado);
                CREATE INDEX IF NOT EXISTS idx_rt_consumptions_order ON rt_consumptions(order_local_id, estado);
                CREATE INDEX IF NOT EXISTS idx_rt_tables_area ON rt_tables(area);
                """
            )

            # ── Modulo Contabilidad: espejo local de las tablas de produccion ──
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cb_movimientos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    tipo TEXT NOT NULL,
                    categoria TEXT,
                    descripcion TEXT NOT NULL,
                    monto_bruto REAL NOT NULL DEFAULT 0,
                    monto REAL NOT NULL DEFAULT 0,
                    retefuente_pct REAL NOT NULL DEFAULT 0,
                    retefuente_monto REAL NOT NULL DEFAULT 0,
                    iva_pct REAL NOT NULL DEFAULT 0,
                    iva_monto REAL NOT NULL DEFAULT 0,
                    reteiva_pct REAL NOT NULL DEFAULT 0,
                    reteiva_monto REAL NOT NULL DEFAULT 0,
                    reteica_pct REAL NOT NULL DEFAULT 0,
                    reteica_monto REAL NOT NULL DEFAULT 0,
                    total_retenciones REAL NOT NULL DEFAULT 0,
                    fecha TEXT,
                    referencia_tipo TEXT,
                    referencia_id INTEGER,
                    notas TEXT,
                    usuario_nombre TEXT,
                    auto_generado INTEGER NOT NULL DEFAULT 0,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS cb_plantillas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    tipo TEXT NOT NULL,
                    categoria TEXT,
                    descripcion TEXT NOT NULL,
                    monto_bruto REAL NOT NULL DEFAULT 0,
                    notas TEXT,
                    activo INTEGER NOT NULL DEFAULT 1,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS cb_cierres (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    nombre TEXT NOT NULL,
                    fecha_inicio TEXT,
                    fecha_fin TEXT,
                    total_ingresos REAL NOT NULL DEFAULT 0,
                    total_egresos REAL NOT NULL DEFAULT 0,
                    total_retenciones REAL NOT NULL DEFAULT 0,
                    saldo REAL NOT NULL DEFAULT 0,
                    notas TEXT,
                    usuario_nombre TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_cb_mov_fecha ON cb_movimientos(fecha, tipo);
                CREATE INDEX IF NOT EXISTS idx_cb_mov_cat ON cb_movimientos(categoria);
                """
            )

    def _ensure_column(self, conn, table, column, ddl):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def get_tenant_modules(self):
        """Flags de módulos del plan cacheados (config_key web -> bool).
        Devuelve None si nunca se cachearon (el caller hace fail-open)."""
        with self.connect() as conn:
            raw = self._get_meta(conn, "tenant_modules")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            return None

    def set_tenant_modules(self, flags: dict) -> None:
        """Persiste los flags de módulos del plan para gating offline."""
        with self.connect() as conn:
            self._set_meta(conn, "tenant_modules", json.dumps(flags, ensure_ascii=True))

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
