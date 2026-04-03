"""
PySide6 GUI for MTG binder scanner.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QObject,
    QThread,
    QTimer,
    Qt,
    QSize,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
)

import scanner_engine
import scryfall


GEMINI_TIER_MODEL_MAP = {
    "2.5": "gemini-2.5-flash",
    "3": "gemini-3-flash-preview",
}

NEW_COLLECTION_LABEL = "<New collection>"


class NumericTableItem(QTableWidgetItem):
    def __lt__(self, other):
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        if left is not None and right is not None:
            return float(left) < float(right)
        return super().__lt__(other)


class CardImagePopup(QWidget):
    """Frameless popup that displays a large card image on hover."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.image_label = QLabel()
        self.image_label.setStyleSheet(
            "border: 2px solid #355075; border-radius: 8px; background-color: #0f141b;"
        )
        layout.addWidget(self.image_label)
        self.hide()
    
    def show_at(self, pixmap: QPixmap, pos):
        """Show the popup with the given pixmap at the specified position."""
        if pixmap.isNull():
            self.hide()
            return
        
        # Scale to a readable size (around 250-300px tall)
        scaled = pixmap.scaledToHeight(300, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        self.resize(scaled.width() + 4, scaled.height() + 4)
        
        # Position the popup near the cursor
        self.move(int(pos.x() + 10), int(pos.y() + 10))
        self.show()


class HoverableTableWidget(QTableWidget):
    """Table widget that shows card images on hover."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.popup = CardImagePopup()
        self.get_card_func = None
        self.get_pixmap_func = None
        self.setMouseTracking(True)
    
    def set_hover_callbacks(self, get_card_func, get_pixmap_func):
        self.get_card_func = get_card_func
        self.get_pixmap_func = get_pixmap_func
    
    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.get_card_func or not self.get_pixmap_func:
            return
        
        item = self.itemAt(event.pos())
        if item is None:
            self.popup.hide()
            return
        
        row = self.row(item)
        card_info = self.get_card_func(row)
        if card_info:
            image_url = card_info.get("image_url")
            if image_url:
                pixmap = self.get_pixmap_func(image_url)
                self.popup.show_at(pixmap, event.globalPos())
            else:
                self.popup.hide()
        else:
            self.popup.hide()
    
    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.popup.hide()


class HoverableListWidget(QListWidget):
    """List widget that shows card images on hover."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.popup = CardImagePopup()
        self.get_card_func = None
        self.get_pixmap_func = None
        self.setMouseTracking(True)
    
    def set_hover_callbacks(self, get_card_func, get_pixmap_func):
        self.get_card_func = get_card_func
        self.get_pixmap_func = get_pixmap_func
    
    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.get_card_func or not self.get_pixmap_func:
            return
        
        item = self.itemAt(event.pos())
        if item is None:
            self.popup.hide()
            return
        
        row = self.row(item)
        card_info = self.get_card_func(row)
        if card_info:
            image_url = card_info.get("image_url")
            if image_url:
                pixmap = self.get_pixmap_func(image_url)
                self.popup.show_at(pixmap, event.globalPos())
            else:
                self.popup.hide()
        else:
            self.popup.hide()
    
    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.popup.hide()


class ResponsiveGridList(HoverableListWidget):
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class CardHoverFilter(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._anims: dict[int, QPropertyAnimation] = {}

    def eventFilter(self, watched, event):
        effect = watched.graphicsEffect()
        if not isinstance(effect, QGraphicsDropShadowEffect):
            return False

        event_type = event.type()
        if event_type == QEvent.Enter:
            self._animate_shadow(watched, effect, 38.0)
        elif event_type == QEvent.Leave:
            self._animate_shadow(watched, effect, 20.0)
        return False

    def _animate_shadow(self, watched, effect: QGraphicsDropShadowEffect, target_blur: float):
        key = id(watched)
        old = self._anims.get(key)
        if old is not None:
            old.stop()

        animation = QPropertyAnimation(effect, b"blurRadius", watched)
        animation.setDuration(180)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.setStartValue(effect.blurRadius())
        animation.setEndValue(target_blur)
        animation.start()
        self._anims[key] = animation



class ScanWorker(QThread):
    status = Signal(str)
    error = Signal(str, bool)
    card_identified = Signal(str, str, str, int, str, str, str, str, str, str)
    done = Signal(bool, str, str, object)

    def __init__(
        self,
        image_folder: str,
        output_path: str,
        provider: str,
        model: str | None,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.image_folder = image_folder
        self.output_path = output_path
        self.provider = provider
        self.model = model
        self.cancel_event = cancel_event

    def run(self):
        try:
            result = scanner_engine.scan_with_callbacks(
                image_folder=self.image_folder,
                output_path=self.output_path,
                provider=self.provider,
                vision_model=self.model,
                on_card_identified=self._on_card,
                on_status=self._on_status,
                on_error=self._on_error,
                cancel_event=self.cancel_event,
                persist_output=False,
                append_existing=False,
            )
            success = bool(result.get("success", True))
            message = str(result.get("message", ""))
            self.done.emit(success, message, self.output_path, result)
        except Exception as exc:
            self.error.emit(f"Fatal error: {exc}", True)
            self.done.emit(False, str(exc), self.output_path, {"success": False, "message": str(exc), "cards": {}, "detections": []})

    def _on_status(self, message: str):
        self.status.emit(message)

    def _on_error(self, message: str, debug: bool = False):
        self.error.emit(message, debug)

    def _on_card(
        self,
        name: str,
        set_code: str,
        number: str,
        count: int,
        match_method: str,
        finish: str = "unknown",
        name_confidence: str = "unknown",
        set_confidence: str = "unknown",
        finish_confidence: str = "unknown",
        image_url: str = "",
    ):
        self.card_identified.emit(
            name,
            set_code,
            number,
            count,
            match_method,
            finish,
            name_confidence,
            set_confidence,
            finish_confidence,
            image_url,
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MTG Binder Scanner")
        self.resize(1200, 800)

        self.scanning = False
        self.cancel_event: threading.Event | None = None
        self.worker: ScanWorker | None = None
        self.last_scan_output: Path | None = None
        self.current_collection_path: Path | None = None

        self.collection_rows: list[dict] = []
        self.pending_detections: list[dict] = []
        self.validation_rows: list[dict] = []
        self.has_unsaved_scan = False
        self._print_options_cache: dict[str, list[dict]] = {}
        self.thumb_memory_cache: dict[str, QPixmap] = {}
        self.app_data_dir = self._resolve_app_data_dir()
        self.collections_dir = self._resolve_collections_dir()
        self.thumb_cache_dir = self._resolve_thumbnail_cache_dir()
        self.log_file_path = self._resolve_log_file_path()

        self._card_hover_filter = CardHoverFilter(self)
        self._status_mode = "idle"
        self._status_tick = 0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(420)
        self._status_timer.timeout.connect(self._animate_status_badge)
        self._fade_anims: list[QPropertyAnimation] = []

        self._apply_styles()
        self._build_ui()

    def _apply_styles(self):
        self.setStyleSheet(
            """
            QWidget { background-color: #0f141b; color: #e8ecf1; font-size: 13px; }
            QFrame#Card { background-color: #171e27; border: 1px solid #2a3441; border-radius: 14px; }
            QFrame#HeroCard {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #182435, stop:1 #1b2f4b);
                border: 1px solid #355075;
                border-radius: 14px;
            }
            QLabel { background: transparent; }
            QLabel#HeroTitle { font-size: 18px; font-weight: 700; color: #f2f6ff; }
            QLabel#HeroSub { color: #c2d0ea; }
            QLabel#SectionTitle { font-size: 15px; font-weight: 700; color: #eef5ff; }
            QLabel#PanelTitle { font-size: 14px; font-weight: 700; color: #eaf2ff; }
            QLabel#FieldLabel { font-size: 11px; font-weight: 700; color: #9fb3cf; }
            QLabel#MetricValue { font-size: 22px; font-weight: 700; color: #57d18d; }
            QLabel#SummaryLabel { font-size: 12px; color: #b8c6da; }
            QLabel#StatusBadge {
                background-color: #23354b;
                border: 1px solid #416086;
                border-radius: 10px;
                padding: 5px 10px;
                font-weight: 600;
                color: #dbe9ff;
            }
            QPushButton {
                background-color: #2c3747;
                border: 1px solid #415268;
                color: #eef3ff;
                padding: 8px 14px;
                min-height: 18px;
                border-radius: 10px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #3a4b62; }
            QPushButton:pressed { background-color: #253140; }
            QPushButton:disabled { background-color: #212b37; color: #8ea2bc; border-color: #334254; }
            QPushButton#PrimaryButton { background-color: #2d7ff9; border-color: #2d7ff9; color: #ffffff; }
            QPushButton#PrimaryButton:hover { background-color: #3c8cff; }
            QPushButton#DangerButton { background-color: #c94545; border-color: #c94545; color: #ffffff; }
            QPushButton#DangerButton:hover { background-color: #db5757; }
            QPushButton#DangerButton:disabled {
                background-color: #2a313d;
                border-color: #39475b;
                color: #91a2b8;
            }
            QPushButton#GhostButton { background-color: transparent; border-color: #3b4556; }
            QPushButton#GhostButton:hover { background-color: #2a3340; }
            QPushButton#ToggleButton {
                background-color: #242b36;
                border-color: #3a4455;
                min-width: 88px;
            }
            QPushButton#ToggleButton:checked {
                background-color: #2d7ff9;
                border-color: #2d7ff9;
                color: white;
            }
            QListWidget#LiveFeed {
                background-color: #0d131a;
                border: 1px solid #2f3b4b;
                border-radius: 12px;
            }
            QListWidget#LiveFeed::item {
                margin: 0px;
                padding: 0px;
                border: none;
            }
            QFrame#StreamCard {
                background-color: #18212c;
                border: 1px solid #2d3d51;
                border-radius: 10px;
            }
            QLabel#StreamTitle { font-size: 13px; font-weight: 700; color: #f0f6ff; }
            QLabel#StreamMeta { font-size: 12px; color: #b5c3d8; }
            QLabel#FinishBadge {
                border-radius: 9px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 700;
                background-color: #2b3646;
                color: #e4ecff;
            }
            QLabel#LogHint { color: #95a4bb; font-size: 11px; }
            QLineEdit, QComboBox, QTextEdit, QTableWidget, QListWidget {
                background-color: #0f151d; border: 1px solid #334255; border-radius: 10px;
                selection-background-color: #2d7ff9;
            }
            QLineEdit, QComboBox { min-height: 34px; padding: 0 10px; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border: 1px solid #4f89d8;
                background-color: #111a24;
            }
            QProgressBar {
                border: 1px solid #3a4a60;
                border-radius: 8px;
                background-color: #101721;
                min-height: 14px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #2d7ff9;
                border-radius: 7px;
            }
            QTabWidget::pane { border: 1px solid #2b3644; border-radius: 12px; }
            QTabBar::tab {
                background: #1a222d;
                padding: 9px 20px;
                margin: 8px 6px 0 6px;
                border-radius: 10px;
                border: 1px solid #2f3b4d;
            }
            QTabBar::tab:selected { background: #273445; border-color: #3f5e86; }
            QTabWidget::tab-bar { alignment: center; }
            QHeaderView::section { background: #1b2430; border: none; border-right: 1px solid #334255; padding: 8px; }
            """
        )

    def _build_ui(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

        tabs = QTabWidget()
        tabs.setTabPosition(QTabWidget.South)
        root_layout.addWidget(tabs)
        self.tabs = tabs

        self.scanner_tab = QWidget()
        tabs.addTab(self.scanner_tab, "Scanner")
        self._build_scanner_tab()

        self.collection_tab = QWidget()
        tabs.addTab(self.collection_tab, "Collection")
        self._build_collection_tab()

        self.setCentralWidget(root)

    def _build_scanner_tab(self):
        layout = QVBoxLayout(self.scanner_tab)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)

        top_bar = QFrame()
        top_bar.setObjectName("HeroCard")
        self._attach_card_effect(top_bar)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 12, 14, 12)
        top_layout.setSpacing(10)

        folder_label = QLabel("Image Folder")
        folder_label.setObjectName("FieldLabel")
        top_layout.addWidget(folder_label)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        top_layout.addWidget(self.folder_edit, 1)

        browse_btn = QPushButton("📂 Browse")
        browse_btn.setObjectName("GhostButton")
        browse_btn.clicked.connect(self._pick_folder)
        top_layout.addWidget(browse_btn)

        status_label = QLabel("Status")
        status_label.setObjectName("FieldLabel")
        top_layout.addWidget(status_label)
        self.scan_status_badge = QLabel("Idle")
        self.scan_status_badge.setObjectName("StatusBadge")
        self.scan_status_badge.setFixedWidth(128)
        self.scan_status_badge.setAlignment(Qt.AlignCenter)
        top_layout.addWidget(self.scan_status_badge)

        self.scan_progress = QProgressBar()
        self.scan_progress.setTextVisible(False)
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setVisible(False)

        content_row = QHBoxLayout()
        content_row.setSpacing(12)

        left_panel = QFrame()
        left_panel.setObjectName("Card")
        self._attach_card_effect(left_panel)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(12)
        left_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        left_panel.setMinimumWidth(250)
        left_panel.setMaximumWidth(360)

        options_title = QLabel("Scan Options")
        options_title.setObjectName("SectionTitle")
        left_layout.addWidget(options_title)

        settings_wrap = QFrame()
        settings_wrap.setObjectName("Card")
        settings_layout = QGridLayout(settings_wrap)
        settings_layout.setContentsMargins(10, 10, 10, 10)
        settings_layout.setHorizontalSpacing(8)
        settings_layout.setVerticalSpacing(8)

        settings_layout.addWidget(QLabel("Provider"), 0, 0)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["openai", "gemini"])
        self.provider_combo.currentTextChanged.connect(self._update_model_combo)
        settings_layout.addWidget(self.provider_combo, 0, 1)

        settings_layout.addWidget(QLabel("Model"), 1, 0)
        self.model_combo = QComboBox()
        settings_layout.addWidget(self.model_combo, 1, 1)
        left_layout.addWidget(settings_wrap)

        action_wrap = QFrame()
        action_wrap.setObjectName("Card")
        action_layout = QVBoxLayout(action_wrap)
        action_layout.setContentsMargins(10, 10, 10, 10)
        action_layout.setSpacing(8)

        self.start_button = QPushButton("▶ Start Scan")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_scan)
        self.cancel_button = QPushButton("■ Cancel")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_scan)
        clear_button = QPushButton("🧹 Clear Feed")
        clear_button.setObjectName("GhostButton")
        clear_button.clicked.connect(self._clear_stream_output)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.cancel_button)
        action_layout.addWidget(clear_button)
        left_layout.addWidget(action_wrap)
        left_layout.addStretch(1)

        right_panel = QFrame()
        right_panel.setObjectName("Card")
        self._attach_card_effect(right_panel)
        output_layout = QVBoxLayout(right_panel)
        self.results_stack = QStackedWidget()
        output_layout.addWidget(self.results_stack, 1)

        self.live_view = QWidget()
        live_layout = QVBoxLayout(self.live_view)
        output_title_row = QHBoxLayout()
        output_title = QLabel("Live Detections")
        output_title.setObjectName("PanelTitle")
        self.live_count_label = QLabel("0 cards")
        self.live_count_label.setObjectName("StatusBadge")
        output_title_row.addWidget(output_title)
        output_title_row.addStretch(1)
        output_title_row.addWidget(self.live_count_label)
        live_layout.addLayout(output_title_row)

        self.live_feed_list = QListWidget()
        self.live_feed_list.setObjectName("LiveFeed")
        self.live_feed_list.setSelectionMode(QListWidget.NoSelection)
        self.live_feed_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.live_feed_list.setFocusPolicy(Qt.NoFocus)
        self.live_feed_list.setSpacing(1)
        self.live_feed_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.live_feed_list.viewport().installEventFilter(self)
        live_layout.addWidget(self.live_feed_list, 1)

        log_hint = QLabel(f"Diagnostics are written to: {self.log_file_path}")
        log_hint.setObjectName("LogHint")
        live_layout.addWidget(log_hint)

        self.validation_view = QWidget()
        validation_layout = QVBoxLayout(self.validation_view)
        validation_title_row = QHBoxLayout()
        validation_title = QLabel("Validate Scan Results")
        validation_title.setObjectName("PanelTitle")
        self.validation_count_label = QLabel("0 pending")
        self.validation_count_label.setObjectName("StatusBadge")
        validation_title_row.addWidget(validation_title)
        validation_title_row.addStretch(1)
        validation_title_row.addWidget(self.validation_count_label)
        validation_layout.addLayout(validation_title_row)

        self.validation_list = QListWidget()
        self.validation_list.setObjectName("LiveFeed")
        self.validation_list.setSelectionMode(QListWidget.NoSelection)
        self.validation_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.validation_list.setFocusPolicy(Qt.NoFocus)
        self.validation_list.setSpacing(6)
        self.validation_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        validation_layout.addWidget(self.validation_list, 1)

        save_row = QHBoxLayout()
        save_label = QLabel("Save Target")
        save_label.setObjectName("FieldLabel")
        save_row.addWidget(save_label)
        self.save_target_combo = QComboBox()
        save_row.addWidget(self.save_target_combo, 1)
        self.save_button = QPushButton("💾 Save Collection")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self._save_validated_collection)
        save_row.addWidget(self.save_button)
        validation_layout.addLayout(save_row)

        self._reload_save_target_options()

        self.results_stack.addWidget(self.live_view)
        self.results_stack.addWidget(self.validation_view)
        self.results_stack.setCurrentWidget(self.live_view)

        content_row.addWidget(left_panel, 2)
        content_row.addWidget(right_panel, 8)

        self._update_model_combo()

        layout.addWidget(top_bar)
        layout.addLayout(content_row, 1)

    def _build_collection_tab(self):
        layout = QVBoxLayout(self.collection_tab)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)

        content_row = QHBoxLayout()
        content_row.setSpacing(12)

        left_panel = QFrame()
        left_panel.setObjectName("Card")
        self._attach_card_effect(left_panel)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)
        left_panel.setMinimumWidth(250)
        left_panel.setMaximumWidth(360)
        left_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        collections_label = QLabel("Collections")
        collections_label.setObjectName("FieldLabel")
        left_layout.addWidget(collections_label)

        self.collections_list = QListWidget()
        self.collections_list.itemClicked.connect(self._load_collection_from_list)
        left_layout.addWidget(self.collections_list, 2)

        browse_btn = QPushButton("📄 Browse")
        browse_btn.setObjectName("GhostButton")
        browse_btn.clicked.connect(self._pick_collection_file)
        left_layout.addWidget(browse_btn)

        refresh_collections_btn = QPushButton("🔄 Refresh")
        refresh_collections_btn.setObjectName("GhostButton")
        refresh_collections_btn.clicked.connect(self._refresh_collections_list)
        left_layout.addWidget(refresh_collections_btn)

        delete_collection_btn = QPushButton("🗑️ Delete")
        delete_collection_btn.setObjectName("GhostButton")
        delete_collection_btn.clicked.connect(self._delete_current_collection)
        left_layout.addWidget(delete_collection_btn)

        self.list_toggle = QPushButton("☰ List")
        self.grid_toggle = QPushButton("▦ Grid")
        self.list_toggle.setObjectName("ToggleButton")
        self.grid_toggle.setObjectName("ToggleButton")
        self.list_toggle.setCheckable(True)
        self.grid_toggle.setCheckable(True)
        self.list_toggle.setChecked(True)
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        view_group.addButton(self.list_toggle)
        view_group.addButton(self.grid_toggle)
        self.list_toggle.clicked.connect(lambda: self._switch_collection_view("list"))
        self.grid_toggle.clicked.connect(lambda: self._switch_collection_view("grid"))
        view_mode_label = QLabel("View Mode")
        view_mode_label.setObjectName("FieldLabel")
        left_layout.addWidget(view_mode_label)
        left_layout.addWidget(self.list_toggle)
        left_layout.addWidget(self.grid_toggle)
        
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        left_layout.addWidget(separator)
        
        total_price_label = QLabel("Total Price")
        total_price_label.setObjectName("FieldLabel")
        left_layout.addWidget(total_price_label)
        self.total_value_label = QLabel("$0.00")
        self.total_value_label.setObjectName("MetricValue")
        left_layout.addWidget(self.total_value_label)
        
        self.summary_label = QLabel("Unique Cards: 0 | Total Copies: 0")
        self.summary_label.setObjectName("SummaryLabel")
        left_layout.addWidget(self.summary_label)
        left_layout.addStretch(1)

        right_panel = QFrame()
        right_panel.setObjectName("Card")
        self._attach_card_effect(right_panel)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.collection_stack = QStackedWidget()
        right_layout.addWidget(self.collection_stack, 1)

        self.table = HoverableTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Card", "Count", "Set", "Rarity", "Finish", "Price"])
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_card_menu)
        self.table.itemDoubleClicked.connect(self._on_table_double_click)

        self.grid_list = ResponsiveGridList()
        self.grid_list.setViewMode(QListView.IconMode)
        self.grid_list.setResizeMode(QListView.Adjust)
        self.grid_list.setMovement(QListView.Static)
        self.grid_list.setSpacing(10)
        self.grid_list.setWordWrap(True)
        self.grid_list.setUniformItemSizes(False)
        self.grid_list.resized.connect(self._update_grid_metrics)
        self.grid_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid_list.customContextMenuRequested.connect(self._show_grid_card_menu)
        self.grid_list.itemDoubleClicked.connect(self._on_grid_double_click)

        self.collection_stack.addWidget(self.table)
        self.collection_stack.addWidget(self.grid_list)
        self._update_grid_metrics()

        content_row.addWidget(left_panel, 2)
        content_row.addWidget(right_panel, 8)
        layout.addLayout(content_row, 1)

        self._refresh_collections_list()
        
        # Setup card image hover callbacks
        def get_card_at_index(index):
            if 0 <= index < len(self.collection_rows):
                return self.collection_rows[index]
            return None
        
        self.table.set_hover_callbacks(get_card_at_index, self._get_card_pixmap)
        self.grid_list.set_hover_callbacks(get_card_at_index, self._get_card_pixmap)





    def _switch_collection_view(self, mode: str):
        if mode == "grid":
            self.collection_stack.setCurrentWidget(self.grid_list)
            self._update_grid_metrics()
            self._render_grid()
            self._animate_view_fade(self.grid_list)
        else:
            self.collection_stack.setCurrentWidget(self.table)
            self._animate_view_fade(self.table)

    def _attach_card_effect(self, frame: QFrame):
        """Add subtle hover-reactive glow to card-like containers."""
        effect = QGraphicsDropShadowEffect(frame)
        effect.setColor(QColor(0, 0, 0, 95))
        effect.setOffset(0, 6)
        effect.setBlurRadius(20.0)
        frame.setGraphicsEffect(effect)
        frame.installEventFilter(self._card_hover_filter)

    def _animate_view_fade(self, widget: QWidget):
        """Fade in a view when switching between list/grid to feel fluid."""
        opacity = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(opacity)
        animation = QPropertyAnimation(opacity, b"opacity", widget)
        animation.setDuration(220)
        animation.setStartValue(0.15)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)

        def _cleanup():
            widget.setGraphicsEffect(None)
            if animation in self._fade_anims:
                self._fade_anims.remove(animation)

        animation.finished.connect(_cleanup)
        self._fade_anims.append(animation)
        animation.start()

    def _set_status_mode(self, mode: str):
        self._status_mode = mode
        self._status_tick = 0
        if mode in {"scanning", "cancelling"}:
            if not self._status_timer.isActive():
                self._status_timer.start()
            self._animate_status_badge()
            return

        self._status_timer.stop()
        if mode == "idle":
            self.scan_status_badge.setText("Idle")
            self.scan_status_badge.setStyleSheet(
                "background-color: #263548; border: 1px solid #3d5474; border-radius: 10px; padding: 4px 10px; font-weight: 600; color: #dbe9ff;"
            )

    def eventFilter(self, watched, event):
        if watched is getattr(self, "live_feed_list", None).viewport() and event.type() == QEvent.Resize:
            self._realign_live_feed_rows()
        return super().eventFilter(watched, event)

    def _realign_live_feed_rows(self):
        row_height = 86
        row_width = max(self.live_feed_list.viewport().width() - 4, 120)
        for index in range(self.live_feed_list.count()):
            item = self.live_feed_list.item(index)
            if item is None:
                continue
            item.setSizeHint(QSize(row_width, row_height))
            row = self.live_feed_list.itemWidget(item)
            if row is not None:
                row.setFixedSize(row_width, row_height)

    def _clear_stream_output(self):
        self.live_feed_list.clear()
        self.live_count_label.setText("0 cards")

    def _resolve_log_file_path(self) -> Path:
        return self.app_data_dir / "scanner.log"

    def _resolve_app_data_dir(self) -> Path:
        if os.name == "nt":
            base_dir = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        elif sys.platform == "darwin":
            base_dir = Path.home() / "Library" / "Application Support"
        else:
            base_dir = Path.home() / ".local" / "share"

        app_dir = base_dir / "MTGBinderScanner"
        app_dir.mkdir(parents=True, exist_ok=True)
        return app_dir

    def _resolve_collections_dir(self) -> Path:
        collections_dir = self.app_data_dir / "collections"
        collections_dir.mkdir(parents=True, exist_ok=True)
        return collections_dir

    def _list_saved_collections(self) -> list[Path]:
        if not self.collections_dir.exists():
            return []
        return sorted(
            [path for path in self.collections_dir.glob("*.json") if path.is_file()],
            key=lambda path: path.name.lower(),
        )

    def _reload_save_target_options(self, selected_path: Path | None = None):
        if not hasattr(self, "save_target_combo"):
            return

        previous_data = self.save_target_combo.currentData()
        self.save_target_combo.blockSignals(True)
        self.save_target_combo.clear()
        self.save_target_combo.addItem(NEW_COLLECTION_LABEL, None)

        for collection_path in self._list_saved_collections():
            self.save_target_combo.addItem(collection_path.stem, str(collection_path))

        target_data = str(selected_path) if selected_path else previous_data
        if target_data:
            index = self.save_target_combo.findData(str(target_data))
            if index >= 0:
                self.save_target_combo.setCurrentIndex(index)
            else:
                self.save_target_combo.setCurrentIndex(0)
        else:
            self.save_target_combo.setCurrentIndex(0)

        self.save_target_combo.blockSignals(False)

    def _sanitize_collection_name(self, raw_name: str) -> str:
        cleaned = "".join(ch for ch in raw_name.strip() if ch.isalnum() or ch in ("-", "_", " "))
        cleaned = cleaned.strip().replace(" ", "_")
        return cleaned or "collection"

    def _build_named_collection_path(self, collection_name: str) -> Path:
        return self.collections_dir / f"{self._sanitize_collection_name(collection_name)}.json"

    def _build_validation_rows(self):
        self.validation_list.clear()
        self.validation_rows = []

        for detection in self.pending_detections:
            row_state = self._create_validation_row(detection)
            self.validation_rows.append(row_state)

        self.validation_count_label.setText(f"{len(self.validation_rows)} pending")

    def _options_for_name(self, card_name: str) -> list[dict]:
        key = card_name.strip().lower()
        cached = self._print_options_cache.get(key)
        if cached is not None:
            return cached

        options = scryfall.get_print_options(card_name)
        self._print_options_cache[key] = options
        return options

    def _create_validation_row(self, detection: dict) -> dict:
        options = self._options_for_name(detection.get("name", ""))
        if not options:
            options = [
                {
                    "name": detection.get("name", "Unknown"),
                    "set": str(detection.get("set", "")).upper(),
                    "set_name": str(detection.get("set_name", "")),
                    "collector_number": str(detection.get("collector_number", "")),
                    "rarity": detection.get("rarity", "unknown"),
                    "prices": detection.get("prices", {}),
                    "finish": str(detection.get("finish", "unknown")).lower(),
                    "image_url": str(detection.get("image_url", "")),
                }
            ]

        container = QFrame()
        container.setObjectName("StreamCard")
        container.setFixedHeight(96)
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(10)

        art_label = QLabel()
        art_label.setFixedSize(60, 80)
        art_label.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(art_label)

        name_label = QLabel(str(detection.get("name", "Unknown")))
        name_label.setObjectName("StreamTitle")
        name_label.setFixedWidth(220)
        name_label.setWordWrap(False)
        name_label.setText(self._elide_live_title(name_label.text(), 210))
        row_layout.addWidget(name_label)

        set_combo = QComboBox()
        number_combo = QComboBox()
        finish_combo = QComboBox()
        set_combo.setFixedWidth(220)
        number_combo.setFixedWidth(110)
        finish_combo.setFixedWidth(130)
        set_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        number_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        finish_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        set_combo.setMinimumContentsLength(10)
        number_combo.setMinimumContentsLength(4)
        finish_combo.setMinimumContentsLength(7)
        row_layout.addWidget(set_combo)
        row_layout.addWidget(number_combo)
        row_layout.addWidget(finish_combo)
        row_layout.addStretch(1)

        item = QListWidgetItem()
        item.setSizeHint(QSize(max(self.validation_list.viewport().width() - 12, 600), 98))
        self.validation_list.addItem(item)
        self.validation_list.setItemWidget(item, container)

        row_state = {
            "detection": detection,
            "options": options,
            "item": item,
            "art_label": art_label,
            "name_label": name_label,
            "set_combo": set_combo,
            "number_combo": number_combo,
            "finish_combo": finish_combo,
            "selected": None,
        }

        self._populate_row_sets(row_state, preferred_set=str(detection.get("set", "")).upper())
        self._populate_row_numbers(row_state, preferred_number=str(detection.get("collector_number", "")))
        self._populate_row_finishes(row_state, preferred_finish=str(detection.get("finish", "unknown")))
        self._update_row_preview(row_state)

        set_combo.currentIndexChanged.connect(lambda _=None, r=row_state: self._on_validation_set_changed(r))
        number_combo.currentIndexChanged.connect(lambda _=None, r=row_state: self._on_validation_number_changed(r))
        finish_combo.currentIndexChanged.connect(lambda _=None, r=row_state: self._update_row_preview(r))
        return row_state

    def _populate_row_sets(self, row_state: dict, preferred_set: str = ""):
        combo = row_state["set_combo"]
        combo.blockSignals(True)
        combo.clear()
        seen = set()
        for opt in row_state["options"]:
            set_code = str(opt.get("set", "")).upper()
            if not set_code or set_code in seen:
                continue
            seen.add(set_code)
            set_name = str(opt.get("set_name", ""))
            label = f"{set_code} — {set_name}" if set_name else set_code
            combo.addItem(label, set_code)

        target = preferred_set if preferred_set else combo.currentData()
        index = combo.findData(target)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _populate_row_numbers(self, row_state: dict, preferred_number: str = ""):
        set_code = str(row_state["set_combo"].currentData() or "")
        combo = row_state["number_combo"]
        combo.blockSignals(True)
        combo.clear()

        seen = set()
        for opt in row_state["options"]:
            if str(opt.get("set", "")).upper() != set_code:
                continue
            number = str(opt.get("collector_number", ""))
            if not number or number in seen:
                continue
            seen.add(number)
            combo.addItem(number, number)

        target = preferred_number if preferred_number else combo.currentData()
        index = combo.findData(target)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _populate_row_finishes(self, row_state: dict, preferred_finish: str = ""):
        set_code = str(row_state["set_combo"].currentData() or "")
        number = str(row_state["number_combo"].currentData() or "")
        combo = row_state["finish_combo"]
        combo.blockSignals(True)
        combo.clear()

        seen = set()
        for opt in row_state["options"]:
            if str(opt.get("set", "")).upper() != set_code:
                continue
            if str(opt.get("collector_number", "")) != number:
                continue
            finish = str(opt.get("finish", "unknown")).lower()
            if finish in seen:
                continue
            seen.add(finish)
            display = {"foil": "Foil", "nonfoil": "Non-foil", "etched": "Etched"}.get(finish, "Unknown")
            combo.addItem(display, finish)

        target = preferred_finish.lower() if preferred_finish else combo.currentData()
        index = combo.findData(target)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _on_validation_set_changed(self, row_state: dict):
        self._populate_row_numbers(row_state)
        self._populate_row_finishes(row_state)
        self._update_row_preview(row_state)

    def _on_validation_number_changed(self, row_state: dict):
        self._populate_row_finishes(row_state)
        self._update_row_preview(row_state)

    def _match_row_selection(self, row_state: dict) -> dict | None:
        set_code = str(row_state["set_combo"].currentData() or "").upper()
        number = str(row_state["number_combo"].currentData() or "")
        finish = str(row_state["finish_combo"].currentData() or "unknown").lower()
        for opt in row_state["options"]:
            if str(opt.get("set", "")).upper() != set_code:
                continue
            if str(opt.get("collector_number", "")) != number:
                continue
            if str(opt.get("finish", "unknown")).lower() != finish:
                continue
            return opt
        return None

    def _update_row_preview(self, row_state: dict):
        selected = self._match_row_selection(row_state)
        if selected is None and row_state["options"]:
            selected = row_state["options"][0]

        row_state["selected"] = selected
        if not selected:
            return

        display_name = str(selected.get("name", "Unknown"))
        row_state["name_label"].setText(self._elide_live_title(display_name, 210))
        pixmap = self._get_card_pixmap(str(selected.get("image_url", "")))
        row_state["art_label"].setPixmap(pixmap.scaled(60, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _save_validated_collection(self):
        if not self.validation_rows:
            QMessageBox.information(self, "Nothing to Save", "No validated cards to save.")
            return

        selected_target = self.save_target_combo.currentData()
        output_path: Path
        if selected_target:
            output_path = Path(str(selected_target))
            append_mode = True
        else:
            name, ok = QInputDialog.getText(self, "New Collection", "Name this collection:")
            if not ok:
                return
            if not str(name).strip():
                QMessageBox.warning(self, "Name Required", "Please enter a collection name.")
                return
            output_path = self._build_named_collection_path(str(name))
            append_mode = output_path.exists()

        output_path.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if append_mode and output_path.exists():
            try:
                with open(output_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}

        for row_state in self.validation_rows:
            selected = row_state.get("selected")
            if not selected:
                continue

            name = str(selected.get("name", "Unknown"))
            set_code = str(selected.get("set", "")).upper()
            collector_number = str(selected.get("collector_number", ""))
            finish = str(selected.get("finish", "unknown")).lower()
            key = f"{name} [{set_code} #{collector_number}] ({finish})"

            existing_entry = existing.get(key, {}) if isinstance(existing.get(key), dict) else {}
            existing_count = int(existing_entry.get("count", 0) or 0)
            existing[key] = {
                **existing_entry,
                "name": name,
                "set": set_code.lower(),
                "set_name": selected.get("set_name", existing_entry.get("set_name", "")),
                "collector_number": collector_number,
                "rarity": selected.get("rarity", existing_entry.get("rarity", "unknown")),
                "prices": selected.get("prices", existing_entry.get("prices", {})),
                "finish": finish,
                "image_uris": {"small": selected.get("image_url", "")},
                "count": existing_count + 1,
            }

        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(dict(sorted(existing.items())), handle, indent=2, ensure_ascii=False)

        self._reload_save_target_options(selected_path=output_path)
        self.current_collection_path = output_path
        self._refresh_collections_list()
        self._load_collection()
        self.results_stack.setCurrentWidget(self.live_view)
        self.pending_detections = []
        self.validation_rows = []
        self.has_unsaved_scan = False
        self.validation_list.clear()
        self.validation_count_label.setText("0 pending")
        self._log(f"Saved validated collection: {output_path}")

    def _discard_pending_scan(self):
        self.pending_detections = []
        self.validation_rows = []
        self.has_unsaved_scan = False
        if hasattr(self, "validation_list"):
            self.validation_list.clear()
            self.validation_count_label.setText("0 pending")
        if hasattr(self, "results_stack"):
            self.results_stack.setCurrentWidget(self.live_view)

    def _confirm_discard_unsaved_scan(self) -> bool:
        if not self.has_unsaved_scan:
            return True

        choice = QMessageBox.question(
            self,
            "Discard Unsaved Scan?",
            "You have a scanned collection that has not been saved. Discard it and continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return False

        self._discard_pending_scan()
        return True

    def _write_log_line(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {message}\n")
        except OSError:
            pass

    def _elide_live_title(self, text: str, width: int = 360) -> str:
        metrics = QFontMetrics(self.font())
        return metrics.elidedText(str(text or ""), Qt.ElideRight, max(width, 80))

    def _finish_badge_style(self, finish: str) -> str:
        normalized = str(finish or "unknown").strip().lower()
        if normalized == "foil":
            return "background-color: #5f3ac7; color: #f4ebff;"
        if normalized == "nonfoil":
            return "background-color: #1f6a50; color: #defee8;"
        return "background-color: #4b5563; color: #edf2f7;"

    def _add_live_detection_card(
        self,
        name: str,
        set_code: str,
        number: str,
        count: int,
        finish: str,
        image_url: str,
    ):
        row_height = 86
        row_width = max(self.live_feed_list.viewport().width() - 4, 120)

        row_item = QListWidgetItem()
        row_item.setSizeHint(QSize(row_width, row_height))
        self.live_feed_list.insertItem(0, row_item)

        card = QFrame()
        card.setObjectName("StreamCard")
        card.setFixedSize(row_width, row_height)
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(7, 6, 7, 6)
        card_layout.setSpacing(8)

        art_label = QLabel()
        art_label.setFixedSize(52, 72)
        art_label.setPixmap(self._get_card_pixmap(image_url).scaled(52, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        art_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(art_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title = QLabel(name)
        title.setObjectName("StreamTitle")
        title.setWordWrap(False)
        meta = QLabel(f"{set_code} #{number}  •  x{count}")
        meta.setObjectName("StreamMeta")
        text_col.addWidget(title)
        text_col.addWidget(meta)
        card_layout.addLayout(text_col, 1)

        finish_badge = QLabel(self._finish_display(finish))
        finish_badge.setObjectName("FinishBadge")
        finish_badge.setStyleSheet(self._finish_badge_style(finish))
        card_layout.addWidget(finish_badge, 0, Qt.AlignTop)

        self.live_feed_list.setItemWidget(row_item, card)
        self.live_feed_list.scrollToItem(row_item, QListWidget.PositionAtTop)
        self._realign_live_feed_rows()
        self.live_count_label.setText(f"{self.live_feed_list.count()} cards")

    def _animate_status_badge(self):
        """Animate the status badge text and color while scan is active."""
        self._status_tick = (self._status_tick + 1) % 3
        dots = "." * (self._status_tick + 1)
        if self._status_mode == "scanning":
            self.scan_status_badge.setText(f"Scanning{dots}")
            glow = "#5f9cff" if self._status_tick % 2 == 0 else "#7db0ff"
            self.scan_status_badge.setStyleSheet(
                f"background-color: #2d7ff9; border: 1px solid {glow}; border-radius: 10px; padding: 4px 10px; font-weight: 700; color: white;"
            )
        elif self._status_mode == "cancelling":
            self.scan_status_badge.setText(f"Cancelling{dots}")
            glow = "#e7a72f" if self._status_tick % 2 == 0 else "#f0b84b"
            self.scan_status_badge.setStyleSheet(
                f"background-color: #a56900; border: 1px solid {glow}; border-radius: 10px; padding: 4px 10px; font-weight: 700; color: #fff6dc;"
            )

    def _update_grid_metrics(self):
        viewport_width = max(self.grid_list.viewport().width(), 320)
        tile_width = 190
        columns = max(1, viewport_width // tile_width)
        cell_width = max(170, viewport_width // columns)
        self.grid_list.setIconSize(QSize(146, 204))
        self.grid_list.setGridSize(QSize(cell_width, 310))

    def _resolve_initial_cards_file(self) -> Path | None:
        saved_collections = self._list_saved_collections()
        if saved_collections:
            return max(saved_collections, key=lambda path: path.stat().st_mtime)

        candidates = [
            Path.cwd() / "cards.json",
            Path.home() / "cards.json",
            Path.home() / "Desktop" / "cards.json",
        ]
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _resolve_thumbnail_cache_dir(self) -> Path:
        cache_dir = self.app_data_dir / "thumb_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _thumbnail_cache_path(self, image_url: str) -> Path:
        digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        return self.thumb_cache_dir / f"{digest}.jpg"

    def _extract_image_url(self, card_info: dict) -> str | None:
        image_uris = card_info.get("image_uris") or {}
        if not isinstance(image_uris, dict):
            return None
        for key in ("small", "normal", "large", "png"):
            value = image_uris.get(key)
            if value:
                return str(value)
        return None

    def _placeholder_pixmap(self) -> QPixmap:
        key = "__placeholder__"
        existing = self.thumb_memory_cache.get(key)
        if existing is not None:
            return existing

        pixmap = QPixmap(146, 204)
        pixmap.fill(QColor("#2b313a"))
        self.thumb_memory_cache[key] = pixmap
        return pixmap

    def _get_card_pixmap(self, image_url: str | None) -> QPixmap:
        cache_key = image_url or "__placeholder__"
        existing = self.thumb_memory_cache.get(cache_key)
        if existing is not None:
            return existing

        if not image_url:
            return self._placeholder_pixmap()

        cache_path = self._thumbnail_cache_path(image_url)
        pixmap = QPixmap()

        if cache_path.exists() and cache_path.is_file() and pixmap.load(str(cache_path)):
            scaled = pixmap.scaled(146, 204, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.thumb_memory_cache[cache_key] = scaled
            return scaled

        try:
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            pixmap.loadFromData(response.content)
            scaled = pixmap.scaled(146, 204, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.save(str(cache_path), "JPG", quality=88)
            self.thumb_memory_cache[cache_key] = scaled
            return scaled
        except Exception:
            return self._placeholder_pixmap()

    def _precache_collection_images(self):
        seen = set()
        for row in self.collection_rows:
            url = row.get("image_url")
            if url in seen:
                continue
            seen.add(url)
            self._get_card_pixmap(url)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Image Folder")
        if path:
            self.folder_edit.setText(path)

    def _pick_collection_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select collection file",
            str(self.collections_dir),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            self._load_collection_by_path(Path(path))

    def _refresh_collections_list(self):
        """Populate the collections list with saved collections."""
        if not hasattr(self, 'collections_list'):
            return
        
        self.collections_list.clear()
        collections = self._list_saved_collections()
        
        for collection_path in collections:
            item = QListWidgetItem(collection_path.stem)
            item.setData(Qt.UserRole, str(collection_path))
            self.collections_list.addItem(item)
    
    def _load_collection_from_list(self, item: QListWidgetItem):
        """Load a collection when clicked in the collections list."""
        collection_path = Path(item.data(Qt.UserRole))
        self._load_collection_by_path(collection_path)
    
    def _load_collection_by_path(self, path: Path):
        """Load a collection file by path."""
        self.current_collection_path = path
        self._load_collection()

    def _delete_current_collection(self):
        """Delete the currently loaded collection."""
        if not self.current_collection_path:
            QMessageBox.warning(self, "No Collection", "No collection is currently loaded.")
            return
        
        collection_name = self.current_collection_path.stem
        reply = QMessageBox.question(
            self,
            "Delete Collection",
            f"Are you sure you want to delete '{collection_name}'?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                self.current_collection_path.unlink()
                self._refresh_collections_list()
                self.current_collection_path = None
                self.collection_rows = []
                self._render_table()
                self._render_grid()
                self.total_value_label.setText("$0.00")
                self.summary_label.setText("Unique Cards: 0 | Total Copies: 0")
                self._log(f"Deleted collection: {collection_name}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete collection: {e}")

    def _show_table_card_menu(self, pos):
        """Show context menu for table card."""
        item = self.table.itemAt(pos)
        if item is None:
            return
        
        row = self.table.row(item)
        if 0 <= row < len(self.collection_rows):
            menu = QMenu(self)
            edit_action = menu.addAction("✏️ Edit Card")
            delete_action = menu.addAction("🗑️ Delete Card")
            
            action = menu.exec(self.table.mapToGlobal(pos))
            if action == edit_action:
                self._edit_card(row)
            elif action == delete_action:
                self._delete_card(row)

    def _show_grid_card_menu(self, pos):
        """Show context menu for grid card."""
        item = self.grid_list.itemAt(pos)
        if item is None:
            return
        
        row = self.grid_list.row(item)
        if 0 <= row < len(self.collection_rows):
            menu = QMenu(self)
            edit_action = menu.addAction("✏️ Edit Card")
            delete_action = menu.addAction("🗑️ Delete Card")
            
            action = menu.exec(self.grid_list.mapToGlobal(pos))
            if action == edit_action:
                self._edit_card(row)
            elif action == delete_action:
                self._delete_card(row)

    def _on_table_double_click(self, item):
        """Handle double-click on table cells."""
        row = self.table.row(item)
        if 0 <= row < len(self.collection_rows):
            self._edit_card(row)

    def _on_grid_double_click(self, item):
        """Handle double-click on grid items."""
        row = self.grid_list.row(item)
        if 0 <= row < len(self.collection_rows):
            self._edit_card(row)

    def _edit_card(self, row: int):
        """Open edit dialog for a card with dynamically updating fields."""
        if not (0 <= row < len(self.collection_rows)):
            return
        
        card = self.collection_rows[row]
        card_name = card.get('name', '')
        
        # Get available print options for this card
        print_options = self._options_for_name(card_name)
        
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit Card: {card_name}")
        dialog.resize(500, 450)
        
        layout = QVBoxLayout(dialog)
        
        # Name field (read-only)
        name_layout = QHBoxLayout()
        name_label = QLabel("Name:")
        name_display = QLineEdit()
        name_display.setText(card_name)
        name_display.setReadOnly(True)
        name_layout.addWidget(name_label, 1)
        name_layout.addWidget(name_display, 3)
        layout.addLayout(name_layout)
        
        # Set Code field
        set_layout = QHBoxLayout()
        set_label = QLabel("Set Code:")
        set_combo = QComboBox()
        set_layout.addWidget(set_label, 1)
        set_layout.addWidget(set_combo, 3)
        layout.addLayout(set_layout)
        
        # Collector Number field
        number_layout = QHBoxLayout()
        number_label = QLabel("Collector #:")
        number_combo = QComboBox()
        number_layout.addWidget(number_label, 1)
        number_layout.addWidget(number_combo, 3)
        layout.addLayout(number_layout)
        
        # Rarity field
        rarity_layout = QHBoxLayout()
        rarity_label = QLabel("Rarity:")
        rarity_combo = QComboBox()
        rarity_layout.addWidget(rarity_label, 1)
        rarity_layout.addWidget(rarity_combo, 3)
        layout.addLayout(rarity_layout)
        
        # Finish field (dropdown)
        finish_layout = QHBoxLayout()
        finish_label = QLabel("Finish:")
        finish_combo = QComboBox()
        finish_combo.addItems(["Non-foil", "Foil"])
        finish_text = card.get('finish', 'Non-foil')
        if finish_text in ["Non-foil", "Foil"]:
            finish_combo.setCurrentText(finish_text)
        finish_layout.addWidget(finish_label, 1)
        finish_layout.addWidget(finish_combo, 3)
        layout.addLayout(finish_layout)
        
        # Count field
        count_layout = QHBoxLayout()
        count_label = QLabel("Count:")
        count_spin = QSpinBox()
        count_spin.setMinimum(1)
        count_spin.setValue(int(card.get('count', 1)))
        count_layout.addWidget(count_label, 1)
        count_layout.addWidget(count_spin, 3)
        layout.addLayout(count_layout)
        
        layout.addStretch()
        
        # Function to update all fields based on current selections
        def update_all_options():
            """Update all combo boxes based on current selections."""
            # Block signals to avoid recursive updates
            set_combo.blockSignals(True)
            number_combo.blockSignals(True)
            rarity_combo.blockSignals(True)
            
            current_set = set_combo.currentText()
            current_number = number_combo.currentText()
            current_rarity = rarity_combo.currentText()
            
            # Update Set Code options (always all available sets)
            all_sets = sorted(set(opt.get('set', '') for opt in print_options))
            set_combo.clear()
            set_combo.addItems(all_sets)
            if current_set in all_sets:
                set_combo.setCurrentText(current_set)
            elif all_sets:
                set_combo.setCurrentIndex(0)
            
            # Update Collector Number options based on set
            sets_filtered = [opt for opt in print_options if opt.get('set', '') == set_combo.currentText()] if set_combo.currentText() else print_options
            all_numbers = sorted(set(opt.get('collector_number', '') for opt in sets_filtered))
            number_combo.clear()
            number_combo.addItems(all_numbers)
            if current_number in all_numbers:
                number_combo.setCurrentText(current_number)
            elif all_numbers:
                number_combo.setCurrentIndex(0)
            
            # Update Rarity options based on set and number
            rarity_filtered = sets_filtered
            if number_combo.currentText():
                rarity_filtered = [opt for opt in rarity_filtered if opt.get('collector_number', '') == number_combo.currentText()]
            all_rarities = sorted(set(opt.get('rarity', '') for opt in rarity_filtered))
            rarity_combo.clear()
            rarity_combo.addItems(all_rarities)
            if current_rarity in all_rarities:
                rarity_combo.setCurrentText(current_rarity)
            elif all_rarities:
                rarity_combo.setCurrentIndex(0)
            
            set_combo.blockSignals(False)
            number_combo.blockSignals(False)
            rarity_combo.blockSignals(False)
        
        # Connect all combo boxes to trigger updates
        set_combo.currentTextChanged.connect(update_all_options)
        number_combo.currentTextChanged.connect(update_all_options)
        rarity_combo.currentTextChanged.connect(update_all_options)
        
        # Initialize with current card values
        current_set = card.get('set_code', '')
        current_number = card.get('collector_number', '')
        current_rarity = card.get('rarity', '')
        
        # Populate initial values
        all_sets = sorted(set(opt.get('set', '') for opt in print_options))
        set_combo.addItems(all_sets)
        if current_set in all_sets:
            set_combo.setCurrentText(current_set)
        
        update_all_options()
        
        # Buttons
        button_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        delete_btn = QPushButton("🗑️ Delete")
        cancel_btn = QPushButton("Cancel")
        delete_btn.setStyleSheet("color: #ff6b6b;")
        button_layout.addWidget(save_btn)
        button_layout.addWidget(delete_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        def save_changes():
            card['count'] = count_spin.value()
            card['set_code'] = set_combo.currentText()
            card['collector_number'] = number_combo.currentText()
            card['rarity'] = rarity_combo.currentText()
            card['finish'] = finish_combo.currentText()
            
            # Update price data from the selected print option
            selected_set = set_combo.currentText()
            selected_number = number_combo.currentText()
            selected_rarity = rarity_combo.currentText()
            
            # Find matching print option in our print_options list
            matched_option = None
            for opt in print_options:
                if (opt.get('set') == selected_set and 
                    opt.get('collector_number') == selected_number and 
                    opt.get('rarity') == selected_rarity):
                    matched_option = opt
                    break
            
            # Update prices if we found a matching option with valid price data
            if matched_option:
                new_prices = matched_option.get('prices', {})
                # Only update if we have actual price data
                if isinstance(new_prices, dict) and new_prices.get('usd'):
                    card['prices'] = new_prices
                    price_usd = self._coerce_price(new_prices.get('usd'))
                    card['price_value'] = price_usd
                    card['price_str'] = f"${price_usd:.2f}"
                
                # Update image if available
                if matched_option.get('image_url'):
                    card['image_url'] = matched_option['image_url']
            
            self._save_collection_to_file()
            self._render_table()
            self._render_grid()
            dialog.accept()
        
        def delete_card_from_dialog():
            reply = QMessageBox.question(
                dialog,
                "Delete Card",
                f"Remove '{card_name}' from this collection?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.collection_rows.pop(row)
                self._save_collection_to_file()
                self._render_table()
                self._render_grid()
                self._log(f"Deleted card from collection: {card_name}")
                dialog.accept()
        
        save_btn.clicked.connect(save_changes)
        delete_btn.clicked.connect(delete_card_from_dialog)
        cancel_btn.clicked.connect(dialog.reject)
        
        dialog.exec()

    def _delete_card(self, row: int):
        """Remove a card from the collection."""
        if not (0 <= row < len(self.collection_rows)):
            return
        
        card = self.collection_rows[row]
        reply = QMessageBox.question(
            self,
            "Delete Card",
            f"Remove '{card['name']}' (x{card['count']}) from this collection?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.collection_rows.pop(row)
            self._save_collection_to_file()
            self._render_table()
            self._render_grid()
            self._log(f"Deleted card from collection: {card['name']}")

    def _save_collection_to_file(self):
        """Save the current collection_rows to the collection file."""
        if not self.current_collection_path:
            return
        
        try:
            # Build the collection dict from collection_rows
            collection_dict = {}
            for row in self.collection_rows:
                # Use card name as key if no unique key exists
                key = f"{row['name']}_{row['set_code']}_{row['collector_number']}_{row['finish']}"
                collection_dict[key] = {
                    "name": row["name"],
                    "count": row["count"],
                    "set": row["set_code"],
                    "set_name": row.get("set_name", ""),
                    "collector_number": row["collector_number"],
                    "rarity": row["rarity"],
                    "finish": row["finish"],
                    "prices": {"usd": row.get("price_value", 0.0)},
                    "price_str": row["price_str"],
                    "image_uris": {"small": row.get("image_url", "")},
                }
            
            with open(self.current_collection_path, "w", encoding="utf-8") as handle:
                json.dump(dict(sorted(collection_dict.items())), handle, indent=2, ensure_ascii=False)
            
            # Recalculate totals
            total_value = sum(row["price_value"] for row in self.collection_rows)
            total_copies = sum(row["count"] for row in self.collection_rows)
            self.total_value_label.setText(f"${total_value:.2f}")
            self.summary_label.setText(f"Unique Cards: {len(self.collection_rows)} | Total Copies: {total_copies}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save collection: {e}")

    def _update_model_combo(self):
        provider = self.provider_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if provider == "openai":
            self.model_combo.addItems(["gpt-4o", "gpt-4o-mini"])
        else:
            self.model_combo.addItems(["2.5 (gemini-2.5-flash)", "3 (gemini-3-flash-preview)"])
        self.model_combo.setCurrentIndex(0)
        self.model_combo.blockSignals(False)

    def _selected_model(self) -> str | None:
        provider = self.provider_combo.currentText()
        selected = self.model_combo.currentText()
        if provider == "openai":
            return selected
        if "2.5" in selected:
            return GEMINI_TIER_MODEL_MAP["2.5"]
        return GEMINI_TIER_MODEL_MAP["3"]

    def _log(self, message: str, is_error: bool = False):
        prefix = "ERROR" if is_error else "INFO"
        self._write_log_line(f"{prefix} {message}")

    def _on_card_identified(
        self,
        name: str,
        set_code: str,
        number: str,
        count: int,
        match_method: str,
        finish: str,
        name_confidence: str,
        set_confidence: str,
        finish_confidence: str,
        image_url: str,
    ):
        self._add_live_detection_card(
            name=name,
            set_code=set_code,
            number=number,
            count=count,
            finish=finish,
            image_url=image_url,
        )

        message = f"{name} [{set_code} #{number}] (x{count}) [{match_method}] [finish={finish}]"
        message += f" [conf name={name_confidence} set={set_confidence} finish={finish_confidence}]"
        self._log(message)

    def _on_status(self, message: str):
        self._log(message)

    def _on_error(self, message: str, debug: bool = False):
        self._log(message, is_error=True)

    def _set_scan_controls(self, active: bool):
        self.scanning = active
        self.start_button.setEnabled(not active)
        self.cancel_button.setEnabled(active)
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(not active)
        if active:
            self.scan_progress.setRange(0, 0)
            self._set_status_mode("scanning")
        else:
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(1)
            self._set_status_mode("idle")

    def _start_scan(self):
        if not self._confirm_discard_unsaved_scan():
            return

        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.critical(self, "Error", "Please select an image folder.")
            return

        output_path = self.app_data_dir / "_session_preview.json"
        self.last_scan_output = output_path

        provider = self.provider_combo.currentText()
        model = self._selected_model()

        self.cancel_event = threading.Event()
        self._clear_stream_output()
        self._discard_pending_scan()
        self._log(f"Starting scan: {folder}")
        self._log(f"Provider: {provider}, Model: {model}")
        self._log("Collection mode: validation pending, save required to persist")
        self._log("=" * 80)

        self.worker = ScanWorker(
            image_folder=folder,
            output_path=str(output_path),
            provider=provider,
            model=model,
            cancel_event=self.cancel_event,
        )
        self.worker.status.connect(self._on_status)
        self.worker.error.connect(self._on_error)
        self.worker.card_identified.connect(self._on_card_identified)
        self.worker.done.connect(self._scan_complete)
        self._set_scan_controls(True)
        self.worker.start()

    def _cancel_scan(self):
        if self.cancel_event:
            self.cancel_event.set()
        self._log("Cancellation requested...")
        self.cancel_button.setEnabled(False)
        self._set_status_mode("cancelling")

    def _scan_complete(self, success: bool, message: str, output_path: str, result: dict):
        self._set_scan_controls(False)
        self._set_status_mode("idle")
        self._log("=" * 80)
        self._log(message or "Scan complete!")

        if not success:
            return

        self.pending_detections = list(result.get("detections", []) or [])
        self._reload_save_target_options()
        self._build_validation_rows()
        self.has_unsaved_scan = len(self.validation_rows) > 0
        if hasattr(self, "results_stack"):
            self.results_stack.setCurrentWidget(self.validation_view)

    def closeEvent(self, event):
        if not self._confirm_discard_unsaved_scan():
            event.ignore()
            return
        super().closeEvent(event)

    def _coerce_price(self, value) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _finish_display(self, raw_finish: str) -> str:
        normalized = str(raw_finish or "unknown").strip().lower()
        return {"foil": "Foil", "nonfoil": "Non-foil"}.get(normalized, "Unknown")

    def _load_collection(self):
        if self.current_collection_path:
            cards_file = str(self.current_collection_path)
        else:
            initial = self._resolve_initial_cards_file()
            if initial:
                cards_file = str(initial)
                self.current_collection_path = initial
            else:
                cards_file = None

        if not cards_file:
            self._pick_collection_file()
            if not self.current_collection_path:
                return
            cards_file = str(self.current_collection_path)

        try:
            with open(cards_file, "r", encoding="utf-8") as handle:
                cards_data = json.load(handle)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, "Error", f"Failed to load cards.json: {exc}")
            return

        if isinstance(cards_data, dict):
            entries = [entry for entry in cards_data.values() if isinstance(entry, dict)]
        elif isinstance(cards_data, list):
            entries = [entry for entry in cards_data if isinstance(entry, dict)]
        else:
            QMessageBox.critical(self, "Error", "Unsupported JSON format. Expected object or array.")
            return

        total_value = 0.0
        total_copies = 0
        rows = []

        for entry in entries:
            name = entry.get("name", "Unknown")
            set_code = str(entry.get("set", "N/A") or "N/A").upper()
            collector_number = str(entry.get("collector_number", "?") or "?")
            rarity = str(entry.get("rarity", "N/A") or "N/A").title()
            try:
                count = int(entry.get("count", 1) or 1)
            except (TypeError, ValueError):
                count = 1

            prices = entry.get("prices", {})
            if not isinstance(prices, dict):
                prices = {}
            price_usd = self._coerce_price(prices.get("usd"))
            finish = self._finish_display(entry.get("finish", "unknown"))
            image_url = self._extract_image_url(entry)

            total_value += price_usd * count
            total_copies += count

            rows.append(
                {
                    "name": name,
                    "count": count,
                    "set_code": set_code,
                    "collector_number": collector_number,
                    "rarity": rarity,
                    "finish": finish,
                    "price_value": price_usd,
                    "price_str": f"${price_usd:.2f}",
                    "image_url": image_url,
                }
            )

        self.collection_rows = sorted(rows, key=lambda r: r["price_value"], reverse=True)
        self._precache_collection_images()
        self._render_table()
        self._render_grid()
        self._animate_view_fade(self.collection_stack.currentWidget())

        self.total_value_label.setText(f"${total_value:.2f}")
        self.summary_label.setText(f"Unique Cards: {len(self.collection_rows)} | Total Copies: {total_copies}")

    def _render_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.collection_rows))

        for row_index, row in enumerate(self.collection_rows):
            name_item = QTableWidgetItem(row["name"])

            count_item = NumericTableItem(str(row["count"]))
            count_item.setData(Qt.UserRole, int(row["count"]))

            set_item = QTableWidgetItem(row["set_code"])
            rarity_item = QTableWidgetItem(row["rarity"])
            finish_item = QTableWidgetItem(row["finish"])

            price_item = NumericTableItem(row["price_str"])
            price_item.setData(Qt.UserRole, float(row["price_value"]))

            self.table.setItem(row_index, 0, name_item)
            self.table.setItem(row_index, 1, count_item)
            self.table.setItem(row_index, 2, set_item)
            self.table.setItem(row_index, 3, rarity_item)
            self.table.setItem(row_index, 4, finish_item)
            self.table.setItem(row_index, 5, price_item)

        self.table.setSortingEnabled(True)
        self.table.sortItems(5, Qt.DescendingOrder)

    def _render_grid(self):
        self.grid_list.clear()
        self._update_grid_metrics()
        for row in self.collection_rows:
            pixmap = self._get_card_pixmap(row.get("image_url"))
            icon = QIcon(pixmap)
            text = (
                f"{row['name']}\n"
                f"x{row['count']} • {row['set_code']} #{row['collector_number']}\n"
                f"{row['finish']} • {row['rarity']}\n"
                f"{row['price_str']}"
            )
            item = QListWidgetItem(icon, text)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setSizeHint(QSize(self.grid_list.gridSize().width() - 10, 300))
            self.grid_list.addItem(item)


def main():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
