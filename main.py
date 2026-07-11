import shutil
import sys
import time
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QDate, QEvent, QLockFile, QObject, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import branding as branding_mod
import cybershop_conf as install_conf
import sync_config as sync_cfg
from local_store import LocalStore, app_data_dir
from sync_client import SyncClient, SyncError


APP_VERSION = "1.0.0.3"
ROLES = ["Administrador", "Empleado", "Cajero", "Mesero", "Contador"]

# Módulos visibles por rol — espejo de los grupos de permisos de security.py
# del backend (ADMIN_FULL, ADMIN_STAFF, POS_OPERATIONAL, RESTAURANT_*…).
# Claves = keys de NAV_ITEMS.
ROLE_MODULES = {
    "Administrador": {"dashboard", "pos", "restaurant", "sales", "products", "inventory", "contabilidad", "quotes", "cobros", "crm", "payroll", "ia", "users", "sync", "config"},
    "Empleado":      {"dashboard", "pos", "restaurant", "sales", "products", "inventory", "quotes", "crm", "ia"},
    "Cajero":        {"dashboard", "pos", "restaurant", "sales"},
    "Mesero":        {"dashboard", "pos", "restaurant"},
    "Contador":      {"dashboard", "sales", "contabilidad", "cobros", "payroll"},
    "Cliente":       {"dashboard"},               # no debería operar el POS
    # Compatibilidad con el mapeo viejo
    "Inventario":    {"dashboard", "products", "inventory", "sales"},
}
DEFAULT_ROLE_MODULES = {"dashboard", "pos"}  # fallback prudente para roles desconocidos

# Gating por PLAN del tenant: config_key de cliente_config (web) -> nav key del
# desktop. El maestro (CyberShopAdmin) activa/desactiva estos flags por plan; el
# desktop los pulea en /api/v1/sync/config y oculta los módulos fuera del plan.
# 'sales' (historial de recibos POS) pertenece al POS, por eso comparte
# pos_habilitado y NO el módulo web 'orders'/'pedidos'.
TENANT_MODULE_MAP = {
    "pos":          "pos_habilitado",
    "sales":        "pos_habilitado",
    "restaurant":   "restaurant_tables_habilitado",
    "products":     "inventario_habilitado",
    "inventory":    "inventario_habilitado",
    "contabilidad": "contabilidad_habilitada",
    "users":        "usuarios_habilitado",
    "quotes":       "cotizaciones_habilitado",
    "cobros":       "cuentas_cobro_habilitado",
    "crm":          "crm_habilitado",
    "payroll":      "nomina_habilitada",
    "ia":           "ia_habilitado",
}
# Módulos de sistema: nunca dependen del plan (el usuario debe poder
# sincronizar/configurar para des-restringirse a sí mismo).
SYSTEM_MODULES = {"dashboard", "sync", "config"}


def modules_for_role(role: str) -> set:
    return ROLE_MODULES.get((role or "").strip(), DEFAULT_ROLE_MODULES)


def tenant_allowed_modules(cached_flags) -> set | None:
    """Set de nav keys permitidas por el PLAN del tenant. None si no hay flags
    cacheados todavía (=> sin restricción, fail-open)."""
    if not cached_flags:                       # None o {} -> sin restricción
        return None
    allowed = set(SYSTEM_MODULES)
    for nav_key, config_key in TENANT_MODULE_MAP.items():
        val = cached_flags.get(config_key)
        if val is None or bool(val):           # clave ausente => permitido (fail-open por clave)
            allowed.add(nav_key)
    return allowed


def _asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


def _default_app_icon() -> QIcon:
    ico = _asset_path("cybershop.ico")
    if ico.is_file():
        return QIcon(str(ico))
    png = _asset_path("cybershop.png")
    return QIcon(str(png)) if png.is_file() else QIcon()


def _version_cmp(a: str, b: str) -> int:
    """Compara dos versiones tipo '1.2.3'. Devuelve -1, 0, 1 (a < b, a == b, a > b).
    Tolerante a partes no numéricas y diferentes longitudes."""
    def parse(v):
        out = []
        for chunk in (v or "").split("."):
            try:
                out.append(int(chunk))
            except ValueError:
                # parte no numérica → tratar como 0 + sufijo string
                out.append(0)
        return out
    pa, pb = parse(a), parse(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def _now_iso_local() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


class _BgWorker(QThread):
    """Ejecuta una función (llamada de red) en un hilo aparte para no congelar
    la UI. Emite `done(objeto)` en éxito o `failed(str)` en error. Reutilizable
    para IA (respuestas del LLM tardan) y para el sync bidireccional."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 — se propaga a la UI vía señal
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


def _ai_build_client():
    """Construye un SyncClient desde sync_config para hablar con la IA (proxy).
    Devuelve None si el sync no está configurado (sin URL/API key)."""
    state = sync_cfg.load(app_data_dir())
    base_url = state.get("base_url", "")
    api_key = state.get("api_key", "")
    if not base_url or not api_key:
        return None
    try:
        return SyncClient(base_url, api_key)
    except ValueError:
        return None


# =============================================================================
# Stylesheet template (con placeholders $primario, etc.)
# =============================================================================
QSS_TEMPLATE = """
/* ═══════════════════════════════════════════════════════════════
   CyberShop POS Desktop — Design System
   Tokens (vienen de branding.json):
     $primario, $primario_oscuro, $acento, $acento_secundario,
     $peligro, $sidebar_inicio, $sidebar_fin, $fondo, $superficie
     y sus _rgb (R, G, B) para usar en rgba().
   Convenciones:
     - Radios: 8px chips, 12px botones/inputs, 16-22px tarjetas
     - Pesos: 500 cuerpo, 600 etiquetas, 700 labels destacados,
              800 títulos de sección, 850 hero/metric values
     - Tipografía: Segoe UI Variable cuando esté, fallback a Segoe UI
   ════════════════════════════════════════════════════════════════ */

/* ─── Base ──────────────────────────────────────────────────────── */
QWidget {
    font-family: "Segoe UI Variable Display", "Segoe UI", Tahoma, sans-serif;
    font-size: 14px;
    color: #1f2937;
    background: $fondo;
}
/* Los QLabel son transparentes por defecto para que se vea el fondo real de
   la tarjeta/panel que los contiene. Sin esto, cada label pinta un rectangulo
   opaco con el $fondo global y aparecen "parches" de color dentro de las
   tarjetas. Los labels que necesitan fondo propio (syncBadge, userAvatar,
   moduleCardIcon...) lo declaran via su selector de id, que tiene mayor
   especificidad y gana sobre esta regla. */
QLabel { background: transparent; }
QToolTip {
    background: #1f2937;
    color: #f9fafb;
    border: 0;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}

/* ─── Sidebar ───────────────────────────────────────────────────── */
QFrame#sidebar {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 $sidebar_inicio,
        stop:1 $sidebar_fin
    );
    border-right: 1px solid rgba(0,0,0,0.18);
}
/* Todos los QLabel dentro del sidebar deben ser transparentes
   para que el degradado azul de QFrame#sidebar se vea por debajo
   y el texto blanco no quede oculto por el fondo cream global. */
QFrame#sidebar QLabel { background: transparent; }
QFrame#userCard QLabel { background: transparent; }
QLabel#brand {
    color: #ffffff;
    background: transparent;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: 0.2px;
    padding: 2px 0 0 2px;
}
QLabel#brandSubtitle {
    color: rgba(255,255,255,0.92);
    background: transparent;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2.2px;
    text-transform: uppercase;
    padding: 0 0 10px 2px;
}
QLabel#sidebarSection {
    color: rgba(255,255,255,0.95);
    background: transparent;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 2.0px;
    text-transform: uppercase;
    padding: 16px 8px 6px 8px;
    border-bottom: 1px solid rgba(255,255,255,0.10);
    margin: 0 4px 4px 4px;
}
QLabel#syncBadge {
    color: #ffffff;
    background: rgba(255,255,255,0.10);
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 999px;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
}
QLabel#syncBadge[state="online"] {
    background: rgba($acento_secundario_rgb, 0.22);
    border: 1px solid rgba($acento_secundario_rgb, 0.45);
}
QLabel#syncBadge[state="syncing"] {
    background: rgba(255, 184, 0, 0.20);
    border: 1px solid rgba(255, 184, 0, 0.45);
}
QLabel#syncBadge[state="offline"] {
    background: rgba($peligro_rgb, 0.20);
    border: 1px solid rgba($peligro_rgb, 0.45);
    color: #ffe7e2;
}
QFrame#userCard {
    background: rgba(255,255,255,0.10);
    border: 1px solid rgba(255,255,255,0.20);
    border-radius: 12px;
}
QLabel#userAvatar {
    background: $acento;
    color: #ffffff;
    border-radius: 18px;
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    max-height: 36px;
    font-size: 14px;
    font-weight: 850;
    letter-spacing: 0.2px;
}
QLabel#userName {
    color: #ffffff;
    background: transparent;
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.1px;
}
QLabel#userRole {
    color: rgba(255,255,255,0.78);
    background: transparent;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#navButton {
    text-align: left;
    border: 0;
    border-left: 3px solid transparent;
    border-radius: 10px;
    padding: 9px 12px 9px 14px;
    background: transparent;
    color: rgba(255,255,255,0.92);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.1px;
    min-height: 34px;
}
QPushButton#navButton:hover {
    background: rgba(255,255,255,0.12);
    color: #ffffff;
}
QPushButton#navButton[active="true"] {
    background: rgba(255,255,255,0.22);
    color: #ffffff;
    border-left: 3px solid $acento;
    font-weight: 800;
}
QPushButton#logoutButton {
    text-align: left;
    border: 0;
    border-radius: 12px;
    padding: 12px 14px;
    background: transparent;
    color: rgba(255, 230, 226, 0.95);
    font-weight: 650;
}
QPushButton#logoutButton:hover {
    background: rgba($peligro_rgb, 0.28);
    color: #ffffff;
}

/* ─── Footer ────────────────────────────────────────────────────── */
QFrame#footer {
    background: #ffffff;
    border-top: 1px solid #e5e7eb;
}
QLabel#footerText {
    color: #6b7280;
    font-size: 11px;
    font-weight: 500;
}

/* ─── Tipografía ────────────────────────────────────────────────── */
QLabel#pageTitle {
    font-size: 26px;
    font-weight: 800;
    color: $primario_oscuro;
    letter-spacing: -0.3px;
    padding-bottom: 2px;
}
QLabel#pageSubtitle {
    color: #6b7280;
    font-size: 13px;
    font-weight: 500;
}
QLabel#muted {
    color: #6b7280;
    line-height: 1.55;
}
QLabel#eyebrow {
    color: $primario;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.6px;
    text-transform: uppercase;
}
QLabel#eyebrowMuted {
    color: #6b7280;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.4px;
    text-transform: uppercase;
}
QLabel#heroTitle {
    color: $primario_oscuro;
    font-size: 18px;
    font-weight: 800;
    letter-spacing: -0.1px;
}
QLabel#highlightText {
    color: $primario_oscuro;
    font-size: 13px;
    font-weight: 750;
}

/* ─── Tarjetas y paneles ────────────────────────────────────────── */
QFrame#metricCard, QFrame#sectionPanel, QFrame#loginCard {
    background: $superficie;
    border: 1px solid rgba($primario_rgb, 0.16);
    border-radius: 14px;
}
QFrame#metricCard {
    border-radius: 12px;
}
QFrame#metricCard:hover {
    border-color: rgba($primario_rgb, 0.32);
}
QLabel#metricValue {
    font-size: 26px;
    font-weight: 850;
    color: $primario;
    letter-spacing: -0.3px;
}
QLabel#metricValue[state="empty"] {
    color: #9ca3af;
    font-size: 18px;
    font-weight: 700;
}
QLabel#metricLabel {
    color: #6b7280;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}

/* ─── Dashboard hero ────────────────────────────────────────────── */
QFrame#dashboardHero {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $superficie,
        stop:1 rgba($primario_rgb, 0.05)
    );
    border: 1px solid rgba($primario_rgb, 0.18);
    border-radius: 14px;
}
QFrame#heroHighlight {
    background: rgba($primario_rgb, 0.06);
    border: 1px solid rgba($primario_rgb, 0.18);
    border-left: 3px solid $acento;
    border-radius: 10px;
}

/* ─── Module grid (atajos a módulos) ─────────────────────────── */
QFrame#moduleCard {
    background: $superficie;
    border: 1px solid rgba($primario_rgb, 0.16);
    border-radius: 12px;
}
QFrame#moduleCard:hover {
    border-color: rgba($primario_rgb, 0.40);
    background: rgba($primario_rgb, 0.04);
}
QLabel#moduleCardIcon {
    background: rgba($primario_rgb, 0.10);
    color: $primario;
    border-radius: 10px;
    min-width: 38px;
    max-width: 38px;
    min-height: 38px;
    max-height: 38px;
    font-size: 18px;
    font-weight: 800;
}
QLabel#moduleCardTitle {
    color: $primario_oscuro;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.1px;
}
QLabel#moduleCardDetail {
    color: #6b7280;
    font-size: 11px;
    font-weight: 600;
}
QLabel#moduleCardShortcut {
    color: #9ca3af;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.0px;
}

/* ─── POS scanner ───────────────────────────────────────────────── */
QFrame#scannerPanel {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 $primario,
        stop:1 $primario_oscuro
    );
    border-radius: 18px;
    border: 1px solid rgba($acento_secundario_rgb, 0.45);
}
QFrame#scannerPanel QLabel { color: #ffffff; }
QLabel#scannerIcon {
    color: $acento_secundario;
    font-family: "Consolas", monospace;
    font-size: 26px;
    font-weight: 900;
    letter-spacing: 1px;
}
QLineEdit#barcodeInput {
    background: rgba(255,255,255,0.97);
    border: 2px solid $acento_secundario;
    border-radius: 12px;
    padding: 6px 14px;
    font-size: 16px;
    font-weight: 700;
    color: $primario_oscuro;
    min-height: 42px;
}
QLineEdit#barcodeInput:focus {
    border-color: $acento;
    background: #ffffff;
}
QLabel#scannerBadge {
    background: rgba(160,160,160,0.18);
    color: #6b7280;
    border: 1px solid rgba(160,160,160,0.45);
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.4px;
    text-transform: uppercase;
}
QLabel#scannerBadge[state="on"] {
    background: rgba($acento_secundario_rgb, 0.20);
    color: #5a7a14;
    border: 1px solid rgba($acento_secundario_rgb, 0.55);
}
QLabel#scannerBadge[state="off"] {
    background: rgba($peligro_rgb, 0.12);
    color: $peligro;
    border: 1px solid rgba($peligro_rgb, 0.32);
}

/* ─── Botones ───────────────────────────────────────────────────── */
QPushButton {
    min-height: 42px;
    border: 0;
    border-radius: 12px;
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 $primario,
        stop:1 $primario_oscuro
    );
    color: #ffffff;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.2px;
    padding: 0 16px;
}
QPushButton:hover { background: $primario_oscuro; }
QPushButton:pressed { background: $primario_oscuro; padding-top: 1px; }
QPushButton:disabled {
    background: #f3f4f6;
    color: #6b7280;
    border: 1px solid #e5e7eb;
}
QPushButton#primaryAction {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 $acento_secundario,
        stop:1 $acento_secundario
    );
    color: $primario_oscuro;
    font-weight: 800;
}
QPushButton#primaryAction:hover {
    background: $acento_secundario;
    color: #ffffff;
}
QPushButton#secondaryAction {
    background: transparent;
    border: 1px solid rgba($primario_rgb, 0.32);
    color: $primario;
    font-weight: 650;
}
QPushButton#secondaryAction:hover {
    background: rgba($primario_rgb, 0.08);
    border-color: $primario;
}
QPushButton#dangerAction {
    background: transparent;
    border: 1px solid rgba($peligro_rgb, 0.40);
    color: $peligro;
    font-weight: 650;
}
QPushButton#dangerAction:hover {
    background: rgba($peligro_rgb, 0.10);
    border-color: $peligro;
}
QPushButton#inlineDanger {
    background: rgba($peligro_rgb, 0.10);
    color: $peligro;
    border: 1px solid rgba($peligro_rgb, 0.30);
    border-radius: 8px;
    min-height: 28px;
    padding: 0 10px;
    font-weight: 700;
}
QPushButton#inlineDanger:hover { background: rgba($peligro_rgb, 0.18); }

/* ─── Login ─────────────────────────────────────────────────────── */
QWidget#loginRoot {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $fondo,
        stop:0.5 rgba($primario_rgb, 0.07),
        stop:1 $fondo
    );
}
QFrame#loginCard {
    border-radius: 20px;
    border: 1px solid rgba($primario_rgb, 0.20);
}
QLabel#loginTitle {
    font-size: 26px;
    font-weight: 850;
    color: $primario_oscuro;
    letter-spacing: -0.3px;
}
QLabel#loginHint {
    color: #4b5563;
    font-size: 12px;
    font-weight: 500;
    background: rgba($primario_rgb, 0.06);
    border: 1px solid rgba($primario_rgb, 0.18);
    border-radius: 8px;
    padding: 8px 12px;
}

/* ─── Inputs ────────────────────────────────────────────────────── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 42px;
    border: 1px solid #d1d5db;
    border-radius: 12px;
    padding: 0 12px;
    background: #ffffff;
    color: #1f2937;
    selection-background-color: rgba($primario_rgb, 0.30);
    selection-color: #ffffff;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
    border-color: #9ca3af;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: $primario;
    background: #ffffff;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    background: #f3f4f6;
    color: #6b7280;
    border-color: #e5e7eb;
}
QCheckBox {
    spacing: 8px;
    font-weight: 600;
    color: #374151;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1.5px solid #d1d5db;
    border-radius: 5px;
    background: #ffffff;
}
QCheckBox::indicator:hover { border-color: $primario; }
QCheckBox::indicator:checked {
    background: $primario;
    border-color: $primario;
}

/* ─── Tablas ────────────────────────────────────────────────────── */
QTableWidget {
    background: #ffffff;
    border: 1px solid rgba($primario_rgb, 0.18);
    border-radius: 12px;
    gridline-color: #e5e7eb;
    alternate-background-color: rgba($primario_rgb, 0.035);
    selection-background-color: rgba($primario_rgb, 0.18);
    selection-color: $primario_oscuro;
}
QTableWidget::item { padding: 13px 12px; }
QHeaderView::section {
    background: rgba($primario_rgb, 0.08);
    color: #374151;
    border: 0;
    border-bottom: 1px solid rgba($primario_rgb, 0.20);
    padding: 13px 12px;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    font-size: 11px;
}

/* ─── Tabs ──────────────────────────────────────────────────────── */
QTabBar::tab {
    background: transparent;
    color: #6b7280;
    padding: 10px 18px;
    border: 0;
    border-bottom: 2px solid transparent;
    font-weight: 650;
}
QTabBar::tab:hover { color: $primario; }
QTabBar::tab:selected {
    color: $primario;
    border-bottom: 2px solid $primario;
    font-weight: 750;
}

/* ─── ScrollBars ────────────────────────────────────────────────── */
QScrollBar:vertical {
    border: 0;
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba($primario_rgb, 0.20);
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: rgba($primario_rgb, 0.40); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    border: 0;
    background: transparent;
    height: 10px;
    margin: 2px 4px 2px 4px;
}
QScrollBar::handle:horizontal {
    background: rgba($primario_rgb, 0.20);
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: rgba($primario_rgb, 0.40); }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ─── Live toggle (cache ↔ VPS en vivo) ─────────────────────────── */
QCheckBox#liveToggle {
    spacing: 8px;
    color: #4b5563;
    font-weight: 700;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    font-size: 11px;
}
QCheckBox#liveToggle::indicator {
    width: 36px;
    height: 18px;
    border-radius: 9px;
    background: #e5e7eb;
    border: 1px solid #d1d5db;
}
QCheckBox#liveToggle::indicator:hover {
    border-color: $primario;
}
QCheckBox#liveToggle::indicator:checked {
    background: $acento_secundario;
    border: 1px solid $acento_secundario;
}

/* ─── Diálogos: paleta consistente ──────────────────────────────── */
QDialog {
    background: $fondo;
}

/* ─── Form labels (etiquetas de campos en formularios) ──────────── */
QLabel#formLabel {
    color: #4b5563;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.3px;
}

/* ─── Page badge (chips en superficies claras: barras de acciones) ─ */
QLabel#pageBadge {
    color: $primario_oscuro;
    background: rgba($primario_rgb, 0.10);
    border: 1px solid rgba($primario_rgb, 0.25);
    border-radius: 999px;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
}
QLabel#pageBadge[state="warning"] {
    color: #92400e;
    background: rgba(255, 184, 0, 0.18);
    border: 1px solid rgba(255, 184, 0, 0.50);
}
QLabel#pageBadge[state="success"] {
    color: #1f6f2e;
    background: rgba($acento_secundario_rgb, 0.18);
    border: 1px solid rgba($acento_secundario_rgb, 0.50);
}
QLabel#pageBadge[state="danger"] {
    color: $peligro;
    background: rgba($peligro_rgb, 0.10);
    border: 1px solid rgba($peligro_rgb, 0.35);
}

/* ─── Misc ──────────────────────────────────────────────────────── */
QFrame#colorSwatch {
    border: 1px solid rgba(0,0,0,0.14);
    border-radius: 8px;
    min-width: 30px;
    min-height: 30px;
    max-width: 30px;
    max-height: 30px;
}
QFrame#divider {
    background: rgba($primario_rgb, 0.10);
    max-height: 1px;
    min-height: 1px;
}

/* ─── Pagina de Configuracion / Marca ───────────────────────────── */
QScrollArea#configScroll {
    border: 0;
    background: transparent;
}
QScrollArea#configScroll > QWidget > QWidget { background: transparent; }
QScrollArea#dashboardScroll {
    border: 0;
    background: transparent;
}
QScrollArea#dashboardScroll > QWidget > QWidget { background: transparent; }
QLabel#sectionTitle {
    color: $primario_oscuro;
    font-size: 16px;
    font-weight: 800;
    letter-spacing: 0.2px;
}
QLabel#sectionDesc {
    color: #6b7280;
    font-size: 12px;
    font-weight: 500;
}
QLabel#colorGroupLabel {
    color: $primario;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.4px;
    text-transform: uppercase;
}
QLabel#colorHint {
    color: #9ca3af;
    font-size: 11px;
    font-weight: 500;
}
QLabel#fieldLabel {
    color: #374151;
    font-size: 13px;
    font-weight: 600;
}
QFrame#actionBar {
    background: $superficie;
    border-top: 1px solid rgba($primario_rgb, 0.16);
}
QLineEdit#hexInput {
    font-family: "Cascadia Mono", "Consolas", monospace;
    letter-spacing: 0.5px;
}

/* ─── Restaurante: salón y mesas ─────────────────────────────────── */
QFrame#rtTableCard {
    background: $superficie;
    border: 1px solid rgba($primario_rgb, 0.16);
    border-left: 5px solid rgba($primario_rgb, 0.30);
    border-radius: 12px;
}
QFrame#rtTableCard[estado="disponible"] { border-left-color: $acento_secundario; }
QFrame#rtTableCard[estado="ocupada"]    { border-left-color: $acento; background: rgba($acento_rgb, 0.05); }
QFrame#rtTableCard[estado="reservada"]  { border-left-color: $primario; background: rgba($primario_rgb, 0.05); }
QFrame#rtTableCard[estado="cuenta_solicitada"] { border-left-color: $peligro; background: rgba($peligro_rgb, 0.06); }
QFrame#rtTableCard[selected="true"] {
    border: 2px solid $primario;
    border-left: 5px solid $primario;
}
QLabel#rtCardName { font-size: 15px; font-weight: 800; color: $primario_oscuro; }
QLabel#rtCardBadge {
    font-size: 10px; font-weight: 800; color: #6b7280;
    text-transform: uppercase; letter-spacing: 0.6px;
}
QLabel#rtCardMeta { font-size: 11px; color: #6b7280; font-weight: 600; }
QLabel#rtCardTotal { font-size: 20px; font-weight: 850; color: $primario; letter-spacing: -0.3px; }
QLabel#rtCardFree { font-size: 13px; font-weight: 700; color: $acento_secundario; }
QLabel#rtCardPending { font-size: 10px; font-weight: 700; color: $acento; }

QLabel#rtDetailTitle { font-size: 18px; font-weight: 800; color: $primario_oscuro; }
QLabel#rtDetailTotal { font-size: 18px; font-weight: 850; color: $primario; padding: 4px 0; }
QFrame#rtAddBox {
    background: rgba($primario_rgb, 0.04);
    border: 1px solid rgba($primario_rgb, 0.14);
    border-radius: 10px;
}
QFrame#rtConsRow {
    background: $superficie;
    border: 1px solid rgba($primario_rgb, 0.10);
    border-radius: 8px;
}
QLabel#rtConsText { font-size: 13px; font-weight: 600; color: #1f2937; }
QLabel#rtConsSub { font-size: 13px; font-weight: 800; color: $primario_oscuro; }
QPushButton#rtStateBtn {
    border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: 800;
    border: 1px solid rgba($primario_rgb, 0.20); color: $primario_oscuro;
    background: rgba($primario_rgb, 0.06);
}
QPushButton#rtStateBtn[estado="pendiente"]  { background: rgba($peligro_rgb, 0.12); border-color: rgba($peligro_rgb, 0.35); color: $peligro; }
QPushButton#rtStateBtn[estado="preparando"] { background: rgba(255, 184, 0, 0.18); border-color: rgba(255, 184, 0, 0.45); color: #8a5a00; }
QPushButton#rtStateBtn[estado="servido"]    { background: rgba($acento_secundario_rgb, 0.20); border-color: rgba($acento_secundario_rgb, 0.50); color: $primario_oscuro; }
QPushButton#rtStateBtn:hover { border-color: $primario; }
QLabel#rtSyncLabel { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 999px; }
QLabel#rtSyncLabel[state="ok"] { color: $primario_oscuro; background: rgba($acento_secundario_rgb, 0.18); }
QLabel#rtSyncLabel[state="pending"] { color: $acento; background: rgba($acento_rgb, 0.14); }
QLabel#rtConsNotas { font-size: 11px; color: #6b7280; font-weight: 500; }
QLabel#rtHint { font-size: 11px; color: #6b7280; font-weight: 500; padding-bottom: 2px; }
/* Pill de estado en el panel de detalle */
QLabel#rtStatePill {
    font-size: 11px; font-weight: 800; padding: 4px 12px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.4px;
    color: #6b7280; background: rgba(0,0,0,0.06);
}
QLabel#rtStatePill[estado="disponible"] { color: $primario_oscuro; background: rgba($acento_secundario_rgb, 0.22); }
QLabel#rtStatePill[estado="ocupada"]    { color: #8a5a00; background: rgba($acento_rgb, 0.18); }
QLabel#rtStatePill[estado="reservada"]  { color: $primario; background: rgba($primario_rgb, 0.14); }
QLabel#rtStatePill[estado="cuenta_solicitada"] { color: $peligro; background: rgba($peligro_rgb, 0.14); }
/* Leyenda de colores */
QLabel#rtLegend { font-size: 12px; font-weight: 700; color: #6b7280; }
QLabel#rtLegend[estado="disponible"] { color: $acento_secundario; }
QLabel#rtLegend[estado="ocupada"]    { color: $acento; }
QLabel#rtLegend[estado="reservada"]  { color: $primario; }
QLabel#rtLegend[estado="cuenta_solicitada"] { color: $peligro; }
/* Barra de progreso de preparación */
QFrame#rtProgressBox {
    background: rgba($primario_rgb, 0.04);
    border: 1px solid rgba($primario_rgb, 0.12);
    border-radius: 10px;
}
QLabel#rtProgPct { font-size: 13px; font-weight: 850; color: $primario; }
QFrame#rtProgBar { min-height: 14px; max-height: 14px; border-radius: 7px; }
QFrame#rtProgSeg { min-height: 14px; }
QFrame#rtProgSeg[estado="servido"]    { background: $acento_secundario; }
QFrame#rtProgSeg[estado="preparando"] { background: #f5a623; }
QFrame#rtProgSeg[estado="pendiente"]  { background: rgba($peligro_rgb, 0.55); }
QFrame#rtProgSeg[estado="vacio"]      { background: rgba(0,0,0,0.08); }
QLabel#rtProgLegend { font-size: 11px; font-weight: 600; color: #6b7280; }
/* Recibo (vista cajero) */
QFrame#rtReceipt {
    background: $superficie;
    border: 1px solid rgba($primario_rgb, 0.14);
    border-radius: 10px;
}
QLabel#rtRcptQty { font-size: 14px; font-weight: 800; color: $primario; }
QLabel#rtRcptName { font-size: 14px; font-weight: 600; color: #1f2937; }
QLabel#rtRcptSub { font-size: 14px; font-weight: 800; color: $primario_oscuro; }
QLabel#rtRcptChip {
    font-size: 10px; font-weight: 800; padding: 2px 8px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.3px;
}
QLabel#rtRcptChip[estado="servido"]    { color: $primario_oscuro; background: rgba($acento_secundario_rgb, 0.22); }
QLabel#rtRcptChip[estado="preparando"] { color: #8a5a00; background: rgba(255,184,0,0.20); }
QLabel#rtRcptChip[estado="pendiente"]  { color: $peligro; background: rgba($peligro_rgb, 0.12); }
QLabel#rtCashierTotal { font-size: 32px; font-weight: 850; color: $primario; letter-spacing: -0.5px; }
"""


# =============================================================================
# Login
# =============================================================================
class LoginView(QWidget):
    def __init__(self, store: LocalStore, on_login, brand_callback):
        super().__init__()
        self.store = store
        self.on_login = on_login
        self._brand_callback = brand_callback
        self._build()
        self.apply_branding()

    def _build(self):
        self.setObjectName("loginRoot")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.addStretch(1)

        self.card = QFrame()
        self.card.setObjectName("loginCard")
        self.card.setFixedWidth(440)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(36, 32, 36, 32)
        card_layout.setSpacing(10)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setVisible(False)

        eyebrow = QLabel("BIENVENIDO")
        eyebrow.setObjectName("eyebrow")
        eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title = QLabel()
        self.title.setObjectName("loginTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        email_label = QLabel("Correo")
        email_label.setObjectName("eyebrowMuted")
        self.email = QLineEdit("admin@cybershop.local")
        self.email.setPlaceholderText("tu@correo.com")

        password_label = QLabel("Contraseña")
        password_label.setObjectName("eyebrowMuted")
        self.password = QLineEdit("admin123")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("••••••••")

        self.status = QLabel("Usa tu correo y contraseña del panel web. Local: admin@cybershop.local / admin123")
        self.status.setObjectName("loginHint")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button = QPushButton("Entrar")
        button.setObjectName("primaryAction")
        button.clicked.connect(self._login)
        self.password.returnPressed.connect(self._login)
        self.email.returnPressed.connect(self._login)

        card_layout.addWidget(self.logo_label)
        card_layout.addWidget(eyebrow)
        card_layout.addWidget(self.title)
        card_layout.addWidget(self.subtitle)
        card_layout.addSpacing(18)
        card_layout.addWidget(email_label)
        card_layout.addWidget(self.email)
        card_layout.addSpacing(4)
        card_layout.addWidget(password_label)
        card_layout.addWidget(self.password)
        card_layout.addSpacing(14)
        card_layout.addWidget(button)
        card_layout.addSpacing(6)
        card_layout.addWidget(self.status)

        layout.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

    def apply_branding(self):
        branding = self._brand_callback()
        empresa = branding["empresa"]
        self.title.setText(f"{empresa['nombre']}  POS")
        slogan = (empresa.get("slogan") or "").strip()
        self.subtitle.setText(slogan or "Inicia sesión para gestionar ventas, inventario y clientes.")
        logo_path = empresa.get("logo_path") or ""
        if logo_path and Path(logo_path).is_file():
            pix = QPixmap(logo_path)
            if not pix.isNull():
                pix = pix.scaledToHeight(72, Qt.TransformationMode.SmoothTransformation)
                self.logo_label.setPixmap(pix)
                self.logo_label.setVisible(True)
                return
        self.logo_label.clear()
        self.logo_label.setVisible(False)

    def _login(self):
        email = self.email.text().strip()
        password = self.password.text()

        # 1) Intento remoto contra el VPS (si hay sync configurado).
        try:
            remote_user = self._try_remote_login(email, password)
        except RuntimeError as exc:
            # El VPS rechazó las credenciales o el rol/estado: NO caer a local.
            self.status.setText(str(exc))
            self.status.setStyleSheet("color: #b42318;")
            return

        if remote_user is not None:
            try:
                user = self.store.cache_remote_login(remote_user, password)
            except Exception as exc:  # noqa: BLE001 — surface any persistence error
                self.status.setText(f"Error guardando sesión local: {exc}")
                self.status.setStyleSheet("color: #b42318;")
                return
            self.status.setStyleSheet("")
            self.on_login(user)
            return

        # 2) Fallback local (sync no configurado o sin red).
        user = self.store.authenticate(email, password)
        if not user:
            self.status.setText("Correo o contraseña incorrectos.")
            self.status.setStyleSheet("color: #b42318;")
            return
        self.status.setStyleSheet("")
        if user.must_change_password:
            dialog = ChangePasswordDialog(self, store=self.store, user=user, mandatory=True)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.status.setText("Debes cambiar la contraseña para continuar.")
                self.status.setStyleSheet("color: #b42318;")
                return
            user.must_change_password = False
        self.on_login(user)

    def _try_remote_login(self, email: str, password: str):
        """Intenta validar contra /api/v1/sync/auth.

        Retorna:
          - dict con datos del usuario si el VPS confirma las credenciales
          - None si el sync no está configurado o no hay red (caer a local)

        Levanta RuntimeError si el VPS rechazó el login con un mensaje
        que el caller debe mostrar tal cual (NO debe caer a local).
        """
        state = sync_cfg.load(app_data_dir())
        base_url = state.get("base_url", "")
        api_key = state.get("api_key", "")
        if not base_url or not api_key:
            return None  # Sync no configurado.
        try:
            client = SyncClient(base_url, api_key)
            result = client.remote_login(email, password)
        except SyncError as exc:
            if exc.status_code in (401, 403):
                raise RuntimeError(str(exc) or "Credenciales inválidas.") from exc
            return None  # Red caída o 5xx: cae a local.
        except (ValueError, OSError):
            return None
        return result.get("user") or {}


# =============================================================================
# Dialogo cambio de contrasena
# =============================================================================
class ChangePasswordDialog(QDialog):
    def __init__(self, parent, store: LocalStore, user, mandatory: bool = False):
        super().__init__(parent)
        self.store = store
        self.user = user
        self.mandatory = mandatory
        self.setWindowTitle("Cambiar contrasena")
        self.setMinimumWidth(400)
        if mandatory:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        form = QFormLayout(self)
        intro = QLabel(
            "Tu cuenta usa la contrasena por defecto.\nDefine una nueva (minimo 6 caracteres) para continuar."
            if mandatory else
            "Define una nueva contrasena para tu cuenta."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        form.addRow(intro)

        self.new_password = QLineEdit()
        self.new_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Nueva contrasena:", self.new_password)
        form.addRow("Confirmar:", self.confirm_password)

        self.feedback = QLabel("")
        form.addRow(self.feedback)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save)
        if not mandatory:
            buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
            buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._save)
        form.addRow(buttons)

    def _save(self):
        pwd = self.new_password.text()
        confirm = self.confirm_password.text()
        if pwd != confirm:
            self.feedback.setText("Las contrasenas no coinciden.")
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")
            return
        try:
            self.store.change_password(self.user.id, pwd)
        except ValueError as exc:
            self.feedback.setText(str(exc))
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")
            return
        self.accept()

    def reject(self):
        if self.mandatory:
            return  # bloquea cierre con Esc
        super().reject()


# =============================================================================
# Widgets compartidos: LiveToggle, OrderStatusDialog, CategoriesDialog
# =============================================================================
class LiveToggle(QWidget):
    """Pill switch que conmuta entre modo cache (default) y VPS en vivo.

    Uso:
        self.live = LiveToggle()
        self.live.toggled.connect(self._on_live_changed)
        # ...
        def _on_live_changed(self, enabled: bool):
            if enabled:
                try:
                    self._refresh_live()
                except Exception:
                    self.live.set_state(False)
                    self.live.show_error("Sin conexión")
            else:
                self.refresh()
    """

    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("liveToggleRoot")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._chk = QCheckBox("VPS en vivo")
        self._chk.setObjectName("liveToggle")
        self._chk.setToolTip(
            "Cache local (off): los datos vienen de la BD local sincronizada.\n"
            "VPS en vivo (on): los datos se traen del servidor en cada refresco."
        )
        self._chk.toggled.connect(self.toggled.emit)
        layout.addWidget(self._chk)

    def is_live(self) -> bool:
        return self._chk.isChecked()

    def set_state(self, enabled: bool):
        # Bloquear señal para no recursar al revertir
        self._chk.blockSignals(True)
        self._chk.setChecked(bool(enabled))
        self._chk.blockSignals(False)

    def show_error(self, msg: str):
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), msg, self)


class OrderStatusDialog(QDialog):
    """Diálogo para editar estado_pago y estado_envio de un pedido web.

    Al guardar, encola un cambio en outbox vía local_store.enqueue_order_status_update.
    El sync timer recoge el cambio y lo empuja al VPS.
    """

    PAGO_OPTIONS = ["PENDIENTE", "APROBADO", "RECHAZADO", "REEMBOLSADO"]
    ENVIO_OPTIONS = ["POR_DESPACHAR", "DESPACHADO", "ENTREGADO", "CANCELADO"]

    def __init__(self, parent, store: LocalStore, order: dict, on_saved=None):
        super().__init__(parent)
        self.store = store
        self.order = order
        self.on_saved = on_saved or (lambda: None)
        self.setWindowTitle(f"Editar pedido #{order.get('remote_id', '?')}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        eyebrow = QLabel("PEDIDO WEB")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        title = QLabel(f"#{order.get('reference', order.get('remote_id', '?'))}")
        title.setObjectName("loginTitle")
        layout.addWidget(title)

        info_lines = [
            f"Cliente: {order.get('customer_name') or '-'}",
            f"Total: $ {order.get('total', 0):,.0f}".replace(",", "."),
            f"Método: {order.get('payment_method') or '-'}",
        ]
        info = QLabel("\n".join(info_lines))
        info.setObjectName("muted")
        layout.addWidget(info)
        layout.addSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        self.pago_combo = QComboBox()
        self.pago_combo.addItems(self.PAGO_OPTIONS)
        current_pago = (order.get("status_payment") or "").upper()
        idx = self.pago_combo.findText(current_pago)
        if idx >= 0:
            self.pago_combo.setCurrentIndex(idx)

        self.envio_combo = QComboBox()
        self.envio_combo.addItems(self.ENVIO_OPTIONS)
        current_envio = (order.get("status_shipping") or "").upper()
        idx = self.envio_combo.findText(current_envio)
        if idx >= 0:
            self.envio_combo.setCurrentIndex(idx)

        form.addRow("Estado del pago:", self.pago_combo)
        form.addRow("Estado del envío:", self.envio_combo)
        layout.addLayout(form)

        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        layout.addWidget(self.feedback)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        new_pago = self.pago_combo.currentText().strip()
        new_envio = self.envio_combo.currentText().strip()
        old_pago = (self.order.get("status_payment") or "").upper()
        old_envio = (self.order.get("status_shipping") or "").upper()

        if new_pago == old_pago and new_envio == old_envio:
            self.feedback.setText("Sin cambios.")
            self.feedback.setStyleSheet("color: #6b7280;")
            return

        try:
            self.store.enqueue_order_status_update(
                remote_id=int(self.order.get("remote_id")),
                estado_pago=new_pago if new_pago != old_pago else None,
                estado_envio=new_envio if new_envio != old_envio else None,
                updated_at_iso=self.order.get("updated_at"),
            )
        except (ValueError, sqlite_error_class()) as exc:
            self.feedback.setText(f"Error: {exc}")
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")
            return

        self.on_saved()
        self.accept()


def sqlite_error_class():
    """Proxy: devuelve sqlite3.Error para usar en except (evita import en cabecera del archivo)."""
    import sqlite3
    return sqlite3.Error


class CategoriesDialog(QDialog):
    """Modal CRUD de categorías. Operaciones encolan outbox para PUSH al VPS."""

    def __init__(self, parent, store: LocalStore, on_changed=None):
        super().__init__(parent)
        self.store = store
        self.on_changed = on_changed or (lambda: None)
        self.setWindowTitle("Categorías")
        self.setMinimumSize(560, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        eyebrow = QLabel("BIDIRECCIONAL CON VPS")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        title = QLabel("Gestionar categorías")
        title.setObjectName("loginTitle")
        layout.addWidget(title)

        sub = QLabel(
            "Crea, renombra o elimina categorías. Los cambios se sincronizan "
            "con el VPS automáticamente cuando hay conexión."
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        actions_row = QHBoxLayout()
        new_btn = QPushButton("+  Nueva")
        new_btn.setObjectName("primaryAction")
        new_btn.clicked.connect(self._new)
        rename_btn = QPushButton("Renombrar")
        rename_btn.setObjectName("secondaryAction")
        rename_btn.clicked.connect(self._rename)
        delete_btn = QPushButton("Eliminar")
        delete_btn.setObjectName("dangerAction")
        delete_btn.clicked.connect(self._delete)
        actions_row.addWidget(new_btn)
        actions_row.addWidget(rename_btn)
        actions_row.addWidget(delete_btn)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Categoría", "# productos", "Estado sync"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        layout.addWidget(self.feedback)

        close_btn = QPushButton("Cerrar")
        close_btn.setObjectName("secondaryAction")
        close_btn.clicked.connect(self.accept)
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

        self._refresh()

    def _refresh(self):
        cats = self.store.list_generos_with_product_count()
        self.table.setRowCount(len(cats))
        self._categories = cats
        for row_idx, cat in enumerate(cats):
            self.table.setItem(row_idx, 0, QTableWidgetItem(cat["nombre"]))
            self.table.setItem(row_idx, 1, QTableWidgetItem(str(cat["productos_count"])))
            sync_state = "Sincronizado" if cat.get("remote_id") else "Pendiente push"
            self.table.setItem(row_idx, 2, QTableWidgetItem(sync_state))

    def _selected_category(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._categories):
            return None
        return self._categories[row]

    def _new(self):
        name, ok = QInputDialog.getText(self, "Nueva categoría", "Nombre:")
        if not ok or not name.strip():
            return
        try:
            self.store.enqueue_category_create(name.strip())
        except ValueError as exc:
            self._error(str(exc))
            return
        self._info(f'Categoría "{name.strip()}" creada. Push al VPS pendiente.')
        self._refresh()
        self.on_changed()

    def _rename(self):
        cat = self._selected_category()
        if not cat:
            self._error("Selecciona una categoría.")
            return
        new_name, ok = QInputDialog.getText(
            self, "Renombrar categoría", "Nuevo nombre:", text=cat["nombre"]
        )
        if not ok or not new_name.strip() or new_name.strip() == cat["nombre"]:
            return
        try:
            self.store.enqueue_category_update(int(cat["id"]), new_name.strip())
        except ValueError as exc:
            self._error(str(exc))
            return
        self._info(f'Renombrada a "{new_name.strip()}". Push al VPS pendiente.')
        self._refresh()
        self.on_changed()

    def _delete(self):
        cat = self._selected_category()
        if not cat:
            self._error("Selecciona una categoría.")
            return
        confirm = QMessageBox.question(
            self, "Confirmar",
            f'Eliminar la categoría "{cat["nombre"]}"?',
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.enqueue_category_delete(int(cat["id"]))
        except ValueError as exc:
            self._error(str(exc))
            return
        self._info(f'Eliminada. Push al VPS pendiente.')
        self._refresh()
        self.on_changed()

    def _error(self, msg: str):
        self.feedback.setText(msg)
        self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")

    def _info(self, msg: str):
        self.feedback.setText(msg)
        self.feedback.setStyleSheet("color: #117a36; font-weight: 600;")


# =============================================================================
# Dashboard
# =============================================================================
class DashboardPage(QWidget):
    def __init__(self, store: LocalStore, open_section):
        super().__init__()
        self.store = store
        self.open_section = open_section
        self._build()

    def _build(self):
        # Layout raíz: solo el área desplazable, para que el dashboard tenga aire
        # y se pueda hacer scroll en vez de quedar todo amontonado en una pantalla.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("dashboardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        content = QWidget()
        content.setObjectName("dashboardContent")
        scroll.setWidget(content)

        body = QVBoxLayout(content)
        body.setContentsMargins(30, 26, 30, 30)
        body.setSpacing(20)

        header = QLabel("Menú administrativo")
        header.setObjectName("pageTitle")
        subtitle = QLabel("Centro de operaciones local con módulos independientes y una sola base de datos.")
        subtitle.setObjectName("muted")
        body.addWidget(header)
        body.addWidget(subtitle)
        body.addSpacing(6)
        body.addWidget(self._hero_panel())

        body.addSpacing(8)
        local_eyebrow = QLabel("TU NEGOCIO  ·  LOCAL")
        local_eyebrow.setObjectName("eyebrow")
        body.addWidget(local_eyebrow)
        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(16)
        body.addLayout(self.metrics_grid)

        # Mini-gráficos locales (tendencia, top productos, salud de stock)
        body.addSpacing(8)
        self.charts_row = QHBoxLayout()
        self.charts_row.setSpacing(16)
        body.addLayout(self.charts_row)

        body.addSpacing(8)
        body.addWidget(self._vps_metrics_panel())
        body.addSpacing(8)
        body.addWidget(self._module_grid())
        body.addStretch(1)
        self.refresh()
        # Pull stats VPS asíncrono en arranque
        QTimer.singleShot(800, self._pull_vps_stats)

    def _vps_metrics_panel(self):
        panel = QFrame()
        panel.setObjectName("sectionPanel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)

        header = QHBoxLayout()
        eyebrow = QLabel("MÉTRICAS DEL VPS  ·  TIEMPO REAL")
        eyebrow.setObjectName("eyebrow")
        header.addWidget(eyebrow)
        header.addStretch(1)
        self.vps_status = QLabel("—")
        self.vps_status.setObjectName("muted")
        header.addWidget(self.vps_status)
        refresh_btn = QPushButton("↻  Refrescar")
        refresh_btn.setObjectName("secondaryAction")
        refresh_btn.setMinimumWidth(120)
        refresh_btn.clicked.connect(self._pull_vps_stats)
        header.addWidget(refresh_btn)
        outer.addLayout(header)

        self.vps_grid = QGridLayout()
        self.vps_grid.setSpacing(16)
        outer.addLayout(self.vps_grid)

        # Inicializa con cards en estado vacío
        colores = _cb_brand_colors()
        self._vps_cards_data = [
            ("ventas_web_hoy",     "◎", "Ventas web · hoy",    colores["acento_secundario"]),
            ("ventas_web_semana",  "∑", "Ventas web · 7 días", colores["primario"]),
            ("pedidos_pendientes", "◷", "Pedidos pendientes",  colores["acento"]),
            ("productos_total",    "▦", "Productos en VPS",    colores["primario_oscuro"]),
        ]
        self._vps_cards = {}
        for idx, (key, icon, label, accent) in enumerate(self._vps_cards_data):
            card = _CbKpiCard(icon, label, "Sin datos", "—", accent)
            card.set_value("Sin datos", empty=True)
            self._vps_cards[key] = card
            self.vps_grid.addWidget(card, idx // 4, idx % 4)
        return panel

    def _pull_vps_stats(self):
        """Llama /api/v1/sync/stats y actualiza las cards."""
        sync_state = sync_cfg.load(app_data_dir())
        base_url = sync_state.get("base_url", "")
        api_key = sync_state.get("api_key", "")
        if not base_url or not api_key:
            self._set_vps_cards_offline("Sync no configurada")
            return
        try:
            client = SyncClient(base_url, api_key)
            stats = client.pull_stats()
        except (ValueError, SyncError):
            self._set_vps_cards_offline("Sin conexión")
            return

        # Actualizar cards: (valor grande, subtítulo)
        formatters = {
            "ventas_web_hoy":     lambda v: (_rt_money(v.get("total", 0)), f"{v.get('count', 0)} venta(s) hoy"),
            "ventas_web_semana":  lambda v: (_rt_money(v.get("total", 0)), f"{v.get('count', 0)} venta(s) en 7 días"),
            "pedidos_pendientes": lambda v: (str(v), "por gestionar"),
            "productos_total":    lambda v: (str(v), "catálogo en producción"),
        }
        for key, _icon, _label, _accent in self._vps_cards_data:
            value = stats.get(key, 0)
            if value is None:
                self._vps_cards[key].set_value("Sin datos", "—", empty=True)
            else:
                texto, sub = formatters[key](value)
                self._vps_cards[key].set_value(texto, sub)

        from datetime import datetime
        self.vps_status.setText(
            f"Actualizado · {datetime.now().strftime('%H:%M:%S')}"
        )

    def _set_vps_cards_offline(self, reason: str):
        for key, _icon, _label, _accent in self._vps_cards_data:
            self._vps_cards[key].set_value("Sin datos", "—", empty=True)
        self.vps_status.setText(reason)

    def _hero_panel(self):
        hero = QFrame()
        hero.setObjectName("dashboardHero")
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        copy = QVBoxLayout()
        copy.setSpacing(4)
        eyebrow = QLabel("Flujo diario recomendado")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Empieza por POS e inventario, luego revisa la sincronización.")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        detail = QLabel(
            "El tablero mantiene las operaciones principales disponibles aunque no exista conexión a internet. "
            "Atajos: F1 Dashboard · F2 Productos · F3 POS · F4 Inventario · F5 Ventas · F6 Usuarios · F7 Sync · F8 Configuración."
        )
        detail.setObjectName("muted")
        detail.setWordWrap(True)
        copy.addWidget(eyebrow)
        copy.addWidget(title)
        copy.addSpacing(2)
        copy.addWidget(detail)
        copy.addStretch(1)

        highlights = QVBoxLayout()
        highlights.setSpacing(8)
        highlights.setContentsMargins(0, 0, 0, 0)
        for label, value in [
            ("Operación diaria", "POS e inventario"),
            ("Catálogo", "Productos y precios"),
            ("Conexión", "Cambios pendientes"),
        ]:
            item = QFrame()
            item.setObjectName("heroHighlight")
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(12, 8, 12, 8)
            item_layout.setSpacing(2)
            small = QLabel(label)
            small.setObjectName("eyebrowMuted")
            strong = QLabel(value)
            strong.setObjectName("highlightText")
            item_layout.addWidget(small)
            item_layout.addWidget(strong)
            highlights.addWidget(item)

        layout.addLayout(copy, stretch=3)
        layout.addLayout(highlights, stretch=2)
        return hero

    def refresh(self):
        colores = _cb_brand_colors()
        metrics = self.store.dashboard_metrics()
        extras = self.store.dashboard_extras()

        clear_layout(self.metrics_grid)
        ventas_hoy_n = extras["ventas_7d"][-1][2] if extras["ventas_7d"] else 0
        stock_color = colores["peligro"] if metrics["low_stock"] else colores["acento_secundario"]
        sync_color = colores["acento"] if metrics["pending_sync"] else colores["acento_secundario"]
        cards = [
            ("▦", "Productos activos", str(metrics["products"]), "en catálogo local", colores["primario"]),
            ("⚠", "Stock bajo", str(metrics["low_stock"]),
             "requieren reposición" if metrics["low_stock"] else "todo en orden", stock_color),
            ("$", "Ventas hoy", f"${metrics['today_sales']:,.0f}".replace(",", "."),
             f"{ventas_hoy_n} venta(s) registrada(s)", colores["acento_secundario"]),
            ("⟳", "Pendiente sync", str(metrics["pending_sync"]),
             "cambios por enviar" if metrics["pending_sync"] else "todo sincronizado", sync_color),
        ]
        for index, (icon, label, value, sub, accent) in enumerate(cards):
            self.metrics_grid.addWidget(_CbKpiCard(icon, label, value, sub, accent), 0, index)

        clear_layout(self.charts_row)
        self.charts_row.addWidget(self._sales_trend_panel(extras, colores), 5)
        self.charts_row.addWidget(self._top_products_panel(extras, colores), 4)
        self.charts_row.addWidget(self._stock_health_panel(extras, colores), 3)

    def _sales_trend_panel(self, extras, colores):
        panel = QFrame(); panel.setObjectName("sectionPanel"); panel.setMinimumHeight(240)
        lay = QVBoxLayout(panel); lay.setContentsMargins(16, 12, 16, 10); lay.setSpacing(4)
        head = QHBoxLayout()
        eyebrow = QLabel("VENTAS · ÚLTIMOS 7 DÍAS"); eyebrow.setObjectName("eyebrow")
        head.addWidget(eyebrow); head.addStretch(1)
        total7 = sum(v for _, v, _ in extras["ventas_7d"])
        total_lbl = QLabel(_cb_money_compact(total7))
        total_lbl.setStyleSheet(f"color: {colores['primario']}; font-size: 15px; font-weight: 850;")
        head.addWidget(total_lbl)
        lay.addLayout(head)
        dias_semana = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        from datetime import date as _date
        labels = []
        for iso, _v, _c in extras["ventas_7d"]:
            try:
                labels.append(dias_semana[_date.fromisoformat(iso).weekday()])
            except ValueError:
                labels.append("")
        spark = _CbSparkline()
        spark.set_data([v for _, v, _ in extras["ventas_7d"]], labels, colores["primario"])
        lay.addWidget(spark, 1)
        return panel

    def _top_products_panel(self, extras, colores):
        panel = QFrame(); panel.setObjectName("sectionPanel"); panel.setMinimumHeight(240)
        lay = QVBoxLayout(panel); lay.setContentsMargins(16, 12, 16, 10); lay.setSpacing(6)
        eyebrow = QLabel("TOP PRODUCTOS · 30 DÍAS"); eyebrow.setObjectName("eyebrow")
        lay.addWidget(eyebrow)
        top = extras["top_products"]
        if not top:
            vacio = QLabel("Aún no hay ventas para rankear."); vacio.setObjectName("muted")
            lay.addWidget(vacio, 1, Qt.AlignmentFlag.AlignCenter)
            return panel
        palette = _cb_palette(colores)
        max_u = max(p["unidades"] for p in top) or 1
        lay.addStretch(1)
        for i, prod in enumerate(top):
            color = palette[i % len(palette)]
            head = QHBoxLayout()
            name = QLabel(prod["name"])
            name.setStyleSheet("color: #374151; font-size: 11px; font-weight: 700;")
            head.addWidget(name, 1)
            val = QLabel(f"{prod['unidades']} uds · {_cb_money_compact(prod['total'])}")
            val.setStyleSheet(f"color: {color.name()}; font-size: 11px; font-weight: 800;")
            head.addWidget(val)
            lay.addLayout(head)
            lay.addWidget(_CbMiniBar(prod["unidades"] / max_u, color.name()))
        lay.addStretch(1)
        return panel

    def _stock_health_panel(self, extras, colores):
        panel = QFrame(); panel.setObjectName("sectionPanel"); panel.setMinimumHeight(240)
        lay = QVBoxLayout(panel); lay.setContentsMargins(16, 12, 16, 10); lay.setSpacing(4)
        eyebrow = QLabel("SALUD DEL STOCK"); eyebrow.setObjectName("eyebrow")
        lay.addWidget(eyebrow)
        ok, low = extras["stock_ok"], extras["stock_low"]
        total = ok + low
        donut = _CbDonutChart()
        donut.setMinimumSize(120, 120)
        pct = (ok / total * 100) if total else 0
        donut.set_data(
            [("Stock sano", ok, QColor(colores["acento_secundario"])),
             ("Stock bajo", low, QColor(colores["peligro"]))],
            "stock sano", f"{pct:.0f}%")
        lay.addWidget(donut, 1)
        leyenda = QLabel(f"● {ok} sanos&nbsp;&nbsp;&nbsp;<span style='color:{colores['peligro']}'>● {low} bajos</span>")
        leyenda.setStyleSheet(f"color: {colores['acento_secundario']}; font-size: 11px; font-weight: 700;")
        leyenda.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(leyenda)
        return panel

    def _module_grid(self):
        frame = QFrame()
        frame.setObjectName("sectionPanel")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)

        eyebrow_row = QHBoxLayout()
        eyebrow = QLabel("ATAJOS A MÓDULOS")
        eyebrow.setObjectName("eyebrow")
        eyebrow_row.addWidget(eyebrow)
        eyebrow_row.addStretch(1)
        outer.addLayout(eyebrow_row)

        layout = QGridLayout()
        layout.setSpacing(16)
        outer.addLayout(layout)

        colores = _cb_brand_colors()
        modules = [
            ("▦", "Productos",   "CRUD local y precios",  "F2", "products",  colores["primario"]),
            ("◰", "POS",         "Venta directa offline", "F3", "pos",       colores["acento_secundario"]),
            ("⊞", "Inventario",  "Entradas y salidas",    "F4", "inventory", colores["acento"]),
            ("$", "Ventas",      "Historial y recibos",   "F5", "sales",     colores["primario_oscuro"]),
            ("◉", "Usuarios",    "CRUD local",            "F6", "users",     colores["peligro"]),
            ("⟳", "Sync",        "Estado de la BD local", "F7", "sync",      colores["primario"]),
        ]
        for index, (icon, title, detail, shortcut, target, accent) in enumerate(modules):
            card = self._module_card(icon, title, detail, shortcut, target, accent)
            layout.addWidget(card, index // 3, index % 3)
        return frame

    def _module_card(self, icon, title, detail, shortcut, target, accent=None):
        card = QFrame()
        card.setObjectName("moduleCard")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setToolTip(f"{title} · {shortcut}")

        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        icon_label = QLabel(icon)
        icon_label.setObjectName("moduleCardIcon")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if accent:
            ac = QColor(accent)
            icon_label.setStyleSheet(
                f"background: rgba({ac.red()},{ac.green()},{ac.blue()},0.12); color: {accent};"
            )
        row.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_label = QLabel(title)
        title_label.setObjectName("moduleCardTitle")
        detail_label = QLabel(detail)
        detail_label.setObjectName("moduleCardDetail")
        text_col.addWidget(title_label)
        text_col.addWidget(detail_label)
        row.addLayout(text_col, 1)

        shortcut_label = QLabel(shortcut)
        shortcut_label.setObjectName("moduleCardShortcut")
        shortcut_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(shortcut_label)

        def _on_click(event, name=target):
            if event.button() == Qt.MouseButton.LeftButton:
                self.open_section(name)
        card.mousePressEvent = _on_click
        return card


# =============================================================================
# Productos
# =============================================================================
class ProductsPage(QWidget):
    def __init__(self, store: LocalStore, on_changed, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.can = can_callback or (lambda m, a: True)
        self.selected_id = None
        self._all_products = []
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Header: título + acción "Gestionar categorías" + LiveToggle
        header_row = QHBoxLayout()
        title = QLabel("Productos")
        title.setObjectName("pageTitle")
        header_row.addWidget(title)
        header_row.addStretch(1)

        self.cats_btn = QPushButton("⚙  Gestionar categorías")
        self.cats_btn.setObjectName("secondaryAction")
        self.cats_btn.setToolTip("Crear, renombrar o eliminar categorías (sincroniza al VPS)")
        self.cats_btn.clicked.connect(self._open_categories_dialog)
        header_row.addWidget(self.cats_btn)

        self.live = LiveToggle()
        self.live.toggled.connect(self._on_live_toggled)
        header_row.addWidget(self.live)
        layout.addLayout(header_row)

        form = QFrame()
        form.setObjectName("sectionPanel")
        form_outer = QVBoxLayout(form)
        form_outer.setContentsMargins(16, 14, 16, 14)
        form_outer.setSpacing(10)

        ed_head = QHBoxLayout()
        self.editor_title = QLabel("NUEVO PRODUCTO")
        self.editor_title.setObjectName("eyebrow")
        ed_head.addWidget(self.editor_title)
        ed_head.addStretch(1)
        editor_hint = QLabel("Selecciona una fila para editar · SKU y nombre son obligatorios")
        editor_hint.setObjectName("muted")
        ed_head.addWidget(editor_hint)
        form_outer.addLayout(ed_head)

        form_layout = QGridLayout()
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(4)
        form_outer.addLayout(form_layout)

        def _flabel(text):
            lbl = QLabel(text.upper())
            lbl.setObjectName("eyebrowMuted")
            return lbl

        self.sku = QLineEdit()
        self.barcode = QLineEdit()
        self.barcode.setPlaceholderText("EAN-13, UPC u otro codigo")
        self.name = QLineEdit()
        self.category = QLineEdit("General")
        self.stock = QSpinBox()
        self.stock.setRange(0, 1_000_000)
        self.min_stock = QSpinBox()
        self.min_stock.setRange(0, 1_000_000)
        self.price = QDoubleSpinBox()
        self.price.setRange(0, 999_999_999)
        self.price.setDecimals(2)
        self.price.setPrefix("$")

        # Genero (combo desde tabla generos local sincronizada)
        self.genero_combo = QComboBox()
        self.genero_combo.addItem("(sin genero)", None)
        for g in self.store.list_generos():
            self.genero_combo.addItem(g["nombre"], g.get("remote_id") or g["id"])

        # Imagen
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("Ruta a imagen (opcional). Web usara placeholder si vacio.")
        browse_img = QPushButton("Examinar...")
        browse_img.setObjectName("secondaryAction")
        browse_img.clicked.connect(self._browse_image)
        clear_img = QPushButton("Quitar")
        clear_img.setObjectName("dangerAction")
        clear_img.clicked.connect(lambda: self.image_path.setText(""))
        image_row = QHBoxLayout()
        image_row.addWidget(self.image_path, 1)
        image_row.addWidget(browse_img)
        image_row.addWidget(clear_img)

        fields = [
            ("SKU", self.sku),
            ("Codigo de barras", self.barcode),
            ("Nombre", self.name),
            ("Categoria (libre)", self.category),
            ("Stock", self.stock),
            ("Minimo", self.min_stock),
            ("Precio", self.price),
            ("Genero (sync con web)", self.genero_combo),
        ]
        for index, (label, widget) in enumerate(fields):
            form_layout.addWidget(_flabel(label), index // 3 * 2, index % 3)
            form_layout.addWidget(widget, index // 3 * 2 + 1, index % 3)
        # Imagen ocupa toda la fila siguiente
        next_row = ((len(fields) - 1) // 3 + 1) * 2
        form_layout.addWidget(_flabel("Imagen"), next_row, 0)
        form_layout.addLayout(image_row, next_row + 1, 0, 1, 3)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("✓  Guardar producto")
        self.save_btn.clicked.connect(self._save)
        self.new_btn = QPushButton("＋  Nuevo")
        self.new_btn.setObjectName("secondaryAction")
        self.new_btn.clicked.connect(self._clear_form)
        self.delete_btn = QPushButton("⊘  Desactivar")
        self.delete_btn.setObjectName("dangerAction")
        self.delete_btn.clicked.connect(self._delete)
        # Eliminar catálogo = solo Admin/Propietario (CATALOG_DELETE). El resto de
        # roles operativos puede crear/editar pero no eliminar.
        self.delete_btn.setVisible(self.can("products", "delete"))
        self.save_btn.setVisible(self.can("products", "create") or self.can("products", "edit"))
        actions.addWidget(self.save_btn)
        actions.addWidget(self.new_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        self.live_badge = QLabel("●  VPS EN VIVO · SOLO LECTURA")
        self.live_badge.setObjectName("pageBadge")
        self.live_badge.setProperty("state", "warning")
        self.live_badge.setVisible(False)
        actions.addWidget(self.live_badge)
        form_outer.addLayout(actions)

        layout.addWidget(form)

        # Buscador + chips de resumen
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Buscar por SKU, barcode, nombre o categoría")
        self.search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search, 1)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(6)
        search_row.addLayout(self.chips_row)
        layout.addLayout(search_row)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["ID", "SKU", "Barcode", "Nombre", "Categoria", "Stock", "Minimo", "Precio"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._load_selected)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self):
        self._all_products = self.store.products()
        # Refrescar combo de generos (puede haber cambios desde sync)
        current_g = self.genero_combo.currentData()
        self.genero_combo.clear()
        self.genero_combo.addItem("(sin genero)", None)
        for g in self.store.list_generos():
            self.genero_combo.addItem(g["nombre"], g.get("remote_id") or g["id"])
        if current_g is not None:
            idx = self.genero_combo.findData(current_g)
            if idx >= 0:
                self.genero_combo.setCurrentIndex(idx)
        self._apply_filter()

    def _browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Elegir imagen", "", "Imagenes (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.image_path.setText(path)

    def _apply_filter(self):
        needle = self.search.text().strip().lower()
        rows = self._all_products if not needle else [
            p for p in self._all_products
            if needle in (p.get("sku") or "").lower()
            or needle in ((p.get("barcode") or "").lower())
            or needle in p["name"].lower()
            or needle in (p.get("category") or "").lower()
        ]
        colores = _cb_brand_colors()
        palette = _cb_palette(colores)
        cat_colors = {}
        peligro = QColor(colores["peligro"])
        self.table.setRowCount(len(rows))
        for row_index, product in enumerate(rows):
            values = [
                product["id"],
                product["sku"],
                product.get("barcode") or "",
                product["name"],
                product["category"],
                product["stock"],
                product["min_stock"],
                f"${float(product['price']):,.0f}".replace(",", "."),
            ]
            low_stock = int(product["stock"]) <= int(product["min_stock"])
            cat = product.get("category") or "General"
            if cat not in cat_colors:
                cat_colors[cat] = palette[len(cat_colors) % len(palette)]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, product["id"])
                if col_index == 4:
                    item.setForeground(QBrush(cat_colors[cat]))
                if col_index == 5 and low_stock:
                    item.setText(f"{value} ⚠")
                    item.setForeground(QBrush(peligro))
                    bold = item.font(); bold.setBold(True); item.setFont(bold)
                if col_index == 7:
                    item.setForeground(QBrush(QColor(colores["primario_oscuro"])))
                self.table.setItem(row_index, col_index, item)

        # Chips de resumen del catálogo completo
        total = len(self._all_products)
        low = sum(1 for p in self._all_products if int(p["stock"]) <= int(p["min_stock"]))
        valor = sum(float(p["price"]) * int(p["stock"]) for p in self._all_products)
        clear_layout(self.chips_row)
        self.chips_row.addWidget(_cb_chip(f"▦ {total} productos", colores["primario"]))
        if low:
            self.chips_row.addWidget(_cb_chip(f"⚠ {low} con stock bajo", colores["peligro"]))
        else:
            self.chips_row.addWidget(_cb_chip("✓ Stock saludable", colores["acento_secundario"]))
        self.chips_row.addWidget(_cb_chip(f"Inventario {_cb_money_compact(valor)}", colores["acento"]))

    def _load_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item0 = self.table.item(row, 0)
        if item0 is None:
            return
        pid = item0.data(Qt.ItemDataRole.UserRole)
        prod = next((p for p in self._all_products if p["id"] == pid), None)
        if prod is None:
            return  # modo "VPS en vivo": tabla de solo lectura, no carga al editor
        self.selected_id = prod["id"]
        self.sku.setText(prod["sku"] or "")
        self.barcode.setText(prod.get("barcode") or "")
        self.name.setText(prod["name"])
        self.category.setText(prod.get("category") or "General")
        self.stock.setValue(int(prod["stock"]))
        self.min_stock.setValue(int(prod["min_stock"]))
        self.price.setValue(float(prod["price"]))
        self.image_path.setText(prod.get("image_path") or "")
        gid = prod.get("genero_id")
        idx = self.genero_combo.findData(gid) if gid is not None else 0
        self.genero_combo.setCurrentIndex(idx if idx >= 0 else 0)
        colores = _cb_brand_colors()
        self.editor_title.setText(f"EDITANDO · {prod['name'].upper()}")
        self.editor_title.setStyleSheet(f"color: {colores['acento']};")

    def _save(self):
        if not self.sku.text().strip() or not self.name.text().strip():
            QMessageBox.warning(self, "Producto", "SKU y nombre son obligatorios.")
            return
        try:
            self.store.save_product(
                self.selected_id,
                self.sku.text().strip(),
                self.name.text().strip(),
                self.category.text().strip() or "General",
                self.stock.value(),
                self.min_stock.value(),
                self.price.value(),
                barcode=self.barcode.text().strip(),
                image_path=self.image_path.text().strip(),
                genero_id=self.genero_combo.currentData(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Producto", str(exc))
            return
        self._clear_form()
        self.refresh()
        self.on_changed()

    def _delete(self):
        if not self.selected_id:
            return
        confirm = QMessageBox.question(
            self,
            "Desactivar producto",
            "Esto oculta el producto pero conserva su historial. Continuar?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_product(self.selected_id)
        self._clear_form()
        self.refresh()
        self.on_changed()

    def _clear_form(self):
        self.selected_id = None
        self.table.clearSelection()
        self.sku.clear()
        self.barcode.clear()
        self.name.clear()
        self.category.setText("General")
        self.stock.setValue(0)
        self.min_stock.setValue(0)
        self.price.setValue(0)
        self.image_path.clear()
        self.genero_combo.setCurrentIndex(0)
        self.editor_title.setText("NUEVO PRODUCTO")
        self.editor_title.setStyleSheet("")

    # ── Categorías (modal CRUD bidi con VPS) ──
    def _open_categories_dialog(self):
        dlg = CategoriesDialog(self, self.store, on_changed=self._on_categories_changed)
        dlg.exec()

    def _on_categories_changed(self):
        # Refrescar el combo de géneros al cerrar el diálogo
        try:
            current_data = self.genero_combo.currentData()
            self.genero_combo.clear()
            self.genero_combo.addItem("(sin genero)", None)
            for g in self.store.list_generos():
                self.genero_combo.addItem(g["nombre"], g.get("remote_id") or g["id"])
            # Restaurar selección si existe
            for i in range(self.genero_combo.count()):
                if self.genero_combo.itemData(i) == current_data:
                    self.genero_combo.setCurrentIndex(i)
                    break
        except Exception:
            pass
        self.on_changed()

    # ── Live toggle: VPS en vivo vs cache local ──
    def _on_live_toggled(self, live: bool):
        self._set_crud_enabled(not live)
        if live:
            self._refresh_live_from_vps()
        else:
            self.refresh()

    def _set_crud_enabled(self, enabled: bool):
        for w in (self.save_btn, self.new_btn, self.delete_btn,
                  self.sku, self.barcode, self.name, self.category,
                  self.stock, self.min_stock, self.price, self.image_path,
                  self.genero_combo, self.cats_btn):
            try:
                w.setEnabled(enabled)
            except Exception:
                pass
        self.live_badge.setVisible(not enabled)

    def _refresh_live_from_vps(self):
        """Trae productos del VPS en tiempo real y los muestra (read-only)."""
        sync_state = sync_cfg.load(app_data_dir())
        base_url = sync_state.get("base_url", "")
        api_key = sync_state.get("api_key", "")
        if not base_url or not api_key:
            self.live.set_state(False)
            self._set_crud_enabled(True)
            self.live.show_error("Sincronización no configurada")
            return
        try:
            client = SyncClient(base_url, api_key)
            resp = client.pull_products_live(limit=2000)
        except (ValueError, SyncError):
            self.live.set_state(False)
            self._set_crud_enabled(True)
            self.live.show_error("Sin conexión — volviendo a cache")
            return
        items = resp.get("items", [])
        # Renderizar en la tabla con el formato que espera la UI existente
        self._render_live_products(items)

    def _render_live_products(self, items):
        """Renderiza productos del VPS directamente en la tabla, sin tocar SQLite."""
        if not hasattr(self, "table"):
            return
        self.table.setRowCount(len(items))
        # Columnas: ID, SKU, Barcode, Nombre, Categoria, Stock, Minimo, Precio
        for row_idx, p in enumerate(items):
            cells = [
                str(p.get("remote_id", "")),
                p.get("sku", "") or "",
                p.get("barcode", "") or "",
                p.get("name", "") or "",
                p.get("category", "") or "",
                str(p.get("stock", 0)),
                "—",  # minimo no viene en /sync/products
                f"$ {float(p.get('price', 0)):,.0f}".replace(",", "."),
            ]
            for col_idx, val in enumerate(cells):
                self.table.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))
        # Invalidar la lista local que usan otros métodos para evitar editar
        # algo que ya no representa el cache.
        self._all_products = []
        self.selected_id = None

# =============================================================================
# Inventario
# =============================================================================
class InventoryPage(QWidget):
    def __init__(self, store: LocalStore, on_changed, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.can = can_callback or (lambda m, a: True)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Inventario")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        panel = QFrame()
        panel.setObjectName("sectionPanel")
        form = QGridLayout(panel)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self.product = QComboBox()
        self.quantity = QSpinBox()
        self.quantity.setRange(-1_000_000, 1_000_000)
        self.quantity.setValue(1)
        self.reason = QLineEdit("Ajuste manual")
        apply_btn = QPushButton("Aplicar movimiento")
        apply_btn.clicked.connect(self._apply)

        form.addWidget(QLabel("Producto"), 0, 0)
        form.addWidget(self.product, 1, 0)
        form.addWidget(QLabel("Cantidad (+ entrada / - salida)"), 0, 1)
        form.addWidget(self.quantity, 1, 1)
        form.addWidget(QLabel("Motivo"), 0, 2)
        form.addWidget(self.reason, 1, 2)
        form.addWidget(apply_btn, 1, 3)
        layout.addWidget(panel)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Fecha", "SKU", "Producto", "Cantidad", "Motivo"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self):
        current_id = self.product.currentData()
        self.product.clear()
        for product in self.store.product_options():
            self.product.addItem(f"{product['sku']} - {product['name']} (stock {product['stock']})", product["id"])
        if current_id is not None:
            idx = self.product.findData(current_id)
            if idx >= 0:
                self.product.setCurrentIndex(idx)

        movements = self.store.inventory_movements()
        self.table.setRowCount(len(movements))
        for row_index, movement in enumerate(movements):
            values = [
                movement["created_at"],
                movement["sku"],
                movement["name"],
                movement["quantity_delta"],
                movement["reason"],
            ]
            for col_index, value in enumerate(values):
                self.table.setItem(row_index, col_index, QTableWidgetItem(str(value)))

    def _apply(self):
        product_id = self.product.currentData()
        if not product_id:
            QMessageBox.information(self, "Inventario", "No hay productos. Crea uno primero en Productos.")
            return
        try:
            self.store.adjust_stock(product_id, self.quantity.value(), self.reason.text())
        except Exception as exc:
            QMessageBox.warning(self, "Inventario", str(exc))
            return
        self.refresh()
        self.on_changed()


# =============================================================================
# Scanner engine
# =============================================================================
class ScannerEngine(QObject):
    """Detecta entrada de pistolas USB que emulan teclado.

    Replica el ScannerEngine de la web (facturacion_pos.html):
      - acumula pulsaciones rapidas (< 50 ms entre teclas) en un buffer
      - cuando llega Enter o tras 200 ms de silencio, emite el barcode
      - ignora duplicados dentro de 500 ms
    """

    barcode_scanned = pyqtSignal(str)
    enabled_changed = pyqtSignal(bool)

    THRESHOLD_MS = 50
    SILENCE_MS = 200
    MIN_LENGTH = 3
    DUPLICATE_MS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.buffer = ""
        self.last_key_time = 0
        self.last_barcode = ""
        self.last_scan_time = 0
        self.enabled = False
        self._silence_timer = QTimer(self)
        self._silence_timer.setSingleShot(True)
        self._silence_timer.timeout.connect(self._flush)

    def set_enabled(self, value: bool):
        new_value = bool(value)
        if new_value == self.enabled:
            return
        self.enabled = new_value
        if not self.enabled:
            self.buffer = ""
            self._silence_timer.stop()
        self.enabled_changed.emit(self.enabled)

    def eventFilter(self, obj, event):  # noqa: N802
        if not self.enabled or event.type() != QEvent.Type.KeyPress:
            return False

        key_event: QKeyEvent = event  # type: ignore[assignment]
        now_ms = int(time.monotonic() * 1000)
        text = key_event.text()
        key = key_event.key()

        # Si el foco está en el campo de barras del POS, dejamos que el QLineEdit
        # maneje TODO nativamente (cada tecla una vez + returnPressed ->
        # _handle_barcode_input). NO bufferizamos en paralelo: hacerlo provocaba
        # códigos corruptos (un carácter de más / duplicado) al mezclarse el
        # contenido del campo con el del buffer. El escáner global (buffer) queda
        # solo para cuando el foco NO está en el campo.
        focused = QApplication.focusWidget()
        if focused is not None and focused.objectName() == "barcodeInput":
            self.buffer = ""
            self._silence_timer.stop()
            return False

        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab):
            if len(self.buffer) >= self.MIN_LENGTH:
                self._silence_timer.stop()
                self._flush()
                return True
            return False

        if not text or len(text) != 1 or key_event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False

        if not text.isprintable():
            return False

        diff = now_ms - self.last_key_time
        self.last_key_time = now_ms

        if diff > self.THRESHOLD_MS and self.buffer:
            self.buffer = ""

        is_rapid = diff <= self.THRESHOLD_MS or not self.buffer
        if not is_rapid:
            return False

        self.buffer += text
        self._silence_timer.start(self.SILENCE_MS)
        # Foco fuera del campo: consumimos las teclas del escaneo para que no se
        # disparen en otros widgets; el _flush enruta el código al carrito.
        if len(self.buffer) >= 2:
            return True
        return False

    def _flush(self):
        raw = self.buffer
        self.buffer = ""
        cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
        if len(cleaned) < self.MIN_LENGTH:
            return

        now_ms = int(time.monotonic() * 1000)
        if cleaned == self.last_barcode and (now_ms - self.last_scan_time) < self.DUPLICATE_MS:
            return
        self.last_barcode = cleaned
        self.last_scan_time = now_ms
        self.barcode_scanned.emit(cleaned)


# =============================================================================
# POS
# =============================================================================
class PosPage(QWidget):
    def __init__(self, store: LocalStore, on_changed, scanner: "ScannerEngine | None" = None, brand_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.can = can_callback or (lambda m, a: True)
        self.cart = []
        self.scanner = scanner
        self._is_active = False
        self._brand_callback = brand_callback or (lambda: branding_mod.DEFAULTS)
        if self.scanner is not None:
            self.scanner.barcode_scanned.connect(self._on_barcode_scanned)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("POS local")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.scanner_status = QLabel("Lector inactivo")
        self.scanner_status.setObjectName("scannerBadge")
        header.addWidget(self.scanner_status)
        layout.addLayout(header)

        scanner_panel = QFrame()
        scanner_panel.setObjectName("scannerPanel")
        scanner_layout = QHBoxLayout(scanner_panel)
        scanner_layout.setContentsMargins(18, 14, 18, 14)
        scanner_layout.setSpacing(12)

        icon = QLabel("|||")
        icon.setObjectName("scannerIcon")
        scanner_layout.addWidget(icon)

        self.barcode_input = QLineEdit()
        self.barcode_input.setObjectName("barcodeInput")
        self.barcode_input.setPlaceholderText("Escanear codigo de barras o escribir referencia / SKU y Enter")
        self.barcode_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.barcode_input.returnPressed.connect(self._handle_barcode_input)
        scanner_layout.addWidget(self.barcode_input, 1)

        scan_btn = QPushButton("Buscar")
        scan_btn.clicked.connect(self._handle_barcode_input)
        scanner_layout.addWidget(scan_btn)

        layout.addWidget(scanner_panel)

        colores = _cb_brand_colors()
        content = QHBoxLayout()
        content.setSpacing(12)

        # ─── Columna izquierda: alta manual + carrito ───
        left = QVBoxLayout()
        left.setSpacing(10)

        panel = QFrame()
        panel.setObjectName("sectionPanel")
        form = QGridLayout(panel)
        form.setContentsMargins(16, 12, 16, 14)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(4)

        self.product = QComboBox()
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setValue(1)
        add = QPushButton("＋  Agregar")
        add.clicked.connect(self._add_item)
        sell = QPushButton("✓  Finalizar venta")
        sell.setObjectName("primaryAction")
        sell.setMinimumHeight(48)
        sell.clicked.connect(self._finish_sale)
        clear = QPushButton("Limpiar carrito")
        clear.setObjectName("secondaryAction")
        clear.clicked.connect(self._clear_cart)

        lbl_prod = QLabel("PRODUCTO (BÚSQUEDA MANUAL)"); lbl_prod.setObjectName("eyebrowMuted")
        lbl_cant = QLabel("CANTIDAD"); lbl_cant.setObjectName("eyebrowMuted")
        form.addWidget(lbl_prod, 0, 0)
        form.addWidget(self.product, 1, 0)
        form.addWidget(lbl_cant, 0, 1)
        form.addWidget(self.quantity, 1, 1)
        form.addWidget(add, 1, 2)
        form.setColumnStretch(0, 1)
        left.addWidget(panel)

        # Carrito: tabla o estado vacío
        self.cart_stack = QStackedWidget()
        empty = QFrame()
        empty.setObjectName("sectionPanel")
        ev = QVBoxLayout(empty)
        ev.addStretch(1)
        e_icon = QLabel("⌗")
        e_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_icon.setStyleSheet(f"color: {colores['primario']}; font-size: 44px; font-weight: 800;")
        ev.addWidget(e_icon)
        e_txt = QLabel("Carrito vacío")
        e_txt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_txt.setStyleSheet(f"color: {colores['primario_oscuro']}; font-size: 17px; font-weight: 800;")
        ev.addWidget(e_txt)
        e_sub = QLabel("Escanea un código de barras o agrega un producto manualmente.")
        e_sub.setObjectName("muted")
        e_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(e_sub)
        ev.addStretch(1)
        self.cart_stack.addWidget(empty)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Producto", "Cantidad", "Precio", "Subtotal", "", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnHidden(5, True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.cart_stack.addWidget(self.table)
        left.addWidget(self.cart_stack, 1)

        self.feedback = QLabel("Listo. Conecta tu pistola USB y escanea, o escribe el codigo arriba.")
        self.feedback.setObjectName("muted")
        left.addWidget(self.feedback)
        content.addLayout(left, 1)

        # ─── Columna derecha: ticket de venta ───
        ticket = QFrame()
        ticket.setObjectName("sectionPanel")
        ticket.setFixedWidth(280)
        tv = QVBoxLayout(ticket)
        tv.setContentsMargins(18, 16, 18, 16)
        tv.setSpacing(6)
        t_head = QLabel("RESUMEN DE VENTA")
        t_head.setObjectName("eyebrow")
        tv.addWidget(t_head)
        self.items_label = QLabel("0 artículos")
        self.items_label.setObjectName("muted")
        tv.addWidget(self.items_label)
        tv.addSpacing(6)
        t_total_cap = QLabel("TOTAL A COBRAR")
        t_total_cap.setObjectName("eyebrowMuted")
        tv.addWidget(t_total_cap)
        self.total_label = QLabel("$0")
        self.total_label.setStyleSheet(
            f"color: {colores['primario_oscuro']}; font-size: 36px; font-weight: 850; letter-spacing: -0.8px;"
        )
        tv.addWidget(self.total_label)
        tv.addStretch(1)
        tv.addWidget(sell)
        tv.addWidget(clear)
        content.addWidget(ticket)

        layout.addLayout(content, 1)

        if self.scanner is not None:
            self.scanner.enabled_changed.connect(self._update_scanner_badge)
            self._update_scanner_badge(self.scanner.enabled)

        self.refresh()

    def set_active(self, active: bool):
        self._is_active = bool(active)

    def _update_scanner_badge(self, active: bool):
        if active:
            self.scanner_status.setText("Lector activo")
            self.scanner_status.setProperty("state", "on")
        else:
            self.scanner_status.setText("Lector inactivo")
            self.scanner_status.setProperty("state", "off")
        self.scanner_status.style().unpolish(self.scanner_status)
        self.scanner_status.style().polish(self.scanner_status)

    def focus_barcode(self):
        self.barcode_input.setFocus()
        self.barcode_input.selectAll()

    def _handle_barcode_input(self):
        code = self.barcode_input.text().strip()
        if not code:
            return
        self._add_by_barcode(code)
        self.barcode_input.clear()
        self.barcode_input.setFocus()

    def _on_barcode_scanned(self, code: str):
        if not self._is_active:
            return
        self.barcode_input.setText(code)
        self._add_by_barcode(code)
        QTimer.singleShot(150, self.barcode_input.clear)
        self.barcode_input.setFocus()

    def _add_by_barcode(self, code: str):
        product = self.store.find_product_by_barcode(code)
        if not product:
            QApplication.beep()
            self._notify(f"Producto no encontrado para '{code}'.", error=True)
            return

        existing = next((item for item in self.cart if item["product_id"] == product["id"]), None)
        already_in_cart = existing["quantity"] if existing else 0
        available = int(product["stock"])
        # Control de inventario: no permitir agregar más de lo disponible.
        if already_in_cart + 1 > available:
            QApplication.beep()
            self._notify(
                f"Sin stock suficiente para {product['name']} (disponible: {available}, en carrito: {already_in_cart}).",
                error=True,
            )
            return
        if existing:
            existing["quantity"] += 1
        else:
            self.cart.append(
                {
                    "product_id": int(product["id"]),
                    "name": product["name"],
                    "quantity": 1,
                    "unit_price": float(product["price"]),
                    "stock": available,
                }
            )
        self._render_cart()
        self._notify(f"+ {product['name']} (codigo {code})")

    def refresh(self):
        current_id = self.product.currentData()["id"] if self.product.currentData() else None
        self.product.clear()
        for product in self.store.product_options():
            label = f"{product['sku']} - {product['name']} (${product['price']:,.0f}, stock {product['stock']})"
            self.product.addItem(label, product)
        if current_id is not None:
            for i in range(self.product.count()):
                if self.product.itemData(i)["id"] == current_id:
                    self.product.setCurrentIndex(i)
                    break

    def _notify(self, message: str, error: bool = False):
        self.feedback.setText(message)
        self.feedback.setStyleSheet("color: #b42318; font-weight: 700;" if error else "")

    def _add_item(self):
        product = self.product.currentData()
        if not product:
            QMessageBox.information(self, "POS", "No hay productos disponibles.")
            return
        quantity = self.quantity.value()
        existing = next((item for item in self.cart if item["product_id"] == product["id"]), None)
        already_in_cart = existing["quantity"] if existing else 0
        available = int(product["stock"])
        if already_in_cart + quantity > available:
            QApplication.beep()
            self._notify(
                f"Sin stock suficiente para {product['name']} (disponible: {available}, en carrito: {already_in_cart}).",
                error=True,
            )
            return
        if existing:
            existing["quantity"] += quantity
        else:
            self.cart.append(
                {
                    "product_id": product["id"],
                    "name": product["name"],
                    "quantity": quantity,
                    "unit_price": float(product["price"]),
                    "stock": available,
                }
            )
        self._render_cart()
        self._notify(f"+ {product['name']} x {quantity}")
        self.quantity.setValue(1)

    def _finish_sale(self):
        if not self.cart:
            QMessageBox.information(self, "POS", "El carrito esta vacio.")
            return
        try:
            sale = self.store.create_sale(self.cart)
        except Exception as exc:
            QMessageBox.warning(self, "POS", str(exc))
            return
        detail = self.store.sale_detail(sale["sale_id"])
        self._show_receipt(detail or {"sale": sale, "items": self.cart})
        self._clear_cart()
        self.refresh()
        self.on_changed()

    def _show_receipt(self, detail):
        sale = detail.get("sale", {})
        items = detail.get("items", [])
        text = build_receipt_text(sale, items, branding=self._brand_callback())
        dialog = ReceiptDialog(text, self)
        dialog.exec()

    def _clear_cart(self):
        self.cart = []
        self._render_cart()
        self._notify("Carrito limpiado.")

    def _render_cart(self):
        colores = _cb_brand_colors()
        self.cart_stack.setCurrentIndex(1 if self.cart else 0)
        self.table.setRowCount(len(self.cart))
        total = 0.0
        unidades = 0
        for row_index, item in enumerate(self.cart):
            subtotal = item["quantity"] * item["unit_price"]
            total += subtotal
            unidades += item["quantity"]

            name_item = QTableWidgetItem(item["name"])
            self.table.setItem(row_index, 0, name_item)
            self.table.setCellWidget(row_index, 1, self._qty_widget(item, colores))
            price_item = QTableWidgetItem(f"${item['unit_price']:,.0f}".replace(",", "."))
            price_item.setForeground(QBrush(QColor("#6b7280")))
            self.table.setItem(row_index, 2, price_item)
            sub_item = QTableWidgetItem(f"${subtotal:,.0f}".replace(",", "."))
            sub_item.setForeground(QBrush(QColor(colores["primario_oscuro"])))
            bold = sub_item.font(); bold.setBold(True); sub_item.setFont(bold)
            self.table.setItem(row_index, 3, sub_item)
            btn = QPushButton("✕")
            btn.setObjectName("inlineDanger")
            btn.setFixedWidth(34)
            btn.setStyleSheet("padding: 0; min-height: 24px;")
            btn.setToolTip("Quitar del carrito")
            btn.clicked.connect(lambda _checked=False, pid=item["product_id"]: self._remove_item(pid))
            wrap = QWidget(); wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 2, 0, 2)
            wl.addStretch(1); wl.addWidget(btn); wl.addStretch(1)
            self.table.setCellWidget(row_index, 4, wrap)
            self.table.setItem(row_index, 5, QTableWidgetItem(str(item["product_id"])))
        self.total_label.setText(f"${total:,.0f}".replace(",", "."))
        n = len(self.cart)
        self.items_label.setText(f"{n} artículo(s) · {unidades} unidad(es)" if n else "0 artículos")

    def _qty_widget(self, item, colores):
        """Control −/+ de cantidad para una fila del carrito."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        ac = QColor(colores["primario"])
        btn_style = (
            f"QPushButton {{ background: rgba({ac.red()},{ac.green()},{ac.blue()},0.10);"
            f" color: {colores['primario']}; border: 0; border-radius: 12px;"
            f" font-size: 14px; font-weight: 800; padding: 0;"
            f" min-height: 24px; min-width: 26px; }}"
            f"QPushButton:hover {{ background: rgba({ac.red()},{ac.green()},{ac.blue()},0.22); }}"
        )
        minus = QPushButton("−"); plus = QPushButton("＋")
        for b in (minus, plus):
            b.setFixedSize(26, 24)
            b.setStyleSheet(btn_style)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        qty = QLabel(str(item["quantity"]))
        qty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qty.setMinimumWidth(28)
        qty.setStyleSheet(f"color: {colores['primario_oscuro']}; font-weight: 800;")
        pid = item["product_id"]
        minus.clicked.connect(lambda _=False: self._change_qty(pid, -1))
        plus.clicked.connect(lambda _=False: self._change_qty(pid, +1))
        h.addStretch(1)
        h.addWidget(minus); h.addWidget(qty); h.addWidget(plus)
        h.addStretch(1)
        return w

    def _change_qty(self, product_id, delta):
        item = next((i for i in self.cart if i["product_id"] == product_id), None)
        if item is None:
            return
        available = int(item.get("stock", 0))
        if delta > 0 and item["quantity"] + delta > available:
            QApplication.beep()
            self._notify(f"Sin stock suficiente para {item['name']} (disponible: {available}).", error=True)
            return
        item["quantity"] += delta
        if item["quantity"] <= 0:
            self.cart.remove(item)
            self._notify("Item removido del carrito.")
        self._render_cart()

    def _remove_item(self, product_id):
        self.cart = [item for item in self.cart if item["product_id"] != product_id]
        self._render_cart()
        self._notify("Item removido del carrito.")


# =============================================================================
# Ventas
# =============================================================================
class SalesPage(QWidget):
    def __init__(self, store: LocalStore, brand_callback=None):
        super().__init__()
        self.store = store
        self._brand_callback = brand_callback or (lambda: branding_mod.DEFAULTS)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Ventas")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.summary_grid = QGridLayout()
        self.summary_grid.setSpacing(12)
        layout.addLayout(self.summary_grid)

        actions = QHBoxLayout()
        refresh = QPushButton("Actualizar")
        refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        actions.addWidget(refresh)
        actions.addStretch(1)
        layout.addLayout(actions)

        # Tabs: ventas locales (POS desktop) y ventas web (pedidos)
        self.tabs = QTabWidget()

        # Tab 1: ventas locales (lo que habia antes)
        local_widget = QWidget()
        local_layout = QVBoxLayout(local_widget)
        local_layout.setContentsMargins(0, 8, 0, 0)
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Recibo", "Fecha", "Items", "Total"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemDoubleClicked.connect(self._open_detail)
        local_layout.addWidget(self.table)
        local_hint = QLabel("Doble click sobre una fila para ver el detalle del recibo.")
        local_hint.setObjectName("muted")
        local_layout.addWidget(local_hint)
        self.tabs.addTab(local_widget, "Ventas POS desktop")

        # Tab 2: ventas web (cache de pedidos PayU) — editable bidi
        remote_widget = QWidget()
        remote_layout = QVBoxLayout(remote_widget)
        remote_layout.setContentsMargins(0, 8, 0, 0)

        # Header row con LiveToggle
        remote_header = QHBoxLayout()
        remote_title = QLabel("Pedidos del ecommerce web")
        remote_title.setObjectName("highlightText")
        remote_header.addWidget(remote_title)
        remote_header.addStretch(1)
        self.remote_live = LiveToggle()
        self.remote_live.toggled.connect(self._on_remote_live_toggled)
        remote_header.addWidget(self.remote_live)
        remote_layout.addLayout(remote_header)

        self.remote_table = QTableWidget()
        self.remote_table.setColumnCount(8)
        self.remote_table.setHorizontalHeaderLabels(
            ["ID", "Referencia", "Cliente", "Pago", "Envio", "Total", "Fecha", "Acciones"]
        )
        self.remote_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.remote_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_table.verticalHeader().setDefaultSectionSize(44)
        self.remote_table.setAlternatingRowColors(True)
        remote_layout.addWidget(self.remote_table)
        remote_hint = QLabel(
            "Edita el estado de pago/envío con el botón en cada fila. "
            "Los cambios se sincronizan al VPS automáticamente."
        )
        remote_hint.setObjectName("muted")
        remote_hint.setWordWrap(True)
        remote_layout.addWidget(remote_hint)
        self.tabs.addTab(remote_widget, "Pedidos web")

        layout.addWidget(self.tabs)
        self.refresh()

    def refresh(self):
        clear_layout(self.summary_grid)
        summary = self.store.sales_summary()
        cards = [
            ("Hoy", f"${summary['today']['total']:,.0f}  ({summary['today']['cnt']} recibos)"),
            ("Mes actual", f"${summary['month']['total']:,.0f}  ({summary['month']['cnt']} recibos)"),
            ("Historico", f"${summary['all']['total']:,.0f}  ({summary['all']['cnt']} recibos)"),
        ]
        for index, (label, value) in enumerate(cards):
            self.summary_grid.addWidget(metric_card(label, value), 0, index)

        sales = self.store.sales(limit=200)
        self.table.setRowCount(len(sales))
        for row_index, sale in enumerate(sales):
            values = [
                sale["id"],
                sale["receipt_number"],
                sale["created_at"],
                sale.get("total_items") or 0,
                f"${sale['total']:,.0f}",
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, sale["id"])
                self.table.setItem(row_index, col_index, item)

        # Pedidos web (cache)
        remote = self.store.list_remote_sales(limit=200)
        self._render_remote_orders(remote)

    def _render_remote_orders(self, remote):
        self.remote_table.setRowCount(len(remote))
        for row_index, r in enumerate(remote):
            values = [
                r["remote_id"],
                r.get("reference") or "",
                r.get("customer_name") or "",
                r.get("status_payment") or "",
                r.get("status_shipping") or "",
                f"${(r.get('total') or 0):,.0f}",
                r.get("updated_at") or "",
            ]
            for col_index, value in enumerate(values):
                self.remote_table.setItem(row_index, col_index, QTableWidgetItem(str(value)))
            # Botón "Editar estado" en columna 7 (deshabilitado en live mode)
            edit_btn = QPushButton("Editar estado")
            edit_btn.setObjectName("inlineDanger" if False else "secondaryAction")
            edit_btn.setEnabled(not (hasattr(self, "remote_live") and self.remote_live.is_live()))
            edit_btn.clicked.connect(lambda checked=False, order=r: self._open_order_dialog(order))
            self.remote_table.setCellWidget(row_index, 7, edit_btn)

    def _open_order_dialog(self, order: dict):
        dlg = OrderStatusDialog(self, self.store, order, on_saved=self._on_order_saved)
        dlg.exec()

    def _on_order_saved(self):
        # Refrescar tabla con cache local (que ya fue actualizado optimistamente)
        self.refresh()
        # Disparar sync inmediato si hay otros pendientes — el SyncPage tiene el timer,
        # aquí solo notificamos al shell que algo cambió.

    def _on_remote_live_toggled(self, live: bool):
        if not live:
            self.refresh()
            return
        sync_state = sync_cfg.load(app_data_dir())
        base_url = sync_state.get("base_url", "")
        api_key = sync_state.get("api_key", "")
        if not base_url or not api_key:
            self.remote_live.set_state(False)
            self.remote_live.show_error("Sincronización no configurada")
            return
        try:
            client = SyncClient(base_url, api_key)
            resp = client.pull_orders_live(limit=500)
        except (ValueError, SyncError):
            self.remote_live.set_state(False)
            self.remote_live.show_error("Sin conexión")
            return
        # El response tiene shape {items: [...]} — adaptamos al formato de cache
        items = resp.get("items", [])
        adapted = [
            {
                "remote_id":       item.get("remote_id"),
                "reference":       item.get("reference"),
                "customer_name":   item.get("customer_name"),
                "status_payment":  item.get("status_payment"),
                "status_shipping": item.get("status_shipping"),
                "total":           item.get("total"),
                "updated_at":      item.get("updated_at"),
            }
            for item in items
        ]
        self._render_remote_orders(adapted)

    def _open_detail(self, item):
        row = item.row()
        sale_id_item = self.table.item(row, 0)
        if not sale_id_item:
            return
        sale_id = int(sale_id_item.text())
        detail = self.store.sale_detail(sale_id)
        if not detail:
            QMessageBox.warning(self, "Ventas", "No se encontro el detalle de la venta.")
            return
        text = build_receipt_text(detail["sale"], detail["items"], branding=self._brand_callback())
        ReceiptDialog(text, self).exec()


# =============================================================================
# Usuarios
# =============================================================================
class UsersPage(QWidget):
    def __init__(self, store: LocalStore, on_changed, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.can = can_callback or (lambda m, a: True)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Header con título + LiveToggle
        header_row = QHBoxLayout()
        title = QLabel("Usuarios locales")
        title.setObjectName("pageTitle")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self.live = LiveToggle()
        self.live.toggled.connect(self._on_live_toggled)
        header_row.addWidget(self.live)
        layout.addLayout(header_row)

        info = QLabel(
            "Solo se usan dentro de esta instalacion. Para sincronizacion con la web se necesita el modulo de sync."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        layout.addWidget(info)

        actions = QHBoxLayout()
        self.new_btn = QPushButton("Nuevo usuario")
        self.new_btn.clicked.connect(self._create)
        self.edit_btn = QPushButton("Editar seleccionado")
        self.edit_btn.setObjectName("secondaryAction")
        self.edit_btn.clicked.connect(self._edit_selected)
        self.deactivate_btn = QPushButton("Desactivar seleccionado")
        self.deactivate_btn.setObjectName("dangerAction")
        self.deactivate_btn.clicked.connect(self._deactivate_selected)
        actions.addWidget(self.new_btn)
        actions.addWidget(self.edit_btn)
        actions.addWidget(self.deactivate_btn)
        actions.addStretch(1)
        self.live_badge = QLabel("●  VPS EN VIVO · SOLO LECTURA")
        self.live_badge.setObjectName("pageBadge")
        self.live_badge.setProperty("state", "warning")
        self.live_badge.setVisible(False)
        actions.addWidget(self.live_badge)
        layout.addLayout(actions)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Email", "Nombre", "Rol", "Activo"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.refresh()

    # ── Live toggle: cache local ↔ VPS en vivo ──
    def _on_live_toggled(self, live: bool):
        self._set_crud_enabled(not live)
        if live:
            self._refresh_live_from_vps()
        else:
            self.refresh()

    def _set_crud_enabled(self, enabled: bool):
        self.new_btn.setEnabled(enabled)
        self.edit_btn.setEnabled(enabled)
        self.deactivate_btn.setEnabled(enabled)
        self.live_badge.setVisible(not enabled)

    def _refresh_live_from_vps(self):
        sync_state = sync_cfg.load(app_data_dir())
        base_url = sync_state.get("base_url", "")
        api_key = sync_state.get("api_key", "")
        if not base_url or not api_key:
            self.live.set_state(False)
            self._set_crud_enabled(True)
            self.live.show_error("Sincronización no configurada")
            return
        try:
            client = SyncClient(base_url, api_key)
            resp = client.pull_users_live(limit=1000)
        except (ValueError, SyncError):
            self.live.set_state(False)
            self._set_crud_enabled(True)
            self.live.show_error("Sin conexión")
            return
        items = resp.get("items", [])
        self.table.setRowCount(len(items))
        for row_idx, u in enumerate(items):
            values = [
                str(u.get("remote_id", "")),
                u.get("email", ""),
                u.get("nombre", ""),
                u.get("rol_nombre", ""),
                "Si" if (u.get("estado") or "").lower() != "deshabilitado" else "No",
            ]
            for col_idx, val in enumerate(values):
                self.table.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))

    def refresh(self):
        users = self.store.users()
        self.table.setRowCount(len(users))
        for row_index, user in enumerate(users):
            values = [
                user["id"],
                user["email"],
                user["name"],
                user["role"],
                "Si" if user["active"] else "No",
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, user["id"])
                self.table.setItem(row_index, col_index, item)

    def _selected_user(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item0 = self.table.item(row, 0)
        if not item0:
            return None
        user_id = int(item0.text())
        return next((u for u in self.store.users() if u["id"] == user_id), None)

    def _create(self):
        dialog = UserEditorDialog(self, store=self.store, user=None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.on_changed()

    def _edit_selected(self):
        user = self._selected_user()
        if not user:
            QMessageBox.information(self, "Usuarios", "Selecciona un usuario primero.")
            return
        dialog = UserEditorDialog(self, store=self.store, user=user)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.on_changed()

    def _deactivate_selected(self):
        user = self._selected_user()
        if not user:
            QMessageBox.information(self, "Usuarios", "Selecciona un usuario primero.")
            return
        if not user["active"]:
            QMessageBox.information(self, "Usuarios", "Ese usuario ya esta desactivado.")
            return
        confirm = QMessageBox.question(
            self,
            "Desactivar usuario",
            f"Confirmar desactivacion de '{user['email']}'? Conserva su historial.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.deactivate_user(user["id"])
        except ValueError as exc:
            QMessageBox.warning(self, "Usuarios", str(exc))
            return
        self.refresh()


class UserEditorDialog(QDialog):
    def __init__(self, parent, store: LocalStore, user=None):
        super().__init__(parent)
        self.store = store
        self.user = user
        self.setWindowTitle("Nuevo usuario" if not user else f"Editar {user['email']}")
        self.setMinimumWidth(380)

        form = QFormLayout(self)
        self.email = QLineEdit(user["email"] if user else "")
        self.name = QLineEdit(user["name"] if user else "")
        self.role = QComboBox()
        self.role.addItems(ROLES)
        if user and user["role"] in ROLES:
            self.role.setCurrentText(user["role"])
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "Obligatoria al crear" if not user else "Dejar en blanco para no cambiar"
        )
        self.active = QCheckBox("Usuario activo")
        self.active.setChecked(bool(user["active"]) if user else True)

        form.addRow("Correo", self.email)
        form.addRow("Nombre", self.name)
        form.addRow("Rol", self.role)
        form.addRow("Contrasena", self.password)
        form.addRow(self.active)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        try:
            self.store.save_user(
                self.user["id"] if self.user else None,
                self.email.text(),
                self.name.text(),
                self.role.currentText(),
                password=self.password.text() or None,
                active=self.active.isChecked(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Usuarios", str(exc))
            return
        self.accept()


# =============================================================================
# Recibo
# =============================================================================
class ReceiptDialog(QDialog):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recibo")
        self.setMinimumSize(440, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setStyleSheet(
            "QTextEdit { font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; "
            "background: #ffffff; color: #091C5A; border: 1px solid #e3e6f0; border-radius: 10px; padding: 10px; }"
        )
        body.setPlainText(text)
        layout.addWidget(body, 1)

        actions = QHBoxLayout()
        copy_btn = QPushButton("Copiar al portapapeles")
        copy_btn.setObjectName("secondaryAction")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(text))
        close_btn = QPushButton("Cerrar")
        close_btn.clicked.connect(self.accept)
        actions.addWidget(copy_btn)
        actions.addStretch(1)
        actions.addWidget(close_btn)
        layout.addLayout(actions)


def build_receipt_text(sale, items, branding=None):
    empresa = (branding or {}).get("empresa", {}) if branding else {}
    nombre = (empresa.get("nombre") or "CyberShop").upper()
    pie = empresa.get("recibo_pie") or "Gracias por su compra."
    lines = []
    lines.append(f"{nombre} - RECIBO LOCAL")
    if empresa.get("direccion"):
        lines.append(empresa["direccion"])
    if empresa.get("telefono"):
        lines.append(f"Tel: {empresa['telefono']}")
    if empresa.get("website"):
        lines.append(empresa["website"])
    lines.append("=" * 40)
    lines.append(f"Recibo : {sale.get('receipt_number') or sale.get('receipt') or '-'}")
    lines.append(f"Fecha  : {sale.get('created_at', '-')}")
    lines.append("-" * 40)
    lines.append(f"{'Producto':<22}{'Cant':>5}{'Subtot':>13}")
    lines.append("-" * 40)
    total = 0.0
    for item in items:
        name = (item.get("name") or "")[:22]
        qty = int(item.get("quantity", 0))
        unit = float(item.get("unit_price", 0))
        subtotal = float(item.get("line_total", qty * unit))
        total += subtotal
        lines.append(f"{name:<22}{qty:>5}{('$' + format(subtotal, ',.0f')):>13}")
    lines.append("-" * 40)
    grand_total = float(sale.get("total", total))
    lines.append(f"{'TOTAL':<22}{'':>5}{('$' + format(grand_total, ',.0f')):>13}")
    lines.append("=" * 40)
    lines.append(pie)
    return "\n".join(lines)


# =============================================================================
# Sync / estado de la BD
# =============================================================================
class SyncPage(QWidget):
    def __init__(self, store: LocalStore, on_changed, base_dir: Path, user_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self._base_dir = base_dir
        self._user_callback = user_callback or (lambda: None)
        self._sync_state = sync_cfg.load(base_dir)
        self._sync_worker = None          # hilo de fondo del sync (no solapar)
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._sync_now_silent)
        self._build()
        self._restart_auto_timer()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Sincronizacion")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Configura la URL del servidor y la API key entregada por el admin. "
            "Las ventas y movimientos del POS suben al VPS, los productos del VPS bajan al desktop."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # Panel de configuracion remota
        sync_panel = QFrame()
        sync_panel.setObjectName("sectionPanel")
        sync_form = QFormLayout(sync_panel)
        sync_form.setContentsMargins(16, 16, 16, 16)
        sync_form.setSpacing(8)

        self.url_input = QLineEdit(self._sync_state.get("base_url", ""))
        self.url_input.setPlaceholderText("https://cybershopcol.com")

        self.key_input = QLineEdit(self._sync_state.get("api_key", ""))
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("cs_sync_...")

        self.auto_check = QCheckBox("Sincronizar automaticamente cada")
        self.auto_check.setChecked(bool(self._sync_state.get("enabled", False)))
        self.auto_check.toggled.connect(self._on_auto_toggle)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setSuffix(" seg")
        self.interval_spin.setValue(int(self._sync_state.get("interval_sec", 30)))
        self.interval_spin.valueChanged.connect(self._on_interval_change)

        auto_row = QHBoxLayout()
        auto_row.addWidget(self.auto_check)
        auto_row.addWidget(self.interval_spin)
        auto_row.addStretch(1)

        sync_form.addRow("URL servidor:", self.url_input)
        sync_form.addRow("API key:", self.key_input)
        sync_form.addRow("", _wrap_layout(auto_row))

        actions_row = QHBoxLayout()
        save_btn = QPushButton("Guardar configuracion")
        save_btn.setObjectName("secondaryAction")
        save_btn.clicked.connect(self._save_sync_config)
        self.test_btn = QPushButton("Probar conexion")
        self.test_btn.setObjectName("secondaryAction")
        self.test_btn.clicked.connect(self._test_connection)
        self.sync_now_btn = QPushButton("Sincronizar ahora")
        self.sync_now_btn.setObjectName("primaryAction")
        self.sync_now_btn.clicked.connect(self._sync_now_clicked)
        actions_row.addWidget(save_btn)
        actions_row.addWidget(self.test_btn)
        actions_row.addWidget(self.sync_now_btn)
        actions_row.addStretch(1)
        sync_form.addRow("", _wrap_layout(actions_row))

        self.sync_status = QLabel(self._format_status())
        self.sync_status.setWordWrap(True)
        sync_form.addRow("Estado:", self.sync_status)

        layout.addWidget(sync_panel)

        self.info_panel = QFrame()
        self.info_panel.setObjectName("sectionPanel")
        info_layout = QFormLayout(self.info_panel)
        info_layout.setContentsMargins(16, 16, 16, 16)
        self.lbl_path = QLabel("-")
        self.lbl_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_path.setWordWrap(True)
        self.lbl_size = QLabel("-")
        self.lbl_users = QLabel("-")
        self.lbl_products = QLabel("-")
        self.lbl_sales = QLabel("-")
        self.lbl_movements = QLabel("-")
        self.lbl_outbox = QLabel("-")
        info_layout.addRow("Archivo BD:", self.lbl_path)
        info_layout.addRow("Tamano:", self.lbl_size)
        info_layout.addRow("Usuarios:", self.lbl_users)
        info_layout.addRow("Productos activos:", self.lbl_products)
        info_layout.addRow("Ventas:", self.lbl_sales)
        info_layout.addRow("Movimientos inventario:", self.lbl_movements)
        info_layout.addRow("Outbox (pendiente / total):", self.lbl_outbox)
        layout.addWidget(self.info_panel)

        actions = QHBoxLayout()
        refresh = QPushButton("Actualizar")
        refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        full_pull = QPushButton("⭳  Forzar descarga completa de productos")
        full_pull.setObjectName("secondaryAction")
        full_pull.setToolTip("Reinicia el cursor y vuelve a bajar TODO el catálogo "
                             "(recupera códigos de barras de productos nuevos)")
        full_pull.clicked.connect(self._force_full_products)
        mark_synced = QPushButton("Marcar outbox como sincronizada")
        mark_synced.setObjectName("secondaryAction")
        mark_synced.clicked.connect(self._mark_synced)
        clear_demo = QPushButton("Limpiar datos demo (productos/ventas)")
        clear_demo.setObjectName("dangerAction")
        clear_demo.clicked.connect(self._clear_demo)
        actions.addWidget(refresh)
        actions.addWidget(full_pull)
        actions.addWidget(mark_synced)
        actions.addWidget(clear_demo)
        actions.addStretch(1)
        layout.addLayout(actions)

        # Panel de operaciones RECHAZADAS por el servidor (licencia/rol).
        # Oculto si no hay rechazos. Evita que el outbox reintente indefinidamente.
        self.rejections_panel = QFrame()
        self.rejections_panel.setObjectName("sectionPanel")
        rej_layout = QVBoxLayout(self.rejections_panel)
        rej_layout.setContentsMargins(16, 12, 16, 12); rej_layout.setSpacing(8)
        rej_head = QHBoxLayout()
        self.rejections_label = QLabel("")
        self.rejections_label.setStyleSheet("color:#b42318; font-weight:800;")
        self.rejections_label.setWordWrap(True)
        rej_head.addWidget(self.rejections_label, 1)
        rej_dismiss = QPushButton("Descartar avisos")
        rej_dismiss.setObjectName("secondaryAction")
        rej_dismiss.clicked.connect(self._dismiss_rejections)
        rej_head.addWidget(rej_dismiss)
        rej_layout.addLayout(rej_head)
        self.rejections_detail = QLabel("")
        self.rejections_detail.setObjectName("muted"); self.rejections_detail.setWordWrap(True)
        rej_layout.addWidget(self.rejections_detail)
        self.rejections_panel.setVisible(False)
        layout.addWidget(self.rejections_panel)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "Entidad", "Entidad ID", "Accion", "Creado", "Sincronizado"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # ─── Panel de diagnóstico bidi: matriz por entidad ───
        diag_eyebrow = QLabel("DIAGNÓSTICO BIDIRECCIONAL")
        diag_eyebrow.setObjectName("eyebrow")
        layout.addWidget(diag_eyebrow)

        self.bidi_table = QTableWidget()
        self.bidi_table.setColumnCount(5)
        self.bidi_table.setHorizontalHeaderLabels(
            ["Entidad", "PULL ←VPS", "PUSH →VPS", "Cursor", "Último intento"]
        )
        self.bidi_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.bidi_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.bidi_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.bidi_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.bidi_table.setAlternatingRowColors(True)
        self.bidi_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bidi_table.setMaximumHeight(260)
        layout.addWidget(self.bidi_table)

        self.refresh()

    def refresh(self):
        info = self.store.db_info()
        counts = info["counts"]
        self.lbl_path.setText(info["path"])
        self.lbl_size.setText(f"{info['size_bytes']:,} bytes")
        self.lbl_users.setText(str(counts["users"]))
        self.lbl_products.setText(str(counts["products"]))
        self.lbl_sales.setText(str(counts["sales"]))
        self.lbl_movements.setText(str(counts["movements"]))
        self.lbl_outbox.setText(f"{counts['outbox_pending']} / {counts['outbox_total']}")

        items = self.store.outbox_items()
        self.table.setRowCount(len(items))
        for row_index, item in enumerate(items):
            values = [
                item["id"],
                item["entity"],
                item["entity_id"],
                item["action"],
                item["created_at"],
                item["synced_at"] or "Pendiente",
            ]
            for col_index, value in enumerate(values):
                self.table.setItem(row_index, col_index, QTableWidgetItem(str(value)))

        self._refresh_bidi_matrix()
        self._refresh_rejections()

    def _refresh_rejections(self):
        """Muestra las operaciones rechazadas por el servidor (licencia/rol)."""
        rejects = self.store.get_rejections(limit=50)
        if not rejects:
            self.rejections_panel.setVisible(False)
            return
        self.rejections_label.setText(
            f"⚠ {len(rejects)} operación(es) rechazada(s) por el servidor"
        )
        # Resumen legible por motivo/entidad (máx. algunas líneas)
        lineas = []
        for r in rejects[:8]:
            ent = r.get("entity") or "?"
            acc = r.get("action") or "?"
            motivo = (r.get("motivo") or "sin motivo").strip()
            lineas.append(f"• {ent}/{acc}: {motivo}")
        if len(rejects) > 8:
            lineas.append(f"… y {len(rejects) - 8} más")
        self.rejections_detail.setText("\n".join(lineas))
        self.rejections_panel.setVisible(True)

    def _dismiss_rejections(self):
        self.store.clear_rejections()
        self._refresh_rejections()

    def _refresh_bidi_matrix(self):
        """Pinta la matriz de bidireccionalidad por entidad. Lee los cursores
        y last_sync_status de sync_config.json para mostrar contexto real."""
        state = sync_cfg.load(self._base_dir)
        rows = [
            # (entidad, PULL, PUSH, cursor_field)
            ("Productos",        "✓", "✓", "cursor_products"),
            ("Categorías",       "✓", "✓", "cursor_generos"),
            ("Usuarios",         "✓", "✓", "cursor_users"),
            ("Ventas POS",       "—", "✓", None),
            ("Movim. inventario", "—", "✓", None),
            ("Pedidos web",      "✓", "✓", "cursor_sales_web"),
            ("Auditoría stock",  "✓", "—", "cursor_inventory_log"),
            ("Branding",         "✓", "—", None),
        ]
        last_at = state.get("last_sync_at") or "—"
        last_status = state.get("last_sync_status") or "sin intentos"
        last_str = f"{last_at}  ({last_status})"

        self.bidi_table.setRowCount(len(rows))
        for row_idx, (entidad, pull, push, cursor_field) in enumerate(rows):
            self.bidi_table.setItem(row_idx, 0, QTableWidgetItem(entidad))

            pull_item = QTableWidgetItem(pull)
            pull_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.bidi_table.setItem(row_idx, 1, pull_item)

            push_item = QTableWidgetItem(push)
            push_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.bidi_table.setItem(row_idx, 2, push_item)

            cursor_val = state.get(cursor_field, "") if cursor_field else "—"
            self.bidi_table.setItem(row_idx, 3, QTableWidgetItem(cursor_val or "—"))

            self.bidi_table.setItem(row_idx, 4, QTableWidgetItem(last_str))

    def _mark_synced(self):
        confirm = QMessageBox.question(
            self,
            "Marcar outbox",
            "Esto marca todos los pendientes como sincronizados (no envia nada). Continuar?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.reset_outbox()
        self.refresh()
        self.on_changed()

    # ── Sync remoto ──────────────────────────────────────────
    def _format_status(self):
        last_sync = self._sync_state.get("last_sync_at") or "nunca"
        last_status = self._sync_state.get("last_sync_status") or "sin intentos"
        return f"Ultimo intento: {last_sync} - {last_status}"

    def _save_sync_config(self):
        url = self.url_input.text().strip().rstrip("/")
        key = self.key_input.text().strip()
        sync_cfg.update(
            self._base_dir,
            base_url=url,
            api_key=key,
            enabled=self.auto_check.isChecked(),
            interval_sec=self.interval_spin.value(),
        )
        self._sync_state = sync_cfg.load(self._base_dir)
        self._restart_auto_timer()
        self._set_status_text("Configuracion guardada.", error=False)

    def _on_auto_toggle(self, checked):
        sync_cfg.update(self._base_dir, enabled=bool(checked))
        self._sync_state["enabled"] = bool(checked)
        self._restart_auto_timer()

    def _on_interval_change(self, value):
        sync_cfg.update(self._base_dir, interval_sec=int(value))
        self._sync_state["interval_sec"] = int(value)
        self._restart_auto_timer()

    def _restart_auto_timer(self):
        self._auto_timer.stop()
        if self._sync_state.get("enabled") and self._sync_state.get("base_url") and self._sync_state.get("api_key"):
            interval_ms = max(5, int(self._sync_state.get("interval_sec", 30))) * 1000
            self._auto_timer.start(interval_ms)
            # Sync inicial poco después de arrancar (no esperar al primer tick).
            QTimer.singleShot(1500, self._sync_now_silent)

    def _build_client(self):
        return SyncClient(self._sync_state.get("base_url", ""), self._sync_state.get("api_key", ""))

    def _test_connection(self):
        try:
            client = self._build_client()
            info = client.health()
        except (ValueError, SyncError) as exc:
            self._set_status_text(f"Conexion fallo: {exc}", error=True)
            return
        msg = f"Conexion OK -> tenant_db={info.get('tenant_db')}, server_time={info.get('server_time')}"
        self._set_status_text(msg, error=False)

    def _sync_now_clicked(self):
        self._start_sync(silent=False)

    def _sync_now_silent(self):
        """Variante para timer automatico: no muestra dialogos."""
        self._start_sync(silent=True)

    def _start_sync(self, silent: bool):
        """Lanza el sync bidireccional en un hilo de fondo (no congela la UI).
        No solapa ejecuciones: si ya hay un sync corriendo, ignora la petición."""
        if self._sync_worker is not None and self._sync_worker.isRunning():
            return
        if not self._sync_state.get("base_url") or not self._sync_state.get("api_key"):
            if not silent:
                self._set_status_text("Configura la URL y la API key primero.", error=True)
            return
        self._set_syncing_ui(True)
        self._sync_worker = _BgWorker(lambda: self._do_sync(), self)
        self._sync_worker.done.connect(lambda stats: self._on_sync_done(stats, silent))
        self._sync_worker.failed.connect(lambda msg: self._on_sync_failed(msg, silent))
        self._sync_worker.start()

    def _on_sync_done(self, stats, silent: bool):
        self._set_syncing_ui(False)
        suffix = f" ({stats['pushed_errors']} errores)" if stats.get("pushed_errors") else ""
        self._record_status("ok" + suffix)
        msg = self._format_stats(stats)
        if silent:
            self.sync_status.setText(self._format_status() + " | " + msg)
            self.sync_status.setStyleSheet("")
        else:
            self._set_status_text(msg, error=stats.get("pushed_errors", 0) > 0)
        self.refresh()
        self.on_changed()

    def _on_sync_failed(self, msg, silent: bool):
        self._set_syncing_ui(False)
        self._record_status(f"error: {str(msg)[:120]}")
        if silent:
            self.sync_status.setText(self._format_status())
            self.sync_status.setStyleSheet("color: #b42318;")
        else:
            self._set_status_text(f"Sync fallo: {msg}", error=True)

    def _set_syncing_ui(self, busy: bool):
        """Deshabilita los botones de sync mientras corre en segundo plano."""
        self.sync_now_btn.setEnabled(not busy)
        self.test_btn.setEnabled(not busy)
        self.sync_now_btn.setText("Sincronizando…" if busy else "Sincronizar ahora")

    def _force_full_products(self):
        """Reinicia el cursor de productos y dispara un sync: baja TODO el catálogo
        (recupera códigos de barras de altas recientes en la web)."""
        if self._sync_worker is not None and self._sync_worker.isRunning():
            QMessageBox.information(self, "Sincronización", "Ya hay un sync en curso; espera a que termine.")
            return
        sync_cfg.update(self._base_dir, cursor_products="", last_pull_at="")
        self._sync_state["cursor_products"] = ""
        self._sync_state["last_pull_at"] = ""
        self._set_status_text("Descargando catálogo completo…", error=False)
        self._start_sync(silent=False)

    def _format_stats(self, stats):
        pulled_total = (stats["pulled_products"] + stats["pulled_users"] +
                        stats["pulled_generos"] + stats["pulled_sales_web"] +
                        stats["pulled_inventory_log"])
        parts = [
            f"{pulled_total} bajaron",
            f"{stats['pushed_applied']} subieron",
        ]
        if stats["pushed_stale"]:
            parts.append(f"{stats['pushed_stale']} descartados (server tenia version mas nueva)")
        if stats.get("pushed_forbidden"):
            parts.append(f"{stats['pushed_forbidden']} rechazados (licencia/rol)")
        if stats["pushed_errors"]:
            parts.append(f"{stats['pushed_errors']} errores")
        return ", ".join(parts)

    def _do_sync(self):
        """Ejecuta sync bidireccional completo.

        Orden:
        1. Pull generos (FK necesaria para productos).
        2. Pull productos (con include_inactive para tombstones).
        3. Pull usuarios (perfil).
        4. Pull pedidos web (cache).
        5. Pull inventory_log web (cache).
        6. Push outbox (sale, inventory_movement, product, user).

        Retorna dict con conteos por categoria.
        """
        client = self._build_client()
        stats = {"pulled_generos": 0, "pulled_products": 0, "pulled_users": 0,
                 "pulled_sales_web": 0, "pulled_inventory_log": 0, "pulled_restaurant": 0, "pulled_contabilidad": 0,
                 "pulled_quotes": 0, "pulled_cobros": 0, "pulled_crm": 0, "pulled_nomina": 0, "pulled_modules": 0,
                 "pushed_applied": 0, "pushed_skipped": 0, "pushed_stale": 0,
                 "pushed_forbidden": 0, "pushed_errors": 0}

        # --- Pull flags de módulos del plan (gating por tenant) ---
        # Aislado en su propio try/except: nunca debe romper el sync de datos.
        try:
            cfg = client.pull_config()
            mods = cfg.get("modules")
            if isinstance(mods, dict) and mods:   # solo persistir si trae algo (no pisar caché buena con {})
                self.store.set_tenant_modules(mods)
                stats["pulled_modules"] = len(mods)
            perms = cfg.get("permissions")
            if isinstance(perms, dict) and perms:  # manifiesto rol→{modules,actions}
                self.store.set_permissions_manifest(perms)
        except SyncError as exc:
            print(f"[sync] pull_config: {exc}")
        except Exception as exc:
            print(f"[sync] pull_config inesperado: {exc}")

        # --- Pull generos ---
        # Auto-sanación: si la tabla local está vacía pero el cursor quedó
        # adelantado (p. ej. tras 'Limpiar datos'), forzar pull completo.
        try:
            gen_since = self._sync_state.get("cursor_generos") or None
            if self.store.count_generos() == 0:
                gen_since = None
            r = client.pull_generos(since=gen_since)
            for g in r.get("items", []):
                try:
                    self.store.upsert_genero_from_remote(g)
                    stats["pulled_generos"] += 1
                except Exception as exc:
                    print(f"[sync] genero fallo: {exc}")
            if r.get("cursor"):
                sync_cfg.update(self._base_dir, cursor_generos=r["cursor"])
                self._sync_state["cursor_generos"] = r["cursor"]
        except SyncError as exc:
            print(f"[sync] pull_generos: {exc}")

        # --- Pull productos (incluyendo tombstones) ---
        try:
            since = self._sync_state.get("cursor_products") or self._sync_state.get("last_pull_at") or None
            if self.store.count_products() == 0:
                since = None  # auto-sanación: tabla vacía → pull completo
            r = client.pull_products(since=since, limit=500, include_inactive=True)
            for p in r.get("items", []):
                try:
                    self.store.upsert_product_from_remote(p)
                    stats["pulled_products"] += 1
                except Exception as exc:
                    print(f"[sync] producto fallo {p.get('sku')}: {exc}")
            if r.get("cursor"):
                sync_cfg.update(self._base_dir, cursor_products=r["cursor"], last_pull_at=r["cursor"])
                self._sync_state["cursor_products"] = r["cursor"]
        except SyncError as exc:
            print(f"[sync] pull_products: {exc}")

        # --- Pull usuarios ---
        try:
            r = client.pull_users(since=self._sync_state.get("cursor_users") or None)
            for u in r.get("items", []):
                try:
                    self.store.upsert_user_from_remote(u)
                    stats["pulled_users"] += 1
                except Exception as exc:
                    print(f"[sync] user fallo: {exc}")
            if r.get("cursor"):
                sync_cfg.update(self._base_dir, cursor_users=r["cursor"])
                self._sync_state["cursor_users"] = r["cursor"]
        except SyncError as exc:
            print(f"[sync] pull_users: {exc}")

        # --- Pull pedidos web (cache read-only) ---
        try:
            r = client.pull_sales_web(since=self._sync_state.get("cursor_sales_web") or None)
            for s in r.get("items", []):
                try:
                    self.store.cache_remote_sale(s)
                    stats["pulled_sales_web"] += 1
                except Exception as exc:
                    print(f"[sync] sale_web fallo: {exc}")
            if r.get("cursor"):
                sync_cfg.update(self._base_dir, cursor_sales_web=r["cursor"])
                self._sync_state["cursor_sales_web"] = r["cursor"]
        except SyncError as exc:
            print(f"[sync] pull_sales_web: {exc}")

        # --- Pull inventory_log web (cache) ---
        try:
            r = client.pull_inventory_log(since=self._sync_state.get("cursor_inventory_log") or None)
            for i in r.get("items", []):
                try:
                    self.store.cache_remote_inventory(i)
                    stats["pulled_inventory_log"] += 1
                except Exception as exc:
                    print(f"[sync] inv_log fallo: {exc}")
            if r.get("cursor"):
                sync_cfg.update(self._base_dir, cursor_inventory_log=r["cursor"])
                self._sync_state["cursor_inventory_log"] = r["cursor"]
        except SyncError as exc:
            print(f"[sync] pull_inventory_log: {exc}")

        # --- Pull restaurant snapshot (módulo de mesas) ---
        try:
            snap = client.pull_restaurant_snapshot()
            self.store.replace_restaurant_snapshot(snap)
            stats["pulled_restaurant"] = len(snap.get("tables", []))
        except SyncError as exc:
            print(f"[sync] pull_restaurant_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001 — no romper el ciclo por restaurante
            print(f"[sync] restaurant snapshot fallo: {exc}")

        # --- Pull contabilidad snapshot ---
        try:
            csnap = client.pull_contabilidad_snapshot()
            self.store.replace_contabilidad_snapshot(csnap)
            stats["pulled_contabilidad"] = len(csnap.get("movimientos", []))
        except SyncError as exc:
            print(f"[sync] pull_contabilidad_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] contabilidad snapshot fallo: {exc}")

        # --- Pull cotizaciones snapshot ---
        try:
            qsnap = client.pull_quotes_snapshot()
            self.store.replace_quotes_snapshot(qsnap)
            stats["pulled_quotes"] = len(qsnap.get("cotizaciones", []))
        except SyncError as exc:
            print(f"[sync] pull_quotes_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] quotes snapshot fallo: {exc}")

        # --- Pull cuentas de cobro snapshot ---
        try:
            ccsnap = client.pull_cobros_snapshot()
            self.store.replace_cobros_snapshot(ccsnap)
            stats["pulled_cobros"] = len(ccsnap.get("cuentas", []))
        except SyncError as exc:
            print(f"[sync] pull_cobros_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] cobros snapshot fallo: {exc}")

        # --- Pull CRM snapshot ---
        try:
            crmsnap = client.pull_crm_snapshot()
            self.store.replace_crm_snapshot(crmsnap)
            stats["pulled_crm"] = len(crmsnap.get("contactos", []))
        except SyncError as exc:
            print(f"[sync] pull_crm_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] crm snapshot fallo: {exc}")

        # --- Pull Nómina snapshot ---
        try:
            nomsnap = client.pull_nomina_snapshot()
            self.store.replace_nomina_snapshot(nomsnap)
            stats["pulled_nomina"] = len(nomsnap.get("empleados", []))
        except SyncError as exc:
            print(f"[sync] pull_nomina_snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] nomina snapshot fallo: {exc}")

        # --- Push outbox ---
        pending = self.store.pending_outbox(limit=100)
        if pending:
            push_payload = [
                {"local_id": p["id"], "entity": p["entity"], "action": p["action"], "payload": p["payload"]}
                for p in pending
            ]
            try:
                push_resp = client.push_outbox(push_payload)
                done_ids = []
                rejections = []
                by_local = {p["id"]: p for p in pending}
                for r in push_resp.get("results", []):
                    status = r.get("status")
                    if status == "applied":
                        done_ids.append(r["local_id"])
                        stats["pushed_applied"] += 1
                    elif status == "skipped":
                        done_ids.append(r["local_id"])
                        stats["pushed_skipped"] += 1
                    elif status == "stale":
                        # LWW perdio: marcar como sync (descartar) y confiar en pull proximo
                        done_ids.append(r["local_id"])
                        stats["pushed_stale"] += 1
                    elif status == "forbidden":
                        # Licencia/rol: NO reintentar indefinidamente. Descartar del
                        # outbox y registrar el motivo para mostrarlo en la UI.
                        done_ids.append(r["local_id"])
                        stats["pushed_forbidden"] = stats.get("pushed_forbidden", 0) + 1
                        src = by_local.get(r["local_id"], {})
                        rejections.append({
                            "entity": src.get("entity"), "action": src.get("action"),
                            "motivo": r.get("error") or "No autorizado por licencia/rol.",
                        })
                    else:
                        stats["pushed_errors"] += 1
                self.store.mark_outbox_synced(done_ids)
                if rejections:
                    self.store.record_rejections(rejections)
                # Confía las filas de restaurante empujadas para que el próximo
                # pull adopte la verdad del servidor sin duplicar.
                if any(p["entity"] == "restaurant_op" for p in pending):
                    self.store.mark_restaurant_pushed()
                if any(p["entity"] == "contabilidad_op" for p in pending):
                    self.store.mark_contabilidad_pushed()
                if any(p["entity"] == "quote_op" for p in pending):
                    self.store.mark_quotes_pushed()
                if any(p["entity"] == "cobro_op" for p in pending):
                    self.store.mark_cobros_pushed()
                if any(p["entity"] == "crm_op" for p in pending):
                    self.store.mark_crm_pushed()
                if any(p["entity"] == "nomina_op" for p in pending):
                    self.store.mark_nomina_pushed()
            except SyncError as exc:
                print(f"[sync] push_outbox: {exc}")
                stats["pushed_errors"] += len(pending)

        # Persistir contador de stale para mostrar en UI
        sync_cfg.update(self._base_dir, last_stale_count=str(stats["pushed_stale"]))
        return stats

    def _record_status(self, status_text):
        from datetime import datetime
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sync_cfg.update(
            self._base_dir,
            last_sync_status=status_text,
            last_sync_at=now_iso,
        )
        self._sync_state["last_sync_status"] = status_text
        self._sync_state["last_sync_at"] = now_iso

    def _set_status_text(self, text, error=False):
        self.sync_status.setText(text + " | " + self._format_status())
        self.sync_status.setStyleSheet("color: #b42318; font-weight: 700;" if error else "color: #5a7a14; font-weight: 700;")

    def _clear_demo(self):
        user = self._user_callback()
        if not user or user.role != "Administrador":
            QMessageBox.warning(
                self,
                "Permiso requerido",
                "Solo un usuario con rol Administrador puede limpiar los datos.",
            )
            return
        confirm = QMessageBox.question(
            self,
            "Limpiar datos",
            "Borra TODOS los productos, ventas, movimientos y la outbox. Los usuarios se conservan. Continuar?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.clear_demo_data(acting_user=user)
        except PermissionError as exc:
            QMessageBox.warning(self, "Permiso requerido", str(exc))
            return
        self.refresh()
        self.on_changed()


# =============================================================================
# Configuracion / Branding
# =============================================================================
COLOR_FIELDS = [
    ("primario", "Color primario (botones, links)"),
    ("primario_oscuro", "Primario oscuro (sidebar, hover)"),
    ("acento", "Acento (hover menu, focus)"),
    ("acento_secundario", "Acento secundario (escaner, finalizar venta)"),
    ("peligro", "Peligro (errores, eliminar)"),
    ("sidebar_inicio", "Sidebar gradiente inicio"),
    ("sidebar_fin", "Sidebar gradiente fin"),
    ("fondo", "Fondo general"),
    ("superficie", "Superficie de tarjetas (fondo de cards/paneles)"),
]
# Agrupacion visual de los colores en la pantalla de Configuracion.
# (titulo de grupo, descripcion, lista de claves de COLOR_FIELDS).
COLOR_GROUPS = [
    (
        "Identidad de marca",
        "Colores principales de botones, enlaces, acentos y estados.",
        ["primario", "primario_oscuro", "acento", "acento_secundario", "peligro"],
    ),
    (
        "Interfaz y superficies",
        "Menu lateral, lienzo general y fondo de las tarjetas.",
        ["sidebar_inicio", "sidebar_fin", "fondo", "superficie"],
    ),
]
EMPRESA_FIELDS = [
    ("nombre", "Nombre de la empresa"),
    ("slogan", "Slogan / subtitulo del login"),
    ("ventana_titulo", "Titulo de la ventana"),
    ("email", "Email de contacto"),
    ("telefono", "Telefono"),
    ("direccion", "Direccion"),
    ("website", "Sitio web"),
    ("recibo_pie", "Pie del recibo (POS)"),
]


class ColorPickerRow(QWidget):
    """Fila con label, swatch de color, lineedit del hex y boton 'Elegir...'."""

    def __init__(self, key: str, label: str, value: str, on_change):
        super().__init__()
        self.key = key
        self.on_change = on_change
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(10)

        self.label = QLabel(label)
        self.label.setObjectName("fieldLabel")
        self.label.setMinimumWidth(280)
        self.label.setWordWrap(True)
        self.swatch = QFrame()
        self.swatch.setObjectName("colorSwatch")
        self.input = QLineEdit(value)
        self.input.setObjectName("hexInput")
        self.input.setMaxLength(9)
        self.input.setFixedWidth(120)
        self.input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input.textChanged.connect(self._on_text_change)
        self.input.editingFinished.connect(self._notify)
        button = QPushButton("Elegir...")
        button.setObjectName("secondaryAction")
        button.setMinimumWidth(96)
        button.clicked.connect(self._open_picker)

        layout.addWidget(self.label)
        layout.addStretch(1)
        layout.addWidget(self.swatch)
        layout.addWidget(self.input)
        layout.addWidget(button)
        self._refresh_swatch(value)

    def value(self) -> str:
        return self.input.text().strip()

    def set_value(self, hex_value: str):
        self.input.setText(hex_value)

    def _on_text_change(self, text):
        self._refresh_swatch(text.strip())

    def _refresh_swatch(self, value: str):
        if branding_mod._is_hex_color(value):
            self.swatch.setStyleSheet(f"QFrame#colorSwatch {{ background: {value}; }}")
        else:
            self.swatch.setStyleSheet("QFrame#colorSwatch { background: #f0f0f0; }")

    def _open_picker(self):
        from PyQt6.QtGui import QColor
        current = self.input.text().strip()
        initial = QColor(current) if branding_mod._is_hex_color(current) else QColor("#ffffff")
        chosen = QColorDialog.getColor(initial, self, f"Elegir {self.key}")
        if chosen.isValid():
            self.set_value(chosen.name())
            self._notify()

    def _notify(self):
        if self.on_change:
            self.on_change()


class ConfiguracionPage(QWidget):
    """Edita branding (colores, empresa, logo) y aplica al instante."""

    def __init__(self, branding_state, base_dir: Path, on_apply):
        super().__init__()
        self._state = branding_state  # dict mutable compartido con DesktopShell
        self._base_dir = base_dir
        self._on_apply = on_apply
        self._color_rows = {}
        self._empresa_inputs = {}
        self._build()

    @staticmethod
    def _section_panel(title: str, desc: str):
        """Crea un panel de seccion con encabezado (titulo + descripcion) y un
        separador. Devuelve (panel, content_layout) para seguir agregando."""
        panel = QFrame()
        panel.setObjectName("sectionPanel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(20, 18, 20, 20)
        outer.setSpacing(12)

        head = QVBoxLayout()
        head.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        desc_label = QLabel(desc)
        desc_label.setObjectName("sectionDesc")
        desc_label.setWordWrap(True)
        head.addWidget(title_label)
        head.addWidget(desc_label)
        outer.addLayout(head)

        divider = QFrame()
        divider.setObjectName("divider")
        outer.addWidget(divider)
        return panel, outer

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Contenido desplazable ───────────────────────────────────
        scroll = QScrollArea()
        scroll.setObjectName("configScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        scroll.setWidget(content)

        title = QLabel("Configuración / Marca")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Personaliza la identidad de este cliente. Los cambios se previsualizan "
            "al instante y se guardan en branding.json al pulsar 'Guardar y aplicar'."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ── Seccion: Datos de la empresa ────────────────────────────
        empresa_panel, empresa_outer = self._section_panel(
            "Datos de la empresa",
            "Identidad textual que aparece en el login, la ventana y los recibos del POS.",
        )
        empresa_form = QFormLayout()
        empresa_form.setContentsMargins(0, 0, 0, 0)
        empresa_form.setHorizontalSpacing(18)
        empresa_form.setVerticalSpacing(12)
        empresa_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        empresa_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, label in EMPRESA_FIELDS:
            input_field = QLineEdit(self._state["empresa"].get(key, ""))
            self._empresa_inputs[key] = input_field
            field_label = QLabel(label + ":")
            field_label.setObjectName("fieldLabel")
            empresa_form.addRow(field_label, input_field)

        # Logo
        logo_row = QHBoxLayout()
        logo_row.setSpacing(8)
        self.logo_path_input = QLineEdit(self._state["empresa"].get("logo_path", ""))
        self.logo_path_input.setPlaceholderText("Ruta absoluta al archivo PNG/JPG (opcional)")
        browse = QPushButton("Examinar...")
        browse.setObjectName("secondaryAction")
        browse.clicked.connect(self._browse_logo)
        clear_logo = QPushButton("Quitar")
        clear_logo.setObjectName("dangerAction")
        clear_logo.clicked.connect(lambda: self.logo_path_input.setText(""))
        logo_row.addWidget(self.logo_path_input, 1)
        logo_row.addWidget(browse)
        logo_row.addWidget(clear_logo)
        logo_label = QLabel("Logo:")
        logo_label.setObjectName("fieldLabel")
        empresa_form.addRow(logo_label, _wrap_layout(logo_row))
        empresa_outer.addLayout(empresa_form)
        layout.addWidget(empresa_panel)

        # ── Seccion: Colores de marca (agrupados) ───────────────────
        color_panel, color_outer = self._section_panel(
            "Colores de marca",
            "Cada color usa formato hexadecimal (#RRGGBB). Usa 'Elegir...' para "
            "abrir el selector visual.",
        )
        labels = dict(COLOR_FIELDS)
        for gi, (group_title, group_desc, keys) in enumerate(COLOR_GROUPS):
            if gi > 0:
                color_outer.addSpacing(4)
            group_label = QLabel(group_title)
            group_label.setObjectName("colorGroupLabel")
            color_outer.addWidget(group_label)
            group_hint = QLabel(group_desc)
            group_hint.setObjectName("colorHint")
            group_hint.setWordWrap(True)
            color_outer.addWidget(group_hint)
            for key in keys:
                row = ColorPickerRow(
                    key,
                    labels.get(key, key),
                    self._state["colores"].get(key, branding_mod.DEFAULTS["colores"][key]),
                    on_change=None,
                )
                self._color_rows[key] = row
                color_outer.addWidget(row)
        layout.addWidget(color_panel)
        layout.addStretch(1)

        root.addWidget(scroll, 1)

        # ── Barra de acciones fija (no se desplaza) ─────────────────
        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        actions = QHBoxLayout(action_bar)
        actions.setContentsMargins(28, 12, 28, 12)
        actions.setSpacing(10)

        self.feedback = QLabel("Listo.")
        self.feedback.setObjectName("muted")
        actions.addWidget(self.feedback)
        actions.addStretch(1)

        reset_btn = QPushButton("Restaurar predeterminados")
        reset_btn.setObjectName("dangerAction")
        reset_btn.clicked.connect(self._reset)
        import_btn = QPushButton("Importar JSON...")
        import_btn.setObjectName("secondaryAction")
        import_btn.clicked.connect(self._import)
        export_btn = QPushButton("Exportar JSON...")
        export_btn.setObjectName("secondaryAction")
        export_btn.clicked.connect(self._export)
        preview_btn = QPushButton("Previsualizar (sin guardar)")
        preview_btn.setObjectName("secondaryAction")
        preview_btn.clicked.connect(self._preview)
        save_btn = QPushButton("Guardar y aplicar")
        save_btn.setObjectName("primaryAction")
        save_btn.clicked.connect(self._save_apply)
        for btn in (reset_btn, import_btn, export_btn, preview_btn, save_btn):
            actions.addWidget(btn)

        root.addWidget(action_bar)

    def refresh(self):
        for key, input_field in self._empresa_inputs.items():
            input_field.setText(self._state["empresa"].get(key, ""))
        for key, row in self._color_rows.items():
            row.set_value(self._state["colores"].get(key, ""))
        self.logo_path_input.setText(self._state["empresa"].get("logo_path", ""))

    def _collect(self):
        empresa = {key: input_field.text().strip() for key, input_field in self._empresa_inputs.items()}
        empresa["logo_path"] = self.logo_path_input.text().strip()
        colores = {key: row.value() for key, row in self._color_rows.items()}
        return {"empresa": empresa, "colores": colores}

    def _preview(self):
        try:
            payload = self._collect()
            for key, value in payload["colores"].items():
                if not branding_mod._is_hex_color(value):
                    raise ValueError(f"Color '{key}' invalido: '{value}'.")
            self._state["empresa"].update(payload["empresa"])
            self._state["colores"].update(payload["colores"])
            self._on_apply()
            self.feedback.setText("Previsualizacion aplicada (sin guardar).")
            self.feedback.setStyleSheet("")
        except ValueError as exc:
            self.feedback.setText(str(exc))
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")

    def _save_apply(self):
        try:
            payload = self._collect()
            path = branding_mod.save_branding(self._base_dir, payload)
            new_state = branding_mod.load_branding(self._base_dir)
            self._state["empresa"] = new_state["empresa"]
            self._state["colores"] = new_state["colores"]
            self.refresh()
            self._on_apply()
            self.feedback.setText(f"Guardado en {path}")
            self.feedback.setStyleSheet("color: #5a7a14; font-weight: 700;")
        except ValueError as exc:
            self.feedback.setText(str(exc))
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")

    def _browse_logo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Elegir logo", "", "Imagenes (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if path:
            self.logo_path_input.setText(path)

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar branding", "branding.json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            shutil.copy(branding_mod.branding_file(self._base_dir), path)
            self.feedback.setText(f"Exportado a {path}")
            self.feedback.setStyleSheet("color: #5a7a14; font-weight: 700;")
        except Exception as exc:
            self.feedback.setText(f"Error al exportar: {exc}")
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Importar branding", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            shutil.copy(path, branding_mod.branding_file(self._base_dir))
            new_state = branding_mod.load_branding(self._base_dir)
            self._state["empresa"] = new_state["empresa"]
            self._state["colores"] = new_state["colores"]
            self.refresh()
            self._on_apply()
            self.feedback.setText(f"Importado desde {path}")
            self.feedback.setStyleSheet("color: #5a7a14; font-weight: 700;")
        except Exception as exc:
            self.feedback.setText(f"Error al importar: {exc}")
            self.feedback.setStyleSheet("color: #b42318; font-weight: 700;")

    def _reset(self):
        confirm = QMessageBox.question(
            self,
            "Restaurar predeterminados",
            "Borra branding.json y vuelve a los colores/datos de fabrica. Continuar?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        new_state = branding_mod.reset_branding(self._base_dir)
        self._state["empresa"] = new_state["empresa"]
        self._state["colores"] = new_state["colores"]
        self.refresh()
        self._on_apply()
        self.feedback.setText("Restaurado a valores de fabrica.")
        self.feedback.setStyleSheet("")


def _rt_money(value) -> str:
    return f"${float(value or 0):,.0f}".replace(",", ".")


# =============================================================================
# Restaurante — tarjeta de mesa
# =============================================================================
class _RtTableCard(QFrame):
    """Tarjeta clickable de una mesa en la grilla del salón."""

    clicked = pyqtSignal(int)

    def __init__(self, table: dict, selected: bool):
        super().__init__()
        self.table_id = int(table["id"])
        self.setObjectName("rtTableCard")
        self.setProperty("estado", table.get("estado") or "disponible")
        self.setProperty("selected", "true" if selected else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(190)
        self.setMaximumWidth(260)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        name = QLabel(table.get("nombre") or table.get("codigo") or "Mesa")
        name.setObjectName("rtCardName")
        top.addWidget(name)
        top.addStretch(1)
        badge = QLabel(_RT_STATE_LABELS.get(table.get("estado"), table.get("estado") or ""))
        badge.setObjectName("rtCardBadge")
        top.addWidget(badge)
        lay.addLayout(top)

        meta = QLabel(f"{table.get('codigo') or ''} · {table.get('capacidad') or 0} pers.")
        meta.setObjectName("rtCardMeta")
        lay.addWidget(meta)

        total = float(table.get("total_acumulado") or 0)
        if table.get("order_local_id"):
            total_label = QLabel(_rt_money(total))
            total_label.setObjectName("rtCardTotal")
            lay.addWidget(total_label)
            sub = []
            if table.get("comensales"):
                sub.append(f"{table['comensales']} comensales")
            pend = int(table.get("pendientes") or 0)
            serv = int(table.get("servidos") or 0)
            sub.append(f"{pend} pend · {serv} serv")
            info = QLabel("  ·  ".join(sub))
            info.setObjectName("rtCardMeta")
            lay.addWidget(info)
        else:
            free = QLabel("Libre")
            free.setObjectName("rtCardFree")
            lay.addWidget(free)

        if int(table.get("synced", 1)) == 0:
            dot = QLabel("● pendiente de sync")
            dot.setObjectName("rtCardPending")
            lay.addWidget(dot)

    def mousePressEvent(self, event):
        self.clicked.emit(self.table_id)
        super().mousePressEvent(event)


_RT_STATE_LABELS = {
    "disponible": "Disponible",
    "ocupada": "Ocupada",
    "reservada": "Reservada",
    "cuenta_solicitada": "Cuenta solicitada",
}
_RT_CONSUMPTION_LABELS = {
    "pendiente": "Pendiente",
    "preparando": "Preparando",
    "servido": "Servido",
}
_RT_CONSUMPTION_NEXT = {"pendiente": "preparando", "preparando": "servido", "servido": "servido"}


# =============================================================================
# Restaurante — página principal (atención de mesas)
# =============================================================================
class RestaurantPage(QWidget):
    """Atención de mesas offline-first. Espejo local de las tablas de
    producción + outbox; toda acción funciona sin internet."""

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._area = None
        self._selected_table = None
        self._view_mode = None  # "operativa" | "cajero" (default por rol en el 1er refresh)
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _role(self) -> str:
        u = self._user()
        return getattr(u, "role", "") if u else ""

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(14)

        # Encabezado
        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Restaurante · Atención de mesas")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Cada tarjeta es una mesa del salón donde se sientan los clientes. "
            "Toca una mesa para abrir su cuenta, anotar lo que piden y cobrar. "
            "Funciona sin internet; los cambios se sincronizan al reconectar."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        controls = QVBoxLayout()
        controls.setSpacing(6)
        top_controls = QHBoxLayout()
        top_controls.setSpacing(8)
        area_lbl = QLabel("Área:")
        area_lbl.setObjectName("fieldLabel")
        top_controls.addWidget(area_lbl)
        self.area_combo = QComboBox()
        self.area_combo.setMinimumWidth(150)
        self.area_combo.currentIndexChanged.connect(self._on_area_changed)
        top_controls.addWidget(self.area_combo)
        view_lbl = QLabel("Vista:")
        view_lbl.setObjectName("fieldLabel")
        top_controls.addWidget(view_lbl)
        self.view_combo = QComboBox()
        self.view_combo.addItem("Operativa (mesero)", "operativa")
        self.view_combo.addItem("Resumen (cajero)", "cajero")
        self.view_combo.currentIndexChanged.connect(self._on_view_changed)
        top_controls.addWidget(self.view_combo)
        refresh_btn = QPushButton("↻  Refrescar")
        refresh_btn.setObjectName("secondaryAction")
        refresh_btn.clicked.connect(self.refresh)
        top_controls.addWidget(refresh_btn)
        controls.addLayout(top_controls)
        self.sync_label = QLabel("")
        self.sync_label.setObjectName("rtSyncLabel")
        self.sync_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls.addWidget(self.sync_label)
        header.addLayout(controls)
        root.addLayout(header)

        # Leyenda de estados (qué significa cada color)
        legend = QHBoxLayout()
        legend.setSpacing(16)
        leg_title = QLabel("Qué significa cada color:")
        leg_title.setObjectName("muted")
        legend.addWidget(leg_title)
        for estado, label in _RT_STATE_LABELS.items():
            chip = QLabel("● " + label)
            chip.setObjectName("rtLegend")
            chip.setProperty("estado", estado)
            legend.addWidget(chip)
        legend.addStretch(1)
        root.addLayout(legend)

        # Cuerpo: grilla de mesas (izq) + detalle de la mesa (der)
        body = QHBoxLayout()
        body.setSpacing(16)

        grid_scroll = QScrollArea()
        grid_scroll.setObjectName("configScroll")
        grid_scroll.setWidgetResizable(True)
        grid_host = QWidget()
        self.grid = QGridLayout(grid_host)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        grid_scroll.setWidget(grid_host)
        body.addWidget(grid_scroll, 3)

        # Panel de detalle con scroll (las cuentas largas no se cortan)
        self.detail_panel = QFrame()
        self.detail_panel.setObjectName("sectionPanel")
        self.detail_panel.setMinimumWidth(380)
        self.detail_panel.setMaximumWidth(460)
        panel_outer = QVBoxLayout(self.detail_panel)
        panel_outer.setContentsMargins(0, 0, 0, 0)
        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("configScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_host = QWidget()
        self.detail_layout = QVBoxLayout(detail_host)
        self.detail_layout.setContentsMargins(18, 16, 18, 16)
        self.detail_layout.setSpacing(10)
        detail_scroll.setWidget(detail_host)
        panel_outer.addWidget(detail_scroll)
        body.addWidget(self.detail_panel, 2)

        root.addLayout(body, 1)
        self.refresh()

    # ─── Render ────────────────────────────────────────────────────
    def refresh(self):
        # Vista por defecto según rol (solo la primera vez)
        if self._view_mode is None:
            u = self._user()
            self._view_mode = "cajero" if (u and getattr(u, "role", "") == "Cajero") else "operativa"
            self.view_combo.blockSignals(True)
            self.view_combo.setCurrentIndex(1 if self._view_mode == "cajero" else 0)
            self.view_combo.blockSignals(False)

        areas = self.store.rt_list_areas()
        self.area_combo.blockSignals(True)
        current = self.area_combo.currentText()
        self.area_combo.clear()
        if areas:
            self.area_combo.addItem("Todas las áreas", None)
            for a in areas:
                self.area_combo.addItem(a, a)
            idx = self.area_combo.findText(current) if current else 0
            self.area_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.area_combo.blockSignals(False)
        self._area = self.area_combo.currentData()

        pend = self.store.rt_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync")
            self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado")
            self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label)
        self.sync_label.style().polish(self.sync_label)

        self._render_grid()
        self._render_detail()

    def _render_grid(self):
        clear_layout(self.grid)
        tables = self.store.rt_list_tables(self._area)
        if not tables:
            empty = QLabel("No hay mesas. Crea el plano desde la web (módulo Salón) y sincroniza.")
            empty.setObjectName("muted")
            empty.setWordWrap(True)
            self.grid.addWidget(empty, 0, 0)
            return
        cols = 3
        for i, t in enumerate(tables):
            card = _RtTableCard(t, selected=(t["id"] == self._selected_table))
            card.clicked.connect(self._select_table)
            self.grid.addWidget(card, i // cols, i % cols)

    def _select_table(self, table_id: int):
        self._selected_table = int(table_id)
        self._render_grid()
        self._render_detail()

    def _render_detail(self):
        clear_layout(self.detail_layout)
        if self._selected_table is None:
            hint = QLabel("👈  Toca una mesa en el plano para ver o abrir su cuenta.")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            self.detail_layout.addWidget(hint)
            self.detail_layout.addStretch(1)
            return
        detail = self.store.rt_table_detail(self._selected_table)
        if not detail:
            self.detail_layout.addWidget(QLabel("Mesa no encontrada."))
            self.detail_layout.addStretch(1)
            return
        table = detail["table"]
        order = detail["order"]

        # Encabezado: nombre de la mesa + pill de estado
        head_row = QHBoxLayout()
        head = QLabel(table.get("nombre") or table.get("codigo") or "Mesa")
        head.setObjectName("rtDetailTitle")
        head_row.addWidget(head)
        head_row.addStretch(1)
        pill = QLabel(_RT_STATE_LABELS.get(table.get("estado"), table.get("estado")))
        pill.setObjectName("rtStatePill")
        pill.setProperty("estado", table.get("estado"))
        head_row.addWidget(pill)
        self.detail_layout.addLayout(head_row)

        meta = QLabel(f"Código {table.get('codigo') or '—'}  ·  Capacidad {table.get('capacidad') or 0} personas")
        meta.setObjectName("rtCardMeta")
        self.detail_layout.addWidget(meta)

        if not order:
            self._render_open_form()
        else:
            self._render_open_order(order)
        self.detail_layout.addStretch(1)

    def _render_open_form(self):
        divider = QFrame(); divider.setObjectName("divider")
        self.detail_layout.addWidget(divider)

        sec = QLabel("ABRIR LA MESA")
        sec.setObjectName("eyebrow")
        self.detail_layout.addWidget(sec)
        info = QLabel("Esta mesa está libre. Ingresa cuántas personas se sientan y ábrela para empezar a anotar lo que piden.")
        info.setObjectName("muted"); info.setWordWrap(True)
        self.detail_layout.addWidget(info)

        form = QFormLayout(); form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.open_cliente = QLineEdit(); self.open_cliente.setPlaceholderText("Nombre del cliente (opcional)")
        self.open_comensales = QSpinBox(); self.open_comensales.setRange(1, 50); self.open_comensales.setValue(2)
        self.open_comensales.setSuffix(" personas")
        cliente_lbl = QLabel("Cliente:"); cliente_lbl.setObjectName("fieldLabel")
        com_lbl = QLabel("Comensales:"); com_lbl.setObjectName("fieldLabel")
        form.addRow(cliente_lbl, self.open_cliente)
        form.addRow(com_lbl, self.open_comensales)
        self.detail_layout.addLayout(form)

        open_btn = QPushButton("🍽  Abrir mesa")
        open_btn.setObjectName("primaryAction")
        open_btn.clicked.connect(self._open_table)
        self.detail_layout.addWidget(open_btn)

        divider2 = QFrame(); divider2.setObjectName("divider")
        self.detail_layout.addWidget(divider2)
        other = QLabel("¿No vas a atenderla aún?")
        other.setObjectName("muted")
        self.detail_layout.addWidget(other)
        state_row = QHBoxLayout()
        reserve_btn = QPushButton("Marcar reservada")
        reserve_btn.setObjectName("secondaryAction")
        reserve_btn.clicked.connect(lambda: self._set_table_state("reservada"))
        free_btn = QPushButton("Marcar disponible")
        free_btn.setObjectName("secondaryAction")
        free_btn.clicked.connect(lambda: self._set_table_state("disponible"))
        state_row.addWidget(reserve_btn); state_row.addWidget(free_btn)
        self.detail_layout.addLayout(state_row)

    def _render_open_order(self, order: dict):
        divider = QFrame(); divider.setObjectName("divider")
        self.detail_layout.addWidget(divider)

        meta = []
        if order.get("cliente_nombre"):
            meta.append(f"👤 {order['cliente_nombre']}")
        meta.append(f"{order.get('comensales') or 1} comensales")
        meta_lbl = QLabel("   ·   ".join(meta))
        meta_lbl.setObjectName("muted")
        self.detail_layout.addWidget(meta_lbl)

        detail = self.store.rt_table_detail(self._selected_table)
        consumptions = detail["consumptions"]

        # ── Barra de progreso de preparación (Pendiente → Preparando → Servido) ──
        self.detail_layout.addWidget(self._progress_widget(consumptions))

        # Vista cajero: resumen tipo recibo (sin controles de cocina ni alta de ítems)
        if self._view_mode == "cajero":
            self._render_cashier_summary(order, consumptions)
            return

        # ── Consumos (lo que ha pedido la mesa) — vista operativa ──
        cons_title = QLabel("CONSUMOS DE LA MESA")
        cons_title.setObjectName("eyebrow")
        self.detail_layout.addWidget(cons_title)
        if not consumptions:
            empty = QLabel("Aún no se ha pedido nada. Agrega el primer consumo abajo.")
            empty.setObjectName("muted"); empty.setWordWrap(True)
            self.detail_layout.addWidget(empty)
        else:
            hint = QLabel("Toca el estado de cada ítem para avanzarlo: Pendiente → Preparando → Servido.")
            hint.setObjectName("rtHint"); hint.setWordWrap(True)
            self.detail_layout.addWidget(hint)
            for c in consumptions:
                self.detail_layout.addWidget(self._consumption_row(c))

        total_lbl = QLabel(f"Total a cobrar:  {_rt_money(order.get('total_acumulado'))}")
        total_lbl.setObjectName("rtDetailTotal")
        self.detail_layout.addWidget(total_lbl)

        # ── Agregar consumo ──
        addbox = QFrame(); addbox.setObjectName("rtAddBox")
        add_l = QVBoxLayout(addbox); add_l.setContentsMargins(14, 12, 14, 14); add_l.setSpacing(8)
        add_title = QLabel("AGREGAR CONSUMO"); add_title.setObjectName("eyebrow")
        add_l.addWidget(add_title)

        self.prod_combo = QComboBox()
        self.prod_combo.addItem("Producto del catálogo…", None)
        self.prod_combo.addItem("✎  Escribir algo libre (sin catálogo)", "__free__")
        for p in self.store.products():
            if p.get("remote_id"):
                self.prod_combo.addItem(f"{p['name']}  —  {_rt_money(p['price'])}", p["remote_id"])
        self.prod_combo.currentIndexChanged.connect(self._on_prod_changed)
        add_l.addWidget(self.prod_combo)

        self.free_desc = QLineEdit(); self.free_desc.setPlaceholderText("¿Qué se pidió?")
        add_l.addWidget(self.free_desc)
        self.free_price_row = QWidget()
        price_row = QHBoxLayout(self.free_price_row); price_row.setContentsMargins(0, 0, 0, 0); price_row.setSpacing(8)
        price_lbl = QLabel("Precio:"); price_lbl.setObjectName("fieldLabel")
        self.free_price = QDoubleSpinBox(); self.free_price.setRange(0, 100000000); self.free_price.setDecimals(0)
        self.free_price.setPrefix("$ "); self.free_price.setGroupSeparatorShown(True)
        price_row.addWidget(price_lbl); price_row.addWidget(self.free_price, 1)
        add_l.addWidget(self.free_price_row)

        qty_row = QHBoxLayout(); qty_row.setSpacing(8)
        qty_lbl = QLabel("Cantidad:"); qty_lbl.setObjectName("fieldLabel")
        self.qty_spin = QSpinBox(); self.qty_spin.setRange(1, 100); self.qty_spin.setValue(1)
        qty_row.addWidget(qty_lbl); qty_row.addWidget(self.qty_spin); qty_row.addStretch(1)
        add_l.addLayout(qty_row)
        self.cons_notas = QLineEdit(); self.cons_notas.setPlaceholderText("Notas para cocina (opcional)")
        add_l.addWidget(self.cons_notas)
        add_btn = QPushButton("＋  Agregar a la cuenta")
        add_btn.setObjectName("primaryAction")
        add_btn.clicked.connect(self._add_consumption)
        add_btn.setVisible(self.can("restaurant", "create"))
        add_l.addWidget(add_btn)
        self.detail_layout.addWidget(addbox)
        self._on_prod_changed()  # estado inicial de campos libres

        # ── Acciones de la cuenta ──
        act_title = QLabel("ACCIONES"); act_title.setObjectName("eyebrow")
        self.detail_layout.addWidget(act_title)
        charge_btn = QPushButton(f"💳  Cobrar y cerrar  ·  {_rt_money(order.get('total_acumulado'))}")
        charge_btn.setObjectName("primaryAction")
        charge_btn.clicked.connect(self._close_table)
        charge_btn.setVisible(self.can("restaurant", "charge"))  # RESTAURANT_CHARGE (no Empleado)
        self.detail_layout.addWidget(charge_btn)

        actions = QHBoxLayout()
        bill_btn = QPushButton("Pedir la cuenta")
        bill_btn.setObjectName("secondaryAction")
        bill_btn.setToolTip("Marca la mesa como 'Cuenta solicitada' (el cliente pidió la cuenta).")
        bill_btn.clicked.connect(lambda: self._set_table_state("cuenta_solicitada"))
        actions.addWidget(bill_btn)
        # Cancelar cuenta: RESTAURANT_CANCEL (Administrador/Cajero) — vía manifiesto
        if self.can("restaurant", "cancel"):
            cancel_btn = QPushButton("Cancelar cuenta")
            cancel_btn.setObjectName("dangerAction")
            cancel_btn.setToolTip("Anula la cuenta sin cobrar y libera la mesa.")
            cancel_btn.clicked.connect(self._cancel_order)
            actions.addWidget(cancel_btn)
        self.detail_layout.addLayout(actions)

    def _progress_widget(self, consumptions) -> QWidget:
        """Barra segmentada del avance de preparación de la mesa."""
        total = len(consumptions)
        serv = sum(1 for c in consumptions if c["estado"] == "servido")
        prep = sum(1 for c in consumptions if c["estado"] == "preparando")
        pend = sum(1 for c in consumptions if c["estado"] == "pendiente")

        box = QFrame(); box.setObjectName("rtProgressBox")
        v = QVBoxLayout(box); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel("PREPARACIÓN DEL PEDIDO"); t.setObjectName("eyebrow")
        head.addWidget(t); head.addStretch(1)
        if total:
            pct = QLabel(f"{round(100*serv/total)}%"); pct.setObjectName("rtProgPct")
            head.addWidget(pct)
        v.addLayout(head)

        bar = QFrame(); bar.setObjectName("rtProgBar")
        bl = QHBoxLayout(bar); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        if total == 0:
            empty_seg = QFrame(); empty_seg.setObjectName("rtProgSeg"); empty_seg.setProperty("estado", "vacio")
            bl.addWidget(empty_seg, 1)
        else:
            for count, estado in ((serv, "servido"), (prep, "preparando"), (pend, "pendiente")):
                if count:
                    seg = QFrame(); seg.setObjectName("rtProgSeg"); seg.setProperty("estado", estado)
                    bl.addWidget(seg, count)
        v.addWidget(bar)

        if total:
            legend = QLabel(f"✓ {serv} servido(s)   ·   🍳 {prep} preparando   ·   ⏳ {pend} pendiente(s)")
        else:
            legend = QLabel("Sin ítems todavía.")
        legend.setObjectName("rtProgLegend")
        v.addWidget(legend)
        return box

    def _render_cashier_summary(self, order: dict, consumptions):
        """Vista de cajero: recibo resumido + cobro. Sin controles de cocina."""
        divider = QFrame(); divider.setObjectName("divider")
        self.detail_layout.addWidget(divider)
        sec = QLabel("RESUMEN DE LA CUENTA"); sec.setObjectName("eyebrow")
        self.detail_layout.addWidget(sec)

        # Recibo: agrupa ítems iguales (misma descripción + precio)
        receipt = QFrame(); receipt.setObjectName("rtReceipt")
        rl = QVBoxLayout(receipt); rl.setContentsMargins(14, 12, 14, 12); rl.setSpacing(6)
        if not consumptions:
            rl.addWidget(QLabel("Sin consumos."))
        grouped = {}
        order_keys = []
        for c in consumptions:
            key = (c["descripcion"], round(float(c["precio_unitario"]), 2))
            if key not in grouped:
                grouped[key] = {"cantidad": 0, "subtotal": 0.0, "estados": []}
                order_keys.append(key)
            grouped[key]["cantidad"] += int(c["cantidad"])
            grouped[key]["subtotal"] += float(c["subtotal"])
            grouped[key]["estados"].append(c["estado"])
        for key in order_keys:
            desc, _price = key
            g = grouped[key]
            line = QHBoxLayout(); line.setSpacing(8)
            qty = QLabel(f"{g['cantidad']}×"); qty.setObjectName("rtRcptQty")
            name = QLabel(desc); name.setObjectName("rtRcptName"); name.setWordWrap(True)
            sub = QLabel(_rt_money(g["subtotal"])); sub.setObjectName("rtRcptSub")
            line.addWidget(qty); line.addWidget(name, 1)
            # chip de estado: si todos servidos -> listo; si hay pendientes -> en cocina
            if all(e == "servido" for e in g["estados"]):
                chip = QLabel("listo"); chip.setProperty("estado", "servido")
            elif any(e == "pendiente" for e in g["estados"]):
                chip = QLabel("en cocina"); chip.setProperty("estado", "pendiente")
            else:
                chip = QLabel("preparando"); chip.setProperty("estado", "preparando")
            chip.setObjectName("rtRcptChip")
            line.addWidget(chip)
            line.addWidget(sub)
            rl.addLayout(line)
        self.detail_layout.addWidget(receipt)

        total_lbl = QLabel(_rt_money(order.get("total_acumulado")))
        total_lbl.setObjectName("rtCashierTotal")
        cap = QLabel("TOTAL A COBRAR"); cap.setObjectName("eyebrow")
        self.detail_layout.addWidget(cap)
        self.detail_layout.addWidget(total_lbl)

        charge_btn = QPushButton(f"💳  Cobrar y cerrar  ·  {_rt_money(order.get('total_acumulado'))}")
        charge_btn.setObjectName("primaryAction")
        charge_btn.clicked.connect(self._close_table)
        charge_btn.setVisible(self.can("restaurant", "charge"))  # RESTAURANT_CHARGE (no Empleado)
        self.detail_layout.addWidget(charge_btn)
        bill_btn = QPushButton("Marcar 'cuenta solicitada'")
        bill_btn.setObjectName("secondaryAction")
        bill_btn.clicked.connect(lambda: self._set_table_state("cuenta_solicitada"))
        self.detail_layout.addWidget(bill_btn)

    def _consumption_row(self, c: dict) -> QWidget:
        row = QFrame(); row.setObjectName("rtConsRow")
        h = QHBoxLayout(row); h.setContentsMargins(10, 8, 10, 8); h.setSpacing(8)
        left = QVBoxLayout(); left.setSpacing(1)
        txt = QLabel(f"{c['cantidad']}×  {c['descripcion']}")
        txt.setObjectName("rtConsText"); txt.setWordWrap(True)
        left.addWidget(txt)
        if c.get("notas"):
            notas = QLabel(f"📝 {c['notas']}"); notas.setObjectName("rtConsNotas"); notas.setWordWrap(True)
            left.addWidget(notas)
        h.addLayout(left, 1)
        sub = QLabel(_rt_money(c["subtotal"]))
        sub.setObjectName("rtConsSub")
        h.addWidget(sub)
        state_btn = QPushButton(_RT_CONSUMPTION_LABELS.get(c["estado"], c["estado"]))
        state_btn.setObjectName("rtStateBtn")
        state_btn.setProperty("estado", c["estado"])
        state_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if int(c.get("synced", 1)) == 0 and c.get("remote_id") is None:
            state_btn.setEnabled(False)
            state_btn.setToolTip("Disponible para avanzar una vez que sincronice.")
        elif c["estado"] != "servido":
            state_btn.clicked.connect(lambda _=False, cid=c["local_id"], st=c["estado"]: self._advance_consumption(cid, st))
        else:
            state_btn.setEnabled(False)
        h.addWidget(state_btn)
        return row

    # ─── Acciones ──────────────────────────────────────────────────
    def _on_area_changed(self):
        self._area = self.area_combo.currentData()
        self._render_grid()

    def _on_view_changed(self):
        self._view_mode = self.view_combo.currentData() or "operativa"
        self._render_detail()

    def _on_prod_changed(self):
        """Muestra los campos libres solo cuando se eligió 'escribir algo libre'."""
        is_free = self.prod_combo.currentData() == "__free__"
        self.free_desc.setVisible(is_free)
        self.free_price_row.setVisible(is_free)

    def _open_table(self):
        try:
            self.store.rt_open_table(
                self._selected_table, user=self._user(),
                cliente=self.open_cliente.text(), comensales=self.open_comensales.value(),
            )
        except ValueError as exc:
            self._error(str(exc)); return
        self._after_change()

    def _add_consumption(self):
        data = self.prod_combo.currentData()
        if data is None:
            self._error("Elige un producto del catálogo o la opción 'Escribir algo libre'.")
            return
        is_free = data == "__free__"
        producto_id = None if is_free else data
        try:
            self.store.rt_add_consumption(
                self._selected_table, user=self._user(),
                producto_id=producto_id,
                descripcion=self.free_desc.text() if is_free else "",
                precio_unitario=self.free_price.value() if is_free else 0,
                cantidad=self.qty_spin.value(),
                notas=self.cons_notas.text(),
            )
        except ValueError as exc:
            self._error(str(exc)); return
        self._after_change()

    def _advance_consumption(self, consumption_local_id: int, current_state: str):
        new_state = _RT_CONSUMPTION_NEXT.get(current_state, "servido")
        try:
            self.store.rt_set_consumption_state(consumption_local_id, new_state, user=self._user())
        except ValueError as exc:
            self._error(str(exc)); return
        self._after_change()

    def _set_table_state(self, new_state: str):
        try:
            self.store.rt_set_table_state(self._selected_table, new_state, user=self._user())
        except ValueError as exc:
            self._error(str(exc)); return
        self._after_change()

    def _close_table(self):
        methods = ["EFECTIVO", "TARJETA", "TRANSFERENCIA", "MIXTO"]
        method, ok = QInputDialog.getItem(self, "Cobrar y cerrar", "Método de pago:", methods, 0, False)
        if not ok:
            return
        try:
            self.store.rt_close_table(self._selected_table, payment_method=method, user=self._user())
        except ValueError as exc:
            self._error(str(exc)); return
        QMessageBox.information(self, "Mesa cobrada", "Cuenta cerrada. Se registrará el ingreso al sincronizar.")
        self._after_change()

    def _cancel_order(self):
        confirm = QMessageBox.question(self, "Cancelar cuenta",
                                       "¿Cancelar la cuenta abierta de esta mesa? Esta acción no cobra.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.rt_cancel_order(self._selected_table, user=self._user())
        except ValueError as exc:
            self._error(str(exc)); return
        self._after_change()

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()

    def _error(self, msg: str):
        QMessageBox.warning(self, "Restaurante", msg)


_CB_TIPO_LABEL = {"ingreso": "Ingreso", "egreso": "Egreso"}


def _cb_cat_label(cat: str) -> str:
    return (cat or "").replace("_", " ").capitalize() if cat else "—"


def _cb_brand_colors() -> dict:
    """Colores institucionales vigentes (branding.json) para pintar gráficos."""
    return branding_mod.load_branding(app_data_dir())["colores"]


def _cb_palette(colores: dict) -> list:
    """Paleta cíclica para series categóricas, derivada de la marca."""
    base = [
        QColor(colores["primario"]),
        QColor(colores["acento"]),
        QColor(colores["acento_secundario"]),
        QColor(colores["primario_oscuro"]),
        QColor(colores["peligro"]),
    ]
    return base + [c.lighter(140) for c in base]


def _cb_money_compact(value: float) -> str:
    """$1.2M / $850K para etiquetas cortas dentro de los gráficos."""
    v = float(value or 0)
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(v) >= 10_000:
        return f"${v / 1_000:.0f}K"
    return _rt_money(v)


class _CbKpiCard(QFrame):
    """Tarjeta KPI con franja de acento, ícono y subtítulo, en color de marca."""

    def __init__(self, icon: str, label: str, value: str, sub: str, accent: str):
        super().__init__()
        self._accent = accent
        ac = QColor(accent)
        self.setStyleSheet(
            f"QFrame {{ background: #ffffff; border: 1px solid #e5e7eb;"
            f" border-top: 3px solid {accent}; border-radius: 14px; }}"
            f"QLabel {{ border: 0; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        top = QHBoxLayout(); top.setSpacing(8)
        chip = QLabel(icon)
        chip.setFixedSize(30, 30)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(
            f"background: rgba({ac.red()},{ac.green()},{ac.blue()},0.14);"
            f" color: {accent}; border-radius: 15px; font-size: 14px;"
        )
        top.addWidget(chip)
        lbl = QLabel(label.upper())
        lbl.setStyleSheet("color: #6b7280; font-size: 10px; font-weight: 800; letter-spacing: 1.4px;")
        top.addWidget(lbl, 1)
        lay.addLayout(top)
        self._value_lbl = QLabel(value)
        self._value_style = f"color: {accent}; font-size: 24px; font-weight: 850; letter-spacing: -0.4px;"
        self._value_lbl.setStyleSheet(self._value_style)
        lay.addWidget(self._value_lbl)
        self._sub_lbl = QLabel(sub)
        self._sub_lbl.setStyleSheet("color: #9ca3af; font-size: 11px; font-weight: 600;")
        lay.addWidget(self._sub_lbl)

    def set_value(self, value: str, sub: str | None = None, empty: bool = False):
        self._value_lbl.setText(value)
        if empty:
            self._value_lbl.setStyleSheet("color: #c2c8d4; font-size: 24px; font-weight: 850; letter-spacing: -0.4px;")
        else:
            self._value_lbl.setStyleSheet(self._value_style)
        if sub is not None:
            self._sub_lbl.setText(sub)


def _cb_chip(text: str, color: str) -> QLabel:
    """Pastilla informativa con tinte del color de marca."""
    c = QColor(color)
    chip = QLabel(text)
    chip.setStyleSheet(
        f"background: rgba({c.red()},{c.green()},{c.blue()},0.12); color: {color};"
        f" border-radius: 11px; padding: 4px 10px; font-size: 11px; font-weight: 800;"
    )
    return chip


class _CbSparkline(QWidget):
    """Línea de tendencia compacta con relleno degradado y punto final."""

    def __init__(self):
        super().__init__()
        self.values: list = []
        self.labels: list = []
        self.color = QColor("#122C94")
        self.setMinimumHeight(86)

    def set_data(self, values, labels, color: str):
        self.values = [float(v) for v in values]
        self.labels = labels
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if not self.values or max(self.values) <= 0:
            p.setPen(QColor("#9ca3af"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Sin ventas registradas")
            return
        margin_b = 16
        plot_h = h - 10 - margin_b
        max_v = max(self.values)
        n = len(self.values)
        step = w / max(1, n - 1)
        pts = [QPointF(i * step, 10 + plot_h * (1 - v / max_v)) for i, v in enumerate(self.values)]
        # relleno bajo la curva
        fill = QPainterPath()
        fill.moveTo(QPointF(0, 10 + plot_h))
        for pt in pts:
            fill.lineTo(pt)
        fill.lineTo(QPointF(w, 10 + plot_h))
        fill.closeSubpath()
        soft = QColor(self.color); soft.setAlpha(36)
        p.fillPath(fill, QBrush(soft))
        # línea
        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        pen = QPen(self.color, 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPath(line)
        # punto final
        p.setBrush(QBrush(self.color))
        p.drawEllipse(pts[-1], 3.5, 3.5)
        # etiquetas eje X
        f = QFont(self.font()); f.setPointSize(7)
        p.setFont(f)
        p.setPen(QColor("#9ca3af"))
        for i, lbl in enumerate(self.labels):
            p.drawText(QRectF(i * step - step / 2, h - 13, step, 12),
                       Qt.AlignmentFlag.AlignHCenter, lbl)
        p.end()


class _CbFlowChart(QWidget):
    """Barras agrupadas ingresos vs egresos por sub-período, con tooltip por barra."""

    def __init__(self):
        super().__init__()
        self.labels: list = []
        self.ingresos: list = []
        self.egresos: list = []
        self.color_in = QColor("#a6c438")
        self.color_eg = QColor("#b42318")
        self._bar_hits: list = []  # (QRectF, texto tooltip)
        self.setMinimumHeight(210)
        self.setMouseTracking(True)

    def set_data(self, labels, ingresos, egresos, color_in: str, color_eg: str):
        self.labels = labels
        self.ingresos = ingresos
        self.egresos = egresos
        self.color_in = QColor(color_in)
        self.color_eg = QColor(color_eg)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        for rect, tip in self._bar_hits:
            if rect.contains(pos):
                QToolTip.showText(event.globalPosition().toPoint(), tip, self)
                return
        QToolTip.hideText()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        self._bar_hits = []
        max_val = max(self.ingresos + self.egresos + [0])
        if not self.labels or max_val <= 0:
            p.setPen(QColor("#9ca3af"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Sin movimientos en el período.")
            return
        margin_l, margin_b, margin_t = 8, 24, 14
        plot_w = w - margin_l - 8
        plot_h = h - margin_t - margin_b
        # líneas guía horizontales
        p.setPen(QPen(QColor("#eef1f6"), 1))
        for i in range(1, 4):
            y = margin_t + plot_h * i / 4
            p.drawLine(margin_l, int(y), w - 8, int(y))
        p.setPen(QPen(QColor("#d1d5db"), 1))
        p.drawLine(margin_l, margin_t + plot_h, w - 8, margin_t + plot_h)

        n = len(self.labels)
        slot = plot_w / n
        bar_w = max(3.0, min(18.0, slot * 0.32))
        gap = bar_w * 0.25
        label_font = QFont(self.font()); label_font.setPointSize(7)
        label_step = max(1, n // 10)  # no amontonar etiquetas del eje X
        for i in range(n):
            cx = margin_l + slot * i + slot / 2
            for value, color, tipo in (
                (self.ingresos[i], self.color_in, "Ingresos"),
                (self.egresos[i], self.color_eg, "Egresos"),
            ):
                x = cx - bar_w - gap / 2 if tipo == "Ingresos" else cx + gap / 2
                bh = plot_h * (value / max_val)
                rect = QRectF(x, margin_t + plot_h - bh, bar_w, bh)
                path = QPainterPath()
                path.addRoundedRect(rect, 3, 3)
                p.fillPath(path, QBrush(color))
                hit = QRectF(x, margin_t, bar_w, plot_h)
                self._bar_hits.append((hit, f"{self.labels[i]} · {tipo}: {_rt_money(value)}"))
            if i % label_step == 0:
                p.setPen(QColor("#6b7280"))
                p.setFont(label_font)
                p.drawText(QRectF(cx - slot / 2, margin_t + plot_h + 4, slot, 16),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.labels[i])
        p.end()


class _CbDonutChart(QWidget):
    """Dona de distribución con total en el centro."""

    def __init__(self):
        super().__init__()
        self.items: list = []  # (label, value, QColor)
        self.center_title = ""
        self.center_value: str | None = None
        self.setMinimumSize(170, 190)

    def set_data(self, items, center_title: str, center_value: str | None = None):
        self.items = [(lbl, float(v), col) for lbl, v, col in items if float(v) > 0]
        self.center_title = center_title
        self.center_value = center_value
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = sum(v for _, v, _ in self.items)
        side = min(self.width(), self.height()) - 16
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        if total <= 0:
            p.setPen(QPen(QColor("#e5e7eb"), 16))
            p.drawEllipse(rect.adjusted(10, 10, -10, -10))
            p.setPen(QColor("#9ca3af"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Sin datos")
            return
        start = 90 * 16  # arriba, sentido horario
        ring = max(14.0, side * 0.13)
        arc_rect = rect.adjusted(ring / 2 + 4, ring / 2 + 4, -ring / 2 - 4, -ring / 2 - 4)
        for _, value, color in self.items:
            span = -int(round(5760 * value / total))
            pen = QPen(color, ring)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(arc_rect, start, span)
            start += span
        p.setPen(QColor("#111827"))
        f = QFont(self.font()); f.setPointSize(13); f.setWeight(QFont.Weight.ExtraBold)
        p.setFont(f)
        p.drawText(rect.adjusted(0, -10, 0, -10), Qt.AlignmentFlag.AlignCenter,
                   self.center_value if self.center_value is not None else _cb_money_compact(total))
        p.setPen(QColor("#6b7280"))
        f2 = QFont(self.font()); f2.setPointSize(7); f2.setWeight(QFont.Weight.Bold)
        p.setFont(f2)
        p.drawText(rect.adjusted(0, 18, 0, 18), Qt.AlignmentFlag.AlignCenter, self.center_title.upper())
        p.end()


class _CbMiniBar(QWidget):
    """Barra horizontal de progreso para desgloses (retenciones, categorías)."""

    def __init__(self, pct: float, color: str):
        super().__init__()
        self.pct = max(0.0, min(1.0, pct))
        self.color = QColor(color)
        self.setFixedHeight(8)
        self.setMinimumWidth(70)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 1, self.width(), 6), 3, 3)
        p.fillPath(track, QBrush(QColor("#eef1f6")))
        if self.pct > 0:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(0, 1, max(6.0, self.width() * self.pct), 6), 3, 3)
            p.fillPath(fill, QBrush(self.color))
        p.end()


class MovimientoDialog(QDialog):
    """Alta de un movimiento contable con preview de impuestos/retenciones."""

    def __init__(self, parent, store: LocalStore, categorias):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Nuevo movimiento")
        self.setMinimumWidth(440)
        self.result_data = None
        form = QFormLayout(self)
        form.setSpacing(10)

        self.tipo = QComboBox(); self.tipo.addItem("Ingreso", "ingreso"); self.tipo.addItem("Egreso", "egreso")
        self.tipo.currentIndexChanged.connect(self._on_tipo)
        self.categoria = QComboBox(); self.categoria.setEditable(True)
        for c in categorias:
            self.categoria.addItem(_cb_cat_label(c), c)
        self.descripcion = QLineEdit()
        self.monto = QDoubleSpinBox(); self.monto.setRange(0, 1e12); self.monto.setDecimals(0)
        self.monto.setPrefix("$ "); self.monto.setGroupSeparatorShown(True)
        self.monto.valueChanged.connect(self._update_preview)
        self.fecha = QDateEdit(QDate.currentDate()); self.fecha.setCalendarPopup(True); self.fecha.setDisplayFormat("yyyy-MM-dd")
        self.notas = QLineEdit()

        form.addRow("Tipo:", self.tipo)
        form.addRow("Categoría:", self.categoria)
        form.addRow("Descripción:", self.descripcion)
        form.addRow("Monto bruto:", self.monto)
        form.addRow("Fecha:", self.fecha)
        form.addRow("Notas:", self.notas)

        # Retenciones (solo ingreso)
        self.ret_box = QFrame(); self.ret_box.setObjectName("rtAddBox")
        rl = QFormLayout(self.ret_box); rl.setContentsMargins(12, 10, 12, 10); rl.setSpacing(6)
        self.refuente = self._pct(); self.iva = self._pct(); self.reteiva = self._pct(); self.reteica = self._pct()
        rl.addRow("ReteFuente %:", self.refuente)
        rl.addRow("IVA %:", self.iva)
        rl.addRow("ReteIVA % (sobre IVA):", self.reteiva)
        rl.addRow("ReteICA %:", self.reteica)
        form.addRow(self.ret_box)

        self.preview = QLabel("Neto: $ 0"); self.preview.setObjectName("rtDetailTotal")
        form.addRow(self.preview)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)
        self._on_tipo()

    def _pct(self):
        s = QDoubleSpinBox(); s.setRange(0, 100); s.setDecimals(2); s.setSuffix(" %")
        s.valueChanged.connect(self._update_preview)
        return s

    def _on_tipo(self):
        is_ing = self.tipo.currentData() == "ingreso"
        self.ret_box.setVisible(is_ing)
        self._update_preview()

    def _update_preview(self):
        bruto = self.monto.value()
        if self.tipo.currentData() == "ingreso":
            calc = LocalStore.cb_calcular_impuestos(bruto, self.refuente.value(), self.iva.value(), self.reteiva.value(), self.reteica.value())
            self.preview.setText(f"Retenciones: {_rt_money(calc['total_retenciones'])}   ·   Neto: {_rt_money(calc['monto_neto'])}")
        else:
            self.preview.setText(f"Neto: {_rt_money(bruto)}")

    def _accept(self):
        if not self.descripcion.text().strip():
            QMessageBox.warning(self, "Contabilidad", "La descripción es obligatoria."); return
        if self.monto.value() <= 0:
            QMessageBox.warning(self, "Contabilidad", "El monto debe ser mayor a cero."); return
        cat = self.categoria.currentData() or self.categoria.currentText().strip() or "otro"
        self.result_data = {
            "tipo": self.tipo.currentData(), "categoria": cat,
            "descripcion": self.descripcion.text().strip(),
            "monto_bruto": self.monto.value(), "fecha": self.fecha.date().toString("yyyy-MM-dd"),
            "notas": self.notas.text().strip(),
            "retefuente_pct": self.refuente.value(), "iva_pct": self.iva.value(),
            "reteiva_pct": self.reteiva.value(), "reteica_pct": self.reteica.value(),
        }
        self.accept()


class PlantillaDialog(QDialog):
    def __init__(self, parent, categorias):
        super().__init__(parent)
        self.setWindowTitle("Nueva plantilla recurrente")
        self.setMinimumWidth(400)
        self.result_data = None
        form = QFormLayout(self); form.setSpacing(10)
        self.tipo = QComboBox(); self.tipo.addItem("Ingreso", "ingreso"); self.tipo.addItem("Egreso", "egreso")
        self.categoria = QComboBox(); self.categoria.setEditable(True)
        for c in categorias:
            self.categoria.addItem(_cb_cat_label(c), c)
        self.descripcion = QLineEdit()
        self.monto = QDoubleSpinBox(); self.monto.setRange(0, 1e12); self.monto.setDecimals(0)
        self.monto.setPrefix("$ "); self.monto.setGroupSeparatorShown(True)
        self.notas = QLineEdit()
        form.addRow("Tipo:", self.tipo); form.addRow("Categoría:", self.categoria)
        form.addRow("Descripción:", self.descripcion); form.addRow("Monto:", self.monto)
        form.addRow("Notas:", self.notas)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _accept(self):
        if not self.descripcion.text().strip() or self.monto.value() <= 0:
            QMessageBox.warning(self, "Contabilidad", "Descripción y monto (>0) son obligatorios."); return
        self.result_data = {
            "tipo": self.tipo.currentData(),
            "categoria": self.categoria.currentData() or self.categoria.currentText().strip() or "otro",
            "descripcion": self.descripcion.text().strip(), "monto_bruto": self.monto.value(),
            "notas": self.notas.text().strip(),
        }
        self.accept()


class CierreDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nuevo cierre de período")
        self.setMinimumWidth(400)
        self.result_data = None
        form = QFormLayout(self); form.setSpacing(10)
        self.nombre = QLineEdit(); self.nombre.setPlaceholderText("Ej. Cierre Junio 2026")
        hoy = QDate.currentDate()
        self.fi = QDateEdit(QDate(hoy.year(), hoy.month(), 1)); self.fi.setCalendarPopup(True); self.fi.setDisplayFormat("yyyy-MM-dd")
        self.ff = QDateEdit(hoy); self.ff.setCalendarPopup(True); self.ff.setDisplayFormat("yyyy-MM-dd")
        self.notas = QLineEdit()
        form.addRow("Nombre:", self.nombre); form.addRow("Desde:", self.fi); form.addRow("Hasta:", self.ff)
        form.addRow("Notas:", self.notas)
        info = QLabel("Los totales (ingresos, egresos, retenciones, saldo) los calcula el servidor sobre todo el período.")
        info.setObjectName("muted"); info.setWordWrap(True)
        form.addRow(info)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _accept(self):
        if not self.nombre.text().strip():
            QMessageBox.warning(self, "Contabilidad", "El nombre es obligatorio."); return
        self.result_data = {
            "nombre": self.nombre.text().strip(),
            "fecha_inicio": self.fi.date().toString("yyyy-MM-dd"),
            "fecha_fin": self.ff.date().toString("yyyy-MM-dd"),
            "notas": self.notas.text().strip(),
        }
        self.accept()


# =============================================================================
# Contabilidad — página principal (offline-first)
# =============================================================================
class ContabilidadPage(QWidget):
    """Contabilidad offline-first: dashboard, movimientos, plantillas y cierres.
    Espejo local + outbox; sincroniza con producción."""

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._periodo = "mes"
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18); root.setSpacing(12)
        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("Contabilidad"); title.setObjectName("pageTitle")
        sub = QLabel("Ingresos, egresos, retenciones y cierres. Funciona sin internet; sincroniza con producción.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.sync_label = QLabel(""); self.sync_label.setObjectName("rtSyncLabel")
        header.addWidget(self.sync_label)
        refresh = QPushButton("↻  Refrescar"); refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tab_dash = QWidget(); self.tab_mov = QWidget(); self.tab_pla = QWidget(); self.tab_cie = QWidget()
        self.tabs.addTab(self.tab_dash, "Dashboard")
        self.tabs.addTab(self.tab_mov, "Movimientos")
        self.tabs.addTab(self.tab_pla, "Plantillas")
        self.tabs.addTab(self.tab_cie, "Cierres")
        QVBoxLayout(self.tab_dash); QVBoxLayout(self.tab_mov); QVBoxLayout(self.tab_pla); QVBoxLayout(self.tab_cie)
        for t in (self.tab_dash, self.tab_mov, self.tab_pla, self.tab_cie):
            t.layout().setContentsMargins(4, 12, 4, 4); t.layout().setSpacing(10)
        root.addWidget(self.tabs, 1)
        self.refresh()

    # ─── Refresh ───────────────────────────────────────────────────
    def refresh(self):
        pend = self.store.cb_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync"); self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado"); self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label); self.sync_label.style().polish(self.sync_label)
        self._render_dashboard(); self._render_movimientos(); self._render_plantillas(); self._render_cierres()

    # ─── Dashboard ─────────────────────────────────────────────────
    def _render_dashboard(self):
        lay = self.tab_dash.layout(); clear_layout(lay)
        colores = _cb_brand_colors()
        d = self.store.cb_dashboard(self._periodo)

        bar = QHBoxLayout()
        lbl = QLabel("Período:"); lbl.setObjectName("fieldLabel")
        bar.addWidget(lbl)
        self.periodo_combo = QComboBox()
        for txt, val in [("Esta semana", "semana"), ("Este mes", "mes"), ("Mes anterior", "mes_ant"), ("Este año", "anio")]:
            self.periodo_combo.addItem(txt, val)
        idx = max(0, self.periodo_combo.findData(self._periodo))
        self.periodo_combo.setCurrentIndex(idx)
        self.periodo_combo.currentIndexChanged.connect(self._on_periodo)
        bar.addWidget(self.periodo_combo)
        rango = QLabel(f"{d['rango'][0]}  →  {d['rango'][1]}")
        rango.setObjectName("muted")
        bar.addWidget(rango)
        bar.addStretch(1)
        lay.addLayout(bar)

        # KPIs con acento institucional
        cards = QGridLayout(); cards.setSpacing(10)
        cards.addWidget(_CbKpiCard("▲", "Ingresos netos", _rt_money(d["ingresos"]),
                                   f"{d['num_ingresos']} movimiento(s)", colores["acento_secundario"]), 0, 0)
        cards.addWidget(_CbKpiCard("▼", "Egresos", _rt_money(d["egresos"]),
                                   f"{d['num_egresos']} movimiento(s)", colores["peligro"]), 0, 1)
        saldo_color = colores["primario"] if d["saldo"] >= 0 else colores["peligro"]
        margen = (d["saldo"] / d["ingresos"] * 100) if d["ingresos"] else 0
        cards.addWidget(_CbKpiCard("Σ", "Saldo", _rt_money(d["saldo"]),
                                   f"Margen {margen:.0f}% sobre ingresos" if d["ingresos"] else "Sin ingresos en el período",
                                   saldo_color), 0, 2)
        cards.addWidget(_CbKpiCard("％", "Retenciones", _rt_money(d["retenciones"]),
                                   f"Bruto ingresos {_cb_money_compact(d['ingresos_bruto'])}", colores["acento"]), 0, 3)
        lay.addLayout(cards)

        # Fila de gráficos: flujo temporal + dona por categoría
        charts = QHBoxLayout(); charts.setSpacing(10)

        flow_panel = QFrame(); flow_panel.setObjectName("sectionPanel")
        fl = QVBoxLayout(flow_panel); fl.setContentsMargins(16, 12, 16, 10); fl.setSpacing(6)
        fhead = QHBoxLayout()
        fhead.addWidget(self._eyebrow("FLUJO DEL PERÍODO"))
        fhead.addStretch(1)
        for txt, col in (("● Ingresos", colores["acento_secundario"]), ("● Egresos", colores["peligro"])):
            chip = QLabel(txt)
            chip.setStyleSheet(f"color: {col}; font-size: 11px; font-weight: 700;")
            fhead.addWidget(chip)
        fl.addLayout(fhead)
        labels, serie_in, serie_eg = self._series_for_period(d["rango"])
        flow = _CbFlowChart()
        flow.set_data(labels, serie_in, serie_eg, colores["acento_secundario"], colores["peligro"])
        fl.addWidget(flow, 1)
        charts.addWidget(flow_panel, 3)

        cat_panel = QFrame(); cat_panel.setObjectName("sectionPanel")
        cl = QVBoxLayout(cat_panel); cl.setContentsMargins(16, 12, 16, 10); cl.setSpacing(6)
        cl.addWidget(self._eyebrow("DISTRIBUCIÓN POR CATEGORÍA"))
        cats = d["por_categoria"]
        if not cats:
            donut_body = QHBoxLayout()
            donut = _CbDonutChart(); donut.set_data([], "movido")
            donut_body.addWidget(donut, 1)
            cl.addLayout(donut_body, 1)
        else:
            palette = _cb_palette(colores)
            top = cats[:5]
            resto = sum(float(r["total"]) for r in cats[5:])
            items, total_cat = [], sum(float(r["total"]) for r in cats)
            for i, r in enumerate(top):
                items.append((f"{_cb_cat_label(r['categoria'])} ({_CB_TIPO_LABEL.get(r['tipo'], r['tipo'])})",
                              float(r["total"]), palette[i % len(palette)]))
            if resto > 0:
                items.append(("Otros", resto, QColor("#9ca3af")))
            donut_body = QHBoxLayout(); donut_body.setSpacing(10)
            donut = _CbDonutChart()
            donut.set_data(items, "movido")
            donut_body.addWidget(donut, 1)
            legend = QVBoxLayout(); legend.setSpacing(6)
            legend.addStretch(1)
            for label_txt, value, color in items:
                row = QVBoxLayout(); row.setSpacing(2)
                head = QHBoxLayout()
                name = QLabel(label_txt)
                name.setStyleSheet(f"color: #374151; font-size: 11px; font-weight: 700;")
                head.addWidget(name, 1)
                val = QLabel(_cb_money_compact(value))
                val.setStyleSheet(f"color: {color.name()}; font-size: 11px; font-weight: 800;")
                head.addWidget(val)
                row.addLayout(head)
                row.addWidget(_CbMiniBar(value / total_cat if total_cat else 0, color.name()))
                legend.addLayout(row)
            legend.addStretch(1)
            donut_body.addLayout(legend, 1)
            cl.addLayout(donut_body, 1)
        charts.addWidget(cat_panel, 2)
        lay.addLayout(charts, 1)

        # Desglose de retenciones e IVA con barras
        det = QFrame(); det.setObjectName("sectionPanel")
        dl = QVBoxLayout(det); dl.setContentsMargins(16, 12, 16, 12); dl.setSpacing(8)
        dl.addWidget(self._eyebrow("DESGLOSE DE RETENCIONES E IVA"))
        conceptos = [("ReteFuente", d["retefuente"]), ("IVA", d["iva"]),
                     ("ReteIVA", d["reteiva"]), ("ReteICA", d["reteica"])]
        max_ret = max([v for _, v in conceptos] + [0])
        ret_row = QHBoxLayout(); ret_row.setSpacing(18)
        for nombre, valor in conceptos:
            cell = QVBoxLayout(); cell.setSpacing(3)
            head = QHBoxLayout()
            n = QLabel(nombre); n.setStyleSheet("color: #6b7280; font-size: 11px; font-weight: 700;")
            head.addWidget(n, 1)
            v = QLabel(_rt_money(valor))
            v.setStyleSheet(f"color: {colores['primario_oscuro']}; font-size: 12px; font-weight: 800;")
            head.addWidget(v)
            cell.addLayout(head)
            cell.addWidget(_CbMiniBar(valor / max_ret if max_ret else 0, colores["acento"]))
            ret_row.addLayout(cell, 1)
        dl.addLayout(ret_row)
        lay.addWidget(det)

    def _series_for_period(self, rango):
        """Agrega ingresos/egresos por día (semana/mes) o por mes (año)."""
        from datetime import date as _date, timedelta as _td
        movs = self.store.cb_list_movimientos(fecha_ini=rango[0], fecha_fin=rango[1], limit=100000)
        if self._periodo == "anio":
            labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
            ing = [0.0] * 12; egr = [0.0] * 12
            for m in movs:
                try:
                    mes = int((m["fecha"] or "")[5:7]) - 1
                except ValueError:
                    continue
                if 0 <= mes < 12:
                    (ing if m["tipo"] == "ingreso" else egr)[mes] += float(m["monto"] or 0)
            return labels, ing, egr
        try:
            ini = _date.fromisoformat(rango[0]); fin = _date.fromisoformat(rango[1])
        except ValueError:
            return [], [], []
        days = []
        cursor = ini
        while cursor <= fin and len(days) < 62:
            days.append(cursor); cursor += _td(days=1)
        dias_semana = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        if self._periodo == "semana":
            labels = [dias_semana[dd.weekday()] for dd in days]
        else:
            labels = [str(dd.day) for dd in days]
        idx = {dd.isoformat(): i for i, dd in enumerate(days)}
        ing = [0.0] * len(days); egr = [0.0] * len(days)
        for m in movs:
            i = idx.get((m["fecha"] or "")[:10])
            if i is None:
                continue
            (ing if m["tipo"] == "ingreso" else egr)[i] += float(m["monto"] or 0)
        return labels, ing, egr

    def _on_periodo(self):
        self._periodo = self.periodo_combo.currentData() or "mes"
        self._render_dashboard()

    # ─── Movimientos ───────────────────────────────────────────────
    def _render_movimientos(self):
        lay = self.tab_mov.layout(); clear_layout(lay)
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.f_tipo = QComboBox(); self.f_tipo.addItem("Todos", None); self.f_tipo.addItem("Ingresos", "ingreso"); self.f_tipo.addItem("Egresos", "egreso")
        self.f_cat = QComboBox(); self.f_cat.addItem("Todas las categorías", None)
        for c in self.store.cb_categorias():
            self.f_cat.addItem(_cb_cat_label(c), c)
        self.f_tipo.currentIndexChanged.connect(self._render_mov_table)
        self.f_cat.currentIndexChanged.connect(self._render_mov_table)
        bar.addWidget(QLabel("Filtro:")); bar.addWidget(self.f_tipo); bar.addWidget(self.f_cat); bar.addStretch(1)
        nuevo = QPushButton("＋  Nuevo movimiento"); nuevo.setObjectName("primaryAction"); nuevo.clicked.connect(self._nuevo_movimiento)
        export = QPushButton("Exportar CSV"); export.setObjectName("secondaryAction"); export.clicked.connect(self._export_csv)
        bar.addWidget(export); bar.addWidget(nuevo)
        lay.addLayout(bar)

        self.mov_table = QTableWidget(0, 6)
        self.mov_table.setHorizontalHeaderLabels(["Fecha", "Tipo", "Categoría", "Descripción", "Neto", ""])
        self.mov_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.mov_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mov_table.verticalHeader().setVisible(False)
        self.mov_table.verticalHeader().setDefaultSectionSize(44)
        lay.addWidget(self.mov_table, 1)
        self._render_mov_table()

    def _render_mov_table(self):
        tipo = self.f_tipo.currentData(); cat = self.f_cat.currentData()
        rows = self.store.cb_list_movimientos(tipo=tipo, categoria=cat)
        self.mov_table.setRowCount(len(rows))
        for i, m in enumerate(rows):
            self.mov_table.setItem(i, 0, QTableWidgetItem(m["fecha"] or ""))
            self.mov_table.setItem(i, 1, QTableWidgetItem(_CB_TIPO_LABEL.get(m["tipo"], m["tipo"])))
            self.mov_table.setItem(i, 2, QTableWidgetItem(_cb_cat_label(m["categoria"])))
            desc = m["descripcion"] + ("  ⏳" if m["synced"] == 0 else "")
            self.mov_table.setItem(i, 3, QTableWidgetItem(desc))
            monto = QTableWidgetItem(("− " if m["tipo"] == "egreso" else "") + _rt_money(m["monto"]))
            self.mov_table.setItem(i, 4, monto)
            if m["auto_generado"]:
                tag = QTableWidgetItem("auto"); tag.setToolTip("Automático (venta) — no se puede eliminar")
                self.mov_table.setItem(i, 5, tag)
            else:
                btn = QPushButton("Eliminar"); btn.setObjectName("inlineDanger")
                btn.clicked.connect(lambda _=False, lid=m["local_id"]: self._eliminar_movimiento(lid))
                self.mov_table.setCellWidget(i, 5, btn)

    def _nuevo_movimiento(self):
        dlg = MovimientoDialog(self, self.store, self.store.cb_categorias())
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.cb_crear_movimiento(
                d["tipo"], d["categoria"], d["descripcion"], d["monto_bruto"], fecha=d["fecha"], notas=d["notas"],
                retefuente_pct=d["retefuente_pct"], iva_pct=d["iva_pct"], reteiva_pct=d["reteiva_pct"],
                reteica_pct=d["reteica_pct"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    def _eliminar_movimiento(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar este movimiento?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.cb_eliminar_movimiento(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    def _export_csv(self):
        rows = self.store.cb_list_movimientos(tipo=self.f_tipo.currentData(), categoria=self.f_cat.currentData(), limit=100000)
        path, _ = QFileDialog.getSaveFileName(self, "Exportar movimientos", "contabilidad.csv", "CSV (*.csv)")
        if not path:
            return
        import csv
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Fecha", "Tipo", "Categoria", "Descripcion", "Monto bruto", "Retenciones", "Neto", "Notas"])
                for m in rows:
                    w.writerow([m["fecha"], m["tipo"], m["categoria"], m["descripcion"],
                                m["monto_bruto"], m["total_retenciones"], m["monto"], m.get("notas") or ""])
            QMessageBox.information(self, "Exportar", f"Exportado: {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Exportar", f"No se pudo guardar: {exc}")

    # ─── Plantillas ────────────────────────────────────────────────
    def _render_plantillas(self):
        lay = self.tab_pla.layout(); clear_layout(lay)
        bar = QHBoxLayout()
        info = QLabel("Movimientos recurrentes. 'Generar' crea los del mes actual (sin duplicar).")
        info.setObjectName("muted"); info.setWordWrap(True)
        bar.addWidget(info, 1)
        gen = QPushButton("Generar movimientos del mes"); gen.setObjectName("secondaryAction"); gen.clicked.connect(self._generar)
        nueva = QPushButton("＋  Nueva plantilla"); nueva.setObjectName("primaryAction"); nueva.clicked.connect(self._nueva_plantilla)
        bar.addWidget(gen); bar.addWidget(nueva)
        lay.addLayout(bar)
        self.pla_table = QTableWidget(0, 6)
        self.pla_table.setHorizontalHeaderLabels(["Tipo", "Categoría", "Descripción", "Monto", "Activa", ""])
        self.pla_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.pla_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pla_table.verticalHeader().setVisible(False)
        self.pla_table.verticalHeader().setDefaultSectionSize(44)
        lay.addWidget(self.pla_table, 1)
        rows = self.store.cb_list_plantillas()
        self.pla_table.setRowCount(len(rows))
        for i, p in enumerate(rows):
            self.pla_table.setItem(i, 0, QTableWidgetItem(_CB_TIPO_LABEL.get(p["tipo"], p["tipo"])))
            self.pla_table.setItem(i, 1, QTableWidgetItem(_cb_cat_label(p["categoria"])))
            self.pla_table.setItem(i, 2, QTableWidgetItem(p["descripcion"]))
            self.pla_table.setItem(i, 3, QTableWidgetItem(_rt_money(p["monto_bruto"])))
            tg = QPushButton("Sí" if p["activo"] else "No"); tg.setObjectName("secondaryAction")
            tg.clicked.connect(lambda _=False, lid=p["local_id"]: self._toggle_plantilla(lid))
            self.pla_table.setCellWidget(i, 4, tg)
            dl = QPushButton("Eliminar"); dl.setObjectName("inlineDanger")
            dl.clicked.connect(lambda _=False, lid=p["local_id"]: self._eliminar_plantilla(lid))
            self.pla_table.setCellWidget(i, 5, dl)

    def _nueva_plantilla(self):
        dlg = PlantillaDialog(self, self.store.cb_categorias())
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.cb_crear_plantilla(d["tipo"], d["categoria"], d["descripcion"], d["monto_bruto"], notas=d["notas"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    def _toggle_plantilla(self, local_id):
        try:
            self.store.cb_toggle_plantilla(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    def _eliminar_plantilla(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar esta plantilla?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.cb_eliminar_plantilla(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    def _generar(self):
        self.store.cb_generar_plantillas(user=self._user())
        QMessageBox.information(self, "Plantillas", "Generación encolada. Los movimientos del mes aparecerán al sincronizar.")
        self._after_change()

    # ─── Cierres ───────────────────────────────────────────────────
    def _render_cierres(self):
        lay = self.tab_cie.layout(); clear_layout(lay)
        bar = QHBoxLayout()
        info = QLabel("Resumen de un período (ingresos, egresos, retenciones, saldo).")
        info.setObjectName("muted"); bar.addWidget(info, 1)
        nuevo = QPushButton("＋  Nuevo cierre"); nuevo.setObjectName("primaryAction"); nuevo.clicked.connect(self._nuevo_cierre)
        bar.addWidget(nuevo)
        lay.addLayout(bar)
        self.cie_table = QTableWidget(0, 7)
        self.cie_table.setHorizontalHeaderLabels(["Nombre", "Desde", "Hasta", "Ingresos", "Egresos", "Saldo", ""])
        self.cie_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.cie_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cie_table.verticalHeader().setVisible(False)
        self.cie_table.verticalHeader().setDefaultSectionSize(44)
        lay.addWidget(self.cie_table, 1)
        rows = self.store.cb_list_cierres()
        self.cie_table.setRowCount(len(rows))
        for i, c in enumerate(rows):
            nombre = c["nombre"] + ("  ⏳" if c["synced"] == 0 else "")
            self.cie_table.setItem(i, 0, QTableWidgetItem(nombre))
            self.cie_table.setItem(i, 1, QTableWidgetItem(c["fecha_inicio"] or ""))
            self.cie_table.setItem(i, 2, QTableWidgetItem(c["fecha_fin"] or ""))
            self.cie_table.setItem(i, 3, QTableWidgetItem(_rt_money(c["total_ingresos"])))
            self.cie_table.setItem(i, 4, QTableWidgetItem(_rt_money(c["total_egresos"])))
            self.cie_table.setItem(i, 5, QTableWidgetItem(_rt_money(c["saldo"])))
            dl = QPushButton("Eliminar"); dl.setObjectName("inlineDanger")
            dl.clicked.connect(lambda _=False, lid=c["local_id"]: self._eliminar_cierre(lid))
            self.cie_table.setCellWidget(i, 6, dl)

    def _nuevo_cierre(self):
        dlg = CierreDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.cb_crear_cierre(d["nombre"], d["fecha_inicio"], d["fecha_fin"], notas=d["notas"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        QMessageBox.information(self, "Cierre", "Cierre encolado. Los totales los calcula el servidor al sincronizar.")
        self._after_change()

    def _eliminar_cierre(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar este cierre?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.cb_eliminar_cierre(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Contabilidad", str(exc)); return
        self._after_change()

    # ─── Helpers ───────────────────────────────────────────────────
    def _eyebrow(self, text):
        l = QLabel(text); l.setObjectName("eyebrow"); return l

    def _muted(self, text):
        l = QLabel(text); l.setObjectName("muted"); l.setWordWrap(True); return l

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# =============================================================================
# Cotizaciones — PDF local + página offline-first
# =============================================================================
_CB_ESTADO_LABEL = {"pendiente": "Pendiente", "aprobada": "Aprobada", "rechazada": "Rechazada"}


class _NumItem(QTableWidgetItem):
    """Celda de tabla que ordena por un valor numérico subyacente (no por texto),
    para que la columna de Total se ordene correctamente."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self._value = float(value or 0)

    def __lt__(self, other):
        try:
            return self._value < other._value
        except AttributeError:
            return super().__lt__(other)


_PDF_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _pdf_money(valor) -> str:
    """Replica helpers.formatear_moneda de la web (locale COP, fallback manual)."""
    import locale as _locale
    try:
        return _locale.currency(float(valor or 0), symbol=True, grouping=True)
    except Exception:
        return f"${float(valor or 0):,.2f}"


def _pdf_brand_colores(branding: dict) -> dict:
    """Construye el dict `colores` que esperan las plantillas PDF de la web,
    mezclando la marca del tenant (primario) con los defaults de Config.BRAND_COLORS."""
    bc = (branding or {}).get("colores", {})
    emp = (branding or {}).get("empresa", {})
    return {
        "primario": bc.get("primario", "#122C94"),
        "primario_oscuro": bc.get("primario_oscuro", "#091C5A"),
        "secundario": "#0e1b33",
        "texto": "#333333",
        "texto_claro": "#888888",
        "fondo_claro": "#f9f9f9",
        "exito": "#28a745",
        "borde": "#000000",
        "website": emp.get("website") or "https://cybershopcol.com",
    }


def _pdf_logo_path(branding: dict) -> str:
    """Ruta local del logo para xhtml2pdf (o '' si no hay)."""
    p = ((branding or {}).get("empresa", {}) or {}).get("logo_path") or ""
    return p if p and Path(p).is_file() else ""


def _pdf_link_callback(uri, rel):
    """Resuelve rutas locales para xhtml2pdf; evita peticiones HTTP."""
    if not uri:
        return uri
    if uri.startswith("file://"):
        return uri[7:]
    return uri


def _render_html_pdf(template_name: str, context: dict, out_path: str) -> None:
    """Renderiza una plantilla HTML (jinja2) y la convierte a PDF (xhtml2pdf).
    Usa las MISMAS plantillas que la web para un diseño idéntico."""
    import jinja2
    from xhtml2pdf import pisa
    tpl_dir = _asset_path("pdf_templates")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(tpl_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    html = env.get_template(template_name).render(**context)
    with open(out_path, "wb") as fh:
        status = pisa.CreatePDF(html, dest=fh, link_callback=_pdf_link_callback)
    if status.err:
        raise RuntimeError("xhtml2pdf no pudo generar el PDF")


def _quote_generate_pdf(path: str, cot: dict, items: list, branding: dict) -> None:
    """Genera el PDF de una cotización con la MISMA plantilla de la web
    (pdf_quote.html) — diseño idéntico, 100% local."""
    # Desglose de totales (espejo de routes/quotes.py)
    subtotal = descuentos = iva_total = 0.0
    items_ctx = []
    for it in items:
        cant = int(it.get("cantidad") or 0)
        precio = float(it.get("precio_unitario") or 0)
        dpct = float(it.get("descuento_porc") or 0)
        ipct = float(it.get("iva_porc") or 0)
        base = cant * precio
        md = base * (dpct / 100)
        menos = base - md
        miva = menos * (ipct / 100) if ipct > 0 else 0
        subtotal += base
        descuentos += md
        iva_total += miva
        items_ctx.append({
            "imagen_local_path": None,
            "descripcion": it.get("descripcion") or "",
            "cantidad": cant,
            "precio_unitario": _pdf_money(precio),
            "subtotal": _pdf_money(it.get("subtotal")),
        })
    fecha = cot.get("fecha") or date.today().isoformat()
    try:
        d = date.fromisoformat(str(fecha)[:10])
        fecha_txt = f"Bogotá {d.day} de {_PDF_MESES[d.month - 1]} de {d.year}"
    except (ValueError, IndexError):
        fecha_txt = f"Bogotá {fecha}"
    num = cot.get("remote_id") or cot.get("local_id") or 0
    ctx = {
        "id": f"COT {str(num).zfill(10)}",
        "fecha": fecha_txt,
        "logo": _pdf_logo_path(branding),
        "colores": _pdf_brand_colores(branding),
        "cliente": {
            "nombre": cot.get("cliente_nombre") or "",
            "documento": cot.get("cliente_documento") or "",
            "direccion": cot.get("cliente_direccion") or "",
            "ciudad": cot.get("cliente_ciudad") or "",
            "representante": cot.get("cliente_representante") or "",
            "localidad": cot.get("cliente_localidad") or "",
            "cargo": cot.get("cliente_cargo") or "",
            "telefono": cot.get("cliente_telefono") or "",
        },
        "items": items_ctx,
        "subtotal": _pdf_money(subtotal),
        "descuento": _pdf_money(descuentos),
        "iva": _pdf_money(iva_total),
        "total": _pdf_money(cot.get("total")),
    }
    _render_html_pdf("pdf_quote.html", ctx, path)


class _QuoteDialog(QDialog):
    """Alta de una cotización: datos del cliente + ítems."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nueva cotización")
        self.setMinimumWidth(640)
        self.result_data = None
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.nombre = QLineEdit(); self.documento = QLineEdit(); self.telefono = QLineEdit()
        self.ciudad = QLineEdit(); self.direccion = QLineEdit(); self.representante = QLineEdit()
        form.addRow("Cliente *", self.nombre)
        form.addRow("Documento", self.documento)
        form.addRow("Teléfono", self.telefono)
        form.addRow("Ciudad", self.ciudad)
        form.addRow("Dirección", self.direccion)
        form.addRow("Representante", self.representante)
        root.addLayout(form)

        bar = QHBoxLayout()
        eyebrow = QLabel("ÍTEMS"); eyebrow.setObjectName("eyebrow")
        bar.addWidget(eyebrow); bar.addStretch(1)
        add = QPushButton("＋  Agregar ítem"); add.setObjectName("secondaryAction"); add.clicked.connect(self._add_row)
        bar.addWidget(add)
        root.addLayout(bar)

        self.items = QTableWidget(0, 5)
        self.items.setHorizontalHeaderLabels(["Descripción", "Cantidad", "P. Unitario", "Desc %", "IVA %"])
        self.items.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.items, 1)
        self._add_row()

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _add_row(self):
        r = self.items.rowCount(); self.items.insertRow(r)
        self.items.setItem(r, 0, QTableWidgetItem(""))
        for col, default in ((1, "1"), (2, "0"), (3, "0"), (4, "0")):
            self.items.setItem(r, col, QTableWidgetItem(default))

    def _accept(self):
        if not self.nombre.text().strip():
            QMessageBox.warning(self, "Cotización", "El nombre del cliente es obligatorio."); return
        items = []
        for r in range(self.items.rowCount()):
            desc = (self.items.item(r, 0).text() if self.items.item(r, 0) else "").strip()
            if not desc:
                continue
            def _num(col, cast):
                try:
                    return cast(self.items.item(r, col).text())
                except (ValueError, AttributeError):
                    return cast(0)
            items.append({"descripcion": desc, "cantidad": _num(1, int),
                          "precio_unitario": _num(2, float), "descuento_porc": _num(3, float),
                          "iva_porc": _num(4, float)})
        if not items:
            QMessageBox.warning(self, "Cotización", "Agrega al menos un ítem con descripción."); return
        self.result_data = {
            "cliente": {
                "cliente_nombre": self.nombre.text().strip(),
                "cliente_documento": self.documento.text().strip(),
                "cliente_telefono": self.telefono.text().strip(),
                "cliente_ciudad": self.ciudad.text().strip(),
                "cliente_direccion": self.direccion.text().strip(),
                "cliente_representante": self.representante.text().strip(),
            },
            "items": items,
        }
        self.accept()


class CotizacionesPage(QWidget):
    """Cotizaciones offline-first: crear (PDF local), listar, aprobar/rechazar,
    eliminar; sincroniza con producción vía outbox quote_op."""

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18); root.setSpacing(12)
        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("Cotizaciones"); title.setObjectName("pageTitle")
        sub = QLabel("Genera cotizaciones con PDF local. Funciona sin internet; sincroniza con producción.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.sync_label = QLabel(""); self.sync_label.setObjectName("rtSyncLabel")
        header.addWidget(self.sync_label)
        self.nueva_btn = QPushButton("＋  Nueva cotización"); self.nueva_btn.setObjectName("primaryAction")
        self.nueva_btn.clicked.connect(self._nueva)
        header.addWidget(self.nueva_btn)
        refresh = QPushButton("↻  Refrescar"); refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        search_row = QHBoxLayout(); search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Buscar por cliente, documento o estado")
        self.search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search, 1)
        hint = QLabel("Clic en una columna para ordenar (ej. Fecha o Total)")
        hint.setObjectName("muted")
        search_row.addWidget(hint)
        root.addLayout(search_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Fecha", "Cliente", "Total", "Estado", "PDF", ""])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(5, 290)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.DescendingOrder)  # por fecha desc por defecto
        root.addWidget(self.table, 1)
        self._all_rows = []
        self.refresh()

    _CELL_BTN = "min-height: 26px; padding: 0 10px; font-size: 12px;"

    def refresh(self):
        pend = self.store.q_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync"); self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado"); self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label); self.sync_label.style().polish(self.sync_label)
        self.nueva_btn.setVisible(self.can("quotes", "create"))
        self._all_rows = self.store.q_list_cotizaciones()
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.text().strip().lower() if hasattr(self, "search") else ""
        rows = [q for q in self._all_rows if not needle or needle in (
            (q.get("cliente_nombre") or "") + " " + (q.get("cliente_documento") or "") + " " + (q.get("estado") or "")
        ).lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, q in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(q["fecha"] or ""))
            cliente = q["cliente_nombre"] + ("  ⏳" if q["synced"] == 0 else "")
            self.table.setItem(i, 1, QTableWidgetItem(cliente))
            self.table.setItem(i, 2, _NumItem(_rt_money(q["total"]), q["total"]))
            self.table.setItem(i, 3, QTableWidgetItem(_CB_ESTADO_LABEL.get(q["estado"], q["estado"])))
            pdf_btn = QPushButton("PDF"); pdf_btn.setObjectName("secondaryAction")
            pdf_btn.setStyleSheet(self._CELL_BTN)
            pdf_btn.clicked.connect(lambda _=False, lid=q["local_id"]: self._pdf(lid))
            pdf_wrap = QWidget(); pw = QHBoxLayout(pdf_wrap); pw.setContentsMargins(4, 0, 4, 0)
            pw.addWidget(pdf_btn)
            self.table.setCellWidget(i, 4, pdf_wrap)
            actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(4, 0, 4, 0); al.setSpacing(4)
            if self.can("quotes", "approve") and q["estado"] == "pendiente":
                ap = QPushButton("Aprobar"); ap.setObjectName("secondaryAction"); ap.setStyleSheet(self._CELL_BTN)
                ap.clicked.connect(lambda _=False, lid=q["local_id"]: self._estado(lid, "aprobada"))
                rj = QPushButton("Rechazar"); rj.setObjectName("inlineDanger"); rj.setStyleSheet(self._CELL_BTN)
                rj.clicked.connect(lambda _=False, lid=q["local_id"]: self._estado(lid, "rechazada"))
                al.addWidget(ap); al.addWidget(rj)
            if self.can("quotes", "delete"):
                dl = QPushButton("Eliminar"); dl.setObjectName("inlineDanger"); dl.setStyleSheet(self._CELL_BTN)
                dl.clicked.connect(lambda _=False, lid=q["local_id"]: self._eliminar(lid))
                al.addWidget(dl)
            al.addStretch(1)
            self.table.setCellWidget(i, 5, actions)
        self.table.setSortingEnabled(True)

    def _nueva(self):
        if not self.can("quotes", "create"):
            return
        dlg = _QuoteDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            local_id = self.store.q_crear_cotizacion(dlg.result_data["cliente"], dlg.result_data["items"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Cotización", str(exc)); return
        self._after_change()
        if QMessageBox.question(self, "Cotización", "Cotización creada. ¿Generar el PDF ahora?") == QMessageBox.StandardButton.Yes:
            self._pdf(local_id)

    def _pdf(self, local_id):
        rows = {r["local_id"]: r for r in self.store.q_list_cotizaciones()}
        cot = rows.get(local_id)
        if not cot:
            return
        items = self.store.q_get_detalle(local_id)
        path, _ = QFileDialog.getSaveFileName(self, "Guardar cotización PDF",
                                              f"Cotizacion_{local_id}.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            branding = branding_mod.load_branding(app_data_dir())
            _quote_generate_pdf(path, cot, items, branding)
            QMessageBox.information(self, "Cotización", f"PDF generado: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Cotización", f"No se pudo generar el PDF: {exc}")

    def _estado(self, local_id, estado):
        try:
            self.store.q_set_estado(local_id, estado, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Cotización", str(exc)); return
        self._after_change()

    def _eliminar(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar esta cotización?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.q_eliminar_cotizacion(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Cotización", str(exc)); return
        self._after_change()

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# =============================================================================
# Cuentas de cobro — PDF local + página offline-first
# =============================================================================
def _cobro_generate_pdf(path: str, cuenta: dict, items: list, branding: dict) -> None:
    """Genera el PDF de una cuenta de cobro con la MISMA plantilla de la web
    (pdf_cuenta_cobro.html) — diseño idéntico, 100% local."""
    total = float(cuenta.get("total") or 0)
    try:
        import num2words
        total_texto = num2words.num2words(total, lang="es").upper() + " PESOS M/CTE"
    except Exception:
        total_texto = f"{total} PESOS M/CTE"
    # Fecha larga + consecutivo con formato de la web (dd-mm-yyyy-id)
    fecha = cuenta.get("fecha") or date.today().isoformat()
    meses_cap = [m.capitalize() for m in _PDF_MESES]
    num = cuenta.get("remote_id") or cuenta.get("local_id") or 0
    try:
        d = date.fromisoformat(str(fecha)[:10])
        fecha_larga = f"{d.day} de {meses_cap[d.month - 1]} de {d.year}"
        consecutivo = f"{d.day:02d}-{d.month:02d}-{d.year}-{num}"
    except (ValueError, IndexError):
        fecha_larga = str(fecha)
        consecutivo = cuenta.get("consecutivo") or f"CDC-{num}"
    items_ctx = [{
        "fecha": it.get("fecha_labor") or "",
        "descripcion": it.get("descripcion") or "",
        "valor_formatted": _pdf_money(it.get("valor")),
    } for it in items]
    emp = (branding or {}).get("empresa", {})
    ctx = {
        "consecutivo": consecutivo,
        "fecha": fecha_larga,
        "logo": _pdf_logo_path(branding),
        "cliente": {
            "nombre": cuenta.get("cliente_nombre") or "",
            "nit": cuenta.get("cliente_nit") or "",
            "direccion": cuenta.get("cliente_direccion") or "",
            "ciudad": cuenta.get("cliente_ciudad") or "",
        },
        "contractor": {
            "nombre": cuenta.get("contractor_nombre") or "",
            "id": cuenta.get("contractor_id") or "",
            "texto_pago": cuenta.get("texto_pago") or "",
            "email": cuenta.get("contractor_email") or "",
            "telefono": cuenta.get("contractor_telefono") or "",
        },
        "items": items_ctx,
        "total_valor": _pdf_money(total),
        "total_texto": total_texto,
        "empresa_website": emp.get("website") or "https://cybershopcol.com",
    }
    _render_html_pdf("pdf_cuenta_cobro.html", ctx, path)


class _CobroDialog(QDialog):
    """Alta de una cuenta de cobro: cliente + quien cobra + ítems (labores)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nueva cuenta de cobro")
        self.setMinimumWidth(640)
        self.result_data = None
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.nombre = QLineEdit(); self.nit = QLineEdit(); self.telefono = QLineEdit()
        self.ciudad = QLineEdit(); self.direccion = QLineEdit()
        self.contractor = QLineEdit(); self.contractor_id = QLineEdit()
        self.texto_pago = QLineEdit()
        form.addRow("Cliente *", self.nombre)
        form.addRow("NIT / CC", self.nit)
        form.addRow("Teléfono", self.telefono)
        form.addRow("Ciudad", self.ciudad)
        form.addRow("Dirección", self.direccion)
        form.addRow("Quien cobra", self.contractor)
        form.addRow("Doc. quien cobra", self.contractor_id)
        form.addRow("Texto de pago", self.texto_pago)
        root.addLayout(form)
        bar = QHBoxLayout()
        eyebrow = QLabel("LABORES / ÍTEMS"); eyebrow.setObjectName("eyebrow")
        bar.addWidget(eyebrow); bar.addStretch(1)
        add = QPushButton("＋  Agregar ítem"); add.setObjectName("secondaryAction"); add.clicked.connect(self._add_row)
        bar.addWidget(add)
        root.addLayout(bar)
        self.items = QTableWidget(0, 3)
        self.items.setHorizontalHeaderLabels(["Fecha (YYYY-MM-DD)", "Descripción", "Valor"])
        self.items.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.items, 1)
        self._add_row()
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _add_row(self):
        r = self.items.rowCount(); self.items.insertRow(r)
        self.items.setItem(r, 0, QTableWidgetItem(QDate.currentDate().toString("yyyy-MM-dd")))
        self.items.setItem(r, 1, QTableWidgetItem(""))
        self.items.setItem(r, 2, QTableWidgetItem("0"))

    def _accept(self):
        if not self.nombre.text().strip():
            QMessageBox.warning(self, "Cuenta de cobro", "El nombre del cliente es obligatorio."); return
        items = []
        for r in range(self.items.rowCount()):
            desc = (self.items.item(r, 1).text() if self.items.item(r, 1) else "").strip()
            if not desc:
                continue
            fecha = (self.items.item(r, 0).text() if self.items.item(r, 0) else "").strip()
            try:
                valor = float(self.items.item(r, 2).text())
            except (ValueError, AttributeError):
                valor = 0.0
            items.append({"fecha_labor": fecha, "descripcion": desc, "valor": valor})
        if not items:
            QMessageBox.warning(self, "Cuenta de cobro", "Agrega al menos un ítem con descripción."); return
        self.result_data = {
            "cliente": {
                "cliente_nombre": self.nombre.text().strip(), "cliente_nit": self.nit.text().strip(),
                "cliente_telefono": self.telefono.text().strip(), "cliente_ciudad": self.ciudad.text().strip(),
                "cliente_direccion": self.direccion.text().strip(),
                "contractor_nombre": self.contractor.text().strip(),
                "contractor_id": self.contractor_id.text().strip(),
                "texto_pago": self.texto_pago.text().strip(),
            },
            "items": items,
        }
        self.accept()


class CuentasCobroPage(QWidget):
    """Cuentas de cobro offline-first: crear (PDF local), listar, eliminar;
    sincroniza con producción vía outbox cobro_op."""

    _CELL_BTN = "min-height: 26px; padding: 0 10px; font-size: 12px;"

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18); root.setSpacing(12)
        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("Cuentas de cobro"); title.setObjectName("pageTitle")
        sub = QLabel("Documentos de cobro con PDF local. Funciona sin internet; sincroniza con producción.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.sync_label = QLabel(""); self.sync_label.setObjectName("rtSyncLabel")
        header.addWidget(self.sync_label)
        self.nueva_btn = QPushButton("＋  Nueva cuenta"); self.nueva_btn.setObjectName("primaryAction")
        self.nueva_btn.clicked.connect(self._nueva)
        header.addWidget(self.nueva_btn)
        refresh = QPushButton("↻  Refrescar"); refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        search_row = QHBoxLayout(); search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Buscar por consecutivo, cliente o quien cobra")
        self.search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search, 1)
        hint = QLabel("Clic en una columna para ordenar (ej. Fecha o Total)")
        hint.setObjectName("muted")
        search_row.addWidget(hint)
        root.addLayout(search_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Fecha", "Consecutivo", "Cliente", "Quien cobra", "Total", "PDF", ""])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(6, 130)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.DescendingOrder)  # por fecha desc por defecto
        root.addWidget(self.table, 1)
        self._all_rows = []
        self.refresh()

    def refresh(self):
        pend = self.store.cc_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync"); self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado"); self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label); self.sync_label.style().polish(self.sync_label)
        self.nueva_btn.setVisible(self.can("cobros", "create"))
        self._all_rows = self.store.cc_list_cuentas()
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.text().strip().lower() if hasattr(self, "search") else ""
        rows = [q for q in self._all_rows if not needle or needle in (
            (q.get("consecutivo") or "") + " " + (q.get("cliente_nombre") or "") + " " + (q.get("contractor_nombre") or "")
        ).lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, q in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(q["fecha"] or ""))
            cons = (q["consecutivo"] or "") + ("  ⏳" if q["synced"] == 0 else "")
            self.table.setItem(i, 1, QTableWidgetItem(cons))
            self.table.setItem(i, 2, QTableWidgetItem(q["cliente_nombre"] or ""))
            self.table.setItem(i, 3, QTableWidgetItem(q["contractor_nombre"] or ""))
            self.table.setItem(i, 4, _NumItem(_rt_money(q["total"]), q["total"]))
            pdf_btn = QPushButton("PDF"); pdf_btn.setObjectName("secondaryAction"); pdf_btn.setStyleSheet(self._CELL_BTN)
            pdf_btn.clicked.connect(lambda _=False, lid=q["local_id"]: self._pdf(lid))
            pdf_wrap = QWidget(); pw = QHBoxLayout(pdf_wrap); pw.setContentsMargins(4, 0, 4, 0); pw.addWidget(pdf_btn)
            self.table.setCellWidget(i, 5, pdf_wrap)
            actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(4, 0, 4, 0); al.setSpacing(4)
            if self.can("cobros", "delete"):
                dl = QPushButton("Eliminar"); dl.setObjectName("inlineDanger"); dl.setStyleSheet(self._CELL_BTN)
                dl.clicked.connect(lambda _=False, lid=q["local_id"]: self._eliminar(lid))
                al.addWidget(dl)
            al.addStretch(1)
            self.table.setCellWidget(i, 6, actions)
        self.table.setSortingEnabled(True)

    def _nueva(self):
        if not self.can("cobros", "create"):
            return
        dlg = _CobroDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            local_id = self.store.cc_crear_cuenta(dlg.result_data["cliente"], dlg.result_data["items"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Cuenta de cobro", str(exc)); return
        self._after_change()
        if QMessageBox.question(self, "Cuenta de cobro", "Cuenta creada. ¿Generar el PDF ahora?") == QMessageBox.StandardButton.Yes:
            self._pdf(local_id)

    def _pdf(self, local_id):
        cuenta = self.store.cc_get_cuenta(local_id)
        if not cuenta:
            return
        items = self.store.cc_get_detalle(local_id)
        path, _ = QFileDialog.getSaveFileName(self, "Guardar cuenta de cobro PDF",
                                              f"CuentaCobro_{cuenta.get('consecutivo') or local_id}.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            branding = branding_mod.load_branding(app_data_dir())
            _cobro_generate_pdf(path, cuenta, items, branding)
            QMessageBox.information(self, "Cuenta de cobro", f"PDF generado: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Cuenta de cobro", f"No se pudo generar el PDF: {exc}")

    def _eliminar(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar esta cuenta de cobro?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.cc_eliminar_cuenta(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Cuenta de cobro", str(exc)); return
        self._after_change()

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# =============================================================================
# CRM — contactos, tareas, actividades y pipeline (offline-first)
# =============================================================================
_CRM_TIPOS = [("cliente", "Cliente"), ("lead", "Lead"), ("proveedor", "Proveedor"), ("socio", "Socio")]
_CRM_TIPO_LABEL = dict(_CRM_TIPOS)
_CRM_PRIORIDADES = [("alta", "Alta"), ("media", "Media"), ("baja", "Baja")]
_CRM_PRIORIDAD_LABEL = dict(_CRM_PRIORIDADES)
_CRM_ACT_TIPOS = [("llamada", "Llamada"), ("email", "Email"), ("reunion", "Reunión"),
                  ("whatsapp", "WhatsApp"), ("visita", "Visita"), ("nota", "Nota")]
_CRM_ETAPAS = [("prospecto", "Prospecto"), ("calificado", "Calificado"), ("propuesta", "Propuesta"),
               ("negociacion", "Negociación"), ("ganada", "Ganada"), ("perdida", "Perdida")]
_CRM_ETAPA_LABEL = dict(_CRM_ETAPAS)


def _crm_tipo_color(colores, tipo):
    return {
        "cliente": colores["acento_secundario"], "lead": colores["acento"],
        "proveedor": colores["primario"], "socio": colores["primario_oscuro"],
    }.get(tipo, colores["primario"])


def _crm_etapa_color(colores, etapa):
    return {
        "prospecto": colores["primario"], "calificado": colores["acento"],
        "propuesta": colores["primario_oscuro"], "negociacion": colores["acento"],
        "ganada": colores["acento_secundario"], "perdida": colores["peligro"],
    }.get(etapa, colores["primario"])


def _crm_prioridad_color(colores, prioridad):
    return {"alta": colores["peligro"], "media": colores["acento"], "baja": colores["acento_secundario"]}.get(prioridad, colores["acento"])


def _crm_initials(nombre: str) -> str:
    parts = [p for p in (nombre or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _crm_avatar(nombre: str, color: str) -> QWidget:
    """Círculo con iniciales (estilo moderno) coloreado por la marca."""
    ac = QColor(color)
    wrap = QWidget()
    h = QHBoxLayout(wrap); h.setContentsMargins(0, 0, 0, 0)
    lbl = QLabel(_crm_initials(nombre))
    lbl.setFixedSize(32, 32)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"background: rgba({ac.red()},{ac.green()},{ac.blue()},0.16); color: {color};"
        f" border-radius: 16px; font-weight: 800; font-size: 12px;"
    )
    h.addStretch(1); h.addWidget(lbl); h.addStretch(1)
    return wrap


def _crm_pill(text: str, color: str) -> QWidget:
    """Pastilla de color centrada para celda de tabla (tipo de contacto)."""
    ac = QColor(color)
    wrap = QWidget()
    h = QHBoxLayout(wrap); h.setContentsMargins(4, 0, 4, 0)
    pill = QLabel(text)
    pill.setStyleSheet(
        f"background: rgba({ac.red()},{ac.green()},{ac.blue()},0.14); color: {color};"
        f" border-radius: 10px; padding: 3px 12px; font-size: 11px; font-weight: 800;"
    )
    h.addStretch(1); h.addWidget(pill); h.addStretch(1)
    return wrap


def _crm_empty_state(icon: str, titulo: str, sub: str, color: str) -> QWidget:
    """Estado vacío amigable con ícono, para tabs sin datos."""
    w = QFrame(); w.setObjectName("sectionPanel")
    v = QVBoxLayout(w); v.setContentsMargins(20, 30, 20, 30); v.setSpacing(6)
    v.addStretch(1)
    ic = QLabel(icon); ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ic.setStyleSheet(f"color: {color}; font-size: 40px; font-weight: 800;")
    v.addWidget(ic)
    t = QLabel(titulo); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
    t.setStyleSheet("font-size: 15px; font-weight: 800; color: #374151;")
    v.addWidget(t)
    s = QLabel(sub); s.setObjectName("muted"); s.setAlignment(Qt.AlignmentFlag.AlignCenter); s.setWordWrap(True)
    v.addWidget(s)
    v.addStretch(1)
    return w


class _CrmContactDialog(QDialog):
    """Alta/edición de un contacto CRM."""

    def __init__(self, parent, data=None, can_use_ai=False):
        super().__init__(parent)
        self.setWindowTitle("Editar contacto" if data else "Nuevo contacto")
        self.setMinimumWidth(460)
        self.result_data = None
        self._ai_worker = None
        data = data or {}
        form = QFormLayout(self)
        form.setSpacing(9)
        self.tipo = QComboBox()
        for v, lbl in _CRM_TIPOS:
            self.tipo.addItem(lbl, v)
        idx = max(0, self.tipo.findData(data.get("tipo") or "cliente"))
        self.tipo.setCurrentIndex(idx)
        self.nombre = QLineEdit(data.get("nombre") or "")
        self.empresa = QLineEdit(data.get("empresa") or "")
        self.cargo = QLineEdit(data.get("cargo") or "")
        self.email = QLineEdit(data.get("email") or "")
        self.telefono = QLineEdit(data.get("telefono") or "")
        self.whatsapp = QLineEdit(data.get("whatsapp") or "")
        self.ciudad = QLineEdit(data.get("ciudad") or "")
        self.direccion = QLineEdit(data.get("direccion") or "")
        self.notas = QTextEdit(data.get("notas") or ""); self.notas.setMaximumHeight(70)
        form.addRow("Tipo *", self.tipo)
        form.addRow("Nombre *", self.nombre)
        form.addRow("Empresa", self.empresa)
        form.addRow("Cargo", self.cargo)
        form.addRow("Email", self.email)
        form.addRow("Teléfono", self.telefono)
        form.addRow("WhatsApp", self.whatsapp)
        form.addRow("Ciudad", self.ciudad)
        form.addRow("Dirección", self.direccion)
        form.addRow("Notas", self.notas)
        # Acción IA contextual: solo si el módulo IA está licenciado y el rol puede
        # usarlo (can_use_ai). Genera una sugerencia de siguiente acción comercial
        # y la coloca en Notas. Requiere conexión (se valida al pulsar).
        if can_use_ai:
            self.ai_btn = QPushButton("✦  Sugerir siguiente acción")
            self.ai_btn.setObjectName("secondaryAction")
            self.ai_btn.setToolTip("La IA propone el próximo paso comercial con este contacto")
            self.ai_btn.clicked.connect(self._ai_sugerir)
            form.addRow("", self.ai_btn)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _ai_sugerir(self):
        if self._ai_worker and self._ai_worker.isRunning():
            return
        client = _ai_build_client()
        if client is None:
            QMessageBox.information(self, "Asistente IA",
                                    "Necesitas conexión configurada para usar la IA."); return
        contexto = (
            f"Contacto: {self.nombre.text().strip() or '—'}. "
            f"Empresa: {self.empresa.text().strip() or '—'}. "
            f"Cargo: {self.cargo.text().strip() or '—'}. "
            f"Tipo: {self.tipo.currentText()}. "
            f"Notas actuales: {self.notas.toPlainText().strip() or 'ninguna'}."
        )
        payload = {"titulo": f"Siguiente acción comercial con {self.nombre.text().strip() or 'el contacto'}",
                   "tipo": "sugerencia breve de seguimiento comercial (CRM)",
                   "detalle": contexto}
        self.ai_btn.setEnabled(False); self.ai_btn.setText("✦  Pensando…")
        self._ai_worker = _BgWorker(lambda: client.ai_accion("contenido", payload), self)
        self._ai_worker.done.connect(self._ai_sugerir_done)
        self._ai_worker.failed.connect(lambda _m: self._ai_sugerir_fail())
        self._ai_worker.start()

    def _ai_sugerir_done(self, resp):
        self.ai_btn.setEnabled(True); self.ai_btn.setText("✦  Sugerir siguiente acción")
        if not isinstance(resp, dict) or not resp.get("success"):
            QMessageBox.information(self, "Asistente IA",
                                    (resp or {}).get("error") or "No pude generar la sugerencia."); return
        texto = (resp.get("texto") or "").strip()
        if not texto:
            return
        actual = self.notas.toPlainText().strip()
        self.notas.setPlainText((actual + "\n\n" if actual else "") + f"✦ IA: {texto}")

    def _ai_sugerir_fail(self):
        self.ai_btn.setEnabled(True); self.ai_btn.setText("✦  Sugerir siguiente acción")
        QMessageBox.information(self, "Asistente IA", "Sin conexión con el asistente. Intenta de nuevo.")

    def _accept(self):
        if not self.nombre.text().strip():
            QMessageBox.warning(self, "CRM", "El nombre es obligatorio."); return
        self.result_data = {
            "tipo": self.tipo.currentData(), "nombre": self.nombre.text().strip(),
            "empresa": self.empresa.text().strip(), "cargo": self.cargo.text().strip(),
            "email": self.email.text().strip(), "telefono": self.telefono.text().strip(),
            "whatsapp": self.whatsapp.text().strip(), "ciudad": self.ciudad.text().strip(),
            "direccion": self.direccion.text().strip(), "notas": self.notas.toPlainText().strip(),
        }
        self.accept()


class _CrmTareaDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nueva tarea"); self.setMinimumWidth(420); self.result_data = None
        form = QFormLayout(self)
        self.titulo = QLineEdit()
        self.prioridad = QComboBox()
        for v, lbl in _CRM_PRIORIDADES:
            self.prioridad.addItem(lbl, v)
        self.prioridad.setCurrentIndex(1)
        self.con_fecha = QCheckBox("Con fecha límite")
        self.fecha = QDateEdit(QDate.currentDate()); self.fecha.setCalendarPopup(True); self.fecha.setEnabled(False)
        self.con_fecha.toggled.connect(self.fecha.setEnabled)
        self.desc = QTextEdit(); self.desc.setMaximumHeight(60)
        form.addRow("Título *", self.titulo)
        form.addRow("Prioridad", self.prioridad)
        form.addRow(self.con_fecha, self.fecha)
        form.addRow("Descripción", self.desc)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _accept(self):
        if not self.titulo.text().strip():
            QMessageBox.warning(self, "CRM", "El título es obligatorio."); return
        self.result_data = {
            "titulo": self.titulo.text().strip(), "prioridad": self.prioridad.currentData(),
            "fecha_limite": self.fecha.date().toString("yyyy-MM-dd") if self.con_fecha.isChecked() else None,
            "descripcion": self.desc.toPlainText().strip(),
        }
        self.accept()


class _CrmActividadDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Registrar actividad"); self.setMinimumWidth(420); self.result_data = None
        form = QFormLayout(self)
        self.tipo = QComboBox()
        for v, lbl in _CRM_ACT_TIPOS:
            self.tipo.addItem(lbl, v)
        self.asunto = QLineEdit()
        self.desc = QTextEdit(); self.desc.setMaximumHeight(70)
        form.addRow("Tipo", self.tipo)
        form.addRow("Asunto", self.asunto)
        form.addRow("Detalle", self.desc)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def data(self):
        return {"tipo": self.tipo.currentData(), "asunto": self.asunto.text().strip(),
                "descripcion": self.desc.toPlainText().strip()}


class _CrmOportunidadDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nueva oportunidad"); self.setMinimumWidth(420); self.result_data = None
        form = QFormLayout(self)
        self.titulo = QLineEdit()
        self.monto = QDoubleSpinBox(); self.monto.setRange(0, 999_999_999); self.monto.setPrefix("$ "); self.monto.setGroupSeparatorShown(True)
        self.prob = QSpinBox(); self.prob.setRange(0, 100); self.prob.setValue(50); self.prob.setSuffix(" %")
        self.etapa = QComboBox()
        for v, lbl in _CRM_ETAPAS:
            self.etapa.addItem(lbl, v)
        form.addRow("Título *", self.titulo)
        form.addRow("Monto estimado", self.monto)
        form.addRow("Probabilidad", self.prob)
        form.addRow("Etapa", self.etapa)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _accept(self):
        if not self.titulo.text().strip():
            QMessageBox.warning(self, "CRM", "El título es obligatorio."); return
        self.result_data = {"titulo": self.titulo.text().strip(), "monto_estimado": self.monto.value(),
                            "probabilidad": self.prob.value(), "etapa": self.etapa.currentData()}
        self.accept()


class _CrmContactDetailDialog(QDialog):
    """Ficha 360° del contacto: datos + actividades + tareas + oportunidades."""

    def __init__(self, parent, store, contacto, can, user_cb):
        super().__init__(parent)
        self.store = store; self.c = contacto; self.can = can; self.user_cb = user_cb
        self.setWindowTitle(f"Contacto · {contacto.get('nombre')}")
        self.setMinimumSize(620, 640)
        self._build()

    def _user(self):
        return self.user_cb() if self.user_cb else None

    def _rid(self):
        return self.c.get("remote_id")

    def _build(self):
        colores = _cb_brand_colors()
        root = QVBoxLayout(self); root.setSpacing(10)
        # Cabecera
        head = QFrame(); head.setObjectName("sectionPanel")
        hl = QVBoxLayout(head); hl.setContentsMargins(16, 12, 16, 12); hl.setSpacing(2)
        top = QHBoxLayout()
        name = QLabel(self.c.get("nombre") or ""); name.setObjectName("pageTitle")
        top.addWidget(name)
        chip = _cb_chip(_CRM_TIPO_LABEL.get(self.c.get("tipo"), self.c.get("tipo") or ""), _crm_tipo_color(colores, self.c.get("tipo")))
        top.addWidget(chip); top.addStretch(1)
        hl.addLayout(top)
        sub = " · ".join(x for x in [self.c.get("empresa"), self.c.get("cargo"), self.c.get("ciudad")] if x)
        if sub:
            s = QLabel(sub); s.setObjectName("muted"); hl.addWidget(s)
        contacto_line = " · ".join(x for x in [self.c.get("telefono"), self.c.get("email"), self.c.get("whatsapp")] if x)
        if contacto_line:
            cl = QLabel(contacto_line); cl.setObjectName("muted"); hl.addWidget(cl)
        if self._rid() is None:
            warn = QLabel("⏳ Contacto local sin sincronizar — sincroniza para poder agregar tareas, actividades y oportunidades.")
            warn.setStyleSheet(f"color: {colores['acento']}; font-weight: 700;"); warn.setWordWrap(True)
            hl.addWidget(warn)
        root.addWidget(head)

        self.tabs = QTabWidget()
        self.tab_act = QWidget(); self.tab_tar = QWidget(); self.tab_opp = QWidget()
        for t in (self.tab_act, self.tab_tar, self.tab_opp):
            QVBoxLayout(t).setContentsMargins(2, 8, 2, 2)
        self.tabs.addTab(self.tab_act, "Actividades")
        self.tabs.addTab(self.tab_tar, "Tareas")
        self.tabs.addTab(self.tab_opp, "Oportunidades")
        root.addWidget(self.tabs, 1)
        self._render_actividades(); self._render_tareas(); self._render_oportunidades()

    def _toolbar(self, layout, btn_text, slot, enabled=True):
        bar = QHBoxLayout(); bar.addStretch(1)
        b = QPushButton(btn_text); b.setObjectName("primaryAction"); b.setEnabled(enabled); b.clicked.connect(slot)
        bar.addWidget(b); layout.addLayout(bar)

    def _clear(self, w):
        lay = w.layout()
        while lay.count() > 0:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                clear_layout(it.layout())

    # Actividades
    def _render_actividades(self):
        self._clear(self.tab_act); lay = self.tab_act.layout()
        can_add = self.can("crm", "create") and self._rid() is not None
        self._toolbar(lay, "＋  Registrar actividad", self._add_actividad, can_add)
        rows = self.store.crm_list_actividades(self._rid()) if self._rid() is not None else []
        if not rows:
            m = QLabel("Sin actividades registradas."); m.setObjectName("muted"); lay.addWidget(m)
        tbl = QTableWidget(len(rows), 3)
        tbl.setHorizontalHeaderLabels(["Fecha", "Tipo", "Asunto / detalle"])
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tbl.verticalHeader().setVisible(False); tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, a in enumerate(rows):
            tbl.setItem(i, 0, QTableWidgetItem((a.get("fecha_actividad") or "")[:16]))
            tbl.setItem(i, 1, QTableWidgetItem(dict(_CRM_ACT_TIPOS).get(a.get("tipo"), a.get("tipo") or "")))
            det = a.get("asunto") or ""
            if a.get("descripcion"):
                det += " — " + a["descripcion"]
            tbl.setItem(i, 2, QTableWidgetItem(det))
        lay.addWidget(tbl, 1)

    def _add_actividad(self):
        dlg = _CrmActividadDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        d = dlg.data()
        try:
            self.store.crm_crear_actividad(self._rid(), d["tipo"], d["asunto"], d["descripcion"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._render_actividades()

    # Tareas
    def _render_tareas(self):
        self._clear(self.tab_tar); lay = self.tab_tar.layout(); colores = _cb_brand_colors()
        can_add = self.can("crm", "create") and self._rid() is not None
        self._toolbar(lay, "＋  Nueva tarea", self._add_tarea, can_add)
        rows = self.store.crm_list_tareas(self._rid()) if self._rid() is not None else []
        if not rows:
            m = QLabel("Sin tareas."); m.setObjectName("muted"); lay.addWidget(m)
        for t in rows:
            lay.addWidget(self._tarea_row(t, colores))

    def _tarea_row(self, t, colores):
        row = QFrame(); row.setObjectName("sectionPanel")
        rl = QHBoxLayout(row); rl.setContentsMargins(12, 8, 12, 8); rl.setSpacing(8)
        done = t["estado"] == "completada"
        chip = _cb_chip(_CRM_PRIORIDAD_LABEL.get(t["prioridad"], t["prioridad"]), _crm_prioridad_color(colores, t["prioridad"]))
        rl.addWidget(chip)
        txt = QLabel(("✓ " if done else "") + (t["titulo"] or ""))
        txt.setStyleSheet("color:#9ca3af; text-decoration: line-through;" if done else "font-weight:600;")
        rl.addWidget(txt, 1)
        if t.get("fecha_limite"):
            f = QLabel(t["fecha_limite"]); f.setObjectName("muted"); rl.addWidget(f)
        if not done and self.can("crm", "edit"):
            b = QPushButton("Completar"); b.setObjectName("secondaryAction"); b.setStyleSheet("min-height:24px;padding:0 8px;")
            b.clicked.connect(lambda _=False, lid=t["local_id"]: self._completar_tarea(lid))
            rl.addWidget(b)
        if self.can("crm", "delete"):
            d = QPushButton("✕"); d.setObjectName("inlineDanger"); d.setStyleSheet("min-height:24px;padding:0;min-width:26px;")
            d.clicked.connect(lambda _=False, lid=t["local_id"]: self._eliminar_tarea(lid))
            rl.addWidget(d)
        return row

    def _add_tarea(self):
        dlg = _CrmTareaDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.crm_crear_tarea(self._rid(), d["titulo"], d["descripcion"], d["prioridad"], d["fecha_limite"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._render_tareas()

    def _completar_tarea(self, lid):
        try:
            self.store.crm_completar_tarea(lid, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._render_tareas()

    def _eliminar_tarea(self, lid):
        self.store.crm_eliminar_tarea(lid, user=self._user()); self._render_tareas()

    # Oportunidades
    def _render_oportunidades(self):
        self._clear(self.tab_opp); lay = self.tab_opp.layout(); colores = _cb_brand_colors()
        can_add = self.can("crm", "create") and self._rid() is not None
        self._toolbar(lay, "＋  Nueva oportunidad", self._add_opp, can_add)
        rows = self.store.crm_list_oportunidades(self._rid()) if self._rid() is not None else []
        if not rows:
            m = QLabel("Sin oportunidades."); m.setObjectName("muted"); lay.addWidget(m)
        for o in rows:
            row = QFrame(); row.setObjectName("sectionPanel")
            rl = QHBoxLayout(row); rl.setContentsMargins(12, 8, 12, 8); rl.setSpacing(8)
            rl.addWidget(_cb_chip(_CRM_ETAPA_LABEL.get(o["etapa"], o["etapa"]), _crm_etapa_color(colores, o["etapa"])))
            t = QLabel(o["titulo"] or ""); t.setStyleSheet("font-weight:600;"); rl.addWidget(t, 1)
            val = QLabel(f"{_rt_money(o['monto_estimado'])} · {o['probabilidad']}%")
            val.setStyleSheet(f"color:{colores['primario_oscuro']};font-weight:800;"); rl.addWidget(val)
            if self.can("crm", "delete"):
                d = QPushButton("✕"); d.setObjectName("inlineDanger"); d.setStyleSheet("min-height:24px;padding:0;min-width:26px;")
                d.clicked.connect(lambda _=False, lid=o["local_id"]: self._eliminar_opp(lid))
                rl.addWidget(d)
            lay.addWidget(row)

    def _add_opp(self):
        dlg = _CrmOportunidadDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.crm_crear_oportunidad(self._rid(), d["titulo"], d["monto_estimado"], d["probabilidad"], d["etapa"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._render_oportunidades()

    def _eliminar_opp(self, lid):
        self.store.crm_eliminar_oportunidad(lid, user=self._user()); self._render_oportunidades()


class CrmPage(QWidget):
    """CRM offline-first: contactos, tareas y pipeline de oportunidades.
    Sincroniza con producción vía outbox crm_op."""

    _CELL_BTN = "min-height:26px;padding:0 10px;font-size:12px;"

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._all_contactos = []
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(24, 22, 24, 16); root.setSpacing(12)
        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("CRM"); title.setObjectName("pageTitle")
        sub = QLabel("Contactos, tareas y pipeline comercial. Funciona sin internet; sincroniza con producción.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.sync_label = QLabel(""); self.sync_label.setObjectName("rtSyncLabel")
        header.addWidget(self.sync_label)
        self.nuevo_btn = QPushButton("＋  Nuevo contacto"); self.nuevo_btn.setObjectName("primaryAction")
        self.nuevo_btn.clicked.connect(self._nuevo_contacto)
        header.addWidget(self.nuevo_btn)
        refresh = QPushButton("↻  Refrescar"); refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        self.kpis = QGridLayout(); self.kpis.setSpacing(10)
        root.addLayout(self.kpis)

        self.tabs = QTabWidget()
        self.tab_contactos = QWidget(); self.tab_tareas = QWidget(); self.tab_pipeline = QWidget()
        for t in (self.tab_contactos, self.tab_tareas, self.tab_pipeline):
            QVBoxLayout(t).setContentsMargins(2, 10, 2, 2)
        self.tabs.addTab(self.tab_contactos, "Contactos")
        self.tabs.addTab(self.tab_tareas, "Tareas")
        self.tabs.addTab(self.tab_pipeline, "Pipeline")
        root.addWidget(self.tabs, 1)
        self.refresh()

    def refresh(self):
        pend = self.store.crm_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync"); self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado"); self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label); self.sync_label.style().polish(self.sync_label)
        self.nuevo_btn.setVisible(self.can("crm", "create"))
        self._render_kpis()
        self._all_contactos = self.store.crm_list_contactos()
        self._render_contactos()
        self._render_tareas()
        self._render_pipeline()

    def _render_kpis(self):
        clear_layout(self.kpis)
        colores = _cb_brand_colors()
        k = self.store.crm_kpis()
        self.kpis.addWidget(_CbKpiCard("☺", "Contactos", str(k["contactos"]), "en la base", colores["primario"]), 0, 0)
        self.kpis.addWidget(_CbKpiCard("✓", "Tareas pendientes", str(k["tareas_pendientes"]),
                                       "por hacer", colores["acento"]), 0, 1)
        self.kpis.addWidget(_CbKpiCard("⚠", "Tareas vencidas", str(k["tareas_vencidas"]),
                                       "requieren atención" if k["tareas_vencidas"] else "al día",
                                       colores["peligro"] if k["tareas_vencidas"] else colores["acento_secundario"]), 0, 2)
        self.kpis.addWidget(_CbKpiCard("$", "Pipeline abierto", _cb_money_compact(k["pipeline"]),
                                       f"{k['oportunidades_abiertas']} oportunidad(es)", colores["acento_secundario"]), 0, 3)

    # ── Contactos ──
    def _render_contactos(self):
        lay = self.tab_contactos.layout(); clear_layout(lay)
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.search = QLineEdit(); self.search.setPlaceholderText("⌕  Buscar por nombre, empresa, email o teléfono")
        self.search.textChanged.connect(self._fill_contactos)
        bar.addWidget(self.search, 1)
        self.f_tipo = QComboBox(); self.f_tipo.addItem("Todos los tipos", None)
        for v, l in _CRM_TIPOS:
            self.f_tipo.addItem(l, v)
        self.f_tipo.currentIndexChanged.connect(self._fill_contactos)
        bar.addWidget(self.f_tipo)
        lay.addLayout(bar)
        self.tabla = QTableWidget(0, 7)
        self.tabla.setHorizontalHeaderLabels(["", "Nombre", "Tipo", "Empresa", "Teléfono", "Email", ""])
        self.tabla.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tabla.setColumnWidth(0, 50)
        self.tabla.setColumnWidth(2, 110)
        self.tabla.setColumnWidth(6, 210)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.verticalHeader().setVisible(False); self.tabla.verticalHeader().setDefaultSectionSize(52)
        self.tabla.setSortingEnabled(True)
        self.tabla.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabla.doubleClicked.connect(lambda _i: self._abrir_contacto())
        lay.addWidget(self.tabla, 1)
        hint = QLabel("Doble clic en un contacto para abrir su ficha 360° (actividades, tareas y oportunidades).")
        hint.setObjectName("muted")
        lay.addWidget(hint)
        self._fill_contactos()

    def _fill_contactos(self):
        colores = _cb_brand_colors()
        needle = self.search.text().strip().lower() if hasattr(self, "search") else ""
        tipo = self.f_tipo.currentData() if hasattr(self, "f_tipo") else None
        rows = [c for c in self._all_contactos
                if (not tipo or c["tipo"] == tipo) and
                (not needle or needle in ((c.get("nombre") or "") + " " + (c.get("empresa") or "") + " " +
                                          (c.get("email") or "") + " " + (c.get("telefono") or "")).lower())]
        self.tabla.setSortingEnabled(False)
        self.tabla.setRowCount(len(rows))
        for i, c in enumerate(rows):
            tipo_color = _crm_tipo_color(colores, c["tipo"])
            self.tabla.setCellWidget(i, 0, _crm_avatar(c.get("nombre"), tipo_color))
            nombre = (c["nombre"] or "") + ("  ⏳" if c["synced"] == 0 else "")
            it = QTableWidgetItem(nombre); it.setData(Qt.ItemDataRole.UserRole, c["local_id"])
            f = it.font(); f.setBold(True); it.setFont(f)
            self.tabla.setItem(i, 1, it)
            self.tabla.setCellWidget(i, 2, _crm_pill(_CRM_TIPO_LABEL.get(c["tipo"], c["tipo"] or ""), tipo_color))
            self.tabla.setItem(i, 2, QTableWidgetItem(c["tipo"] or ""))  # valor oculto para orden
            self.tabla.setItem(i, 3, QTableWidgetItem(c.get("empresa") or ""))
            self.tabla.setItem(i, 4, QTableWidgetItem(c.get("telefono") or ""))
            self.tabla.setItem(i, 5, QTableWidgetItem(c.get("email") or ""))
            actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(4, 0, 4, 0); al.setSpacing(4)
            ver = QPushButton("Abrir"); ver.setObjectName("secondaryAction"); ver.setStyleSheet(self._CELL_BTN)
            ver.clicked.connect(lambda _=False, lid=c["local_id"]: self._abrir_contacto(lid))
            al.addWidget(ver)
            if self.can("crm", "edit"):
                ed = QPushButton("Editar"); ed.setObjectName("secondaryAction"); ed.setStyleSheet(self._CELL_BTN)
                ed.clicked.connect(lambda _=False, lid=c["local_id"]: self._editar_contacto(lid))
                al.addWidget(ed)
            if self.can("crm", "delete"):
                dl = QPushButton("✕"); dl.setObjectName("inlineDanger"); dl.setStyleSheet("min-height:26px;padding:0;min-width:28px;")
                dl.clicked.connect(lambda _=False, lid=c["local_id"]: self._eliminar_contacto(lid))
                al.addWidget(dl)
            al.addStretch(1)
            self.tabla.setCellWidget(i, 6, actions)
        self.tabla.setSortingEnabled(True)
        if not rows:
            self.tabla.setRowCount(1)
            self.tabla.setSpan(0, 0, 1, 7)
            empty = QLabel("Sin contactos. Crea el primero con “＋ Nuevo contacto”." if not needle
                           else "Ningún contacto coincide con la búsqueda.")
            empty.setObjectName("muted"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tabla.setCellWidget(0, 0, empty)

    def _contacto_por_local(self, local_id):
        return next((c for c in self._all_contactos if c["local_id"] == local_id), None)

    def _selected_local(self):
        r = self.tabla.currentRow()
        if r < 0:
            return None
        it = self.tabla.item(r, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _abrir_contacto(self, local_id=None):
        local_id = local_id if local_id is not None else self._selected_local()
        c = self._contacto_por_local(local_id) if local_id is not None else None
        if not c:
            return
        _CrmContactDetailDialog(self, self.store, c, self.can, self.user_callback).exec()
        self._after_change()

    def _nuevo_contacto(self):
        if not self.can("crm", "create"):
            return
        dlg = _CrmContactDialog(self, can_use_ai=self.can("ia", "use"))
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            self.store.crm_crear_contacto(dlg.result_data, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._after_change()

    def _editar_contacto(self, local_id):
        c = self._contacto_por_local(local_id)
        if not c:
            return
        dlg = _CrmContactDialog(self, c, can_use_ai=self.can("ia", "use"))
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            self.store.crm_editar_contacto(local_id, dlg.result_data, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._after_change()

    def _eliminar_contacto(self, local_id):
        if QMessageBox.question(self, "Eliminar", "¿Eliminar este contacto?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.crm_eliminar_contacto(local_id, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._after_change()

    # ── Tareas (global) ──
    def _render_tareas(self):
        lay = self.tab_tareas.layout(); clear_layout(lay)
        colores = _cb_brand_colors()
        nombre_por_rid = {c["remote_id"]: c["nombre"] for c in self._all_contactos if c["remote_id"] is not None}
        rows = self.store.crm_list_tareas(solo_pendientes=False)
        pend = [t for t in rows if t["estado"] == "pendiente"]
        if not pend:
            m = QLabel("No hay tareas pendientes. 🎉"); m.setObjectName("muted"); lay.addWidget(m)
        hoy = date.today().isoformat()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); il = QVBoxLayout(inner); il.setContentsMargins(0, 0, 0, 0); il.setSpacing(8)
        for t in pend:
            row = QFrame(); row.setObjectName("sectionPanel")
            rl = QHBoxLayout(row); rl.setContentsMargins(12, 8, 12, 8); rl.setSpacing(8)
            rl.addWidget(_cb_chip(_CRM_PRIORIDAD_LABEL.get(t["prioridad"], t["prioridad"]), _crm_prioridad_color(colores, t["prioridad"])))
            txt = QLabel(t["titulo"] or ""); txt.setStyleSheet("font-weight:600;")
            rl.addWidget(txt, 1)
            cont = nombre_por_rid.get(t.get("contacto_remote_id"))
            if cont:
                cl = QLabel(cont); cl.setObjectName("muted"); rl.addWidget(cl)
            if t.get("fecha_limite"):
                vencida = t["fecha_limite"] < hoy
                f = QLabel(("⚠ " if vencida else "") + t["fecha_limite"])
                f.setStyleSheet(f"color:{colores['peligro']};font-weight:700;" if vencida else "color:#6b7280;")
                rl.addWidget(f)
            if self.can("crm", "edit"):
                b = QPushButton("Completar"); b.setObjectName("secondaryAction"); b.setStyleSheet(self._CELL_BTN)
                b.clicked.connect(lambda _=False, lid=t["local_id"]: self._completar_tarea(lid))
                rl.addWidget(b)
            il.addWidget(row)
        il.addStretch(1)
        scroll.setWidget(inner); lay.addWidget(scroll, 1)

    def _completar_tarea(self, lid):
        try:
            self.store.crm_completar_tarea(lid, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "CRM", str(exc)); return
        self._after_change()

    # ── Pipeline ──
    def _render_pipeline(self):
        lay = self.tab_pipeline.layout(); clear_layout(lay)
        colores = _cb_brand_colors()
        opps = self.store.crm_list_oportunidades()
        nombre_por_rid = {c["remote_id"]: c["nombre"] for c in self._all_contactos if c["remote_id"] is not None}
        por_etapa = {e: [] for e, _ in _CRM_ETAPAS}
        for o in opps:
            por_etapa.setdefault(o["etapa"], []).append(o)
        cols = QHBoxLayout(); cols.setSpacing(10)
        for etapa, label in _CRM_ETAPAS:
            items = por_etapa.get(etapa, [])
            total = sum(float(o["monto_estimado"] or 0) for o in items)
            color = _crm_etapa_color(colores, etapa)
            colf = QFrame(); colf.setObjectName("sectionPanel")
            cv = QVBoxLayout(colf); cv.setContentsMargins(10, 10, 10, 10); cv.setSpacing(6)
            head = QLabel(f"{label.upper()}  ·  {len(items)}")
            head.setStyleSheet(f"color:{color};font-size:11px;font-weight:800;letter-spacing:0.8px;border-bottom:2px solid {color};padding-bottom:4px;")
            cv.addWidget(head)
            tot = QLabel(_cb_money_compact(total)); tot.setStyleSheet(f"color:{colores['primario_oscuro']};font-weight:800;")
            cv.addWidget(tot)
            for o in items[:30]:
                card = QFrame()
                ac = QColor(color)
                card.setStyleSheet(f"QFrame{{background:#fff;border:1px solid #e5e7eb;border-left:3px solid {color};border-radius:8px;}} QLabel{{border:0;}}")
                cl = QVBoxLayout(card); cl.setContentsMargins(8, 6, 8, 6); cl.setSpacing(1)
                t = QLabel(o["titulo"] or ""); t.setStyleSheet("font-weight:700;font-size:11px;"); t.setWordWrap(True)
                cl.addWidget(t)
                cont = nombre_por_rid.get(o.get("contacto_remote_id"))
                if cont:
                    cc = QLabel(cont); cc.setStyleSheet("color:#9ca3af;font-size:10px;"); cl.addWidget(cc)
                val = QLabel(f"{_cb_money_compact(o['monto_estimado'])} · {o['probabilidad']}%")
                val.setStyleSheet(f"color:{color};font-size:10px;font-weight:800;")
                cl.addWidget(val)
                cv.addWidget(card)
            cv.addStretch(1)
            cols.addWidget(colf)
        wrap = QWidget(); wrap.setLayout(cols)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(wrap)
        lay.addWidget(scroll, 1)

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# =============================================================================
# Nómina — empleados, períodos, liquidación offline (motor nomina_calc)
# =============================================================================
_N_VINCULACIONES = [("EMPLEADO", "Empleado"), ("CONTRATISTA", "Contratista"),
                    ("APRENDIZ_SENA", "Aprendiz SENA")]
_N_VINC_LABEL = dict(_N_VINCULACIONES)
_N_NOVEDAD_TIPOS = [("HED", "Hora extra diurna"), ("HEN", "Hora extra nocturna"),
                    ("HEDF", "H.E. diurna festiva"), ("HENF", "H.E. nocturna festiva"),
                    ("RN", "Recargo nocturno"), ("RD", "Recargo dominical/festivo"),
                    ("INCAPACIDAD_GEN", "Incapacidad general"), ("INCAPACIDAD_LAB", "Incapacidad laboral"),
                    ("LICENCIA_MAT", "Licencia maternidad"), ("LICENCIA_PAT", "Licencia paternidad"),
                    ("LICENCIA_LUTO", "Licencia luto"), ("LICENCIA_NR", "Licencia no remunerada")]
_N_NOVEDAD_LABEL = dict(_N_NOVEDAD_TIPOS)


class _EmpleadoDialog(QDialog):
    """Alta/edición de empleado de nómina."""

    def __init__(self, parent, data=None):
        super().__init__(parent)
        self.setWindowTitle("Editar empleado" if data else "Nuevo empleado")
        self.setMinimumWidth(520)
        self.result_data = None
        data = data or {}
        root = QVBoxLayout(self)
        form = QGridLayout(); form.setHorizontalSpacing(12); form.setVerticalSpacing(6)
        self.tipo_doc = QComboBox()
        for v in ("CC", "CE", "TI", "PAS", "NIT", "PEP"):
            self.tipo_doc.addItem(v, v)
        self.tipo_doc.setCurrentText(data.get("tipo_documento") or "CC")
        self.num_doc = QLineEdit(data.get("numero_documento") or "")
        self.nombres = QLineEdit(data.get("nombres") or "")
        self.apellidos = QLineEdit(data.get("apellidos") or "")
        self.cargo = QLineEdit(data.get("cargo") or "")
        self.vinc = QComboBox()
        for v, lbl in _N_VINCULACIONES:
            self.vinc.addItem(lbl, v)
        i = self.vinc.findData((data.get("tipo_vinculacion") or "EMPLEADO"))
        self.vinc.setCurrentIndex(i if i >= 0 else 0)
        self.salario = QDoubleSpinBox(); self.salario.setRange(0, 999_999_999); self.salario.setPrefix("$ ")
        self.salario.setGroupSeparatorShown(True); self.salario.setValue(float(data.get("salario_base") or 0))
        self.nivel_arl = QComboBox()
        for v in ("I", "II", "III", "IV", "V"):
            self.nivel_arl.addItem(v, v)
        self.nivel_arl.setCurrentText(data.get("nivel_arl") or "I")
        self.fecha_ing = QDateEdit(); self.fecha_ing.setCalendarPopup(True)
        self.fecha_ing.setDate(QDate.fromString(data.get("fecha_ingreso") or QDate.currentDate().toString("yyyy-MM-dd"), "yyyy-MM-dd"))
        self.email = QLineEdit(data.get("email") or "")
        self.telefono = QLineEdit(data.get("telefono") or "")
        self.eps = QLineEdit(data.get("eps") or "")
        self.fondo_pension = QLineEdit(data.get("fondo_pension") or "")
        self.fondo_cesantias = QLineEdit(data.get("fondo_cesantias") or "")
        self.banco = QLineEdit(data.get("banco") or "")
        self.num_cuenta = QLineEdit(data.get("numero_cuenta") or "")

        def _lbl(t):
            l = QLabel(t.upper()); l.setObjectName("eyebrowMuted"); return l
        campos = [
            ("Tipo doc.", self.tipo_doc), ("Documento *", self.num_doc),
            ("Nombres *", self.nombres), ("Apellidos", self.apellidos),
            ("Cargo", self.cargo), ("Vinculación", self.vinc),
            ("Salario base *", self.salario), ("Nivel ARL", self.nivel_arl),
            ("Fecha ingreso", self.fecha_ing), ("Email", self.email),
            ("Teléfono", self.telefono), ("EPS", self.eps),
            ("Fondo pensión", self.fondo_pension), ("Fondo cesantías", self.fondo_cesantias),
            ("Banco", self.banco), ("N° cuenta", self.num_cuenta),
        ]
        for idx, (lbl, w) in enumerate(campos):
            r, c = divmod(idx, 2)
            form.addWidget(_lbl(lbl), r * 2, c)
            form.addWidget(w, r * 2 + 1, c)
        root.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _accept(self):
        if not self.nombres.text().strip():
            QMessageBox.warning(self, "Nómina", "Los nombres son obligatorios."); return
        if self.salario.value() <= 0:
            QMessageBox.warning(self, "Nómina", "El salario base debe ser mayor a cero."); return
        self.result_data = {
            "tipo_documento": self.tipo_doc.currentData(), "numero_documento": self.num_doc.text().strip(),
            "nombres": self.nombres.text().strip(), "apellidos": self.apellidos.text().strip(),
            "cargo": self.cargo.text().strip(), "tipo_vinculacion": self.vinc.currentData(),
            "salario_base": self.salario.value(), "nivel_arl": self.nivel_arl.currentData(),
            "fecha_ingreso": self.fecha_ing.date().toString("yyyy-MM-dd"),
            "email": self.email.text().strip(), "telefono": self.telefono.text().strip(),
            "eps": self.eps.text().strip(), "fondo_pension": self.fondo_pension.text().strip(),
            "fondo_cesantias": self.fondo_cesantias.text().strip(), "banco": self.banco.text().strip(),
            "tipo_cuenta": "ahorros", "numero_cuenta": self.num_cuenta.text().strip(), "direccion": "",
        }
        self.accept()


class _PeriodoDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Nuevo período"); self.setMinimumWidth(420); self.result_data = None
        form = QFormLayout(self)
        hoy = QDate.currentDate()
        self.anio = QSpinBox(); self.anio.setRange(2024, 2030); self.anio.setValue(hoy.year())
        self.mes = QSpinBox(); self.mes.setRange(1, 12); self.mes.setValue(hoy.month())
        self.numero = QComboBox()
        self.numero.addItem("Quincena 1 (1-15)", 1); self.numero.addItem("Quincena 2 (16-30)", 2)
        self.numero.addItem("Mensual (1-30)", 0)
        self.fi = QDateEdit(hoy); self.fi.setCalendarPopup(True)
        self.ff = QDateEdit(hoy); self.ff.setCalendarPopup(True)
        self.numero.currentIndexChanged.connect(self._auto_fechas)
        self.anio.valueChanged.connect(self._auto_fechas)
        self.mes.valueChanged.connect(self._auto_fechas)
        form.addRow("Año", self.anio)
        form.addRow("Mes", self.mes)
        form.addRow("Período", self.numero)
        form.addRow("Desde", self.fi)
        form.addRow("Hasta", self.ff)
        self._auto_fechas()
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _auto_fechas(self):
        a, m, np = self.anio.value(), self.mes.value(), self.numero.currentData()
        if np == 1:
            self.fi.setDate(QDate(a, m, 1)); self.ff.setDate(QDate(a, m, 15))
        elif np == 2:
            self.fi.setDate(QDate(a, m, 16)); self.ff.setDate(QDate(a, m, 30))
        else:
            self.fi.setDate(QDate(a, m, 1)); self.ff.setDate(QDate(a, m, 30))

    def _accept(self):
        self.result_data = {
            "anio": self.anio.value(), "mes": self.mes.value(),
            "numero_periodo": self.numero.currentData() or 0,
            "fecha_inicio": self.fi.date().toString("yyyy-MM-dd"),
            "fecha_fin": self.ff.date().toString("yyyy-MM-dd"),
        }
        self.accept()


class _NovedadDialog(QDialog):
    """Registra una novedad; calcula valor_total con el motor (nomina_calc)."""

    def __init__(self, parent, empleados, fecha_periodo, params):
        super().__init__(parent)
        self.setWindowTitle("Registrar novedad"); self.setMinimumWidth(440)
        self.result_data = None
        self._empleados = empleados
        self._fecha = fecha_periodo
        self._params = params
        form = QFormLayout(self)
        self.empleado = QComboBox()
        for e in empleados:
            self.empleado.addItem(f"{e['nombres']} {e.get('apellidos') or ''}".strip(), e)
        self.tipo = QComboBox()
        for v, lbl in _N_NOVEDAD_TIPOS:
            self.tipo.addItem(lbl, v)
        self.cantidad = QDoubleSpinBox(); self.cantidad.setRange(0, 10000); self.cantidad.setDecimals(1); self.cantidad.setValue(1)
        self.cant_hint = QLabel("horas (extras/recargos) o días (incapacidades/licencias)")
        self.cant_hint.setObjectName("muted")
        self.fecha = QDateEdit(QDate.fromString(fecha_periodo or QDate.currentDate().toString("yyyy-MM-dd"), "yyyy-MM-dd"))
        self.fecha.setCalendarPopup(True)
        form.addRow("Empleado", self.empleado)
        form.addRow("Tipo", self.tipo)
        form.addRow("Cantidad", self.cantidad)
        form.addRow("", self.cant_hint)
        form.addRow("Fecha", self.fecha)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _accept(self):
        emp = self.empleado.currentData()
        if not emp:
            QMessageBox.warning(self, "Nómina", "No hay empleados."); return
        if emp.get("remote_id") is None:
            QMessageBox.warning(self, "Nómina", "Sincroniza el empleado antes de registrar novedades."); return
        import nomina_calc
        tipo = self.tipo.currentData()
        cant = self.cantidad.value()
        fecha = self.fecha.date().toString("yyyy-MM-dd")
        salario = float(emp.get("salario_base") or 0)
        smmlv = float(self._params.get("salario_minimo") or 0)
        if tipo in nomina_calc.TIPOS_EXTRAS:
            vh = nomina_calc.calcular_valor_hora(salario)
            from datetime import date as _d
            valor = nomina_calc.calcular_horas_extras(vh, tipo, cant, _d.fromisoformat(fecha))
        elif tipo in nomina_calc.TIPOS_LICENCIAS_REMUNERADAS:
            valor = nomina_calc.calcular_incapacidad(salario, int(cant), tipo, smmlv) if hasattr(nomina_calc, "calcular_incapacidad") else 0
        else:
            valor = 0
        self.result_data = {"empleado_remote_id": emp["remote_id"], "tipo_novedad": tipo,
                            "cantidad": cant, "valor_total": valor, "fecha_novedad": fecha}
        self.accept()


class NominaPage(QWidget):
    """Nómina offline-first: empleados, períodos con liquidación local (motor
    nomina_calc, idéntico al servidor), novedades y desprendibles."""

    _CELL_BTN = "min-height:26px;padding:0 10px;font-size:12px;"

    def __init__(self, store: LocalStore, on_changed, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self.user_callback = user_callback
        self.can = can_callback or (lambda m, a: True)
        self._build()

    def _user(self):
        return self.user_callback() if self.user_callback else None

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(24, 22, 24, 16); root.setSpacing(12)
        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("Nómina"); title.setObjectName("pageTitle")
        sub = QLabel("Empleados, períodos y liquidación. Cálculo local con el motor verificado; sincroniza con producción.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.sync_label = QLabel(""); self.sync_label.setObjectName("rtSyncLabel")
        header.addWidget(self.sync_label)
        refresh = QPushButton("↻  Refrescar"); refresh.setObjectName("secondaryAction")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        self.kpis = QGridLayout(); self.kpis.setSpacing(10)
        root.addLayout(self.kpis)

        self.tabs = QTabWidget()
        self.tab_emp = QWidget(); self.tab_per = QWidget(); self.tab_par = QWidget()
        for t in (self.tab_emp, self.tab_per, self.tab_par):
            QVBoxLayout(t).setContentsMargins(2, 10, 2, 2)
        self.tabs.addTab(self.tab_emp, "Empleados")
        self.tabs.addTab(self.tab_per, "Períodos")
        self.tabs.addTab(self.tab_par, "Parámetros")
        root.addWidget(self.tabs, 1)
        self.refresh()

    def refresh(self):
        pend = self.store.n_pending_count()
        if pend:
            self.sync_label.setText(f"⏳ {pend} pendiente(s) de sync"); self.sync_label.setProperty("state", "pending")
        else:
            self.sync_label.setText("✓ Sincronizado"); self.sync_label.setProperty("state", "ok")
        self.sync_label.style().unpolish(self.sync_label); self.sync_label.style().polish(self.sync_label)
        self._render_kpis()
        self._render_empleados()
        self._render_periodos()
        self._render_parametros()

    def _render_kpis(self):
        clear_layout(self.kpis)
        colores = _cb_brand_colors()
        emps = self.store.n_list_empleados()
        periodos = self.store.n_list_periodos()
        ultimo_neto = 0.0
        if periodos:
            det = self.store.n_list_detalle(periodos[0]["local_id"])
            ultimo_neto = sum(float(d["neto_pagar"] or 0) for d in det)
        params = self.store.n_get_parametros(date.today().year)
        self.kpis.addWidget(_CbKpiCard("☺", "Empleados activos", str(len(emps)), "en nómina", colores["primario"]), 0, 0)
        self.kpis.addWidget(_CbKpiCard("₲", "Último período (neto)", _cb_money_compact(ultimo_neto),
                                       "total a pagar", colores["acento_secundario"]), 0, 1)
        self.kpis.addWidget(_CbKpiCard("▦", "Períodos", str(len(periodos)), "liquidaciones", colores["acento"]), 0, 2)
        self.kpis.addWidget(_CbKpiCard("$", f"SMMLV {date.today().year}", _cb_money_compact(params["salario_minimo"]),
                                       f"Aux. {_cb_money_compact(params['auxilio_transporte'])}", colores["primario_oscuro"]), 0, 3)

    # ── Empleados ──
    def _render_empleados(self):
        lay = self.tab_emp.layout(); clear_layout(lay)
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.search = QLineEdit(); self.search.setPlaceholderText("⌕  Buscar por nombre, documento o cargo")
        self.search.textChanged.connect(self._fill_empleados)
        bar.addWidget(self.search, 1)
        if self.can("payroll", "create"):
            nuevo = QPushButton("＋  Nuevo empleado"); nuevo.setObjectName("primaryAction")
            nuevo.clicked.connect(self._nuevo_empleado)
            bar.addWidget(nuevo)
        lay.addLayout(bar)
        self.emp_table = QTableWidget(0, 6)
        self.emp_table.setHorizontalHeaderLabels(["Nombre", "Documento", "Cargo", "Vinculación", "Salario", ""])
        self.emp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.emp_table.setColumnWidth(5, 170)
        self.emp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.emp_table.verticalHeader().setVisible(False); self.emp_table.verticalHeader().setDefaultSectionSize(46)
        self.emp_table.setSortingEnabled(True)
        lay.addWidget(self.emp_table, 1)
        self._all_emp = self.store.n_list_empleados()
        self._fill_empleados()

    def _fill_empleados(self):
        colores = _cb_brand_colors()
        needle = self.search.text().strip().lower() if hasattr(self, "search") else ""
        rows = [e for e in self._all_emp if not needle or needle in (
            (e.get("nombres") or "") + " " + (e.get("apellidos") or "") + " " +
            (e.get("numero_documento") or "") + " " + (e.get("cargo") or "")).lower()]
        self.emp_table.setSortingEnabled(False)
        self.emp_table.setRowCount(len(rows))
        for i, e in enumerate(rows):
            nombre = f"{e.get('nombres') or ''} {e.get('apellidos') or ''}".strip() + ("  ⏳" if e["synced"] == 0 else "")
            it = QTableWidgetItem(nombre); it.setData(Qt.ItemDataRole.UserRole, e["local_id"])
            self.emp_table.setItem(i, 0, it)
            self.emp_table.setItem(i, 1, QTableWidgetItem(f"{e.get('tipo_documento') or ''} {e.get('numero_documento') or ''}".strip()))
            self.emp_table.setItem(i, 2, QTableWidgetItem(e.get("cargo") or ""))
            self.emp_table.setItem(i, 3, QTableWidgetItem(_N_VINC_LABEL.get(e.get("tipo_vinculacion"), e.get("tipo_vinculacion") or "")))
            self.emp_table.setItem(i, 4, _NumItem(_rt_money(e.get("salario_base")), e.get("salario_base")))
            actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(4, 0, 4, 0); al.setSpacing(4)
            if self.can("payroll", "edit"):
                ed = QPushButton("Editar"); ed.setObjectName("secondaryAction"); ed.setStyleSheet(self._CELL_BTN)
                ed.clicked.connect(lambda _=False, lid=e["local_id"]: self._editar_empleado(lid))
                al.addWidget(ed)
            if self.can("payroll", "delete"):
                dl = QPushButton("✕"); dl.setObjectName("inlineDanger"); dl.setStyleSheet("min-height:26px;padding:0;min-width:28px;")
                dl.clicked.connect(lambda _=False, lid=e["local_id"]: self._eliminar_empleado(lid))
                al.addWidget(dl)
            al.addStretch(1)
            self.emp_table.setCellWidget(i, 5, actions)
        self.emp_table.setSortingEnabled(True)

    def _nuevo_empleado(self):
        dlg = _EmpleadoDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            self.store.n_crear_empleado(dlg.result_data, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Nómina", str(exc)); return
        self._after_change()

    def _editar_empleado(self, lid):
        e = self.store.n_get_empleado(lid)
        if not e:
            return
        dlg = _EmpleadoDialog(self, e)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        try:
            self.store.n_editar_empleado(lid, dlg.result_data, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Nómina", str(exc)); return
        self._after_change()

    def _eliminar_empleado(self, lid):
        if QMessageBox.question(self, "Eliminar", "¿Desactivar este empleado?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.n_eliminar_empleado(lid, user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Nómina", str(exc)); return
        self._after_change()

    # ── Períodos ──
    def _render_periodos(self):
        lay = self.tab_per.layout(); clear_layout(lay)
        colores = _cb_brand_colors()
        bar = QHBoxLayout()
        info = QLabel("Crea un período y liquídalo offline. El cálculo es idéntico al del servidor.")
        info.setObjectName("muted"); bar.addWidget(info, 1)
        if self.can("payroll", "create"):
            nuevo = QPushButton("＋  Nuevo período"); nuevo.setObjectName("primaryAction")
            nuevo.clicked.connect(self._nuevo_periodo); bar.addWidget(nuevo)
        lay.addLayout(bar)
        periodos = self.store.n_list_periodos()
        if not periodos:
            m = QLabel("Sin períodos. Crea el primero."); m.setObjectName("muted"); lay.addWidget(m); return
        tbl = QTableWidget(len(periodos), 6)
        tbl.setHorizontalHeaderLabels(["Período", "Desde", "Hasta", "Estado", "Neto liquidado", ""])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tbl.setColumnWidth(4, 130)
        tbl.setColumnWidth(5, 320)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.verticalHeader().setVisible(False); tbl.verticalHeader().setDefaultSectionSize(46)
        for i, p in enumerate(periodos):
            np = p.get("numero_periodo")
            etq = f"{p['anio']}-{str(p.get('mes') or 0).zfill(2)} " + ({1: "Q1", 2: "Q2"}.get(np, "Mensual"))
            etq += ("  ⏳" if p["synced"] == 0 else "")
            tbl.setItem(i, 0, QTableWidgetItem(etq))
            tbl.setItem(i, 1, QTableWidgetItem(p.get("fecha_inicio") or ""))
            tbl.setItem(i, 2, QTableWidgetItem(p.get("fecha_fin") or ""))
            est = p.get("estado_local") or "borrador"
            ecol = colores["acento_secundario"] if est == "liquidado" else colores["acento"]
            est_w = _cb_chip(est.capitalize(), ecol)
            tbl.setCellWidget(i, 3, est_w)
            det = self.store.n_list_detalle(p["local_id"])
            neto = sum(float(d["neto_pagar"] or 0) for d in det)
            ni = QTableWidgetItem(_rt_money(neto) if det else "—")
            tbl.setItem(i, 4, ni)
            actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(4, 0, 4, 0); al.setSpacing(4)
            if self.can("payroll", "create"):
                liq = QPushButton("Liquidar"); liq.setObjectName("primaryAction"); liq.setStyleSheet(self._CELL_BTN)
                liq.clicked.connect(lambda _=False, lid=p["local_id"]: self._liquidar(lid))
                al.addWidget(liq)
                nov = QPushButton("Novedades"); nov.setObjectName("secondaryAction"); nov.setStyleSheet(self._CELL_BTN)
                nov.clicked.connect(lambda _=False, lid=p["local_id"]: self._novedades(lid))
                al.addWidget(nov)
            ver = QPushButton("Desprendibles"); ver.setObjectName("secondaryAction"); ver.setStyleSheet(self._CELL_BTN)
            ver.clicked.connect(lambda _=False, lid=p["local_id"]: self._ver_desprendibles(lid))
            al.addWidget(ver); al.addStretch(1)
            tbl.setCellWidget(i, 5, actions)
        lay.addWidget(tbl, 1)

    def _nuevo_periodo(self):
        dlg = _PeriodoDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        d = dlg.result_data
        try:
            self.store.n_crear_periodo(d["anio"], d["mes"], d["numero_periodo"], d["fecha_inicio"], d["fecha_fin"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Nómina", str(exc)); return
        self._after_change()

    def _liquidar(self, periodo_local_id):
        import nomina_calc
        p = self.store.n_get_periodo(periodo_local_id)
        if not p:
            return
        params = self.store.n_get_parametros(p["anio"])
        if not params.get("salario_minimo"):
            QMessageBox.warning(self, "Nómina", f"No hay parámetros para el año {p['anio']}."); return
        emps_raw = self.store.n_list_empleados()
        sin_sync = [e for e in emps_raw if e["remote_id"] is None]
        empleados = [{**e, "id": e["remote_id"] or e["local_id"]} for e in emps_raw]
        novedades = []
        if p.get("remote_id") is not None:
            for nv in self.store.n_list_novedades(p["remote_id"]):
                novedades.append({"empleado_id": nv["empleado_remote_id"], "tipo_novedad": nv["tipo_novedad"],
                                  "cantidad": nv["cantidad"], "valor_total": nv["valor_total"]})
        periodo = {"anio": p["anio"], "numero_periodo": p.get("numero_periodo"),
                   "fecha_inicio": p.get("fecha_inicio"), "fecha_fin": p.get("fecha_fin")}
        res = nomina_calc.liquidar_periodo(periodo, params, empleados, novedades)
        try:
            self.store.n_guardar_liquidacion(periodo_local_id, res["detalles"], user=self._user())
        except ValueError as exc:
            QMessageBox.warning(self, "Nómina", str(exc)); return
        msg = (f"Liquidación calculada (motor local, idéntico al servidor):\n\n"
               f"Empleados: {res['resumen']['empleados']}  ·  Contratistas: {res['resumen']['contratistas']}\n"
               f"Total devengado: {_rt_money(res['resumen']['total_devengado'])}\n"
               f"Total neto a pagar: {_rt_money(res['resumen']['total_neto'])}")
        if sin_sync:
            msg += f"\n\n⚠ {len(sin_sync)} empleado(s) sin sincronizar: se liquidaron con id local provisional."
        alertas = res.get("alertas", [])
        if alertas:
            msg += "\n\nAlertas normativas (" + str(len(alertas)) + "):\n- " + "\n- ".join(a["mensaje"] for a in alertas[:6])
        QMessageBox.information(self, "Liquidación", msg)
        self._after_change()
        self._ver_desprendibles(periodo_local_id)

    def _ver_desprendibles(self, periodo_local_id):
        det = self.store.n_list_detalle(periodo_local_id)
        if not det:
            QMessageBox.information(self, "Desprendibles", "Este período no tiene liquidación. Usa “Liquidar”."); return
        emp_nombre = {}
        for e in self.store.n_list_empleados(incluir_inactivos=True):
            emp_nombre[e["remote_id"] or e["local_id"]] = f"{e.get('nombres') or ''} {e.get('apellidos') or ''}".strip()
        dlg = QDialog(self); dlg.setWindowTitle("Desprendibles del período"); dlg.setMinimumSize(900, 520)
        v = QVBoxLayout(dlg)
        tbl = QTableWidget(len(det), 9)
        tbl.setHorizontalHeaderLabels(["Empleado", "Días", "Básico", "Aux. transp.", "Extras",
                                       "Salud", "Pensión", "Retención", "Neto"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        for i, d in enumerate(det):
            tbl.setItem(i, 0, QTableWidgetItem(emp_nombre.get(d["empleado_remote_id"], f"#{d['empleado_remote_id']}")))
            tbl.setItem(i, 1, QTableWidgetItem(str(d["dias_trabajados"])))
            for col, key in ((2, "sueldo_basico"), (3, "auxilio_transporte"), (4, "horas_extras"),
                             (5, "salud_empleado"), (6, "pension_empleado"), (7, "retencion_fuente"), (8, "neto_pagar")):
                tbl.setItem(i, col, QTableWidgetItem(_rt_money(d.get(key))))
        v.addWidget(tbl, 1)
        total = sum(float(d["neto_pagar"] or 0) for d in det)
        tot = QLabel(f"Total neto a pagar:  {_rt_money(total)}")
        tot.setStyleSheet(f"font-size:15px;font-weight:850;color:{_cb_brand_colors()['primario_oscuro']};")
        v.addWidget(tot, 0, Qt.AlignmentFlag.AlignRight)
        close = QPushButton("Cerrar"); close.setObjectName("secondaryAction"); close.clicked.connect(dlg.accept)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _novedades(self, periodo_local_id):
        p = self.store.n_get_periodo(periodo_local_id)
        if not p:
            return
        if p.get("remote_id") is None:
            QMessageBox.information(self, "Novedades", "Sincroniza el período antes de registrar novedades."); return
        emps = [e for e in self.store.n_list_empleados() if e["remote_id"] is not None]
        if not emps:
            QMessageBox.information(self, "Novedades", "No hay empleados sincronizados."); return
        params = self.store.n_get_parametros(p["anio"])
        dlg = QDialog(self); dlg.setWindowTitle("Novedades del período"); dlg.setMinimumSize(680, 460)
        v = QVBoxLayout(dlg)
        bar = QHBoxLayout(); bar.addStretch(1)
        add = QPushButton("＋  Registrar novedad"); add.setObjectName("primaryAction")
        bar.addWidget(add); v.addLayout(bar)
        tbl = QTableWidget(0, 5)
        tbl.setHorizontalHeaderLabels(["Empleado", "Tipo", "Cantidad", "Valor", ""])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); tbl.verticalHeader().setVisible(False)
        v.addWidget(tbl, 1)
        emp_nombre = {e["remote_id"]: f"{e.get('nombres') or ''} {e.get('apellidos') or ''}".strip() for e in emps}

        def _fill():
            rows = self.store.n_list_novedades(p["remote_id"])
            tbl.setRowCount(len(rows))
            for i, nv in enumerate(rows):
                tbl.setItem(i, 0, QTableWidgetItem(emp_nombre.get(nv["empleado_remote_id"], f"#{nv['empleado_remote_id']}")))
                tbl.setItem(i, 1, QTableWidgetItem(_N_NOVEDAD_LABEL.get(nv["tipo_novedad"], nv["tipo_novedad"])))
                tbl.setItem(i, 2, QTableWidgetItem(str(nv["cantidad"])))
                tbl.setItem(i, 3, QTableWidgetItem(_rt_money(nv["valor_total"])))
                d = QPushButton("✕"); d.setObjectName("inlineDanger"); d.setStyleSheet("min-height:24px;padding:0;min-width:26px;")
                d.clicked.connect(lambda _=False, lid=nv["local_id"]: (self.store.n_eliminar_novedad(lid, user=self._user()), _fill()))
                w = QWidget(); wl = QHBoxLayout(w); wl.setContentsMargins(4, 0, 4, 0); wl.addWidget(d)
                tbl.setCellWidget(i, 4, w)

        def _add():
            nd = _NovedadDialog(dlg, emps, p.get("fecha_fin"), params)
            if nd.exec() != QDialog.DialogCode.Accepted or not nd.result_data:
                return
            r = nd.result_data
            try:
                self.store.n_crear_novedad(p["remote_id"], r["empleado_remote_id"], r["tipo_novedad"],
                                           r["cantidad"], r["valor_total"], r["fecha_novedad"], user=self._user())
            except ValueError as exc:
                QMessageBox.warning(dlg, "Nómina", str(exc)); return
            _fill()
        add.clicked.connect(_add)
        _fill()
        dlg.exec()
        self._after_change()

    # ── Parámetros ──
    def _render_parametros(self):
        lay = self.tab_par.layout(); clear_layout(lay)
        colores = _cb_brand_colors()
        info = QLabel("Parámetros oficiales usados en la liquidación. El servidor es la fuente; offline se usan los valores portados.")
        info.setObjectName("muted"); info.setWordWrap(True); lay.addWidget(info)
        import nomina_calc
        tbl = QTableWidget(0, 5)
        tbl.setHorizontalHeaderLabels(["Año", "SMMLV", "Auxilio transporte", "UVT", "Jornada/sem"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); tbl.verticalHeader().setVisible(False)
        anios = sorted(nomina_calc.PARAMETROS_OFICIALES_NOMINA.keys())
        tbl.setRowCount(len(anios))
        for i, a in enumerate(anios):
            par = self.store.n_get_parametros(a)
            tbl.setItem(i, 0, QTableWidgetItem(str(a)))
            tbl.setItem(i, 1, QTableWidgetItem(_rt_money(par["salario_minimo"])))
            tbl.setItem(i, 2, QTableWidgetItem(_rt_money(par["auxilio_transporte"])))
            tbl.setItem(i, 3, QTableWidgetItem(_rt_money(par["uvt"])))
            tbl.setItem(i, 4, QTableWidgetItem(f"{nomina_calc.JORNADA_LEY_2101.get(a, 42)} h"))
        lay.addWidget(tbl, 1)

    def _after_change(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# =============================================================================
# Asistente IA — Chat del negocio (online-only, licenciado por tenant)
# =============================================================================
_IA_SUGERENCIAS = [
    "¿Cuánto vendí este mes?",
    "¿Qué productos tienen poco stock?",
    "¿Cuáles son mis productos más vendidos?",
    "¿Qué debo comprar esta semana?",
    "¿Qué pedidos están por despachar?",
]


class _IaBubble(QFrame):
    """Burbuja de chat (usuario a la derecha, asistente a la izquierda)."""

    def __init__(self, texto: str, es_usuario: bool, colores: dict, herramienta: str | None = None):
        super().__init__()
        self.setObjectName("iaBubbleUser" if es_usuario else "iaBubbleBot")
        prim = colores.get("primario", "#122c94")
        bg = prim if es_usuario else "#ffffff"
        fg = "#ffffff" if es_usuario else "#1f2937"
        border = "transparent" if es_usuario else "rgba(17,24,39,0.08)"
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ background: {bg}; border: 1px solid {border};"
            f" border-radius: 14px; }}"
        )
        v = QVBoxLayout(self); v.setContentsMargins(14, 10, 14, 10); v.setSpacing(4)
        lbl = QLabel(texto); lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {fg}; font-size: 14px; background: transparent;")
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(lbl)
        if herramienta and not es_usuario:
            tag = QLabel(f"✦ vía {herramienta}")
            tag.setStyleSheet(f"color: {prim}; font-size: 11px; font-weight: 700; background: transparent;")
            v.addWidget(tag)


class AsistenteIAPage(QWidget):
    """Chat 'Pregúntale a tu negocio'. Online-only: consulta al servidor (proxy
    de Ollama) y responde con datos reales del tenant. Si no hay red/licencia,
    muestra un estado vacío elegante y deshabilita el input (sin panel roto).
    Historial persistente local (tabla ia_chat) para revisar aunque no haya red."""

    def __init__(self, store: LocalStore, user_callback=None, can_callback=None):
        super().__init__()
        self.store = store
        self.user_callback = user_callback or (lambda: None)
        self.can = can_callback or (lambda m, a: True)
        self._worker = None
        self._estado_worker = None
        self._online = False
        self._build()

    def _build(self):
        colores = _cb_brand_colors()
        root = QVBoxLayout(self); root.setContentsMargins(24, 22, 24, 18); root.setSpacing(12)

        header = QHBoxLayout()
        col = QVBoxLayout(); col.setSpacing(2)
        title = QLabel("Asistente IA"); title.setObjectName("pageTitle")
        sub = QLabel("Pregúntale a tu negocio. Responde con tus datos reales; requiere conexión.")
        sub.setObjectName("muted"); sub.setWordWrap(True)
        col.addWidget(title); col.addWidget(sub)
        header.addLayout(col, 1)
        self.estado_chip = QLabel("Verificando…")
        self.estado_chip.setObjectName("iaEstadoChip")
        self.estado_chip.setStyleSheet(
            "QLabel#iaEstadoChip { background: rgba(17,24,39,0.06); color:#6b7280;"
            " border-radius: 10px; padding: 6px 12px; font-weight: 700; font-size: 12px; }"
        )
        header.addWidget(self.estado_chip)
        limpiar = QPushButton("Limpiar historial"); limpiar.setObjectName("secondaryAction")
        limpiar.clicked.connect(self._clear_history)
        header.addWidget(limpiar)
        root.addLayout(header)

        # Área de conversación (scroll)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.msg_host = QWidget(); self.msg_host.setStyleSheet("background: transparent;")
        self.msg_layout = QVBoxLayout(self.msg_host)
        self.msg_layout.setContentsMargins(4, 4, 12, 4); self.msg_layout.setSpacing(10)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msg_host)
        root.addWidget(self.scroll, 1)

        # Estado vacío (se muestra cuando no hay red/licencia)
        self.empty_state = _crm_empty_state(
            "✦", "El asistente necesita conexión",
            "Conéctate a internet para preguntarle a tu negocio. El historial de "
            "conversaciones anteriores queda disponible aquí.",
            colores.get("primario", "#122c94"),
        )
        self.empty_state.setVisible(False)
        root.addWidget(self.empty_state)

        # Chips de sugerencias
        self.chips_row = QHBoxLayout(); self.chips_row.setSpacing(8)
        for s in _IA_SUGERENCIAS:
            chip = QPushButton(s); chip.setObjectName("iaChip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(
                "QPushButton#iaChip { background: rgba(17,24,39,0.05); color:#374151;"
                " border: 1px solid rgba(17,24,39,0.08); border-radius: 14px;"
                " padding: 6px 12px; font-size: 12px; } "
                "QPushButton#iaChip:hover { background: rgba(17,24,39,0.10); }"
            )
            chip.clicked.connect(lambda _=False, txt=s: self._enviar(txt))
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)
        self.chips_wrap = _wrap_layout(self.chips_row)
        root.addWidget(self.chips_wrap)

        # Input
        input_row = QHBoxLayout(); input_row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Escribe tu pregunta…")
        self.input.setMinimumHeight(42)
        self.input.returnPressed.connect(self._on_send_clicked)
        self.send_btn = QPushButton("Preguntar"); self.send_btn.setObjectName("primaryAction")
        self.send_btn.setMinimumHeight(42)
        self.send_btn.clicked.connect(self._on_send_clicked)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_btn)
        root.addLayout(input_row)

        self._load_history()

    # ── Ciclo de vida ────────────────────────────────────────────
    def refresh(self):
        """Se llama al mostrar la página: reevalúa disponibilidad en segundo plano."""
        self._set_estado("Verificando…", "#6b7280", enabled=False)
        client = _ai_build_client()
        if client is None:
            self._aplicar_estado({"online": False, "licenciado": False,
                                  "motivo": "Sincronización no configurada."})
            return
        if self._estado_worker and self._estado_worker.isRunning():
            return
        self._estado_worker = _BgWorker(lambda: client.ai_estado(), self)
        self._estado_worker.done.connect(self._aplicar_estado)
        self._estado_worker.failed.connect(lambda _msg: self._aplicar_estado(
            {"online": False, "licenciado": True, "motivo": "Sin conexión con el servidor."}))
        self._estado_worker.start()

    def _aplicar_estado(self, estado: dict):
        online = bool(estado.get("online"))
        licenciado = estado.get("licenciado", True)
        self._online = online and licenciado
        if not licenciado:
            self._set_estado("No incluido en el plan", "#b42318", enabled=False)
        elif online:
            modelo = estado.get("modelo") or "IA"
            self._set_estado(f"● En línea · {modelo}", "#15803d", enabled=True)
        else:
            self._set_estado("● Sin conexión", "#b42318", enabled=False)
        # Estado vacío visible solo cuando NO se puede chatear
        self.empty_state.setVisible(not self._online)
        self.scroll.setVisible(self._online or self.msg_layout.count() > 1)
        self.chips_wrap.setVisible(self._online)

    def _set_estado(self, texto: str, color: str, enabled: bool):
        self.estado_chip.setText(texto)
        self.estado_chip.setStyleSheet(
            f"QLabel#iaEstadoChip {{ background: rgba(17,24,39,0.06); color:{color};"
            f" border-radius: 10px; padding: 6px 12px; font-weight: 700; font-size: 12px; }}"
        )
        self.input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)

    # ── Chat ─────────────────────────────────────────────────────
    def _on_send_clicked(self):
        self._enviar(self.input.text())

    def _enviar(self, pregunta: str):
        pregunta = (pregunta or "").strip()
        if not pregunta or not self._online:
            return
        if self._worker and self._worker.isRunning():
            return
        self.input.clear()
        self._add_bubble(pregunta, es_usuario=True)
        self.store.ia_add_message("user", pregunta)
        thinking = self._add_bubble("Pensando…", es_usuario=False)
        self.send_btn.setEnabled(False); self.input.setEnabled(False)
        client = _ai_build_client()
        if client is None:
            self._reemplazar_thinking(thinking, "No hay conexión configurada.", None)
            self._set_busy(False)
            return
        self._worker = _BgWorker(lambda: client.ai_chat(pregunta), self)
        self._worker.done.connect(lambda resp: self._on_chat_done(thinking, resp))
        self._worker.failed.connect(lambda msg: self._on_chat_fail(thinking, msg))
        self._worker.start()

    def _on_chat_done(self, thinking, resp: dict):
        if not isinstance(resp, dict) or not resp.get("success"):
            motivo = (resp or {}).get("error") if isinstance(resp, dict) else None
            self._reemplazar_thinking(thinking, motivo or "No pude responder ahora.", None)
        else:
            texto = resp.get("respuesta") or "Sin respuesta."
            herramienta = resp.get("herramienta")
            self._reemplazar_thinking(thinking, texto, herramienta)
            self.store.ia_add_message("assistant", texto, herramienta)
        self._set_busy(False)

    def _on_chat_fail(self, thinking, msg: str):
        self._reemplazar_thinking(thinking, "Sin conexión con el asistente. Intenta de nuevo.", None)
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy and self._online)
        self.input.setEnabled(not busy and self._online)
        if not busy:
            self.input.setFocus()

    # ── Render helpers ───────────────────────────────────────────
    def _add_bubble(self, texto: str, es_usuario: bool, herramienta: str | None = None) -> _IaBubble:
        colores = _cb_brand_colors()
        bubble = _IaBubble(texto, es_usuario, colores, herramienta)
        wrap = QHBoxLayout()
        if es_usuario:
            wrap.addStretch(1); wrap.addWidget(bubble, 4)
        else:
            wrap.addWidget(bubble, 4); wrap.addStretch(1)
        container = _wrap_layout(wrap)
        # Insertar antes del stretch final
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, container)
        QTimer.singleShot(30, self._scroll_to_bottom)
        bubble._container = container
        return bubble

    def _reemplazar_thinking(self, thinking: _IaBubble, texto: str, herramienta):
        container = getattr(thinking, "_container", None)
        if container is not None:
            container.setParent(None)
        self._add_bubble(texto, es_usuario=False, herramienta=herramienta)

    def _scroll_to_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _load_history(self):
        for msg in self.store.ia_recent(limit=30):
            self._add_bubble(msg.get("texto") or "", es_usuario=(msg.get("rol") == "user"),
                             herramienta=msg.get("herramienta"))

    def _clear_history(self):
        confirm = QMessageBox.question(self, "Limpiar historial",
                                       "¿Borrar el historial de conversaciones local?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.ia_clear()
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)


# =============================================================================
# Shell
# =============================================================================
class DesktopShell(QMainWindow):
    # (key, icono unicode, etiqueta, atajo, sección)
    NAV_ITEMS = [
        ("dashboard", "▦", "Dashboard",       "F1", "Operación"),
        ("pos",       "⌧", "POS",             "F3", "Operación"),
        ("restaurant","▤", "Restaurante",     "F9", "Operación"),
        ("sales",     "$", "Ventas",          "F5", "Operación"),
        ("products",  "▤", "Productos",       "F2", "Datos"),
        ("inventory", "⊞", "Inventario",      "F4", "Datos"),
        ("contabilidad", "Σ", "Contabilidad", "F10", "Datos"),
        ("quotes",    "✎", "Cotizaciones",    "F11", "Datos"),
        ("cobros",    "₵", "Cuentas de cobro", "F12", "Datos"),
        ("users",     "◯", "Usuarios",        "F6", "Datos"),
        ("crm",       "☺", "CRM",             "",    "Clientes"),
        ("payroll",   "₲", "Nómina",          "",    "Administración"),
        ("ia",        "✦", "Asistente IA",    "",    "Inteligencia"),
        ("sync",      "⟳", "Sincronización",  "F7", "Sistema"),
        ("config",    "✎", "Configuración",   "F8", "Sistema"),
    ]

    def __init__(self):
        super().__init__()
        self.store = LocalStore()
        self.user = None
        self._perms = None          # entrada de permisos del rol actual (manifiesto)
        self._app_dir = app_data_dir()
        self._install_conf = install_conf.load(self._app_dir)
        self._bootstrap_sync_from_install_conf()
        self.branding = branding_mod.load_branding(self._app_dir)
        self._update_skip_version = sync_cfg.load(self._app_dir).get("skip_version", "")
        logo_path = (self.branding.get("empresa") or {}).get("logo_path") or ""
        self.setWindowIcon(
            QIcon(logo_path) if logo_path and Path(logo_path).is_file() else _default_app_icon()
        )
        self.resize(1260, 820)
        self.setMinimumSize(1000, 660)

        self.scanner = ScannerEngine(self)
        QApplication.instance().installEventFilter(self.scanner)

        self.stack = QStackedWidget()
        self.login_view = LoginView(self.store, self._login_success, brand_callback=self._get_branding)
        self.app_view = self._build_app_view()
        self.stack.addWidget(self.login_view)
        self.stack.addWidget(self.app_view)
        self.setCentralWidget(self.stack)
        self.apply_branding()
        self._setup_shortcuts()

    def _get_branding(self):
        return self.branding

    def _get_user(self):
        return self.user

    def _build_app_view(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(252)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(16, 20, 16, 16)
        side_layout.setSpacing(4)

        # Header del sidebar: logo + marca + subtítulo
        self.sidebar_logo = QLabel()
        self.sidebar_logo.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.sidebar_logo.setVisible(False)
        side_layout.addWidget(self.sidebar_logo)

        self.brand_label = QLabel("CyberShop")
        self.brand_label.setObjectName("brand")
        side_layout.addWidget(self.brand_label)

        self.brand_subtitle = QLabel("Punto de venta")
        self.brand_subtitle.setObjectName("brandSubtitle")
        side_layout.addWidget(self.brand_subtitle)

        # Badge de estado de sync (online/syncing/offline)
        self.sync_badge = QLabel("●  Modo local")
        self.sync_badge.setObjectName("syncBadge")
        self.sync_badge.setProperty("state", "offline")
        side_layout.addWidget(self.sync_badge)

        side_layout.addSpacing(12)

        # User card
        self.user_card = QFrame()
        self.user_card.setObjectName("userCard")
        # UX: clic en la tarjeta de usuario → "Tu acceso" (qué módulos puede
        # usar este rol y hasta dónde llega, según lo configuró el dueño).
        self.user_card.setCursor(Qt.CursorShape.PointingHandCursor)
        self.user_card.setToolTip("Haz clic para ver tu acceso")
        self.user_card.mousePressEvent = lambda _ev: self._mostrar_mi_acceso()
        user_outer = QHBoxLayout(self.user_card)
        user_outer.setContentsMargins(12, 10, 12, 10)
        user_outer.setSpacing(10)
        self.user_avatar_label = QLabel("·")
        self.user_avatar_label.setObjectName("userAvatar")
        self.user_avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        user_outer.addWidget(self.user_avatar_label)
        user_text = QVBoxLayout()
        user_text.setSpacing(0)
        self.user_name_label = QLabel("Sin sesión")
        self.user_name_label.setObjectName("userName")
        self.user_role_label = QLabel("—")
        self.user_role_label.setObjectName("userRole")
        user_text.addWidget(self.user_name_label)
        user_text.addWidget(self.user_role_label)
        user_outer.addLayout(user_text, 1)
        side_layout.addWidget(self.user_card)

        # Nav items agrupados por sección, dentro de un scroll transparente para
        # que la barra no se desborde ni recorte rótulos cuando hay muchos
        # módulos o la ventana es baja. El scroll solo aparece si hace falta.
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }"
            " QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            " QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 30px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        nav_host = QWidget()
        nav_host.setStyleSheet("background: transparent;")
        nav_layout = QVBoxLayout(nav_host)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)

        self.nav_buttons = {}
        self.nav_section_labels = {}   # section -> QLabel (para ocultar si queda vacía)
        self.nav_sections = {}         # section -> [keys] (para saber si quedó vacía)
        last_section = None
        for key, icon, label, shortcut, section in self.NAV_ITEMS:
            if section != last_section:
                section_label = QLabel(section)
                section_label.setObjectName("sidebarSection")
                nav_layout.addWidget(section_label)
                self.nav_section_labels[section] = section_label
                last_section = section
            self.nav_sections.setdefault(section, []).append(key)
            button = QPushButton(f"  {icon}    {label}")
            button.setObjectName("navButton")
            button.setToolTip(f"{label} ({shortcut})")
            button.clicked.connect(lambda checked=False, name=key: self._show_section(name))
            self.nav_buttons[key] = button
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        nav_scroll.setWidget(nav_host)
        side_layout.addWidget(nav_scroll, 1)

        logout = QPushButton("  ↪    Cerrar sesión")
        logout.setObjectName("logoutButton")
        logout.clicked.connect(self._logout)
        side_layout.addWidget(logout)

        self.content_stack = QStackedWidget()
        self.pages = {
            "dashboard": DashboardPage(self.store, self._show_section),
            "products": ProductsPage(self.store, self._refresh_shared_pages, can_callback=self.can),
            "pos": PosPage(self.store, self._refresh_shared_pages, scanner=self.scanner, brand_callback=self._get_branding, can_callback=self.can),
            "restaurant": RestaurantPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "contabilidad": ContabilidadPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "quotes": CotizacionesPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "cobros": CuentasCobroPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "crm": CrmPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "payroll": NominaPage(self.store, self._refresh_shared_pages, user_callback=self._get_user, can_callback=self.can),
            "ia": AsistenteIAPage(self.store, user_callback=self._get_user, can_callback=self.can),
            "inventory": InventoryPage(self.store, self._refresh_shared_pages, can_callback=self.can),
            "sales": SalesPage(self.store, brand_callback=self._get_branding),
            "users": UsersPage(self.store, self._refresh_shared_pages, can_callback=self.can),
            "sync": SyncPage(self.store, self._refresh_shared_pages, base_dir=self._app_dir, user_callback=self._get_user),
            "config": ConfiguracionPage(self.branding, self._app_dir, on_apply=self.apply_branding),
        }
        for page in self.pages.values():
            self.content_stack.addWidget(page)

        body_layout.addWidget(sidebar)
        body_layout.addWidget(self.content_stack, stretch=1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        self.footer_left = QLabel(f"CyberShop Desktop {APP_VERSION}")
        self.footer_left.setObjectName("footerText")
        self.footer_right = QLabel("")
        self.footer_right.setObjectName("footerText")
        self.footer_right.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        footer_layout.addWidget(self.footer_left)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.footer_right)

        root_layout.addWidget(body, 1)
        root_layout.addWidget(footer)
        return root

    def _setup_shortcuts(self):
        for key, _icon, _label, shortcut, _section in self.NAV_ITEMS:
            sc = QShortcut(QKeySequence(shortcut), self)
            sc.activated.connect(lambda name=key: self._show_section(name))

    def _login_success(self, user):
        self.user = user
        name = (user.name or "").strip() or "Usuario"
        self.user_name_label.setText(name)
        role = (user.role or "—").strip()
        email = (user.email or "").strip()
        self.user_role_label.setText(f"{role} · {email}" if email else role)
        initial = name[:1].upper() if name else "·"
        self.user_avatar_label.setText(initial)
        info = self.store.db_info()
        self.footer_right.setText(info["path"])
        self._apply_access_control(role)
        self.stack.setCurrentWidget(self.app_view)
        # Aterriza en el primer módulo permitido (dashboard si está disponible)
        landing = "dashboard" if "dashboard" in self._allowed_sections else next(iter(self._allowed_sections), None)
        if landing:
            self._show_section(landing)
        # Kickoff diferido (no bloqueante a la UI): branding sync + update check.
        # Se ejecuta una sola vez por sesión. Si falla por red, silencioso.
        QTimer.singleShot(500, self._pull_branding_from_server)
        QTimer.singleShot(1500, self._check_for_updates)

    def _logout(self):
        confirm = QMessageBox.question(
            self,
            "Cerrar sesión",
            "¿Está seguro? Las ventas en curso del POS se descartarán.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if isinstance(self.pages.get("pos"), PosPage):
            self.pages["pos"]._clear_cart()
        self.user = None
        self.user_name_label.setText("Sin sesión")
        self.user_role_label.setText("—")
        self.user_avatar_label.setText("·")
        self.scanner.set_enabled(False)
        self.stack.setCurrentWidget(self.login_view)

    def _apply_access_control(self, role: str):
        """Muestra solo los módulos permitidos por ROL ∩ PLAN del tenant.
        Los módulos/acciones del rol vienen del MANIFIESTO server-authoritative
        (derivado de security.py) cacheado vía /sync/config; sin manifiesto cae
        a ROLE_MODULES local (fail-open). El plan viene de los flags de
        cliente_config. Oculta botones de nav y etiquetas de sección vacías."""
        manifest = self.store.get_permissions_manifest()
        entry = manifest.get(role) if isinstance(manifest, dict) else None
        self._perms = entry if isinstance(entry, dict) else None
        if self._perms and self._perms.get("modules"):
            role_allowed = set(self._perms["modules"])        # manifiesto del servidor
            # Módulos que el manifiesto cacheado aún NO conoce (p. ej. 'ia' antes
            # de actualizar el servidor): decidir con el mapa local de roles,
            # espejo de la web (licencia + grupo de rol, sin depender del
            # manifiesto). Cuando el servidor emita el módulo, su veredicto gana.
            conocidos = set()
            for e in manifest.values():
                if isinstance(e, dict):
                    conocidos |= set(e.get("modules") or [])
            role_allowed |= (set(modules_for_role(role)) - conocidos)
        else:
            role_allowed = set(modules_for_role(role))        # fallback local
        tenant_allowed = tenant_allowed_modules(self.store.get_tenant_modules())
        if tenant_allowed is None:
            allowed = role_allowed                            # sin restricción de plan
        else:
            allowed = role_allowed & tenant_allowed           # tenant_allowed ya incluye SYSTEM_MODULES
        self._allowed_sections = allowed
        for key, button in self.nav_buttons.items():
            button.setVisible(key in allowed)
        # Oculta el rótulo de una sección si ninguno de sus botones quedó visible
        for section, keys in self.nav_sections.items():
            label = self.nav_section_labels.get(section)
            if label is not None:
                label.setVisible(any(k in allowed for k in keys))

    def can(self, module: str, action: str) -> bool:
        """True si el rol actual puede `action` en `module` según el manifiesto.
        Fail-open: sin manifiesto cacheado o sin entrada de acciones para el
        módulo, permite (cae al gating por módulo). El servidor es la barrera
        dura: rechaza en _apply_* lo que el rol no puede escribir."""
        perms = getattr(self, "_perms", None)
        if not perms:
            return True
        actions = (perms.get("actions") or {}).get(module)
        if actions is None:
            return True
        return action in actions

    def _mostrar_mi_acceso(self):
        """Diálogo 'Tu acceso': qué módulos puede usar el rol actual y hasta
        dónde llega cada uno, en lenguaje simple. Refleja lo que el dueño
        configuró en la web (Roles y Permisos) intersectado con el plan."""
        if self.user is None:
            return
        allowed = getattr(self, "_allowed_sections", set())
        etiquetas = {k: (icon, label) for k, icon, label, _sc, _sec in self.NAV_ITEMS}
        lineas = []
        for key, icon, label, _sc, _sec in self.NAV_ITEMS:
            if key not in allowed or key in ("sync", "config", "dashboard"):
                continue
            acts = ((self._perms or {}).get("actions") or {}).get(key)
            if acts is None:
                nivel = "acceso completo"
            else:
                partes = []
                if "view" in acts:
                    partes.append("ver")
                if any(a in acts for a in ("create", "edit", "use", "charge")):
                    partes.append("crear y modificar")
                if any(a in acts for a in ("delete", "cancel", "approve")):
                    partes.append("eliminar/anular")
                nivel = " · ".join(partes) or "ver"
            lineas.append(f"<tr><td style='padding:3px 12px 3px 0'><b>{icon}  {label}</b></td>"
                          f"<td style='color:#5a7a14'>{nivel}</td></tr>")
        rol = (self.user.role or "—").strip()
        cuerpo = (f"<p><b>{self.user.name}</b> · rol <b>{rol}</b></p>"
                  + ("<table>" + "".join(lineas) + "</table>" if lineas
                     else "<p>Tu rol no tiene módulos habilitados. Habla con el dueño del negocio.</p>")
                  + "<p style='color:#6b7280;font-size:12px'>El dueño puede cambiar esto en la web: "
                    "Usuarios → Roles y Permisos. Los cambios llegan al sincronizar.</p>")
        box = QMessageBox(self)
        box.setWindowTitle("Tu acceso")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(cuerpo)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _show_section(self, name):
        if self.stack.currentWidget() is not self.app_view:
            return
        if name not in self.pages:
            return
        # Control de acceso (rol ∩ plan): ignora navegación a módulos no permitidos
        if name not in getattr(self, "_allowed_sections", set()):
            return
        self._current_section = name
        self._refresh_page(name)
        self.content_stack.setCurrentWidget(self.pages[name])
        for key, button in self.nav_buttons.items():
            button.setProperty("active", key == name)
            button.style().unpolish(button)
            button.style().polish(button)
        pos_page = self.pages.get("pos")
        if isinstance(pos_page, PosPage):
            pos_page.set_active(name == "pos")
        self.scanner.set_enabled(name == "pos")
        if name == "pos":
            QTimer.singleShot(80, self.pages["pos"].focus_barcode)

    def _refresh_shared_pages(self):
        for name in ("dashboard", "products", "pos", "restaurant", "contabilidad", "quotes", "cobros", "crm", "payroll", "inventory", "sales", "users", "sync"):
            self._refresh_page(name)
        # Tras un sync pueden haber cambiado los flags de módulos del plan:
        # re-aplica el gating sin requerir re-login. Si el módulo actualmente
        # visible quedó fuera del plan, rebota a un módulo permitido.
        if getattr(self, "user", None) is not None:
            self._apply_access_control((self.user.role or "—").strip())
            current = getattr(self, "_current_section", None)
            if current and current not in self._allowed_sections:
                landing = "dashboard" if "dashboard" in self._allowed_sections else next(iter(self._allowed_sections), None)
                if landing:
                    self._show_section(landing)

    def _refresh_page(self, name):
        page = self.pages.get(name)
        if hasattr(page, "refresh"):
            page.refresh()

    def apply_branding(self):
        """Re-aplica el branding actual: titulo, sidebar, login, footer y QSS."""
        empresa = self.branding["empresa"]
        self.setWindowTitle(empresa.get("ventana_titulo") or f"{empresa['nombre']} Desktop")
        if hasattr(self, "brand_label"):
            self.brand_label.setText(empresa["nombre"])
        if hasattr(self, "brand_subtitle"):
            slogan = (empresa.get("slogan") or "").strip()
            self.brand_subtitle.setText(slogan or "Punto de venta")
        if hasattr(self, "sidebar_logo"):
            logo_path = empresa.get("logo_path") or ""
            if logo_path and Path(logo_path).is_file():
                pix = QPixmap(logo_path)
                if not pix.isNull():
                    pix = pix.scaledToHeight(48, Qt.TransformationMode.SmoothTransformation)
                    self.sidebar_logo.setPixmap(pix)
                    self.sidebar_logo.setVisible(True)
                else:
                    self.sidebar_logo.setVisible(False)
            else:
                self.sidebar_logo.clear()
                self.sidebar_logo.setVisible(False)
        if hasattr(self, "login_view"):
            self.login_view.apply_branding()
        self.setStyleSheet(branding_mod.render_qss(self.branding, QSS_TEMPLATE))

    def set_sync_state(self, state: str, label: str | None = None):
        """Actualiza el badge de sync. state ∈ {'online','syncing','offline'}."""
        if not hasattr(self, "sync_badge"):
            return
        defaults = {
            "online":  "●  Conectado",
            "syncing": "●  Sincronizando…",
            "offline": "●  Modo local",
        }
        self.sync_badge.setText(label or defaults.get(state, "●  Modo local"))
        self.sync_badge.setProperty("state", state if state in defaults else "offline")
        # Forzar re-aplicación del QSS dependiente del property
        self.sync_badge.style().unpolish(self.sync_badge)
        self.sync_badge.style().polish(self.sync_badge)

    # ── Bootstrap desde .cybershop.conf (escrito por el wizard del instalador) ──
    def _bootstrap_sync_from_install_conf(self):
        """Si el wizard escribió .cybershop.conf y sync_config.json está vacío,
        copia URL y API key al sync_config para no obligar al usuario a meter
        los datos de nuevo en F7."""
        if not install_conf.is_configured(self._install_conf):
            return  # no hay .cybershop.conf con datos válidos
        current = sync_cfg.load(self._app_dir)
        if current.get("base_url") and current.get("api_key"):
            return  # ya configurado, no pisar
        sync_cfg.update(
            self._app_dir,
            base_url=self._install_conf.get("SERVER_URL", ""),
            api_key=self._install_conf.get("SYNC_API_KEY", ""),
            enabled=True,
            interval_sec=install_conf.sync_interval_sec(self._install_conf),
        )

    # ── Pull de branding desde el servidor (no bloqueante) ──
    def _pull_branding_from_server(self):
        """Llama /api/v1/sync/branding y aplica el resultado a branding.json.

        Best-effort: si no hay red o la API key es inválida, no hace nada.
        Respeta sync_config.branding_local_override.
        """
        state = sync_cfg.load(self._app_dir)
        if state.get("branding_local_override"):
            return
        if not state.get("base_url") or not state.get("api_key"):
            self.set_sync_state("offline")
            return
        self.set_sync_state("syncing")
        try:
            client = SyncClient(state["base_url"], state["api_key"])
            remote = client.pull_branding()
        except (ValueError, SyncError):
            self.set_sync_state("offline")
            return  # sin red o key inválida → silencioso
        try:
            self.branding = branding_mod.apply_remote_branding(
                self._app_dir, remote,
                download_logo=lambda url, dest: SyncClient(state["base_url"], state["api_key"]).download_file(url, dest),
            )
            self.apply_branding()
            sync_cfg.update(self._app_dir, last_branding_pull_at=_now_iso_local())
            self.set_sync_state("online")
        except Exception as exc:  # noqa: BLE001
            print(f"[branding-sync] error aplicando branding remoto: {exc}")
            self.set_sync_state("offline")

    # ── Auto-update: check de versión disponible ──
    def _check_for_updates(self):
        """Chequea /api/v1/sync/version. Dos modos:
          - OBLIGATORIO (seguridad): si APP_VERSION < min_required, fuerza la
            actualización SIN importar el toggle de auto-update ni el 'saltar'.
          - OPCIONAL: si hay 'latest' más nueva, el auto-update está activo y no
            fue saltada, ofrece actualizar.
        Best-effort: si no hay red, ignora.
        """
        state = sync_cfg.load(self._app_dir)
        base_url = state.get("base_url") or self._install_conf.get("SERVER_URL", "")
        api_key = state.get("api_key") or self._install_conf.get("SYNC_API_KEY", "")
        if not base_url:
            return
        try:
            client = SyncClient(base_url, api_key or "x")  # api_key opcional para /version
            manifest = client.pull_version()
        except (ValueError, SyncError):
            return
        latest = (manifest.get("latest") or "").strip()
        min_required = (manifest.get("min_required") or "").strip()

        # 1) Parche OBLIGATORIO de seguridad: no se puede saltar ni desactivar.
        if min_required and _version_cmp(APP_VERSION, min_required) < 0:
            self._show_update_dialog(manifest, mandatory=True)
            return

        # 2) Actualización opcional (respeta el toggle y el 'saltar esta versión').
        if not install_conf.auto_update_enabled(self._install_conf):
            return
        if not latest or _version_cmp(latest, APP_VERSION) <= 0:
            return  # ya estamos al día
        if latest == self._update_skip_version:
            return  # el usuario eligió saltar esta versión
        self._show_update_dialog(manifest, mandatory=False)

    def _show_update_dialog(self, manifest, mandatory=False):
        latest = manifest.get("latest", "?")
        notes = (manifest.get("release_notes") or "").strip()
        while True:
            if mandatory:
                text = (
                    "Hay una actualización de SEGURIDAD obligatoria del POS Desktop.\n\n"
                    f"Versión actual:    {APP_VERSION}\n"
                    f"Versión requerida: {manifest.get('min_required', latest)}\n\n"
                    f"{notes if notes else 'Actualización crítica de seguridad.'}\n\n"
                    "Debes actualizar para seguir usando la aplicación."
                )
            else:
                text = (
                    "Hay una versión nueva del POS Desktop disponible.\n\n"
                    f"Versión actual: {APP_VERSION}\n"
                    f"Versión nueva:  {latest}\n\n"
                    f"{notes if notes else 'Notas de versión no disponibles.'}\n\n"
                    "¿Descargar e instalar ahora?"
                )
            msg = QMessageBox(self)
            msg.setWindowTitle("Actualización de seguridad" if mandatory else "Actualización disponible")
            msg.setText(text)
            msg.setIcon(QMessageBox.Icon.Warning if mandatory else QMessageBox.Icon.Information)
            btn_download = msg.addButton("Descargar e instalar", QMessageBox.ButtonRole.AcceptRole)
            btn_exit = btn_later = btn_skip = None
            if mandatory:
                btn_exit = msg.addButton("Salir", QMessageBox.ButtonRole.RejectRole)
            else:
                btn_later = msg.addButton("Más tarde", QMessageBox.ButtonRole.RejectRole)
                btn_skip = msg.addButton("Saltar esta versión", QMessageBox.ButtonRole.DestructiveRole)
            msg.exec()
            clicked = msg.clickedButton()

            if clicked is btn_download:
                if self._download_and_launch_installer(manifest):
                    return  # instalador lanzado; la app se está cerrando
                if mandatory:
                    continue  # falló: re-mostrar (no puede seguir con versión insegura)
                return
            # No eligió descargar:
            if mandatory:
                QApplication.quit()  # no puede usar una versión insegura/obsoleta
                return
            if clicked is btn_skip:
                sync_cfg.update(self._app_dir, skip_version=latest)
                self._update_skip_version = latest
            return  # 'Más tarde' o cerrar → vuelve a aparecer al próximo arranque

    def _download_and_launch_installer(self, manifest):
        """Descarga el instalador, VERIFICA su checksum SHA-256 y lo ejecuta.
        Cierra esta app para permitir el reemplazo de archivos.
        Devuelve True si lo lanzó (la app se cerrará), False si falló."""
        import hashlib
        import subprocess
        import tempfile
        url = (manifest.get("download_url") or "").strip()
        if not url:
            QMessageBox.warning(self, "Sin descarga", "El manifiesto de versión no trae 'download_url'.")
            return False
        expected = (manifest.get("checksum_sha256") or "").strip().lower()
        state = sync_cfg.load(self._app_dir)
        client = SyncClient(state.get("base_url") or self._install_conf.get("SERVER_URL", ""),
                            state.get("api_key") or "x")
        dest = Path(tempfile.gettempdir()) / "CyberShopSetup_update.exe"
        try:
            client.download_file(url, dest)
        except SyncError as exc:
            QMessageBox.warning(self, "Descarga fallida", f"No se pudo descargar la actualización:\n{exc}")
            return False

        # Verificación de integridad (anti-manipulación). Si el manifiesto trae
        # checksum y NO coincide, se aborta y se borra el archivo descargado.
        if expected:
            h = hashlib.sha256()
            try:
                with open(dest, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
            except OSError as exc:
                QMessageBox.warning(self, "Error", f"No se pudo leer el instalador descargado:\n{exc}")
                return False
            actual = h.hexdigest().lower()
            if actual != expected:
                try:
                    dest.unlink()
                except OSError:
                    pass
                QMessageBox.critical(
                    self, "Actualización rechazada",
                    "El instalador descargado NO coincide con la firma esperada "
                    "(checksum SHA-256). Se canceló por seguridad.\n\n"
                    f"esperado: {expected[:16]}…\nobtenido: {actual[:16]}…")
                return False

        try:
            subprocess.Popen([str(dest)], close_fds=True)
        except OSError as exc:
            QMessageBox.warning(self, "Error", f"No se pudo iniciar el instalador:\n{exc}")
            return False
        QApplication.quit()
        return True


# =============================================================================
# Helpers
# =============================================================================
def metric_card(label, value, empty: bool = False):
    frame = QFrame()
    frame.setObjectName("metricCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(2)
    value_label = QLabel(value)
    value_label.setObjectName("metricValue")
    if empty:
        value_label.setProperty("state", "empty")
    label_widget = QLabel(label)
    label_widget.setObjectName("metricLabel")
    layout.addWidget(value_label)
    layout.addWidget(label_widget)
    return frame


def placeholder_page(title, text):
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(28, 24, 28, 24)
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    body = QLabel(text)
    body.setObjectName("muted")
    body.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(body)
    layout.addStretch(1)
    return page


def _wrap_layout(inner_layout):
    """Envuelve un QLayout en un QWidget para usarlo en QFormLayout.addRow."""
    w = QWidget()
    w.setLayout(inner_layout)
    return w


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.deleteLater()
        else:
            child = item.layout()
            if child is not None:
                clear_layout(child)
                child.deleteLater()


def _setup_logging():
    """Logging con rotación en %APPDATA%/CyberShopNative/logs/desktop.log.
    Reemplaza los print sueltos por un archivo consultable ante incidencias."""
    import logging
    from logging.handlers import RotatingFileHandler
    log_dir = app_data_dir() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return logging.getLogger("cybershop")
    logger = logging.getLogger("cybershop")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "desktop.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    return logger


def _install_excepthook(logger):
    """Crash handler global: registra el traceback y muestra un diálogo amable
    con opción de copiar el detalle, en lugar de cerrar la app en silencio."""
    def _hook(exc_type, exc_value, exc_tb):
        import traceback
        detalle = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            logger.error("Excepción no controlada:\n%s", detalle)
        except Exception:
            pass
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("CyberShop Desktop — Error inesperado")
            box.setText("Ocurrió un error inesperado. La aplicación intentará continuar.\n"
                        "Puedes copiar el detalle técnico para soporte.")
            box.setDetailedText(detalle)
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        except Exception:
            # Si ni siquiera hay app Qt viva, al menos quedó en el log.
            sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CyberShop.Desktop")
        except Exception:
            pass

    logger = _setup_logging()
    logger.info("Arranque CyberShop Desktop %s", APP_VERSION)

    app = QApplication(sys.argv)
    app.setWindowIcon(_default_app_icon())

    # Instancia única: evita 2 procesos escribiendo la misma SQLite. El lock vive
    # mientras el proceso esté vivo (QLockFile se libera al salir/crashear).
    lock_path = str(app_data_dir() / "app.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(0)   # si el proceso dueño murió, se considera obsoleto
    if not lock.tryLock(100):
        QMessageBox.information(
            None, "CyberShop Desktop",
            "La aplicación ya está abierta. Se usará la ventana existente.")
        logger.info("Segunda instancia bloqueada por QLockFile; saliendo.")
        return 0
    app._single_instance_lock = lock   # mantener referencia viva

    # Crash handler global (necesita QApplication para el diálogo).
    _install_excepthook(logger)

    quit_action = QAction("Salir")
    quit_action.triggered.connect(app.quit)
    window = DesktopShell()
    window.addAction(quit_action)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
