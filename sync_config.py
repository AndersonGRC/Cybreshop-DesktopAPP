"""Configuracion de sincronizacion del cliente desktop.

Persiste en %APPDATA%/CyberShopNative/sync_config.json (separado de
branding.json para no mezclar concerns: branding viaja entre maquinas,
sync_config no debe — la API key es secreto de esta instalacion).

Claves:
- base_url     : URL HTTPS del servidor, ej. https://cybershopcol.com
- api_key      : SYNC_API_KEY entregada por el admin
- enabled      : si el timer automatico esta encendido
- interval_sec : cada cuantos segundos hacer pull+push (default 30)
- last_pull_at : ISO timestamp del cursor del ultimo pull exitoso
- last_sync_status : "ok" | "error: <msg>" | None
- last_sync_at : ISO timestamp del ultimo intento (exitoso o no)
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "enabled": False,
    "interval_sec": 30,
    "last_pull_at": "",          # legacy (cursor productos)
    "cursor_products": "",
    "cursor_users": "",
    "cursor_generos": "",
    "cursor_sales_web": "",
    "cursor_inventory_log": "",
    "last_sync_status": "",
    "last_sync_at": "",
    "last_stale_count": "0",
    # Auto-update / branding sync
    "skip_version": "",                  # versión saltada por el usuario
    "branding_local_override": False,    # True = no pisar branding local con remoto
    "last_branding_pull_at": "",
}

CONFIG_FILE = "sync_config.json"


def config_path(base_dir: Path) -> Path:
    return Path(base_dir) / CONFIG_FILE


def load(base_dir: Path) -> dict[str, Any]:
    """Carga el config fusionando con defaults. Robusto a archivo faltante/corrupto."""
    path = config_path(base_dir)
    data: dict[str, Any] = deepcopy(DEFAULTS)
    if not path.exists():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        # Cast suave
        if key in ("enabled", "branding_local_override"):
            value = bool(value)
        elif key == "interval_sec":
            try:
                value = max(5, int(value))  # minimo 5 seg para evitar abuso
            except (TypeError, ValueError):
                value = default
        elif not isinstance(value, str):
            value = str(value) if value is not None else default
        data[key] = value
    return data


def save(base_dir: Path, config: dict[str, Any]) -> Path:
    """Guarda el config completo. Garantiza todas las claves presentes."""
    payload = deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in config:
            payload[key] = config[key]
    path = config_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update(base_dir: Path, **fields) -> dict[str, Any]:
    """Carga, actualiza solo los campos provistos y guarda."""
    current = load(base_dir)
    current.update(fields)
    save(base_dir, current)
    return current
