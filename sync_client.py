"""Cliente HTTP para sincronizacion contra /api/v1/sync/* del servidor.

Sin dependencias externas: usa urllib.request para no inflar requirements.

Funciones publicas:
- SyncClient(base_url, api_key)
- SyncClient.health()                  -> dict
- SyncClient.pull_products(since=None) -> dict {items, cursor, ...}
- SyncClient.push_outbox(items)        -> dict {results, ...}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

USER_AGENT = "CyberShopDesktop/0.4 (sync-client)"
DEFAULT_TIMEOUT = 15  # segundos
AI_TIMEOUT = 130  # segundos — el LLM tarda; espejo de AI_TIMEOUT del servidor (120) + margen


class SyncError(Exception):
    """Error de sincronizacion. Atributos opcionales: status_code, error_code, body."""

    def __init__(self, message: str, status_code: int | None = None, error_code: str | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.body = body


@dataclass
class SyncClient:
    base_url: str
    api_key: str
    timeout: int = DEFAULT_TIMEOUT

    def __post_init__(self):
        self.base_url = (self.base_url or "").rstrip("/")
        self.api_key = (self.api_key or "").strip()
        if not self.base_url:
            raise ValueError("base_url es obligatorio")
        if not self.api_key:
            raise ValueError("api_key es obligatorio")

    # ── Endpoints ─────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/sync/health")

    def pull_products(self, since: str | None = None, limit: int = 1000,
                      include_inactive: bool = True) -> dict[str, Any]:
        params = {"limit": int(limit)}
        if since:
            params["since"] = since
        if include_inactive:
            params["include_inactive"] = 1
        return self._request("GET", f"/api/v1/sync/products?{urlencode(params)}")

    def pull_users(self, since: str | None = None, limit: int = 500) -> dict[str, Any]:
        params = {"limit": int(limit)}
        if since:
            params["since"] = since
        return self._request("GET", f"/api/v1/sync/users?{urlencode(params)}")

    def pull_generos(self, since: str | None = None, limit: int = 500) -> dict[str, Any]:
        params = {"limit": int(limit)}
        if since:
            params["since"] = since
        return self._request("GET", f"/api/v1/sync/generos?{urlencode(params)}")

    def pull_sales_web(self, since: str | None = None, limit: int = 200) -> dict[str, Any]:
        params = {"limit": int(limit)}
        if since:
            params["since"] = since
        return self._request("GET", f"/api/v1/sync/sales_web?{urlencode(params)}")

    def pull_inventory_log(self, since: str | None = None, limit: int = 500) -> dict[str, Any]:
        params = {"limit": int(limit)}
        if since:
            params["since"] = since
        return self._request("GET", f"/api/v1/sync/inventory_log?{urlencode(params)}")

    def push_outbox(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/sync/outbox", body={"items": items})

    def pull_branding(self) -> dict[str, Any]:
        """Branding (empresa + colores + logo_url) del tenant.

        Estructura:
          {empresa: {nombre, slogan?, email?, telefono?, ...},
           colores: {primario, primario_oscuro, acento, ...},
           logo_url: 'https://server/static/img/Logo.png',
           updated_at: '...'}
        """
        return self._request("GET", "/api/v1/sync/branding")

    def pull_config(self) -> dict[str, Any]:
        """Info pública del tenant: slug, nombre, plan, estado."""
        return self._request("GET", "/api/v1/sync/config")

    def pull_version(self) -> dict[str, Any]:
        """Manifiesto de versión disponible. NO requiere api_key, pero la mandamos igual."""
        return self._request("GET", "/api/v1/sync/version")

    def pull_stats(self) -> dict[str, Any]:
        """Métricas agregadas del tenant (ventas web, pedidos pendientes, etc.)."""
        return self._request("GET", "/api/v1/sync/stats")

    def pull_restaurant_snapshot(self) -> dict[str, Any]:
        """Estado completo del módulo de mesas: tables, open_orders,
        consumptions y products. Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/restaurant/snapshot")

    def pull_contabilidad_snapshot(self) -> dict[str, Any]:
        """Estado del módulo de contabilidad: movimientos, plantillas,
        cierres y categorias. Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/contabilidad/snapshot")

    def pull_quotes_snapshot(self) -> dict[str, Any]:
        """Estado del módulo de cotizaciones: cabeceras + detalles.
        Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/quotes/snapshot")

    def pull_cobros_snapshot(self) -> dict[str, Any]:
        """Estado del módulo de cuentas de cobro: cabeceras + detalles.
        Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/cobros/snapshot")

    def pull_crm_snapshot(self) -> dict[str, Any]:
        """Estado del módulo CRM: contactos, actividades, tareas y oportunidades.
        Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/crm/snapshot")

    def pull_nomina_snapshot(self) -> dict[str, Any]:
        """Estado del módulo de nómina: empleados, parámetros, períodos, detalle
        y novedades. Snapshot (no incremental)."""
        return self._request("GET", "/api/v1/sync/nomina/snapshot")

    # ── IA (proxy autenticado por X-Sync-Key; online-only) ──────
    def ai_estado(self) -> dict[str, Any]:
        """Estado del asistente: {online, licenciado, modelo, motivo}.
        Timeout corto: es solo un ping de disponibilidad."""
        return self._request("GET", "/api/v1/sync/ai/estado", timeout=20)

    def ai_chat(self, pregunta: str) -> dict[str, Any]:
        """Pregunta al negocio. Responde con datos reales de la BD del tenant.
        Returns: {success, respuesta?, datos?, herramienta?, error?}."""
        return self._request("POST", "/api/v1/sync/ai/chat",
                             body={"pregunta": pregunta}, timeout=AI_TIMEOUT)

    def ai_accion(self, tipo: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Acción puntual de IA (descripcion|reescribir|seo|tags|nombre|
        traducir|respuesta|contenido). Returns: {success, texto?, error?}."""
        return self._request("POST", "/api/v1/sync/ai/accion",
                             body={"tipo": tipo, "payload": payload or {}},
                             timeout=AI_TIMEOUT)

    def remote_login(self, email: str, password: str) -> dict[str, Any]:
        """Valida credenciales contra la tabla `usuarios` del tenant en el VPS.

        Returns: {"user": {"remote_id", "email", "nombre", "rol_id",
                            "rol_nombre", "estado"}}
        Raises:  SyncError. status_code 401 → credenciales inválidas;
                 403 → cuenta inhabilitada o rol bloqueado.
        """
        return self._request("POST", "/api/v1/sync/auth",
                             body={"email": email, "password": password})

    # ── Helpers de fetch en vivo (sin cursor, sin tocar SQLite local) ──
    def pull_products_live(self, limit: int = 5000) -> dict[str, Any]:
        return self.pull_products(since=None, limit=limit, include_inactive=False)

    def pull_users_live(self, limit: int = 1000) -> dict[str, Any]:
        return self.pull_users(since=None, limit=limit)

    def pull_categories_live(self, limit: int = 500) -> dict[str, Any]:
        return self.pull_generos(since=None, limit=limit)

    def pull_orders_live(self, limit: int = 500) -> dict[str, Any]:
        return self.pull_sales_web(since=None, limit=limit)

    def download_file(self, url: str, dest_path) -> int:
        """Descarga un archivo binario (logo, instalador) a dest_path. Retorna bytes escritos.

        url puede ser absoluta (https://...) o relativa (/static/...).
        No envía X-Sync-Key porque estos endpoints son públicos.
        """
        if url.startswith('/'):
            url = f"{self.base_url}{url}"
        elif not url.startswith(('http://', 'https://')):
            raise SyncError(f"URL inválida: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as resp:
                from pathlib import Path
                p = Path(dest_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with open(p, 'wb') as fh:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        total += len(chunk)
                return total
        except urllib.error.URLError as e:
            raise SyncError(f"Error descargando {url}: {e.reason}") from e

    # ── Internals ─────────────────────────────────────────────
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "X-Sync-Key": self.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return _parse_json(resp.read(), resp.status)
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            payload = _safe_json(raw)
            err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
            raise SyncError(
                err.get("message") or f"HTTP {e.code} {e.reason}",
                status_code=e.code,
                error_code=err.get("code"),
                body=payload,
            ) from e
        except urllib.error.URLError as e:
            raise SyncError(f"Error de red: {e.reason}") from e


def _parse_json(raw: bytes, status: int) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SyncError(f"Respuesta no es JSON valido (status={status})") from e


def _safe_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except Exception:
        return raw.decode("utf-8", errors="replace")[:300]
