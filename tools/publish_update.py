"""publish_update.py — genera/actualiza el manifiesto version.json del POS Desktop.

Cierra el pipeline de `build_installer.bat`: calcula el checksum SHA-256 del
instalador y escribe `static/installers/version.json` (lo que sirve
`/api/v1/sync/version` y consume el auto-update de la app instalada).

Uso típico (tras build_installer.bat):
    # versión normal (opcional para el usuario)
    python tools/publish_update.py --version 1.0.1 --notes "Mejoras y correcciones"

    # PARCHE DE SEGURIDAD obligatorio para TODOS los clientes con versión menor
    python tools/publish_update.py --version 1.0.2 --min-required 1.0.2 \
        --notes "Parche de seguridad crítico"

Luego subir al servidor (ambos a /var/www/CyberShop/app/static/installers/):
    - CyberShopSetup_base.exe   (el instalador nuevo)
    - version.json              (este manifiesto)

IMPORTANTE: `--version` debe COINCIDIR con APP_VERSION del build (main.py), que
es la versión que quedará "horneada" en el .exe.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent          # CyberShopDesktop/
INSTALLERS_DIR = PROJECT_ROOT.parent / "CyberShop" / "app" / "static" / "installers"
DEFAULT_FILENAME = "CyberShopSetup_base.exe"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version_in_main() -> str:
    try:
        txt = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', txt)
        return m.group(1) if m else ""
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser(description="Publica una versión del POS Desktop (genera version.json).")
    ap.add_argument("--version", required=True, help="versión que se publica (debe igualar APP_VERSION del build).")
    ap.add_argument("--min-required", default="0.0.0",
                    help="versión mínima OBLIGATORIA. Para un parche de seguridad, ponla igual a --version "
                         "(fuerza la actualización en clientes con versión menor). Default 0.0.0 = no forzar.")
    ap.add_argument("--notes", default="", help="notas de versión que verá el usuario.")
    ap.add_argument("--exe", default="", help="ruta al instalador. Default: static/installers/CyberShopSetup_base.exe")
    ap.add_argument("--filename", default=DEFAULT_FILENAME,
                    help="nombre con el que se sirve el instalador en static/installers/.")
    ap.add_argument("--out", default="", help="dónde escribir version.json. Default: junto al instalador.")
    a = ap.parse_args()

    exe = Path(a.exe) if a.exe else (INSTALLERS_DIR / a.filename)
    if not exe.is_file():
        print(f"ERROR: no existe el instalador: {exe}\n"
              f"Corré build_installer.bat primero, o pasá --exe.", file=sys.stderr)
        sys.exit(1)

    baked = app_version_in_main()
    if baked and baked != a.version:
        print(f"AVISO: APP_VERSION en main.py es '{baked}' pero estás publicando '{a.version}'. "
              f"Asegurate de buildear con APP_VERSION = '{a.version}'.", file=sys.stderr)

    checksum = sha256_file(exe)
    manifest = {
        "latest": a.version,
        "min_required": a.min_required,
        "download_url": f"/static/installers/{a.filename}",
        "checksum_sha256": checksum,
        "release_notes": a.notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    out = Path(a.out) if a.out else (exe.parent / "version.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    forced = a.min_required and a.min_required != "0.0.0"
    print("version.json escrito en:", out)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("\nSHA-256:", checksum)
    print("Tipo   :", "OBLIGATORIO (parche de seguridad)" if forced else "opcional")
    print("\nSiguiente paso: subir al servidor (ambos archivos):")
    print(f"  - {exe.name}  ->  /var/www/CyberShop/app/static/installers/{a.filename}")
    print(f"  - version.json ->  /var/www/CyberShop/app/static/installers/version.json")


if __name__ == "__main__":
    main()
