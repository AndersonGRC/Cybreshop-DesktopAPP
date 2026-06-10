"""Sistema de marca para CyberShop Desktop.

Centraliza colores, datos de empresa y logo en un solo archivo JSON
persistente en %APPDATA%/CyberShopNative/branding.json (o el fallback
local junto a la BD).

Para clonar la configuracion en otra instalacion: copiar branding.json.

Las claves estan agrupadas en:
- empresa: nombre, slogan, email, telefono, direccion, website, logo_path,
           recibo_pie, ventana_titulo
- colores: primario, primario_oscuro, acento, acento_secundario, peligro,
           sidebar_inicio, sidebar_fin, fondo, superficie
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import Any

DEFAULTS: dict[str, dict[str, str]] = {
    "empresa": {
        "nombre": "CyberShop",
        "slogan": "Panel administrativo local sin internet",
        "email": "",
        "telefono": "",
        "direccion": "",
        "website": "",
        "logo_path": "",
        "recibo_pie": "Gracias por su compra.",
        "ventana_titulo": "CyberShop Desktop Offline",
    },
    "colores": {
        "primario": "#122C94",
        "primario_oscuro": "#091C5A",
        "acento": "#fb8500",
        "acento_secundario": "#a6c438",
        "peligro": "#b42318",
        "sidebar_inicio": "#091C5A",
        "sidebar_fin": "#122C94",
        "fondo": "#f8faff",
        "superficie": "#ffffff",
    },
}

# Reglas de validacion ligeras: claves obligatorias por grupo (las que pueden
# romper QSS si faltan o quedan vacias). El resto puede quedar en blanco.
REQUIRED_COLOR_KEYS = {
    "primario",
    "primario_oscuro",
    "acento",
    "acento_secundario",
    "peligro",
    "sidebar_inicio",
    "sidebar_fin",
    "fondo",
    "superficie",
}


def branding_file(base_dir: Path) -> Path:
    return Path(base_dir) / "branding.json"


def load_branding(base_dir: Path) -> dict[str, Any]:
    """Carga branding.json fusionando con defaults. Si falta o esta corrupto,
    devuelve los defaults."""
    path = branding_file(base_dir)
    data: dict[str, Any] = deepcopy(DEFAULTS)
    if not path.exists():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data

    for group, defaults_group in DEFAULTS.items():
        if group not in raw or not isinstance(raw[group], dict):
            continue
        for key, default_value in defaults_group.items():
            value = raw[group].get(key, default_value)
            if not isinstance(value, str):
                value = default_value
            data[group][key] = value.strip() if value else value
    _normalize(data)
    return data


def save_branding(base_dir: Path, branding: dict[str, Any]) -> Path:
    """Guarda branding al disco. Garantiza que todas las claves esten presentes
    y los colores tengan formato valido (#RRGGBB)."""
    payload = deepcopy(DEFAULTS)
    for group, defaults_group in DEFAULTS.items():
        for key in defaults_group:
            value = (branding.get(group, {}) or {}).get(key, defaults_group[key])
            payload[group][key] = (value or "").strip()
    _normalize(payload)
    _validate(payload)
    path = branding_file(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def reset_branding(base_dir: Path) -> dict[str, Any]:
    """Borra branding.json y retorna defaults."""
    path = branding_file(base_dir)
    if path.exists():
        path.unlink()
    return deepcopy(DEFAULTS)


def apply_remote_branding(base_dir: Path, remote: dict[str, Any], download_logo=None) -> dict[str, Any]:
    """Aplica el branding traído del servidor (/api/v1/sync/branding).

    Args:
      base_dir: directorio de datos (%APPDATA%/CyberShopNative o fallback)
      remote: dict con shape {empresa: {...}, colores: {...}, logo_url: ...}
      download_logo: callable(url, dest_path) opcional para bajar el logo.
                     Si None, el logo no se descarga (pero el dict lo refleja).

    Política:
      - Solo sobrescribe claves que vienen con valor no vacío en el remote.
      - Las claves vacías del remote se IGNORAN (preservan el valor local).
      - Si descarga logo exitosamente, actualiza empresa.logo_path al path local.

    Returns: el branding final guardado.
    """
    current = load_branding(base_dir)

    # Empresa: merge no destructivo
    remote_empresa = remote.get("empresa") or {}
    if isinstance(remote_empresa, dict):
        for k, v in remote_empresa.items():
            if k in current["empresa"] and isinstance(v, str) and v.strip():
                current["empresa"][k] = v.strip()

    # Colores: merge respetando solo claves válidas y formato hex
    remote_colores = remote.get("colores") or {}
    if isinstance(remote_colores, dict):
        for k, v in remote_colores.items():
            if k in current["colores"] and isinstance(v, str) and _is_hex_color(v):
                current["colores"][k] = v

    # Logo: descargar si nos dieron función de descarga + URL
    logo_url = remote.get("logo_url") or ""
    if logo_url and download_logo:
        local_logo = Path(base_dir) / "logo_remote.png"
        try:
            download_logo(logo_url, local_logo)
            if local_logo.exists() and local_logo.stat().st_size > 0:
                current["empresa"]["logo_path"] = str(local_logo)
        except Exception:
            pass  # logo es opcional, no romper si falla

    save_branding(base_dir, current)
    return current


def render_qss(branding: dict[str, Any], template: str) -> str:
    """Sustituye placeholders $primario, $acento, etc. en una plantilla QSS.

    Tambien expone derivadas '<color>_rgb' como 'R, G, B' para usar en rgba()
    sin tener que hardcodear los componentes.
    """
    flat: dict[str, str] = {**branding["colores"]}
    for key, value in list(flat.items()):
        rgb = _hex_to_rgb_str(value)
        if rgb:
            flat[f"{key}_rgb"] = rgb
    return Template(template).safe_substitute(**flat)


def _hex_to_rgb_str(value: str) -> str:
    if not _is_hex_color(value):
        return ""
    hex_part = value.lstrip("#")
    if len(hex_part) == 3:
        hex_part = "".join(ch * 2 for ch in hex_part)
    if len(hex_part) >= 6:
        r = int(hex_part[0:2], 16)
        g = int(hex_part[2:4], 16)
        b = int(hex_part[4:6], 16)
        return f"{r}, {g}, {b}"
    return ""


def _normalize(branding: dict[str, Any]) -> None:
    for key in REQUIRED_COLOR_KEYS:
        value = branding["colores"].get(key) or DEFAULTS["colores"][key]
        if not value.startswith("#"):
            value = "#" + value
        branding["colores"][key] = value
    if not branding["empresa"].get("nombre"):
        branding["empresa"]["nombre"] = DEFAULTS["empresa"]["nombre"]
    if not branding["empresa"].get("ventana_titulo"):
        branding["empresa"]["ventana_titulo"] = (
            f"{branding['empresa']['nombre']} Desktop"
        )


def _validate(branding: dict[str, Any]) -> None:
    for key in REQUIRED_COLOR_KEYS:
        value = branding["colores"][key]
        if not _is_hex_color(value):
            raise ValueError(f"Color '{key}' invalido: '{value}'. Use formato #RRGGBB.")


def _is_hex_color(value: str) -> bool:
    if not value or not value.startswith("#"):
        return False
    hex_part = value[1:]
    if len(hex_part) not in (3, 6, 8):
        return False
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False
