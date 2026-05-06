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

    # ── Internals ─────────────────────────────────────────────
    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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
