import shutil
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
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
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import branding as branding_mod
from local_store import LocalStore, app_data_dir


APP_VERSION = "0.3.0"
ROLES = ["Administrador", "Cajero", "Inventario"]


# =============================================================================
# Stylesheet template (con placeholders $primario, etc.)
# =============================================================================
QSS_TEMPLATE = """
QWidget {
    font-family: "Segoe UI", Tahoma, sans-serif;
    font-size: 14px;
    color: #333333;
    background: $fondo;
}
QFrame#sidebar {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 $sidebar_inicio,
        stop:1 $sidebar_fin
    );
}
QLabel#brand {
    color: #ffffff;
    font-size: 22px;
    font-weight: 800;
}
QLabel#offlineBadge {
    color: #ffffff;
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 10px;
    padding: 7px 9px;
    font-size: 12px;
    font-weight: 700;
}
QFrame#userCard {
    background: rgba(255,255,255,0.10);
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 12px;
}
QLabel#userName {
    color: #ffffff;
    font-size: 14px;
    font-weight: 800;
}
QLabel#userRole {
    color: rgba(255,255,255,0.75);
    font-size: 11px;
    font-weight: 600;
}
QPushButton#navButton, QPushButton#logoutButton {
    text-align: left;
    border: 0;
    border-radius: 12px;
    padding: 12px 14px;
    background: transparent;
    color: rgba(255,255,255,0.82);
    font-weight: 650;
}
QPushButton#navButton:hover, QPushButton#navButton[active="true"] {
    background: rgba(255,255,255,0.12);
    color: #ffffff;
    border-left: 3px solid $acento;
}
QPushButton#logoutButton {
    color: #ffd8d3;
}
QFrame#footer {
    background: #ffffff;
    border-top: 1px solid #e3e6f0;
}
QLabel#footerText {
    color: #5a5c69;
    font-size: 11px;
}
QLabel#pageTitle {
    font-size: 28px;
    font-weight: 800;
    color: $primario_oscuro;
}
QLabel#muted {
    color: #5a5c69;
    line-height: 1.5;
}
QLabel#eyebrow {
    color: $primario;
    font-size: 12px;
    font-weight: 850;
    text-transform: uppercase;
}
QLabel#eyebrowMuted {
    color: #5a5c69;
    font-size: 11px;
    font-weight: 800;
    text-transform: uppercase;
}
QLabel#heroTitle {
    color: $primario_oscuro;
    font-size: 22px;
    font-weight: 850;
}
QLabel#highlightText {
    color: $primario_oscuro;
    font-size: 14px;
    font-weight: 800;
}
QFrame#metricCard, QFrame#sectionPanel, QFrame#loginCard {
    background: #ffffff;
    border: 1px solid rgba($primario_rgb, 0.08);
    border-radius: 18px;
}
QFrame#scannerPanel {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 $primario,
        stop:1 $primario_oscuro
    );
    border-radius: 16px;
    border: 1px solid rgba($acento_secundario_rgb, 0.45);
}
QFrame#scannerPanel QLabel {
    color: #ffffff;
}
QLabel#scannerIcon {
    color: $acento_secundario;
    font-family: "Consolas", monospace;
    font-size: 24px;
    font-weight: 900;
    letter-spacing: 1px;
}
QLineEdit#barcodeInput {
    background: rgba(255,255,255,0.96);
    border: 2px solid $acento_secundario;
    border-radius: 12px;
    padding: 6px 14px;
    font-size: 16px;
    font-weight: 750;
    color: $primario_oscuro;
    min-height: 40px;
}
QLineEdit#barcodeInput:focus {
    border-color: $acento;
    background: #ffffff;
}
QLabel#scannerBadge {
    background: rgba(160, 160, 160, 0.18);
    color: #5a5c69;
    border: 1px solid rgba(160, 160, 160, 0.5);
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 12px;
    font-weight: 800;
    text-transform: uppercase;
}
QLabel#scannerBadge[state="on"] {
    background: rgba($acento_secundario_rgb, 0.18);
    color: #5a7a14;
    border: 1px solid rgba($acento_secundario_rgb, 0.5);
}
QLabel#scannerBadge[state="off"] {
    background: rgba($peligro_rgb, 0.10);
    color: $peligro;
    border: 1px solid rgba($peligro_rgb, 0.30);
}
QPushButton#primaryAction {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $acento_secundario,
        stop:1 $acento_secundario
    );
    color: $primario_oscuro;
    font-weight: 850;
}
QPushButton#primaryAction:hover {
    background: $acento_secundario;
    color: #ffffff;
}
QPushButton#secondaryAction {
    background: transparent;
    border: 1px solid rgba($primario_rgb, 0.30);
    color: $primario;
}
QPushButton#secondaryAction:hover {
    background: rgba($primario_rgb, 0.08);
}
QPushButton#dangerAction {
    background: transparent;
    border: 1px solid rgba($peligro_rgb, 0.40);
    color: $peligro;
}
QPushButton#dangerAction:hover {
    background: rgba($peligro_rgb, 0.08);
}
QPushButton#inlineDanger {
    background: rgba($peligro_rgb, 0.08);
    color: $peligro;
    border: 1px solid rgba($peligro_rgb, 0.30);
    border-radius: 8px;
    min-height: 28px;
    padding: 0 10px;
    font-weight: 700;
}
QPushButton#inlineDanger:hover {
    background: rgba($peligro_rgb, 0.16);
}
QFrame#dashboardHero {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(255,255,255,0.98),
        stop:1 rgba($primario_rgb, 0.04)
    );
    border: 1px solid rgba($primario_rgb, 0.10);
    border-radius: 22px;
}
QFrame#heroHighlight {
    background: rgba($primario_rgb, 0.06);
    border: 1px solid rgba($primario_rgb, 0.10);
    border-radius: 16px;
}
QLabel#metricValue {
    font-size: 24px;
    font-weight: 850;
    color: $primario;
}
QPushButton#moduleButton {
    min-height: 82px;
    text-align: left;
    border: 1px solid rgba($primario_rgb, 0.08);
    border-radius: 18px;
    padding: 14px;
    background: rgba(255,255,255,0.94);
    color: $primario_oscuro;
    font-weight: 750;
}
QPushButton#moduleButton:hover {
    border-color: rgba($primario_rgb, 0.18);
    background: rgba($primario_rgb, 0.05);
    color: $primario;
}
QWidget#loginRoot {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $fondo,
        stop:0.52 rgba($primario_rgb, 0.06),
        stop:1 $fondo
    );
}
QLabel#loginTitle {
    font-size: 24px;
    font-weight: 850;
    color: $primario_oscuro;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 38px;
    border: 1px solid #e3e6f0;
    border-radius: 10px;
    padding: 0 10px;
    background: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: $primario;
}
QPushButton {
    min-height: 40px;
    border: 0;
    border-radius: 12px;
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $primario,
        stop:1 $primario
    );
    color: #ffffff;
    font-weight: 750;
    padding: 0 14px;
}
QPushButton:hover {
    background: $primario_oscuro;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid rgba($primario_rgb, 0.08);
    gridline-color: #e7edf4;
    alternate-background-color: rgba($primario_rgb, 0.025);
}
QHeaderView::section {
    background: rgba($primario_rgb, 0.06);
    color: #5a5c69;
    border: 0;
    padding: 9px;
    font-weight: 750;
}
QFrame#colorSwatch {
    border: 1px solid rgba(0,0,0,0.12);
    border-radius: 6px;
    min-width: 26px;
    min-height: 26px;
    max-width: 26px;
    max-height: 26px;
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
        self.card.setFixedWidth(430)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setVisible(False)
        self.title = QLabel()
        self.title.setObjectName("loginTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        self.subtitle.setWordWrap(True)

        self.email = QLineEdit("admin@cybershop.local")
        self.email.setPlaceholderText("Correo")
        self.password = QLineEdit("admin123")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Contrasena")
        self.status = QLabel("Usuario inicial: admin@cybershop.local / admin123")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        button = QPushButton("Entrar")
        button.clicked.connect(self._login)
        self.password.returnPressed.connect(self._login)
        self.email.returnPressed.connect(self._login)

        card_layout.addWidget(self.logo_label)
        card_layout.addWidget(self.title)
        card_layout.addWidget(self.subtitle)
        card_layout.addSpacing(10)
        card_layout.addWidget(QLabel("Correo"))
        card_layout.addWidget(self.email)
        card_layout.addWidget(QLabel("Contrasena"))
        card_layout.addWidget(self.password)
        card_layout.addWidget(button)
        card_layout.addWidget(self.status)

        layout.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

    def apply_branding(self):
        branding = self._brand_callback()
        empresa = branding["empresa"]
        self.title.setText(f"{empresa['nombre']} Desktop")
        self.subtitle.setText(empresa.get("slogan") or "")
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
        user = self.store.authenticate(self.email.text().strip(), self.password.text())
        if not user:
            self.status.setText("Credenciales locales invalidas.")
            self.status.setStyleSheet("color: #b42318;")
            return
        self.status.setStyleSheet("")
        self.on_login(user)


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
        body.setContentsMargins(28, 24, 28, 24)
        body.setSpacing(16)

        header = QLabel("Menu administrativo")
        header.setObjectName("pageTitle")
        subtitle = QLabel("Centro de operaciones local con modulos independientes y una sola base de datos.")
        subtitle.setObjectName("muted")
        body.addWidget(header)
        body.addWidget(subtitle)
        body.addWidget(self._hero_panel())

        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(12)
        body.addLayout(self.metrics_grid)
        body.addWidget(self._module_grid())
        body.addStretch(1)
        self.refresh()

    def _hero_panel(self):
        hero = QFrame()
        hero.setObjectName("dashboardHero")
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(18)

        copy = QVBoxLayout()
        eyebrow = QLabel("Flujo diario recomendado")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Empieza por POS e inventario, luego revisa sincronizacion.")
        title.setObjectName("heroTitle")
        detail = QLabel(
            "Este tablero mantiene las operaciones principales disponibles aunque no exista conexion a internet."
            " Atajos: F1 Dashboard - F2 Productos - F3 POS - F4 Inventario - F5 Ventas - F6 Usuarios - F7 Sync."
        )
        detail.setObjectName("muted")
        detail.setWordWrap(True)
        copy.addWidget(eyebrow)
        copy.addWidget(title)
        copy.addWidget(detail)

        highlights = QVBoxLayout()
        for label, value in [
            ("Operacion diaria", "POS e inventario"),
            ("Catalogo", "Productos y precios"),
            ("Conexion", "Cambios pendientes"),
        ]:
            item = QFrame()
            item.setObjectName("heroHighlight")
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(14, 10, 14, 10)
            small = QLabel(label)
            small.setObjectName("eyebrowMuted")
            strong = QLabel(value)
            strong.setObjectName("highlightText")
            item_layout.addWidget(small)
            item_layout.addWidget(strong)
            highlights.addWidget(item)

        layout.addLayout(copy, stretch=2)
        layout.addLayout(highlights, stretch=1)
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
            self.metrics_grid.addWidget(metric_card(label, value), index // 2, index % 2)

    def _module_grid(self):
        frame = QFrame()
        frame.setObjectName("sectionPanel")
        layout = QGridLayout(frame)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        modules = [
            ("Productos", "CRUD local y precios", "products"),
            ("POS", "Venta directa offline", "pos"),
            ("Inventario", "Entradas y salidas", "inventory"),
            ("Ventas", "Historial y recibos", "sales"),
            ("Usuarios", "CRUD local", "users"),
            ("Sync", "Estado de la BD local", "sync"),
        ]
        for index, (title, detail, target) in enumerate(modules):
            button = QPushButton(f"{title}\n{detail}")
            button.setObjectName("moduleButton")
            button.clicked.connect(lambda checked=False, name=target: self.open_section(name))
            layout.addWidget(button, index // 3, index % 3)
        return frame


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

        title = QLabel("Productos")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

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

        fields = [
            ("SKU", self.sku),
            ("Codigo de barras", self.barcode),
            ("Nombre", self.name),
            ("Categoria", self.category),
            ("Stock", self.stock),
            ("Minimo", self.min_stock),
            ("Precio", self.price),
        ]
        for index, (label, widget) in enumerate(fields):
            form_layout.addWidget(QLabel(label), index // 3 * 2, index % 3)
            form_layout.addWidget(widget, index // 3 * 2 + 1, index % 3)

        actions = QHBoxLayout()
        save = QPushButton("Guardar producto")
        save.clicked.connect(self._save)
        new = QPushButton("Nuevo")
        new.setObjectName("secondaryAction")
        new.clicked.connect(self._clear_form)
        delete = QPushButton("Desactivar")
        delete.setObjectName("dangerAction")
        delete.clicked.connect(self._delete)
        actions.addWidget(save)
        actions.addWidget(new)
        actions.addWidget(delete)
        actions.addStretch(1)

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
        self._apply_filter()

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
        detail = self.store.sale_detail(sale.get("sale_id") or self._latest_sale_id())
        self._show_receipt(detail or {"sale": sale, "items": self.cart})
        self._clear_cart()
        self.refresh()
        self.on_changed()

    def _latest_sale_id(self):
        sales = self.store.sales(limit=1)
        return sales[0]["id"] if sales else None

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

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Recibo", "Fecha", "Items", "Total"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemDoubleClicked.connect(self._open_detail)
        layout.addWidget(self.table)

        hint = QLabel("Doble click sobre una fila para ver el detalle del recibo.")
        hint.setObjectName("muted")
        layout.addWidget(hint)
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

        title = QLabel("Usuarios locales")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        info = QLabel(
            "Solo se usan dentro de esta instalacion. Para sincronizacion con la web se necesita el modulo de sync."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        layout.addWidget(info)

        actions = QHBoxLayout()
        new_btn = QPushButton("Nuevo usuario")
        new_btn.clicked.connect(self._create)
        edit_btn = QPushButton("Editar seleccionado")
        edit_btn.setObjectName("secondaryAction")
        edit_btn.clicked.connect(self._edit_selected)
        deactivate_btn = QPushButton("Desactivar seleccionado")
        deactivate_btn.setObjectName("dangerAction")
        deactivate_btn.clicked.connect(self._deactivate_selected)
        actions.addWidget(new_btn)
        actions.addWidget(edit_btn)
        actions.addWidget(deactivate_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Email", "Nombre", "Rol", "Activo"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.refresh()

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
    def __init__(self, store: LocalStore, on_changed):
        super().__init__()
        self.store = store
        self.on_changed = on_changed
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Sincronizacion / estado local")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Esta instalacion guarda todo en una base SQLite local. La cola outbox "
            "queda preparada para enviar al servidor cuando se habilite la integracion."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

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

    def _clear_demo(self):
        confirm = QMessageBox.question(
            self,
            "Limpiar datos",
            "Borra TODOS los productos, ventas, movimientos y la outbox. Los usuarios se conservan. Continuar?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.clear_demo_data()
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
    NAV_ITEMS = [
        ("dashboard", "Dashboard", "F1"),
        ("products", "Productos", "F2"),
        ("pos", "POS", "F3"),
        ("inventory", "Inventario", "F4"),
        ("sales", "Ventas", "F5"),
        ("users", "Usuarios", "F6"),
        ("sync", "Sincronizacion", "F7"),
        ("config", "Configuracion", "F8"),
    ]

    def __init__(self):
        super().__init__()
        self.store = LocalStore()
        self.user = None
        self._app_dir = app_data_dir()
        self.branding = branding_mod.load_branding(self._app_dir)
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
        sidebar.setFixedWidth(248)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 18, 18, 18)
        side_layout.setSpacing(8)

        self.sidebar_logo = QLabel()
        self.sidebar_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sidebar_logo.setVisible(False)
        side_layout.addWidget(self.sidebar_logo)

        self.brand_label = QLabel("CyberShop")
        self.brand_label.setObjectName("brand")
        mode = QLabel("Modo local offline")
        mode.setObjectName("offlineBadge")
        side_layout.addWidget(self.brand_label)
        side_layout.addWidget(mode)

        self.user_card = QFrame()
        self.user_card.setObjectName("userCard")
        user_layout = QVBoxLayout(self.user_card)
        user_layout.setContentsMargins(12, 10, 12, 10)
        self.user_name_label = QLabel("-")
        self.user_name_label.setObjectName("userName")
        self.user_role_label = QLabel("-")
        self.user_role_label.setObjectName("userRole")
        user_layout.addWidget(self.user_name_label)
        user_layout.addWidget(self.user_role_label)
        side_layout.addWidget(self.user_card)
        side_layout.addSpacing(10)

        self.nav_buttons = {}
        for key, label, shortcut in self.NAV_ITEMS:
            button = QPushButton(f"{label}    {shortcut}")
            button.setObjectName("navButton")
            button.clicked.connect(lambda checked=False, name=key: self._show_section(name))
            self.nav_buttons[key] = button
            side_layout.addWidget(button)

        side_layout.addStretch(1)
        logout = QPushButton("Cerrar sesion")
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
            "sync": SyncPage(self.store, self._refresh_shared_pages),
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
        for key, _label, shortcut in self.NAV_ITEMS:
            sc = QShortcut(QKeySequence(shortcut), self)
            sc.activated.connect(lambda name=key: self._show_section(name))

    def _login_success(self, user):
        self.user = user
        self.user_name_label.setText(user.name)
        self.user_role_label.setText(f"{user.role} - {user.email}")
        info = self.store.db_info()
        self.footer_right.setText(info["path"])
        self.stack.setCurrentWidget(self.app_view)
        self._show_section("dashboard")

    def _logout(self):
        confirm = QMessageBox.question(
            self,
            "Cerrar sesion",
            "Esta seguro? Las ventas en curso del POS se descartaran.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if isinstance(self.pages.get("pos"), PosPage):
            self.pages["pos"]._clear_cart()
        self.user = None
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
        if hasattr(self, "sidebar_logo"):
            logo_path = empresa.get("logo_path") or ""
            if logo_path and Path(logo_path).is_file():
                pix = QPixmap(logo_path)
                if not pix.isNull():
                    pix = pix.scaledToHeight(56, Qt.TransformationMode.SmoothTransformation)
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


# =============================================================================
# Helpers
# =============================================================================
def metric_card(label, value):
    frame = QFrame()
    frame.setObjectName("metricCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    value_label = QLabel(value)
    value_label.setObjectName("metricValue")
    label_widget = QLabel(label)
    label_widget.setObjectName("muted")
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


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.deleteLater()


def main():
    app = QApplication(sys.argv)
    quit_action = QAction("Salir")
    quit_action.triggered.connect(app.quit)
    window = DesktopShell()
    window.addAction(quit_action)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
