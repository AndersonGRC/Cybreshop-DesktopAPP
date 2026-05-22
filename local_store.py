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
        rol_remote = (remote_user.get("rol_nombre") or "Cajero").strip()
        rl = rol_remote.lower()
        if rl in ("admin", "administrador", "super_admin", "propietario"):
            role = "Administrador"
        elif rl in ("cajero", "vendedor", "empleado", "mesero"):
            role = "Cajero"
        elif rl in ("contador",):
            role = "Inventario"
        else:
            role = "Cajero"
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

    def pending_outbox(self, limit: int = 50, entities: tuple[str, ...] = ("sale", "inventory_movement", "product", "user", "category", "order")):
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
