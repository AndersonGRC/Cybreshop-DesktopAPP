r"""Lectura del archivo `.cybershop.conf` del POS Desktop.

Este archivo lo escribe el asistente del instalador (Inno Setup) en
`%APPDATA%\CyberShopNative\.cybershop.conf` con los datos que el cliente
proporcionó durante la instalación: URL del servidor, API key, y opcional
conexión directa a Postgres.

Formato dotenv (espejo del `.cybershop.conf` del lado servidor):
    SERVER_URL=https://cybershopcol.com
    SYNC_API_KEY=cyb_live_...
    TENANT_SLUG=cyber-t001
    TENANT_NOMBRE=Cliente XYZ
    PG_HOST=...
    PG_PORT=5432
    PG_DBNAME=...
    PG_USER=...
    PG_PASSWORD=...
    LOCAL_DB_PATH=...\cybershop_offline.db
    SYNC_INTERVAL_SEC=30
    AUTO_UPDATE_CHECK=true

Si el archivo no existe (instalación manual sin wizard), todas las claves
quedan en blanco y la app pide configuración por F7 como antes.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

CONF_FILENAME = ".cybershop.conf"

DEFAULTS: dict[str, str] = {
    "SERVER_URL":         "",
    "SYNC_API_KEY":       "",
    "TENANT_SLUG":        "",
    "TENANT_NOMBRE":      "",
    "PG_HOST":            "",
    "PG_PORT":            "5432",
    "PG_DBNAME":          "",
    "PG_USER":            "",
    "PG_PASSWORD":        "",
    "LOCAL_DB_PATH":      "",
    "SYNC_INTERVAL_SEC":  "30",
    "AUTO_UPDATE_CHECK":  "true",
}


def conf_path(base_dir: Path) -> Path:
    return Path(base_dir) / CONF_FILENAME


def load(base_dir: Path) -> dict[str, str]:
    """Carga el archivo. Robusto a archivo faltante o líneas malformadas."""
    data = deepcopy(DEFAULTS)
    path = conf_path(base_dir)
    if not path.exists():
        return data
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in DEFAULTS:
            data[k] = v
    return data


def save(base_dir: Path, conf: dict[str, str]) -> Path:
    """Persiste el conf como dotenv en %APPDATA%\\CyberShopNative\\.cybershop.conf."""
    path = conf_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Configuración del POS Desktop — generada por el asistente o por F7"]
    for k in DEFAULTS:
        v = conf.get(k, DEFAULTS[k])
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def update(base_dir: Path, **fields: Any) -> dict[str, str]:
    current = load(base_dir)
    for k, v in fields.items():
        if k in DEFAULTS:
            current[k] = "" if v is None else str(v)
    save(base_dir, current)
    return current


def is_configured(conf: dict[str, str]) -> bool:
    """True si tiene lo mínimo para hablar con el servidor."""
    return bool(conf.get("SERVER_URL") and conf.get("SYNC_API_KEY"))


def auto_update_enabled(conf: dict[str, str]) -> bool:
    return (conf.get("AUTO_UPDATE_CHECK", "true") or "").lower() in ("1", "true", "yes", "si", "sí")


def sync_interval_sec(conf: dict[str, str]) -> int:
    raw = conf.get("SYNC_INTERVAL_SEC", "30")
    try:
        return max(5, int(raw))
    except (TypeError, ValueError):
        return 30
