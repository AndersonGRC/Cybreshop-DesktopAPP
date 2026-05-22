import shutil
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
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


APP_VERSION = "1.0.0"
ROLES = ["Administrador", "Cajero", "Inventario"]


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


# =============================================================================
# Stylesheet template (con placeholders $primario, etc.)
# =============================================================================
QSS_TEMPLATE = """
/* ═══════════════════════════════════════════════════════════════
   CyberShop POS Desktop — Design System
   Tokens (vienen de branding.json):
     $primario, $primario_oscuro, $acento, $acento_secundario,
     $peligro, $sidebar_inicio, $sidebar_fin, $fondo
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
    background: #ffffff;
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
        stop:0 #ffffff,
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
    background: #ffffff;
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
QTableWidget::item { padding: 8px 6px; }
QHeaderView::section {
    background: rgba($primario_rgb, 0.08);
    color: #374151;
    border: 0;
    border-bottom: 1px solid rgba($primario_rgb, 0.20);
    padding: 10px 8px;
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
    border: 1px solid rgba(0,0,0,0.12);
    border-radius: 6px;
    min-width: 26px;
    min-height: 26px;
    max-width: 26px;
    max-height: 26px;
}
QFrame#divider {
    background: rgba($primario_rgb, 0.10);
    max-height: 1px;
    min-height: 1px;
}
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
        body = QVBoxLayout(self)
        body.setContentsMargins(24, 22, 24, 22)
        body.setSpacing(14)

        header = QLabel("Menú administrativo")
        header.setObjectName("pageTitle")
        subtitle = QLabel("Centro de operaciones local con módulos independientes y una sola base de datos.")
        subtitle.setObjectName("muted")
        body.addWidget(header)
        body.addWidget(subtitle)
        body.addSpacing(2)
        body.addWidget(self._hero_panel())

        local_eyebrow = QLabel("TU NEGOCIO  ·  LOCAL")
        local_eyebrow.setObjectName("eyebrow")
        body.addWidget(local_eyebrow)
        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(10)
        body.addLayout(self.metrics_grid)

        body.addWidget(self._vps_metrics_panel())
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
        self.vps_grid.setSpacing(10)
        outer.addLayout(self.vps_grid)

        # Inicializa con cards en estado vacío
        self._vps_cards_data = [
            ("ventas_web_hoy",     "Ventas web · hoy"),
            ("ventas_web_semana",  "Ventas web · 7 días"),
            ("pedidos_pendientes", "Pedidos pendientes"),
            ("productos_total",    "Productos en VPS"),
        ]
        self._vps_cards = {}
        for idx, (key, label) in enumerate(self._vps_cards_data):
            card = metric_card(label, "Sin datos", empty=True)
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

        # Actualizar cards
        formatters = {
            "ventas_web_hoy":     lambda v: f"${v.get('total', 0):,.0f}".replace(",", ".") + f" · {v.get('count', 0)}",
            "ventas_web_semana":  lambda v: f"${v.get('total', 0):,.0f}".replace(",", ".") + f" · {v.get('count', 0)}",
            "pedidos_pendientes": lambda v: str(v),
            "productos_total":    lambda v: str(v),
        }
        for key, _label in self._vps_cards_data:
            value = stats.get(key, 0)
            if value is None:
                self._update_card_value(self._vps_cards[key], "Sin datos", empty=True)
            else:
                self._update_card_value(self._vps_cards[key], formatters[key](value), empty=False)

        from datetime import datetime
        self.vps_status.setText(
            f"Actualizado · {datetime.now().strftime('%H:%M:%S')}"
        )

    def _set_vps_cards_offline(self, reason: str):
        for key, _label in self._vps_cards_data:
            self._update_card_value(self._vps_cards[key], "Sin datos", empty=True)
        self.vps_status.setText(reason)

    def _update_card_value(self, card_frame, new_text: str, empty: bool = False):
        """Actualiza el QLabel#metricValue del card y marca su estado."""
        for child in card_frame.findChildren(QLabel):
            if child.objectName() == "metricValue":
                child.setText(new_text)
                child.setProperty("state", "empty" if empty else "")
                child.style().unpolish(child)
                child.style().polish(child)
                return

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
        clear_layout(self.metrics_grid)
        metrics = self.store.dashboard_metrics()
        cards = [
            ("Productos activos", str(metrics["products"])),
            ("Stock bajo", str(metrics["low_stock"])),
            ("Ventas hoy", f"${metrics['today_sales']:,.0f}"),
            ("Pendiente sync", str(metrics["pending_sync"])),
        ]
        for index, (label, value) in enumerate(cards):
            self.metrics_grid.addWidget(metric_card(label, value), 0, index)

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
        layout.setSpacing(10)
        outer.addLayout(layout)

        modules = [
            ("▦", "Productos",   "CRUD local y precios",  "F2", "products"),
            ("◰", "POS",         "Venta directa offline", "F3", "pos"),
            ("⊞", "Inventario",  "Entradas y salidas",    "F4", "inventory"),
            ("$", "Ventas",      "Historial y recibos",   "F5", "sales"),
            ("◉", "Usuarios",    "CRUD local",            "F6", "users"),
            ("⟳", "Sync",        "Estado de la BD local", "F7", "sync"),
        ]
        for index, (icon, title, detail, shortcut, target) in enumerate(modules):
            card = self._module_card(icon, title, detail, shortcut, target)
            layout.addWidget(card, index // 3, index % 3)
        return frame

    def _module_card(self, icon, title, detail, shortcut, target):
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
    def __init__(self, store: LocalStore, on_changed):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
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
        form_layout = QGridLayout(form)
        form_layout.setContentsMargins(16, 16, 16, 16)
        form_layout.setSpacing(10)

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
            form_layout.addWidget(QLabel(label), index // 3 * 2, index % 3)
            form_layout.addWidget(widget, index // 3 * 2 + 1, index % 3)
        # Imagen ocupa toda la fila siguiente
        next_row = ((len(fields) - 1) // 3 + 1) * 2
        form_layout.addWidget(QLabel("Imagen"), next_row, 0)
        form_layout.addLayout(image_row, next_row + 1, 0, 1, 3)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("Guardar producto")
        self.save_btn.clicked.connect(self._save)
        self.new_btn = QPushButton("Nuevo")
        self.new_btn.setObjectName("secondaryAction")
        self.new_btn.clicked.connect(self._clear_form)
        self.delete_btn = QPushButton("Desactivar")
        self.delete_btn.setObjectName("dangerAction")
        self.delete_btn.clicked.connect(self._delete)
        actions.addWidget(self.save_btn)
        actions.addWidget(self.new_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        self.live_badge = QLabel("●  VPS EN VIVO · SOLO LECTURA")
        self.live_badge.setObjectName("pageBadge")
        self.live_badge.setProperty("state", "warning")
        self.live_badge.setVisible(False)
        actions.addWidget(self.live_badge)

        layout.addWidget(form)
        layout.addLayout(actions)

        # Buscador
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_label = QLabel("Buscar:")
        search_label.setObjectName("muted")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filtrar por SKU, barcode, nombre o categoria")
        self.search.textChanged.connect(self._apply_filter)
        search_row.addWidget(search_label)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["ID", "SKU", "Barcode", "Nombre", "Categoria", "Stock", "Minimo", "Precio"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
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
                f"{product['price']:.2f}",
            ]
            low_stock = int(product["stock"]) <= int(product["min_stock"])
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, product["id"])
                if low_stock and col_index == 5:
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row_index, col_index, item)

    def _load_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item0 = self.table.item(row, 0)
        if item0 is None:
            return
        self.selected_id = int(item0.text())
        self.sku.setText(self.table.item(row, 1).text())
        self.barcode.setText(self.table.item(row, 2).text())
        self.name.setText(self.table.item(row, 3).text())
        self.category.setText(self.table.item(row, 4).text())
        self.stock.setValue(int(self.table.item(row, 5).text()))
        self.min_stock.setValue(int(self.table.item(row, 6).text()))
        self.price.setValue(float(self.table.item(row, 7).text()))
        # Cargar imagen y genero del producto seleccionado
        prod = next((p for p in self._all_products if p["id"] == self.selected_id), None)
        if prod:
            self.image_path.setText(prod.get("image_path") or "")
            gid = prod.get("genero_id")
            if gid is not None:
                idx = self.genero_combo.findData(gid)
                if idx >= 0:
                    self.genero_combo.setCurrentIndex(idx)
                else:
                    self.genero_combo.setCurrentIndex(0)
            else:
                self.genero_combo.setCurrentIndex(0)

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
    def __init__(self, store: LocalStore, on_changed):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
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

        focused = QApplication.focusWidget()
        is_target_input = bool(focused and focused.objectName() == "barcodeInput")
        if is_target_input:
            return False

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
    def __init__(self, store: LocalStore, on_changed, scanner: "ScannerEngine | None" = None, brand_callback=None):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
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

        panel = QFrame()
        panel.setObjectName("sectionPanel")
        form = QGridLayout(panel)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self.product = QComboBox()
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setValue(1)
        add = QPushButton("Agregar")
        add.clicked.connect(self._add_item)
        sell = QPushButton("Finalizar venta")
        sell.setObjectName("primaryAction")
        sell.clicked.connect(self._finish_sale)
        clear = QPushButton("Limpiar")
        clear.setObjectName("secondaryAction")
        clear.clicked.connect(self._clear_cart)

        form.addWidget(QLabel("Producto (busqueda manual)"), 0, 0)
        form.addWidget(self.product, 1, 0)
        form.addWidget(QLabel("Cantidad"), 0, 1)
        form.addWidget(self.quantity, 1, 1)
        form.addWidget(add, 1, 2)
        form.addWidget(sell, 1, 3)
        form.addWidget(clear, 1, 4)
        layout.addWidget(panel)

        self.total_label = QLabel("Total: $0")
        self.total_label.setObjectName("metricValue")
        layout.addWidget(self.total_label)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Producto", "Cantidad", "Precio", "Subtotal", "", "ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnHidden(5, True)
        layout.addWidget(self.table)

        self.feedback = QLabel("Listo. Conecta tu pistola USB y escanea, o escribe el codigo arriba.")
        self.feedback.setObjectName("muted")
        layout.addWidget(self.feedback)

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
        if int(product["stock"]) - already_in_cart <= 0:
            QApplication.beep()
            self._notify(f"Sin stock disponible para {product['name']}.", error=True)
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
        if existing:
            existing["quantity"] += quantity
        else:
            self.cart.append(
                {
                    "product_id": product["id"],
                    "name": product["name"],
                    "quantity": quantity,
                    "unit_price": float(product["price"]),
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
        self.table.setRowCount(len(self.cart))
        total = 0.0
        for row_index, item in enumerate(self.cart):
            subtotal = item["quantity"] * item["unit_price"]
            total += subtotal
            values = [
                item["name"],
                item["quantity"],
                f"${item['unit_price']:,.0f}",
                f"${subtotal:,.0f}",
                "",
                item["product_id"],
            ]
            for col_index, value in enumerate(values):
                if col_index == 4:
                    btn = QPushButton("Quitar")
                    btn.setObjectName("inlineDanger")
                    btn.clicked.connect(lambda _checked=False, pid=item["product_id"]: self._remove_item(pid))
                    self.table.setCellWidget(row_index, col_index, btn)
                else:
                    self.table.setItem(row_index, col_index, QTableWidgetItem(str(value)))
        self.total_label.setText(f"Total: ${total:,.0f}")

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
    def __init__(self, store: LocalStore, on_changed):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
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
        test_btn = QPushButton("Probar conexion")
        test_btn.setObjectName("secondaryAction")
        test_btn.clicked.connect(self._test_connection)
        sync_now_btn = QPushButton("Sincronizar ahora")
        sync_now_btn.setObjectName("primaryAction")
        sync_now_btn.clicked.connect(self._sync_now_clicked)
        actions_row.addWidget(save_btn)
        actions_row.addWidget(test_btn)
        actions_row.addWidget(sync_now_btn)
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
        mark_synced = QPushButton("Marcar outbox como sincronizada")
        mark_synced.setObjectName("secondaryAction")
        mark_synced.clicked.connect(self._mark_synced)
        clear_demo = QPushButton("Limpiar datos demo (productos/ventas)")
        clear_demo.setObjectName("dangerAction")
        clear_demo.clicked.connect(self._clear_demo)
        actions.addWidget(refresh)
        actions.addWidget(mark_synced)
        actions.addWidget(clear_demo)
        actions.addStretch(1)
        layout.addLayout(actions)

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
        try:
            stats = self._do_sync()
        except (ValueError, SyncError) as exc:
            self._record_status(f"error: {exc}")
            self._set_status_text(f"Sync fallo: {exc}", error=True)
            return
        msg = self._format_stats(stats)
        suffix = ""
        if stats["pushed_errors"]:
            suffix = f" ({stats['pushed_errors']} errores)"
        self._record_status("ok" + suffix)
        self._set_status_text(msg, error=stats["pushed_errors"] > 0)
        self.refresh()
        self.on_changed()

    def _sync_now_silent(self):
        """Variante para timer automatico: no muestra dialogos."""
        try:
            stats = self._do_sync()
            suffix = f" ({stats['pushed_errors']} errores)" if stats["pushed_errors"] else ""
            self._record_status("ok" + suffix)
            self.sync_status.setText(self._format_status() + " | " + self._format_stats(stats))
            self.refresh()
            self.on_changed()
        except (ValueError, SyncError) as exc:
            self._record_status(f"error: {str(exc)[:120]}")
            self.sync_status.setText(self._format_status())
            self.sync_status.setStyleSheet("color: #b42318;")

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
                 "pulled_sales_web": 0, "pulled_inventory_log": 0,
                 "pushed_applied": 0, "pushed_skipped": 0, "pushed_stale": 0,
                 "pushed_errors": 0}

        # --- Pull generos ---
        try:
            r = client.pull_generos(since=self._sync_state.get("cursor_generos") or None)
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
                    else:
                        stats["pushed_errors"] += 1
                self.store.mark_outbox_synced(done_ids)
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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.label = QLabel(label)
        self.label.setMinimumWidth(220)
        self.swatch = QFrame()
        self.swatch.setObjectName("colorSwatch")
        self.input = QLineEdit(value)
        self.input.setMaxLength(9)
        self.input.textChanged.connect(self._on_text_change)
        self.input.editingFinished.connect(self._notify)
        button = QPushButton("Elegir...")
        button.setObjectName("secondaryAction")
        button.clicked.connect(self._open_picker)

        layout.addWidget(self.label)
        layout.addWidget(self.swatch)
        layout.addWidget(self.input, 1)
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

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Configuracion / Marca")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Personaliza colores, datos y logo para este cliente. Los cambios se "
            "aplican al instante y se guardan en branding.json al pulsar 'Guardar y aplicar'."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Datos de empresa
        empresa_panel = QFrame()
        empresa_panel.setObjectName("sectionPanel")
        empresa_form = QFormLayout(empresa_panel)
        empresa_form.setContentsMargins(16, 16, 16, 16)
        empresa_form.setSpacing(10)
        empresa_title = QLabel("Datos de la empresa")
        empresa_title.setObjectName("eyebrow")
        empresa_form.addRow(empresa_title)
        for key, label in EMPRESA_FIELDS:
            input_field = QLineEdit(self._state["empresa"].get(key, ""))
            self._empresa_inputs[key] = input_field
            empresa_form.addRow(label + ":", input_field)

        # Logo
        logo_row = QHBoxLayout()
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
        empresa_form.addRow("Logo:", logo_row)

        layout.addWidget(empresa_panel)

        # Colores
        color_panel = QFrame()
        color_panel.setObjectName("sectionPanel")
        color_layout = QVBoxLayout(color_panel)
        color_layout.setContentsMargins(16, 16, 16, 16)
        color_layout.setSpacing(8)
        color_title = QLabel("Colores de marca")
        color_title.setObjectName("eyebrow")
        color_layout.addWidget(color_title)

        for key, label in COLOR_FIELDS:
            row = ColorPickerRow(
                key,
                label,
                self._state["colores"].get(key, branding_mod.DEFAULTS["colores"][key]),
                on_change=None,
            )
            self._color_rows[key] = row
            color_layout.addWidget(row)

        layout.addWidget(color_panel)

        # Acciones
        actions = QHBoxLayout()
        save_btn = QPushButton("Guardar y aplicar")
        save_btn.setObjectName("primaryAction")
        save_btn.clicked.connect(self._save_apply)
        preview_btn = QPushButton("Previsualizar (sin guardar)")
        preview_btn.setObjectName("secondaryAction")
        preview_btn.clicked.connect(self._preview)
        export_btn = QPushButton("Exportar JSON...")
        export_btn.setObjectName("secondaryAction")
        export_btn.clicked.connect(self._export)
        import_btn = QPushButton("Importar JSON...")
        import_btn.setObjectName("secondaryAction")
        import_btn.clicked.connect(self._import)
        reset_btn = QPushButton("Restaurar predeterminados")
        reset_btn.setObjectName("dangerAction")
        reset_btn.clicked.connect(self._reset)
        actions.addWidget(save_btn)
        actions.addWidget(preview_btn)
        actions.addWidget(export_btn)
        actions.addWidget(import_btn)
        actions.addStretch(1)
        actions.addWidget(reset_btn)
        layout.addLayout(actions)

        self.feedback = QLabel("Listo.")
        self.feedback.setObjectName("muted")
        layout.addWidget(self.feedback)
        layout.addStretch(1)

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


# =============================================================================
# Shell
# =============================================================================
class DesktopShell(QMainWindow):
    # (key, icono unicode, etiqueta, atajo, sección)
    NAV_ITEMS = [
        ("dashboard", "▦", "Dashboard",       "F1", "Operación"),
        ("pos",       "⌧", "POS",             "F3", "Operación"),
        ("sales",     "$", "Ventas",          "F5", "Operación"),
        ("products",  "▤", "Productos",       "F2", "Datos"),
        ("inventory", "⊞", "Inventario",      "F4", "Datos"),
        ("users",     "◯", "Usuarios",        "F6", "Datos"),
        ("sync",      "⟳", "Sincronización",  "F7", "Sistema"),
        ("config",    "✎", "Configuración",   "F8", "Sistema"),
    ]

    def __init__(self):
        super().__init__()
        self.store = LocalStore()
        self.user = None
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

        # Nav items agrupados por sección
        self.nav_buttons = {}
        last_section = None
        for key, icon, label, shortcut, section in self.NAV_ITEMS:
            if section != last_section:
                section_label = QLabel(section)
                section_label.setObjectName("sidebarSection")
                side_layout.addWidget(section_label)
                last_section = section
            button = QPushButton(f"  {icon}    {label}")
            button.setObjectName("navButton")
            button.setToolTip(f"{label} ({shortcut})")
            button.clicked.connect(lambda checked=False, name=key: self._show_section(name))
            self.nav_buttons[key] = button
            side_layout.addWidget(button)

        side_layout.addStretch(1)
        logout = QPushButton("  ↪    Cerrar sesión")
        logout.setObjectName("logoutButton")
        logout.clicked.connect(self._logout)
        side_layout.addWidget(logout)

        self.content_stack = QStackedWidget()
        self.pages = {
            "dashboard": DashboardPage(self.store, self._show_section),
            "products": ProductsPage(self.store, self._refresh_shared_pages),
            "pos": PosPage(self.store, self._refresh_shared_pages, scanner=self.scanner, brand_callback=self._get_branding),
            "inventory": InventoryPage(self.store, self._refresh_shared_pages),
            "sales": SalesPage(self.store, brand_callback=self._get_branding),
            "users": UsersPage(self.store, self._refresh_shared_pages),
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
        self.stack.setCurrentWidget(self.app_view)
        self._show_section("dashboard")
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

    def _show_section(self, name):
        if self.stack.currentWidget() is not self.app_view:
            return
        if name not in self.pages:
            return
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
        for name in ("dashboard", "products", "pos", "inventory", "sales", "users", "sync"):
            self._refresh_page(name)

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
        """Llama /api/v1/sync/version. Si hay versión nueva, muestra diálogo.

        Best-effort: si no hay red, ignora.
        """
        if not install_conf.auto_update_enabled(self._install_conf):
            return
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
        if not latest or _version_cmp(latest, APP_VERSION) <= 0:
            return  # ya estamos al día
        if latest == self._update_skip_version:
            return  # el usuario eligió saltar esta versión
        self._show_update_dialog(manifest)

    def _show_update_dialog(self, manifest):
        latest = manifest.get("latest", "?")
        notes = (manifest.get("release_notes") or "").strip()
        download_url = manifest.get("download_url") or ""
        text = (
            f"Hay una versión nueva del POS Desktop disponible.\n\n"
            f"Versión actual: {APP_VERSION}\n"
            f"Versión nueva:  {latest}\n\n"
            f"{notes if notes else 'Notas de versión no disponibles.'}\n\n"
            f"¿Descargar e instalar ahora?"
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Actualización disponible")
        msg.setText(text)
        msg.setIcon(QMessageBox.Icon.Information)
        btn_download = msg.addButton("Descargar e instalar", QMessageBox.ButtonRole.AcceptRole)
        btn_later = msg.addButton("Más tarde", QMessageBox.ButtonRole.RejectRole)
        btn_skip = msg.addButton("Saltar esta versión", QMessageBox.ButtonRole.DestructiveRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is btn_skip:
            sync_cfg.update(self._app_dir, skip_version=latest)
            self._update_skip_version = latest
        elif clicked is btn_download and download_url:
            self._download_and_launch_installer(download_url)
        # btn_later → no hacer nada, vuelve a aparecer al próximo arranque

    def _download_and_launch_installer(self, url):
        """Descarga el instalador a temp y lo ejecuta. Cierra esta app para
        permitir reemplazo de archivos."""
        import os
        import subprocess
        import tempfile
        state = sync_cfg.load(self._app_dir)
        client = SyncClient(state.get("base_url") or self._install_conf.get("SERVER_URL", ""),
                            state.get("api_key") or "x")
        dest = Path(tempfile.gettempdir()) / "CyberShopSetup_update.exe"
        try:
            client.download_file(url, dest)
        except SyncError as exc:
            QMessageBox.warning(self, "Descarga fallida", f"No se pudo descargar la actualización:\n{exc}")
            return
        try:
            subprocess.Popen([str(dest)], close_fds=True)
        except OSError as exc:
            QMessageBox.warning(self, "Error", f"No se pudo iniciar el instalador:\n{exc}")
            return
        QApplication.quit()


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


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CyberShop.Desktop")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setWindowIcon(_default_app_icon())
    quit_action = QAction("Salir")
    quit_action.triggered.connect(app.quit)
    window = DesktopShell()
    window.addAction(quit_action)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
