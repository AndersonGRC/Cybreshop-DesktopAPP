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
    if name == "empleado":
        return "Empleado"
    if name == "contador":
        return "Contador"
    if name == "cliente":
        return "Cliente"
    # Rol PERSONALIZADO creado por el dueño (p.ej. "Vendedor"): conservar el
    # nombre tal cual — el manifiesto de permisos del servidor viene keyed por
    # ese nombre y _apply_access_control lo encuentra. Si el manifiesto no lo
    # trae, el shell cae al fallback prudente (DEFAULT_ROLE_MODULES).
    if rol_nombre and rol_nombre.strip():
        return rol_nombre.strip()
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

    # ─── Cotizaciones (espejo + outbox) ───────────────────────────
    @staticmethod
    def q_calc_item(cantidad, precio_unitario, descuento_porc=0, iva_porc=0):
        """Total de una línea (espejo de quotes.py): cant*precio - desc + iva."""
        cant = int(cantidad or 0)
        precio = float(precio_unitario or 0)
        desc = max(0.0, min(100.0, float(descuento_porc or 0)))
        iva = max(0.0, float(iva_porc or 0))
        subtotal = cant * precio
        menos_desc = subtotal - subtotal * (desc / 100)
        return round(menos_desc + (menos_desc * iva / 100 if iva > 0 else 0), 2)

    def replace_quotes_snapshot(self, data: dict):
        """Reconstruye el espejo de cotizaciones; conserva docs locales (synced=0)."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM q_detalle WHERE cotizacion_local_id IN "
                "(SELECT id FROM q_cotizaciones WHERE synced=1)"
            )
            conn.execute("DELETE FROM q_cotizaciones WHERE synced=1")
            remote_to_local = {}
            for c in (data.get("cotizaciones") or []):
                conn.execute(
                    """INSERT INTO q_cotizaciones
                        (remote_id, cliente_nombre, cliente_documento, cliente_direccion,
                         cliente_ciudad, cliente_telefono, cliente_representante, cliente_cargo,
                         cliente_localidad, total, estado, pdf_path, fecha, synced)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET
                         cliente_nombre=excluded.cliente_nombre, cliente_documento=excluded.cliente_documento,
                         cliente_direccion=excluded.cliente_direccion, cliente_ciudad=excluded.cliente_ciudad,
                         cliente_telefono=excluded.cliente_telefono, cliente_representante=excluded.cliente_representante,
                         cliente_cargo=excluded.cliente_cargo, cliente_localidad=excluded.cliente_localidad,
                         total=excluded.total, estado=excluded.estado, pdf_path=excluded.pdf_path,
                         fecha=excluded.fecha, synced=1""",
                    (c.get("id"), c.get("cliente_nombre"), c.get("cliente_documento"),
                     c.get("cliente_direccion"), c.get("cliente_ciudad"), c.get("cliente_telefono"),
                     c.get("cliente_representante"), c.get("cliente_cargo"), c.get("cliente_localidad"),
                     float(c.get("total") or 0), c.get("estado") or "pendiente", c.get("pdf_path"),
                     c.get("fecha")),
                )
                row = conn.execute("SELECT id FROM q_cotizaciones WHERE remote_id=?", (c.get("id"),)).fetchone()
                if row:
                    remote_to_local[int(c["id"])] = int(row["id"])
            for d in (data.get("detalles") or []):
                lid = remote_to_local.get(int(d.get("cotizacion_id") or 0))
                if lid is None:
                    continue
                conn.execute(
                    """INSERT INTO q_detalle
                        (remote_id, cotizacion_local_id, descripcion, cantidad, precio_unitario,
                         subtotal, descuento_porc, iva_porc)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(remote_id) DO UPDATE SET
                         descripcion=excluded.descripcion, cantidad=excluded.cantidad,
                         precio_unitario=excluded.precio_unitario, subtotal=excluded.subtotal,
                         descuento_porc=excluded.descuento_porc, iva_porc=excluded.iva_porc""",
                    (d.get("id"), lid, d.get("descripcion"), int(d.get("cantidad") or 1),
                     float(d.get("precio_unitario") or 0), float(d.get("subtotal") or 0),
                     float(d.get("descuento_porc") or 0), float(d.get("iva_porc") or 0)),
                )

    def mark_quotes_pushed(self):
        with self.connect() as conn:
            conn.execute("UPDATE q_cotizaciones SET synced=1 WHERE synced=0")

    def q_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='quote_op'"
            ).fetchone()[0])

    def q_list_cotizaciones(self, limit=500):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, cliente_nombre, cliente_documento,
                          total, estado, pdf_path, fecha, synced
                   FROM q_cotizaciones ORDER BY id DESC LIMIT ?""", (int(limit),))]

    def q_get_detalle(self, local_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT descripcion, cantidad, precio_unitario, subtotal, descuento_porc, iva_porc
                   FROM q_detalle WHERE cotizacion_local_id=? ORDER BY id""", (int(local_id),))]

    def q_crear_cotizacion(self, cliente: dict, items: list, user=None):
        nombre = (cliente.get("cliente_nombre") or "").strip()
        if not nombre:
            raise ValueError("El nombre del cliente es obligatorio.")
        norm_items, total = [], 0.0
        for it in (items or []):
            desc = (it.get("descripcion") or "").strip()
            if not desc:
                continue
            cant = int(it.get("cantidad") or 1)
            precio = float(it.get("precio_unitario") or 0)
            dpct = float(it.get("descuento_porc") or 0)
            ipct = float(it.get("iva_porc") or 0)
            sub = self.q_calc_item(cant, precio, dpct, ipct)
            total += sub
            norm_items.append({"descripcion": desc, "cantidad": cant, "precio_unitario": precio,
                               "subtotal": sub, "descuento_porc": dpct, "iva_porc": ipct})
        if not norm_items:
            raise ValueError("Agrega al menos un ítem con descripción.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO q_cotizaciones
                    (cliente_nombre, cliente_documento, cliente_direccion, cliente_ciudad,
                     cliente_telefono, cliente_representante, cliente_cargo, cliente_localidad,
                     total, estado, fecha, synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'pendiente', ?, 0, CURRENT_TIMESTAMP)""",
                (nombre, cliente.get("cliente_documento"), cliente.get("cliente_direccion"),
                 cliente.get("cliente_ciudad"), cliente.get("cliente_telefono"),
                 cliente.get("cliente_representante"), cliente.get("cliente_cargo"),
                 cliente.get("cliente_localidad"), total, date.today().isoformat()),
            )
            local_id = int(cur.lastrowid)
            for it in norm_items:
                conn.execute(
                    """INSERT INTO q_detalle (cotizacion_local_id, descripcion, cantidad,
                         precio_unitario, subtotal, descuento_porc, iva_porc)
                       VALUES (?,?,?,?,?,?,?)""",
                    (local_id, it["descripcion"], it["cantidad"], it["precio_unitario"],
                     it["subtotal"], it["descuento_porc"], it["iva_porc"]),
                )
            payload = {"op": "create_cotizacion", "client_op_uuid": op_uuid, "items": norm_items}
            for k in ("cliente_nombre", "cliente_documento", "cliente_direccion", "cliente_ciudad",
                      "cliente_telefono", "cliente_representante", "cliente_cargo", "cliente_localidad"):
                payload[k] = cliente.get(k)
            payload["cliente_nombre"] = nombre
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "quote_op", op_uuid, "create", payload)
        return local_id

    def q_eliminar_cotizacion(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM q_cotizaciones WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Cotización no encontrada.")
            conn.execute("DELETE FROM q_detalle WHERE cotizacion_local_id=?", (int(local_id),))
            conn.execute("DELETE FROM q_cotizaciones WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_cotizacion", "cotizacion_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "quote_op", _uuid.uuid4().hex, "create", payload)

    def q_set_estado(self, local_id, estado, user=None):
        estado = (estado or "").strip()
        if estado not in ("pendiente", "aprobada", "rechazada"):
            raise ValueError("Estado inválido.")
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM q_cotizaciones WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Cotización no encontrada.")
            conn.execute("UPDATE q_cotizaciones SET estado=? WHERE id=?", (estado, int(local_id)))
            if row["remote_id"] is not None:
                payload = {"op": "set_estado_cotizacion", "cotizacion_id": int(row["remote_id"]), "estado": estado}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "quote_op", _uuid.uuid4().hex, "create", payload)

    # ─── Cuentas de cobro (espejo + outbox) ───────────────────────
    def replace_cobros_snapshot(self, data: dict):
        """Reconstruye el espejo de cuentas de cobro; conserva docs locales (synced=0)."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM cc_detalle WHERE cuenta_local_id IN "
                "(SELECT id FROM cc_cuentas WHERE synced=1)"
            )
            conn.execute("DELETE FROM cc_cuentas WHERE synced=1")
            remote_to_local = {}
            for c in (data.get("cuentas") or []):
                conn.execute(
                    """INSERT INTO cc_cuentas
                        (remote_id, consecutivo, fecha, cliente_nombre, cliente_nit,
                         cliente_direccion, cliente_telefono, cliente_ciudad, contractor_nombre,
                         contractor_id, contractor_telefono, contractor_email, texto_pago,
                         total, pdf_path, synced)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET
                         consecutivo=excluded.consecutivo, fecha=excluded.fecha,
                         cliente_nombre=excluded.cliente_nombre, cliente_nit=excluded.cliente_nit,
                         cliente_direccion=excluded.cliente_direccion, cliente_telefono=excluded.cliente_telefono,
                         cliente_ciudad=excluded.cliente_ciudad, contractor_nombre=excluded.contractor_nombre,
                         contractor_id=excluded.contractor_id, contractor_telefono=excluded.contractor_telefono,
                         contractor_email=excluded.contractor_email, texto_pago=excluded.texto_pago,
                         total=excluded.total, pdf_path=excluded.pdf_path, synced=1""",
                    (c.get("id"), c.get("consecutivo"), c.get("fecha"), c.get("cliente_nombre"),
                     c.get("cliente_nit"), c.get("cliente_direccion"), c.get("cliente_telefono"),
                     c.get("cliente_ciudad"), c.get("contractor_nombre"), c.get("contractor_id"),
                     c.get("contractor_telefono"), c.get("contractor_email"), c.get("texto_pago"),
                     float(c.get("total") or 0), c.get("pdf_path")),
                )
                row = conn.execute("SELECT id FROM cc_cuentas WHERE remote_id=?", (c.get("id"),)).fetchone()
                if row:
                    remote_to_local[int(c["id"])] = int(row["id"])
            for d in (data.get("detalles") or []):
                lid = remote_to_local.get(int(d.get("cuenta_id") or 0))
                if lid is None:
                    continue
                conn.execute(
                    """INSERT INTO cc_detalle (remote_id, cuenta_local_id, fecha_labor, descripcion, valor)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(remote_id) DO UPDATE SET fecha_labor=excluded.fecha_labor,
                         descripcion=excluded.descripcion, valor=excluded.valor""",
                    (d.get("id"), lid, d.get("fecha_labor"), d.get("descripcion"),
                     float(d.get("valor") or 0)),
                )

    def mark_cobros_pushed(self):
        with self.connect() as conn:
            conn.execute("UPDATE cc_cuentas SET synced=1 WHERE synced=0")

    def cc_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='cobro_op'"
            ).fetchone()[0])

    def cc_list_cuentas(self, limit=500):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, consecutivo, cliente_nombre, contractor_nombre,
                          total, pdf_path, fecha, synced
                   FROM cc_cuentas ORDER BY id DESC LIMIT ?""", (int(limit),))]

    def cc_get_detalle(self, local_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT fecha_labor, descripcion, valor
                   FROM cc_detalle WHERE cuenta_local_id=? ORDER BY id""", (int(local_id),))]

    def cc_get_cuenta(self, local_id):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id AS local_id, remote_id, consecutivo, fecha, cliente_nombre, cliente_nit,
                          cliente_direccion, cliente_telefono, cliente_ciudad, contractor_nombre,
                          contractor_id, contractor_telefono, contractor_email, texto_pago, total
                   FROM cc_cuentas WHERE id=?""", (int(local_id),)).fetchone()
            return dict(row) if row else None

    def cc_crear_cuenta(self, cliente: dict, items: list, user=None):
        nombre = (cliente.get("cliente_nombre") or "").strip()
        if not nombre:
            raise ValueError("El nombre del cliente es obligatorio.")
        norm_items, total = [], 0.0
        for it in (items or []):
            desc = (it.get("descripcion") or "").strip()
            if not desc:
                continue
            valor = float(it.get("valor") or 0)
            total += valor
            norm_items.append({"fecha_labor": it.get("fecha_labor") or date.today().isoformat(),
                               "descripcion": desc, "valor": valor})
        if not norm_items:
            raise ValueError("Agrega al menos un ítem con descripción.")
        op_uuid = _uuid.uuid4().hex
        # consecutivo provisional local (el servidor asigna el definitivo al sync)
        with self.connect() as conn:
            n = int(conn.execute("SELECT COUNT(*) FROM cc_cuentas").fetchone()[0]) + 1
            consecutivo = f"LOCAL-{n:04d}"
            cur = conn.execute(
                """INSERT INTO cc_cuentas
                    (consecutivo, fecha, cliente_nombre, cliente_nit, cliente_direccion,
                     cliente_telefono, cliente_ciudad, contractor_nombre, contractor_id,
                     contractor_telefono, contractor_email, texto_pago, total, synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,CURRENT_TIMESTAMP)""",
                (consecutivo, date.today().isoformat(), nombre, cliente.get("cliente_nit"),
                 cliente.get("cliente_direccion"), cliente.get("cliente_telefono"),
                 cliente.get("cliente_ciudad"), cliente.get("contractor_nombre"),
                 cliente.get("contractor_id"), cliente.get("contractor_telefono"),
                 cliente.get("contractor_email"), cliente.get("texto_pago"), total),
            )
            local_id = int(cur.lastrowid)
            for it in norm_items:
                conn.execute(
                    "INSERT INTO cc_detalle (cuenta_local_id, fecha_labor, descripcion, valor) VALUES (?,?,?,?)",
                    (local_id, it["fecha_labor"], it["descripcion"], it["valor"]),
                )
            payload = {"op": "create_cuenta", "client_op_uuid": op_uuid, "items": norm_items,
                       "fecha": date.today().isoformat()}
            for k in ("cliente_nombre", "cliente_nit", "cliente_direccion", "cliente_telefono",
                      "cliente_ciudad", "contractor_nombre", "contractor_id", "contractor_telefono",
                      "contractor_email", "texto_pago"):
                payload[k] = cliente.get(k)
            payload["cliente_nombre"] = nombre
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "cobro_op", op_uuid, "create", payload)
        return local_id

    def cc_eliminar_cuenta(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM cc_cuentas WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Cuenta de cobro no encontrada.")
            conn.execute("DELETE FROM cc_detalle WHERE cuenta_local_id=?", (int(local_id),))
            conn.execute("DELETE FROM cc_cuentas WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_cuenta", "cuenta_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "cobro_op", _uuid.uuid4().hex, "create", payload)

    # ─── CRM (espejo + outbox) ────────────────────────────────────
    _CRM_CONTACTO_COLS = ("tipo", "nombre", "empresa", "cargo", "email", "telefono",
                          "whatsapp", "sitio_web", "direccion", "ciudad", "notas", "origen")

    def replace_crm_snapshot(self, data: dict):
        """Reconstruye el espejo CRM; conserva filas locales (synced=0)."""
        with self.connect() as conn:
            # Contactos
            conn.execute("DELETE FROM crm_contactos WHERE synced=1")
            for c in (data.get("contactos") or []):
                conn.execute(
                    """INSERT INTO crm_contactos (remote_id, tipo, nombre, empresa, cargo, email,
                         telefono, whatsapp, sitio_web, direccion, ciudad, notas, origen, synced, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET tipo=excluded.tipo, nombre=excluded.nombre,
                         empresa=excluded.empresa, cargo=excluded.cargo, email=excluded.email,
                         telefono=excluded.telefono, whatsapp=excluded.whatsapp, sitio_web=excluded.sitio_web,
                         direccion=excluded.direccion, ciudad=excluded.ciudad, notas=excluded.notas,
                         origen=excluded.origen, synced=1""",
                    (c.get("id"), c.get("tipo"), c.get("nombre"), c.get("empresa"), c.get("cargo"),
                     c.get("email"), c.get("telefono"), c.get("whatsapp"), c.get("sitio_web"),
                     c.get("direccion"), c.get("ciudad"), c.get("notas"), c.get("origen"), c.get("created_at")),
                )
            # Actividades
            conn.execute("DELETE FROM crm_actividades WHERE synced=1")
            for a in (data.get("actividades") or []):
                conn.execute(
                    """INSERT INTO crm_actividades (remote_id, contacto_remote_id, tipo, asunto, descripcion, fecha_actividad, synced)
                       VALUES (?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET tipo=excluded.tipo, asunto=excluded.asunto,
                         descripcion=excluded.descripcion, fecha_actividad=excluded.fecha_actividad, synced=1""",
                    (a.get("id"), a.get("contacto_id"), a.get("tipo"), a.get("asunto"),
                     a.get("descripcion"), a.get("fecha_actividad")),
                )
            # Tareas
            conn.execute("DELETE FROM crm_tareas WHERE synced=1")
            for t in (data.get("tareas") or []):
                conn.execute(
                    """INSERT INTO crm_tareas (remote_id, contacto_remote_id, titulo, descripcion,
                         prioridad, estado, fecha_limite, completada_en, synced)
                       VALUES (?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET titulo=excluded.titulo, descripcion=excluded.descripcion,
                         prioridad=excluded.prioridad, estado=excluded.estado, fecha_limite=excluded.fecha_limite,
                         completada_en=excluded.completada_en, synced=1""",
                    (t.get("id"), t.get("contacto_id"), t.get("titulo"), t.get("descripcion"),
                     t.get("prioridad"), t.get("estado"), t.get("fecha_limite"), t.get("completada_en")),
                )
            # Oportunidades
            conn.execute("DELETE FROM crm_oportunidades WHERE synced=1")
            for o in (data.get("oportunidades") or []):
                conn.execute(
                    """INSERT INTO crm_oportunidades (remote_id, contacto_remote_id, titulo, descripcion,
                         monto_estimado, probabilidad, etapa, fecha_cierre_est, synced, created_at)
                       VALUES (?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(remote_id) DO UPDATE SET titulo=excluded.titulo, descripcion=excluded.descripcion,
                         monto_estimado=excluded.monto_estimado, probabilidad=excluded.probabilidad,
                         etapa=excluded.etapa, fecha_cierre_est=excluded.fecha_cierre_est, synced=1""",
                    (o.get("id"), o.get("contacto_id"), o.get("titulo"), o.get("descripcion"),
                     float(o.get("monto_estimado") or 0), int(o.get("probabilidad") or 0),
                     o.get("etapa"), o.get("fecha_cierre_est"), o.get("created_at")),
                )

    def mark_crm_pushed(self):
        with self.connect() as conn:
            for tbl in ("crm_contactos", "crm_actividades", "crm_tareas", "crm_oportunidades"):
                conn.execute(f"UPDATE {tbl} SET synced=1 WHERE synced=0")

    def crm_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='crm_op'").fetchone()[0])

    def crm_list_contactos(self, tipo=None, limit=2000):
        sql = """SELECT id AS local_id, remote_id, tipo, nombre, empresa, cargo, email, telefono,
                        whatsapp, sitio_web, direccion, ciudad, notas, origen, synced
                 FROM crm_contactos WHERE 1=1"""
        params = []
        if tipo:
            sql += " AND tipo=?"; params.append(tipo)
        sql += " ORDER BY nombre LIMIT ?"; params.append(int(limit))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params))]

    def crm_list_tareas(self, contacto_remote_id=None, solo_pendientes=False):
        sql = "SELECT id AS local_id, remote_id, contacto_remote_id, titulo, descripcion, prioridad, estado, fecha_limite, synced FROM crm_tareas WHERE 1=1"
        params = []
        if contacto_remote_id is not None:
            sql += " AND contacto_remote_id=?"; params.append(int(contacto_remote_id))
        if solo_pendientes:
            sql += " AND estado='pendiente'"
        sql += " ORDER BY (estado='completada'), COALESCE(fecha_limite,'9999'), id DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params))]

    def crm_list_oportunidades(self, contacto_remote_id=None):
        sql = "SELECT id AS local_id, remote_id, contacto_remote_id, titulo, descripcion, monto_estimado, probabilidad, etapa, synced FROM crm_oportunidades WHERE 1=1"
        params = []
        if contacto_remote_id is not None:
            sql += " AND contacto_remote_id=?"; params.append(int(contacto_remote_id))
        sql += " ORDER BY id DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params))]

    def crm_list_actividades(self, contacto_remote_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, tipo, asunto, descripcion, fecha_actividad
                   FROM crm_actividades WHERE contacto_remote_id=? ORDER BY id DESC LIMIT 100""",
                (int(contacto_remote_id),))]

    def crm_kpis(self):
        from datetime import date as _d
        hoy = _d.today().isoformat()
        with self.connect() as conn:
            contactos = conn.execute("SELECT COUNT(*) FROM crm_contactos").fetchone()[0]
            t_pend = conn.execute("SELECT COUNT(*) FROM crm_tareas WHERE estado='pendiente'").fetchone()[0]
            t_venc = conn.execute("SELECT COUNT(*) FROM crm_tareas WHERE estado='pendiente' AND fecha_limite IS NOT NULL AND fecha_limite < ?", (hoy,)).fetchone()[0]
            opp_abiertas = conn.execute("SELECT COUNT(*) FROM crm_oportunidades WHERE etapa NOT IN ('ganada','perdida')").fetchone()[0]
            pipeline = conn.execute("SELECT COALESCE(SUM(monto_estimado),0) FROM crm_oportunidades WHERE etapa NOT IN ('ganada','perdida')").fetchone()[0]
        return {"contactos": int(contactos), "tareas_pendientes": int(t_pend),
                "tareas_vencidas": int(t_venc), "oportunidades_abiertas": int(opp_abiertas),
                "pipeline": float(pipeline or 0)}

    # ── Operaciones CRM (optimista local + outbox) ──
    def _crm_contacto_payload(self, data: dict) -> dict:
        return {k: (data.get(k) or None) for k in self._CRM_CONTACTO_COLS}

    def crm_crear_contacto(self, data: dict, user=None):
        nombre = (data.get("nombre") or "").strip()
        if not nombre:
            raise ValueError("El nombre del contacto es obligatorio.")
        tipo = (data.get("tipo") or "cliente").strip()
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO crm_contactos (tipo, nombre, empresa, cargo, email, telefono, whatsapp,
                     sitio_web, direccion, ciudad, notas, origen, synced, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,CURRENT_TIMESTAMP)""",
                (tipo, nombre, data.get("empresa"), data.get("cargo"), data.get("email"),
                 data.get("telefono"), data.get("whatsapp"), data.get("sitio_web"),
                 data.get("direccion"), data.get("ciudad"), data.get("notas"), "desktop"),
            )
            lid = int(cur.lastrowid)
            payload = {"op": "create_contacto", "client_op_uuid": op_uuid}
            payload.update(self._crm_contacto_payload(data)); payload["nombre"] = nombre; payload["tipo"] = tipo
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "crm_op", op_uuid, "create", payload)
        return lid

    def crm_editar_contacto(self, local_id, data: dict, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_contactos WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Contacto no encontrado.")
            sets = ", ".join(f"{c}=?" for c in self._CRM_CONTACTO_COLS)
            conn.execute(f"UPDATE crm_contactos SET {sets} WHERE id=?",
                         tuple(data.get(c) for c in self._CRM_CONTACTO_COLS) + (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "update_contacto", "contacto_id": int(row["remote_id"])}
                payload.update(self._crm_contacto_payload(data))
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    def crm_eliminar_contacto(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_contactos WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Contacto no encontrado.")
            conn.execute("DELETE FROM crm_contactos WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_contacto", "contacto_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    def crm_crear_tarea(self, contacto_remote_id, titulo, descripcion="", prioridad="media", fecha_limite=None, user=None):
        titulo = (titulo or "").strip()
        if not titulo:
            raise ValueError("El título de la tarea es obligatorio.")
        if contacto_remote_id is None:
            raise ValueError("Sincroniza el contacto antes de agregarle tareas.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO crm_tareas (contacto_remote_id, titulo, descripcion, prioridad, estado, fecha_limite, synced)
                   VALUES (?,?,?,?, 'pendiente', ?, 0)""",
                (int(contacto_remote_id), titulo, descripcion or None, prioridad, fecha_limite or None),
            )
            payload = {"op": "create_tarea", "client_op_uuid": op_uuid, "contacto_id": int(contacto_remote_id),
                       "titulo": titulo, "descripcion": descripcion or None, "prioridad": prioridad,
                       "fecha_limite": fecha_limite or None}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "crm_op", op_uuid, "create", payload)

    def crm_completar_tarea(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_tareas WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Tarea no encontrada.")
            conn.execute("UPDATE crm_tareas SET estado='completada', completada_en=CURRENT_TIMESTAMP WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "complete_tarea", "tarea_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    def crm_eliminar_tarea(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_tareas WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Tarea no encontrada.")
            conn.execute("DELETE FROM crm_tareas WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_tarea", "tarea_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    def crm_crear_actividad(self, contacto_remote_id, tipo, asunto, descripcion="", user=None):
        if contacto_remote_id is None:
            raise ValueError("Sincroniza el contacto antes de registrar actividades.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO crm_actividades (contacto_remote_id, tipo, asunto, descripcion, fecha_actividad, synced)
                   VALUES (?,?,?,?,CURRENT_TIMESTAMP,0)""",
                (int(contacto_remote_id), tipo, (asunto or "Actividad"), descripcion or None),
            )
            payload = {"op": "create_actividad", "client_op_uuid": op_uuid, "contacto_id": int(contacto_remote_id),
                       "tipo": tipo, "asunto": asunto or "Actividad", "descripcion": descripcion or None}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "crm_op", op_uuid, "create", payload)

    def crm_crear_oportunidad(self, contacto_remote_id, titulo, monto_estimado=0, probabilidad=50, etapa="prospecto", descripcion="", user=None):
        titulo = (titulo or "").strip()
        if not titulo:
            raise ValueError("El título de la oportunidad es obligatorio.")
        if contacto_remote_id is None:
            raise ValueError("Sincroniza el contacto antes de crear oportunidades.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO crm_oportunidades (contacto_remote_id, titulo, descripcion, monto_estimado, probabilidad, etapa, synced, created_at)
                   VALUES (?,?,?,?,?,?,0,CURRENT_TIMESTAMP)""",
                (int(contacto_remote_id), titulo, descripcion or None, float(monto_estimado or 0), int(probabilidad or 50), etapa),
            )
            payload = {"op": "create_oportunidad", "client_op_uuid": op_uuid, "contacto_id": int(contacto_remote_id),
                       "titulo": titulo, "descripcion": descripcion or None, "monto_estimado": float(monto_estimado or 0),
                       "probabilidad": int(probabilidad or 50), "etapa": etapa}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "crm_op", op_uuid, "create", payload)

    def crm_mover_oportunidad(self, local_id, etapa, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_oportunidades WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Oportunidad no encontrada.")
            conn.execute("UPDATE crm_oportunidades SET etapa=? WHERE id=?", (etapa, int(local_id)))
            if row["remote_id"] is not None:
                payload = {"op": "move_oportunidad", "oportunidad_id": int(row["remote_id"]), "etapa": etapa}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    def crm_eliminar_oportunidad(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM crm_oportunidades WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Oportunidad no encontrada.")
            conn.execute("DELETE FROM crm_oportunidades WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_oportunidad", "oportunidad_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "crm_op", _uuid.uuid4().hex, "create", payload)

    # ─── Nómina (espejo + outbox) ─────────────────────────────────
    _N_EMP_COLS = ("tipo_documento", "numero_documento", "nombres", "apellidos", "email",
                   "telefono", "direccion", "fecha_ingreso", "fecha_retiro", "tipo_vinculacion",
                   "cargo", "salario_base", "nivel_arl", "banco", "tipo_cuenta", "numero_cuenta",
                   "eps", "fondo_pension", "fondo_cesantias")

    def replace_nomina_snapshot(self, data: dict):
        """Reconstruye el espejo de nómina; conserva filas locales (synced=0)."""
        with self.connect() as conn:
            # Empleados
            conn.execute("DELETE FROM n_empleados WHERE synced=1")
            for e in (data.get("empleados") or []):
                conn.execute(
                    """INSERT INTO n_empleados (remote_id, tipo_documento, numero_documento, nombres,
                         apellidos, email, telefono, direccion, fecha_ingreso, fecha_retiro,
                         tipo_vinculacion, cargo, salario_base, nivel_arl, banco, tipo_cuenta,
                         numero_cuenta, eps, fondo_pension, fondo_cesantias, activo, synced)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET tipo_documento=excluded.tipo_documento,
                         numero_documento=excluded.numero_documento, nombres=excluded.nombres,
                         apellidos=excluded.apellidos, email=excluded.email, telefono=excluded.telefono,
                         direccion=excluded.direccion, fecha_ingreso=excluded.fecha_ingreso,
                         fecha_retiro=excluded.fecha_retiro, tipo_vinculacion=excluded.tipo_vinculacion,
                         cargo=excluded.cargo, salario_base=excluded.salario_base, nivel_arl=excluded.nivel_arl,
                         banco=excluded.banco, tipo_cuenta=excluded.tipo_cuenta, numero_cuenta=excluded.numero_cuenta,
                         eps=excluded.eps, fondo_pension=excluded.fondo_pension,
                         fondo_cesantias=excluded.fondo_cesantias, activo=excluded.activo, synced=1""",
                    (e.get("id"), e.get("tipo_documento"), e.get("numero_documento"), e.get("nombres"),
                     e.get("apellidos"), e.get("email"), e.get("telefono"), e.get("direccion"),
                     e.get("fecha_ingreso"), e.get("fecha_retiro"), e.get("tipo_vinculacion") or "EMPLEADO",
                     e.get("cargo"), float(e.get("salario_base") or 0), e.get("nivel_arl") or "I",
                     e.get("banco"), e.get("tipo_cuenta"), e.get("numero_cuenta"), e.get("eps"),
                     e.get("fondo_pension"), e.get("fondo_cesantias"), 1 if e.get("activo", True) else 0),
                )
            # Parámetros (server autoritativo; reemplazo total)
            for p in (data.get("parametros") or []):
                conn.execute(
                    """INSERT INTO n_parametros (anio, salario_minimo, auxilio_transporte, uvt)
                       VALUES (?,?,?,?)
                       ON CONFLICT(anio) DO UPDATE SET salario_minimo=excluded.salario_minimo,
                         auxilio_transporte=excluded.auxilio_transporte, uvt=excluded.uvt""",
                    (int(p.get("anio")), float(p.get("salario_minimo") or 0),
                     float(p.get("auxilio_transporte") or 0), float(p.get("uvt") or 0)),
                )
            # Períodos
            conn.execute("DELETE FROM n_periodos WHERE synced=1")
            for pe in (data.get("periodos") or []):
                conn.execute(
                    """INSERT INTO n_periodos (remote_id, anio, mes, numero_periodo, fecha_inicio,
                         fecha_fin, observaciones, estado_local, synced)
                       VALUES (?,?,?,?,?,?,?, 'liquidado', 1)
                       ON CONFLICT(remote_id) DO UPDATE SET anio=excluded.anio, mes=excluded.mes,
                         numero_periodo=excluded.numero_periodo, fecha_inicio=excluded.fecha_inicio,
                         fecha_fin=excluded.fecha_fin, observaciones=excluded.observaciones, synced=1""",
                    (pe.get("id"), int(pe.get("anio") or 0), pe.get("mes"), pe.get("numero_periodo"),
                     pe.get("fecha_inicio"), pe.get("fecha_fin"), pe.get("observaciones")),
                )
            # Detalle (mapear periodo remoto -> local)
            per_map = {r["remote_id"]: r["id"] for r in conn.execute(
                "SELECT id, remote_id FROM n_periodos WHERE remote_id IS NOT NULL")}
            conn.execute("DELETE FROM n_detalle WHERE synced=1")
            for d in (data.get("detalle") or []):
                lid = per_map.get(d.get("periodo_id"))
                if lid is None:
                    continue
                conn.execute(
                    """INSERT INTO n_detalle (remote_id, periodo_local_id, empleado_remote_id, dias_trabajados,
                         sueldo_basico, auxilio_transporte, horas_extras, total_devengado, salud_empleado,
                         pension_empleado, fondo_solidaridad, retencion_fuente, total_deducido, neto_pagar, synced)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET total_devengado=excluded.total_devengado,
                         total_deducido=excluded.total_deducido, neto_pagar=excluded.neto_pagar, synced=1""",
                    (d.get("id"), lid, d.get("empleado_id"), int(d.get("dias_trabajados") or 0),
                     float(d.get("sueldo_basico") or 0), float(d.get("auxilio_transporte") or 0),
                     float(d.get("horas_extras") or 0), float(d.get("total_devengado") or 0),
                     float(d.get("salud_empleado") or 0), float(d.get("pension_empleado") or 0),
                     float(d.get("fondo_solidaridad") or 0), float(d.get("retencion_fuente") or 0),
                     float(d.get("total_deducido") or 0), float(d.get("neto_pagar") or 0)),
                )
            # Novedades
            conn.execute("DELETE FROM n_novedades WHERE synced=1")
            for nv in (data.get("novedades") or []):
                conn.execute(
                    """INSERT INTO n_novedades (remote_id, periodo_remote_id, empleado_remote_id,
                         tipo_novedad, cantidad, valor_total, fecha_novedad, observacion, synced)
                       VALUES (?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(remote_id) DO UPDATE SET cantidad=excluded.cantidad,
                         valor_total=excluded.valor_total, observacion=excluded.observacion, synced=1""",
                    (nv.get("id"), nv.get("periodo_id"), nv.get("empleado_id"), nv.get("tipo_novedad"),
                     float(nv.get("cantidad") or 0), float(nv.get("valor_total") or 0),
                     nv.get("fecha_novedad"), nv.get("observacion")),
                )

    def mark_nomina_pushed(self):
        with self.connect() as conn:
            for tbl in ("n_empleados", "n_periodos", "n_detalle", "n_novedades"):
                conn.execute(f"UPDATE {tbl} SET synced=1 WHERE synced=0")

    def n_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL AND entity='nomina_op'").fetchone()[0])

    def n_list_empleados(self, incluir_inactivos=False):
        sql = """SELECT id AS local_id, remote_id, tipo_documento, numero_documento, nombres, apellidos,
                        email, telefono, direccion, fecha_ingreso, fecha_retiro, tipo_vinculacion, cargo,
                        salario_base, nivel_arl, banco, tipo_cuenta, numero_cuenta, eps, fondo_pension,
                        fondo_cesantias, activo, synced
                 FROM n_empleados WHERE 1=1"""
        if not incluir_inactivos:
            sql += " AND activo=1"
        sql += " ORDER BY nombres, apellidos"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def n_get_empleado(self, local_id):
        with self.connect() as conn:
            r = conn.execute("SELECT *, id AS local_id FROM n_empleados WHERE id=?", (int(local_id),)).fetchone()
            return dict(r) if r else None

    def n_get_parametros(self, anio):
        """Parámetros del año desde la tabla local; si no hay, usa los oficiales
        portados (offline-first). Devuelve dict salario_minimo/auxilio_transporte/uvt."""
        anio = int(anio)
        with self.connect() as conn:
            r = conn.execute("SELECT anio, salario_minimo, auxilio_transporte, uvt FROM n_parametros WHERE anio=?", (anio,)).fetchone()
        if r and float(r["salario_minimo"] or 0) > 0:
            return dict(r)
        import nomina_calc
        of = nomina_calc.PARAMETROS_OFICIALES_NOMINA.get(anio)
        if of:
            return {"anio": anio, "salario_minimo": of["salario_minimo"],
                    "auxilio_transporte": of["auxilio_transporte"], "uvt": of["uvt"]}
        return {"anio": anio, "salario_minimo": 0, "auxilio_transporte": 0, "uvt": 0}

    def n_list_periodos(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, anio, mes, numero_periodo, fecha_inicio, fecha_fin,
                          observaciones, estado_local, synced
                   FROM n_periodos ORDER BY anio DESC, COALESCE(mes,0) DESC, COALESCE(numero_periodo,0) DESC, id DESC""")]

    def n_get_periodo(self, local_id):
        with self.connect() as conn:
            r = conn.execute("SELECT *, id AS local_id FROM n_periodos WHERE id=?", (int(local_id),)).fetchone()
            return dict(r) if r else None

    def n_list_novedades(self, periodo_remote_id):
        if periodo_remote_id is None:
            return []
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, periodo_remote_id, empleado_remote_id, tipo_novedad,
                          cantidad, valor_total, fecha_novedad, observacion, synced
                   FROM n_novedades WHERE periodo_remote_id=? ORDER BY id DESC""", (int(periodo_remote_id),))]

    def n_list_detalle(self, periodo_local_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id AS local_id, remote_id, empleado_remote_id, dias_trabajados, sueldo_basico,
                          auxilio_transporte, horas_extras, total_devengado, salud_empleado, pension_empleado,
                          fondo_solidaridad, retencion_fuente, total_deducido, neto_pagar, synced
                   FROM n_detalle WHERE periodo_local_id=? ORDER BY id""", (int(periodo_local_id),))]

    # ── Operaciones nómina (optimista local + outbox) ──
    def _n_emp_payload(self, data):
        return {k: data.get(k) for k in self._N_EMP_COLS}

    def n_crear_empleado(self, data, user=None):
        nombres = (data.get("nombres") or "").strip()
        if not nombres:
            raise ValueError("Los nombres son obligatorios.")
        if float(data.get("salario_base") or 0) <= 0:
            raise ValueError("El salario base debe ser mayor a cero.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            cols = ", ".join(self._N_EMP_COLS)
            ph = ", ".join("?" for _ in self._N_EMP_COLS)
            cur = conn.execute(
                f"INSERT INTO n_empleados ({cols}, activo, synced) VALUES ({ph}, 1, 0)",
                tuple(data.get(c) for c in self._N_EMP_COLS),
            )
            lid = int(cur.lastrowid)
            payload = {"op": "create_empleado", "client_op_uuid": op_uuid}
            payload.update(self._n_emp_payload(data))
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "nomina_op", op_uuid, "create", payload)
        return lid

    def n_editar_empleado(self, local_id, data, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM n_empleados WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Empleado no encontrado.")
            sets = ", ".join(f"{c}=?" for c in self._N_EMP_COLS)
            conn.execute(f"UPDATE n_empleados SET {sets} WHERE id=?",
                         tuple(data.get(c) for c in self._N_EMP_COLS) + (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "update_empleado", "empleado_id": int(row["remote_id"])}
                payload.update(self._n_emp_payload(data))
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "nomina_op", _uuid.uuid4().hex, "create", payload)

    def n_eliminar_empleado(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM n_empleados WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Empleado no encontrado.")
            conn.execute("UPDATE n_empleados SET activo=0 WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_empleado", "empleado_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "nomina_op", _uuid.uuid4().hex, "create", payload)

    def n_crear_periodo(self, anio, mes, numero_periodo, fecha_inicio, fecha_fin, observaciones="", user=None):
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO n_periodos (anio, mes, numero_periodo, fecha_inicio, fecha_fin,
                     observaciones, estado_local, synced) VALUES (?,?,?,?,?,?, 'borrador', 0)""",
                (int(anio), int(mes), int(numero_periodo), fecha_inicio, fecha_fin, observaciones or None),
            )
            lid = int(cur.lastrowid)
            payload = {"op": "create_periodo", "client_op_uuid": op_uuid, "anio": int(anio), "mes": int(mes),
                       "numero_periodo": int(numero_periodo), "fecha_inicio": fecha_inicio,
                       "fecha_fin": fecha_fin, "observaciones": observaciones or None}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "nomina_op", op_uuid, "create", payload)
        return lid

    def n_crear_novedad(self, periodo_remote_id, empleado_remote_id, tipo_novedad, cantidad, valor_total,
                        fecha_novedad=None, observacion="", user=None):
        if periodo_remote_id is None or empleado_remote_id is None:
            raise ValueError("Sincroniza el período y el empleado antes de registrar novedades.")
        op_uuid = _uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO n_novedades (periodo_remote_id, empleado_remote_id, tipo_novedad, cantidad,
                     valor_total, fecha_novedad, observacion, synced) VALUES (?,?,?,?,?,?,?,0)""",
                (int(periodo_remote_id), int(empleado_remote_id), tipo_novedad, float(cantidad or 0),
                 float(valor_total or 0), fecha_novedad, observacion or None),
            )
            payload = {"op": "create_novedad", "client_op_uuid": op_uuid,
                       "periodo_id": int(periodo_remote_id), "empleado_id": int(empleado_remote_id),
                       "tipo_novedad": tipo_novedad, "cantidad": float(cantidad or 0),
                       "valor_total": float(valor_total or 0), "fecha_novedad": fecha_novedad,
                       "observacion": observacion or None}
            payload.update(self._rt_user_fields(user))
            self._queue_outbox(conn, "nomina_op", op_uuid, "create", payload)

    def n_eliminar_novedad(self, local_id, user=None):
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM n_novedades WHERE id=?", (int(local_id),)).fetchone()
            if not row:
                raise ValueError("Novedad no encontrada.")
            conn.execute("DELETE FROM n_novedades WHERE id=?", (int(local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "delete_novedad", "novedad_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "nomina_op", _uuid.uuid4().hex, "create", payload)

    def n_guardar_liquidacion(self, periodo_local_id, detalles, user=None):
        """Persiste localmente la liquidación calculada (synced=0) y encola la
        solicitud para que el servidor recalcule con el MISMO motor al sincronizar."""
        with self.connect() as conn:
            row = conn.execute("SELECT remote_id FROM n_periodos WHERE id=?", (int(periodo_local_id),)).fetchone()
            if not row:
                raise ValueError("Período no encontrado.")
            conn.execute("DELETE FROM n_detalle WHERE periodo_local_id=? AND synced=0", (int(periodo_local_id),))
            for d in detalles:
                conn.execute(
                    """INSERT INTO n_detalle (periodo_local_id, empleado_remote_id, dias_trabajados,
                         sueldo_basico, auxilio_transporte, horas_extras, total_devengado, salud_empleado,
                         pension_empleado, fondo_solidaridad, retencion_fuente, total_deducido, neto_pagar, synced)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (int(periodo_local_id), d.get("empleado_id"), int(d.get("dias_trabajados") or 0),
                     d.get("sueldo_basico"), d.get("auxilio_transporte"), d.get("horas_extras"),
                     d.get("total_devengado"), d.get("salud_empleado"), d.get("pension_empleado"),
                     d.get("fondo_solidaridad"), d.get("retencion_fuente"), d.get("total_deducido"),
                     d.get("neto_pagar")),
                )
            conn.execute("UPDATE n_periodos SET estado_local='liquidado' WHERE id=?", (int(periodo_local_id),))
            if row["remote_id"] is not None:
                payload = {"op": "calcular_periodo", "periodo_id": int(row["remote_id"])}
                payload.update(self._rt_user_fields(user))
                self._queue_outbox(conn, "nomina_op", _uuid.uuid4().hex, "create", payload)

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
        # Mapeo de rol web -> rol desktop preservando los 6 roles (rol_id es
        # autoritativo; cae a rol_nombre/role). Necesario para que el manifiesto
        # de permisos se aplique al rol correcto (Empleado/Contador/Mesero, etc.).
        rol = map_role(remote.get("rol_id"), remote.get("rol_nombre") or remote.get("role"))
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

                CREATE TABLE IF NOT EXISTS q_cotizaciones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    cliente_nombre TEXT NOT NULL,
                    cliente_documento TEXT,
                    cliente_direccion TEXT,
                    cliente_ciudad TEXT,
                    cliente_telefono TEXT,
                    cliente_representante TEXT,
                    cliente_cargo TEXT,
                    cliente_localidad TEXT,
                    total REAL NOT NULL DEFAULT 0,
                    estado TEXT NOT NULL DEFAULT 'pendiente',
                    pdf_path TEXT,
                    fecha TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS q_detalle (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    cotizacion_local_id INTEGER NOT NULL,
                    descripcion TEXT,
                    cantidad INTEGER NOT NULL DEFAULT 1,
                    precio_unitario REAL NOT NULL DEFAULT 0,
                    subtotal REAL NOT NULL DEFAULT 0,
                    descuento_porc REAL NOT NULL DEFAULT 0,
                    iva_porc REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_q_det_cot ON q_detalle(cotizacion_local_id);

                CREATE TABLE IF NOT EXISTS cc_cuentas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    consecutivo TEXT,
                    fecha TEXT,
                    cliente_nombre TEXT NOT NULL,
                    cliente_nit TEXT,
                    cliente_direccion TEXT,
                    cliente_telefono TEXT,
                    cliente_ciudad TEXT,
                    contractor_nombre TEXT,
                    contractor_id TEXT,
                    contractor_telefono TEXT,
                    contractor_email TEXT,
                    texto_pago TEXT,
                    total REAL NOT NULL DEFAULT 0,
                    pdf_path TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS cc_detalle (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    cuenta_local_id INTEGER NOT NULL,
                    fecha_labor TEXT,
                    descripcion TEXT,
                    valor REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_cc_det_cuenta ON cc_detalle(cuenta_local_id);

                CREATE TABLE IF NOT EXISTS crm_contactos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    tipo TEXT NOT NULL DEFAULT 'cliente',
                    nombre TEXT NOT NULL,
                    empresa TEXT, cargo TEXT, email TEXT, telefono TEXT, whatsapp TEXT,
                    sitio_web TEXT, direccion TEXT, ciudad TEXT, notas TEXT, origen TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS crm_actividades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    contacto_remote_id INTEGER,
                    tipo TEXT, asunto TEXT, descripcion TEXT, fecha_actividad TEXT,
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS crm_tareas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    contacto_remote_id INTEGER,
                    titulo TEXT NOT NULL, descripcion TEXT,
                    prioridad TEXT NOT NULL DEFAULT 'media',
                    estado TEXT NOT NULL DEFAULT 'pendiente',
                    fecha_limite TEXT, completada_en TEXT,
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS crm_oportunidades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    contacto_remote_id INTEGER,
                    titulo TEXT NOT NULL, descripcion TEXT,
                    monto_estimado REAL NOT NULL DEFAULT 0,
                    probabilidad INTEGER NOT NULL DEFAULT 50,
                    etapa TEXT NOT NULL DEFAULT 'prospecto',
                    fecha_cierre_est TEXT,
                    synced INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_crm_act_cont ON crm_actividades(contacto_remote_id);
                CREATE INDEX IF NOT EXISTS idx_crm_tar_cont ON crm_tareas(contacto_remote_id);
                CREATE INDEX IF NOT EXISTS idx_crm_opo_cont ON crm_oportunidades(contacto_remote_id);

                CREATE TABLE IF NOT EXISTS n_empleados (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    tipo_documento TEXT, numero_documento TEXT,
                    nombres TEXT NOT NULL, apellidos TEXT,
                    email TEXT, telefono TEXT, direccion TEXT,
                    fecha_ingreso TEXT, fecha_retiro TEXT,
                    tipo_vinculacion TEXT NOT NULL DEFAULT 'EMPLEADO',
                    cargo TEXT, salario_base REAL NOT NULL DEFAULT 0,
                    nivel_arl TEXT DEFAULT 'I',
                    banco TEXT, tipo_cuenta TEXT, numero_cuenta TEXT,
                    eps TEXT, fondo_pension TEXT, fondo_cesantias TEXT,
                    activo INTEGER NOT NULL DEFAULT 1,
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS n_parametros (
                    anio INTEGER PRIMARY KEY,
                    salario_minimo REAL NOT NULL DEFAULT 0,
                    auxilio_transporte REAL NOT NULL DEFAULT 0,
                    uvt REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS n_periodos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    anio INTEGER NOT NULL, mes INTEGER, numero_periodo INTEGER,
                    fecha_inicio TEXT, fecha_fin TEXT, observaciones TEXT,
                    estado_local TEXT NOT NULL DEFAULT 'borrador',
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS n_detalle (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    periodo_local_id INTEGER NOT NULL,
                    empleado_remote_id INTEGER,
                    dias_trabajados INTEGER DEFAULT 0,
                    sueldo_basico REAL DEFAULT 0, auxilio_transporte REAL DEFAULT 0,
                    horas_extras REAL DEFAULT 0, total_devengado REAL DEFAULT 0,
                    salud_empleado REAL DEFAULT 0, pension_empleado REAL DEFAULT 0,
                    fondo_solidaridad REAL DEFAULT 0, retencion_fuente REAL DEFAULT 0,
                    total_deducido REAL DEFAULT 0, neto_pagar REAL DEFAULT 0,
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS n_novedades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_id INTEGER UNIQUE,
                    periodo_remote_id INTEGER,
                    empleado_remote_id INTEGER,
                    tipo_novedad TEXT NOT NULL,
                    cantidad REAL NOT NULL DEFAULT 0,
                    valor_total REAL DEFAULT 0,
                    fecha_novedad TEXT, observacion TEXT,
                    synced INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_n_det_per ON n_detalle(periodo_local_id);
                CREATE INDEX IF NOT EXISTS idx_n_nov_per ON n_novedades(periodo_remote_id);
                """
            )

            # ── IA: historial de chat local (persistente, solo lectura offline) ──
            # y operaciones de outbox RECHAZADas por el servidor (licencia/rol),
            # para no reintentarlas indefinidamente y mostrarlas al usuario.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ia_chat (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rol TEXT NOT NULL,          -- 'user' | 'assistant'
                    texto TEXT NOT NULL,
                    herramienta TEXT,           -- tool usada por la IA (opcional)
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_ia_chat_created ON ia_chat(created_at);

                CREATE TABLE IF NOT EXISTS sync_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT, action TEXT,
                    motivo TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
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

    def get_permissions_manifest(self):
        """Manifiesto de permisos rol → {modules, actions} cacheado, o None si
        nunca se cacheó (el caller hace fail-open al gating por rol local)."""
        with self.connect() as conn:
            raw = self._get_meta(conn, "permissions_manifest")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            return None

    def set_permissions_manifest(self, manifest: dict) -> None:
        """Persiste el manifiesto de permisos para gating offline por rol+acción."""
        with self.connect() as conn:
            self._set_meta(conn, "permissions_manifest", json.dumps(manifest, ensure_ascii=True))

    # ── IA: historial de chat local ─────────────────────────────
    def ia_add_message(self, rol: str, texto: str, herramienta: str | None = None) -> None:
        """Guarda un turno del chat (rol='user'|'assistant'). No lanza."""
        if not (texto or "").strip():
            return
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO ia_chat(rol, texto, herramienta) VALUES(?, ?, ?)",
                    (rol, texto, herramienta),
                )
        except Exception:
            pass

    def ia_recent(self, limit: int = 40) -> list:
        """Últimos mensajes del chat, en orden cronológico ascendente."""
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT rol, texto, herramienta, created_at FROM ia_chat "
                    "ORDER BY id DESC LIMIT ?", (int(limit),),
                ).fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception:
            return []

    def ia_clear(self) -> None:
        """Borra el historial de chat local."""
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM ia_chat")
        except Exception:
            pass

    # ── Sync: operaciones rechazadas por el servidor (licencia/rol) ──
    def record_rejections(self, rejections: list) -> None:
        """Persiste rechazos {entity, action, motivo} para mostrarlos en Sync."""
        if not rejections:
            return
        try:
            with self.connect() as conn:
                conn.executemany(
                    "INSERT INTO sync_rejections(entity, action, motivo) VALUES(?, ?, ?)",
                    [(r.get("entity"), r.get("action"), (r.get("motivo") or "")[:200]) for r in rejections],
                )
        except Exception:
            pass

    def get_rejections(self, limit: int = 50) -> list:
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT id, entity, action, motivo, created_at FROM sync_rejections "
                    "ORDER BY id DESC LIMIT ?", (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def rejections_count(self) -> int:
        try:
            with self.connect() as conn:
                return conn.execute("SELECT COUNT(*) FROM sync_rejections").fetchone()[0]
        except Exception:
            return 0

    def clear_rejections(self) -> None:
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM sync_rejections")
        except Exception:
            pass

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
