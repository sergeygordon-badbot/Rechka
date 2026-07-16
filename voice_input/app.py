from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QKeySequenceEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .audio import AudioClip, AudioRecorder, list_input_devices
from .config import (
    AI_TARGET_OPTIONS,
    DECODING_BEAM_SIZES,
    DECODING_OPTIONS,
    INSERTION_OPTIONS,
    LANGUAGE_OPTIONS,
    MODEL_OPTIONS,
    OUTPUT_MODE_OPTIONS,
    AppConfig,
    load_config,
    save_config,
)
from .engine import (
    WhisperEngine,
    merge_incremental_transcript,
    normalize_transcript,
)
from .hotkeys import HOTKEY_OPTIONS, hotkey_label, parse_hotkey
from .prompting import ProcessedText, process_transcript
from .updater import (
    UpdateInfo,
    check_for_update,
    configured_repository,
    download_update,
    launch_update_installer,
)
from .windows import (
    GlobalHotkey,
    autostart_command,
    consume_show_settings_event,
    insert_text,
    play_feedback,
    set_autostart,
)


BG = "#F3F1EB"
CARD = "#FFFFFF"
CARD_LIGHT = "#F8F7F3"
BORDER = "#DDDAD2"
TEXT = "#171816"
MUTED = "#696A63"
ACCENT = "#171816"
ACID = "#C7FF36"
MINT = "#71E5BD"
RECORD = "#E5484D"
SUCCESS = "#10A37F"


def _reverse_map(mapping: dict[str, str]) -> dict[str, str]:
    return {label: key for key, label in mapping.items()}


class VoiceLevelWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(300)
        self.setFixedHeight(38)
        self._levels = [0.04] * 28
        self._smoothed = 0.0

    def reset(self) -> None:
        self._levels = [0.04] * 28
        self._smoothed = 0.0
        self.update()

    def set_level(self, level: float) -> None:
        target = max(0.0, min(1.0, level))
        if target >= self._smoothed:
            self._smoothed = target * 0.72 + self._smoothed * 0.28
        else:
            self._smoothed = max(target, self._smoothed * 0.84)
        self._levels.pop(0)
        self._levels.append(max(0.04, self._smoothed))
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        painter.setPen(QColor(BORDER))
        painter.drawLine(0, height // 2, width, height // 2)
        painter.setPen(Qt.PenStyle.NoPen)
        gap = 3
        bar_width = max(2, (width - gap * (len(self._levels) - 1)) // len(self._levels))
        total_width = bar_width * len(self._levels) + gap * (len(self._levels) - 1)
        start_x = (width - total_width) // 2
        for index, level in enumerate(self._levels):
            bar_height = max(3, int((height - 4) * level))
            x = start_x + index * (bar_width + gap)
            y = (height - bar_height) // 2
            color = QColor(RECORD if level > 0.14 else "#9CA3AF")
            color.setAlpha(245 if level > 0.14 else 145)
            painter.setBrush(color)
            painter.drawRoundedRect(x, y, bar_width, bar_height, 2, 2)
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self, hide_callback: Any) -> None:
        super().__init__()
        self._hide_callback = hide_callback
        self.allow_close = False
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.allow_close:
            event.accept()
            return
        event.ignore()
        self._hide_callback()


class WindowTitleBar(QFrame):
    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self._window = window
        self._drag_offset = None
        self.setObjectName("windowTitleBar")
        self.setFixedHeight(54)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self._window.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._window.move(
                event.globalPosition().toPoint() - self._drag_offset
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class VoiceInputApp:
    def __init__(
        self,
        application: QApplication,
        start_minimized: bool = False,
        show_settings_event: int = 0,
    ) -> None:
        self.application = application
        self.config = load_config()
        try:
            save_config(self.config)
        except OSError:
            pass
        self.start_minimized = start_minimized or self.config.start_minimized
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.recorder = AudioRecorder()
        self.engine = WhisperEngine()
        self.preview_engine = WhisperEngine(cpu_threads=4)
        self.hotkey = GlobalHotkey()
        self._registered_hotkey: str | None = None
        self.state = "loading"
        self._closing = False
        self._model_generation = 0
        self._preview_model_loading = False
        self._preview_ready = False
        self._recording_session = 0
        self._preview_stop: threading.Event | None = None
        self._recording_started_at = 0.0
        self._latest_preview_text = ""
        self._recording_warning = ""
        self._active_output_mode = self.config.output_mode
        self._active_ai_target = self.config.ai_target
        self._active_project_context = self.config.project_context
        self._update_repository = configured_repository()
        self._update_in_progress = False
        self._show_settings_event = show_settings_event
        self.devices: list[dict[str, Any]] = []

        self.window = MainWindow(self.hide_window)
        self._build_window()
        self._build_overlay()
        self._refresh_devices()
        self._populate_from_config()
        self._sync_autostart()
        self.mode_combo.currentIndexChanged.connect(self._on_output_mode_changed)
        self.target_combo.currentIndexChanged.connect(self._on_ai_target_changed)
        self._start_tray()
        self._register_hotkey()
        self._load_model(self.config.model)

        self.timer = QTimer()
        self.timer.timeout.connect(self._process_events)
        self.timer.start(50)
        if self._update_repository:
            QTimer.singleShot(5000, lambda: self.check_for_updates(manual=False))

        if self.start_minimized:
            self.window.hide()
        else:
            self.window.show()

    def _build_window(self) -> None:
        self.window.setWindowTitle("Речка")
        self.window.resize(760, 760)
        self.window.setMinimumSize(660, 650)
        self.window.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {BG};
                color: {TEXT};
                font-family: "Segoe UI";
                font-size: 9.5pt;
            }}
            QFrame#windowTitleBar {{
                background: {CARD};
                border: 0;
                border-bottom: 1px solid {BORDER};
            }}
            QFrame#windowTitleBar QLabel {{
                background: transparent;
                border: 0;
            }}
            QTabWidget::pane {{
                border: 0;
                background: transparent;
                top: 8px;
            }}
            QTabBar::tab {{
                background: {CARD};
                color: {MUTED};
                min-width: 112px;
                padding: 9px 16px;
                margin-right: 7px;
                border: 1px solid {BORDER};
                border-radius: 17px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                color: white;
                background: {TEXT};
                border-color: {TEXT};
            }}
            QComboBox, QLineEdit, QKeySequenceEdit, QTextEdit {{
                background: {CARD};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 10px;
                padding: 8px 10px;
                min-height: 20px;
                selection-background-color: #D1D5DB;
                selection-color: {TEXT};
            }}
            QComboBox:focus, QLineEdit:focus, QKeySequenceEdit:focus,
            QTextEdit:focus {{
                border: 1px solid {TEXT};
            }}
            QComboBox::drop-down {{
                border: 0;
                width: 28px;
            }}
            QComboBox QAbstractItemView {{
                background: {CARD};
                color: {TEXT};
                border: 1px solid {BORDER};
                selection-background-color: {CARD_LIGHT};
                selection-color: {TEXT};
                outline: 0;
            }}
            QCheckBox {{
                color: {TEXT};
                spacing: 7px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
            }}
            QProgressBar {{
                background: {CARD_LIGHT};
                border: 0;
                border-radius: 2px;
                height: 4px;
            }}
            QProgressBar::chunk {{
                background: {ACCENT};
                border-radius: 2px;
            }}
            QScrollArea {{
                background: transparent;
                border: 0;
            }}
            QScrollBar:vertical {{
                width: 7px;
                margin: 2px 0;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                min-height: 32px;
                border-radius: 3px;
                background: #C8C6BE;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            """
        )

        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        title_bar = WindowTitleBar(self.window)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(18, 0, 10, 0)
        title_layout.setSpacing(10)

        icon = self._make_icon(ACCENT)
        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(30, 30))
        icon_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        title_layout.addWidget(icon_label)

        brand = QLabel("Речка")
        brand.setStyleSheet(
            f"color: {TEXT}; font-size: 12pt; font-weight: 700; border: 0;"
        )
        brand.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        title_layout.addWidget(brand)

        local_badge = QLabel("ЛОКАЛЬНО")
        local_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        local_badge.setFixedHeight(24)
        local_badge.setStyleSheet(
            f"color: {TEXT}; background: {ACID}; border-radius: 8px; "
            "padding: 0 8px; font-size: 7pt; font-weight: 700;"
        )
        local_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        title_layout.addWidget(local_badge)
        title_layout.addStretch()

        minimize = QPushButton("—")
        minimize.setObjectName("windowControl")
        minimize.setFixedSize(36, 32)
        minimize.setToolTip("Свернуть")
        minimize.clicked.connect(self.window.showMinimized)
        close = QPushButton("×")
        close.setObjectName("windowControl")
        close.setFixedSize(36, 32)
        close.setToolTip("Скрыть в область уведомлений")
        close.clicked.connect(self.hide_window)
        for button in (minimize, close):
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent;
                    color: {MUTED};
                    border: 0;
                    border-radius: 8px;
                    font-size: 14pt;
                }}
                QPushButton:hover {{
                    background: {CARD_LIGHT};
                    color: {TEXT};
                }}
                """
            )
            title_layout.addWidget(button)
        root_layout.addWidget(title_bar)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dictation_tab(), "Диктовка")
        self.tabs.addTab(self._build_settings_tab(), "Настройки")
        self.tabs.setDocumentMode(True)
        self.tabs.setContentsMargins(24, 18, 24, 22)
        root_layout.addWidget(self.tabs, 1)
        self.window.setCentralWidget(central)

        self.window.setWindowIcon(icon)
        self.application.setWindowIcon(icon)

    def _build_dictation_tab(self) -> QWidget:
        tab = QWidget()
        tab.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("Голос в текст")
        heading.setStyleSheet(
            f"color: {TEXT}; font-size: 20pt; font-weight: 750; "
            "letter-spacing: -0.5px;"
        )
        layout.addWidget(heading)
        subtitle = QLabel(
            "Выберите режим, нажмите горячую клавишу и говорите свободно."
        )
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        layout.addWidget(subtitle)

        mode_card = QFrame()
        mode_card.setStyleSheet(
            f"QFrame {{ background: {CARD}; border: 1px solid {BORDER}; "
            "border-radius: 14px; }}"
            "QLabel { border: 0; background: transparent; }"
        )
        mode_layout = QVBoxLayout(mode_card)
        mode_layout.setContentsMargins(16, 14, 16, 14)
        mode_layout.setSpacing(9)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Режим")
        mode_label.setStyleSheet(f"color: {TEXT}; font-weight: 650;")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(OUTPUT_MODE_OPTIONS.values()))
        self.mode_combo.setMinimumWidth(360)
        mode_row.addWidget(mode_label)
        mode_row.addStretch()
        mode_row.addWidget(self.mode_combo)
        mode_layout.addLayout(mode_row)

        target_row = QHBoxLayout()
        self.target_label = QLabel("AI-система")
        self.target_label.setStyleSheet(f"color: {TEXT}; font-weight: 650;")
        self.target_combo = QComboBox()
        self.target_combo.addItems(list(AI_TARGET_OPTIONS.values()))
        self.target_combo.setMinimumWidth(360)
        target_row.addWidget(self.target_label)
        target_row.addStretch()
        target_row.addWidget(self.target_combo)
        mode_layout.addLayout(target_row)

        self.mode_description = QLabel()
        self.mode_description.setWordWrap(True)
        self.mode_description.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        mode_layout.addWidget(self.mode_description)
        layout.addWidget(mode_card)

        record_card = QFrame()
        record_card.setStyleSheet(
            f"QFrame {{ background: {TEXT}; border: 0; border-radius: 16px; }}"
            "QLabel { border: 0; background: transparent; }"
        )
        record_layout = QVBoxLayout(record_card)
        record_layout.setContentsMargins(22, 18, 22, 18)
        record_layout.setSpacing(10)
        self.status_label = QLabel("Подготовка…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "color: #D8D8D2; font-size: 9pt; font-weight: 600;"
        )
        record_layout.addWidget(self.status_label)

        progress_row = QHBoxLayout()
        progress_row.addStretch()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(240)
        progress_row.addWidget(self.progress)
        progress_row.addStretch()
        record_layout.addLayout(progress_row)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.record_button = QPushButton("Загрузка модели")
        self.record_button.setEnabled(False)
        self.record_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.record_button.setMinimumWidth(230)
        self.record_button.clicked.connect(self.toggle_recording)
        self._style_primary_button(self.record_button, ACID)
        button_row.addWidget(self.record_button)
        button_row.addStretch()
        record_layout.addLayout(button_row)

        self.hotkey_hint = QLabel()
        self.hotkey_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hotkey_hint.setStyleSheet("color: #A8AAA3; font-size: 8.5pt;")
        record_layout.addWidget(self.hotkey_hint)
        layout.addWidget(record_card)

        result_card = QFrame()
        result_card.setStyleSheet(
            f"QFrame {{ background: {CARD}; border: 1px solid {BORDER}; "
            "border-radius: 14px; }}"
            "QLabel { border: 0; background: transparent; }"
        )
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(16, 13, 16, 15)
        result_layout.setSpacing(9)
        result_header = QHBoxLayout()
        last_label = QLabel("Последний результат")
        last_label.setStyleSheet(f"color: {TEXT}; font-weight: 650;")
        result_header.addWidget(last_label)
        result_header.addStretch()
        copy_result = QPushButton("Копировать")
        clear_result = QPushButton("Очистить")
        for button in (copy_result, clear_result):
            self._style_secondary_button(button)
            result_header.addWidget(button)
        copy_result.clicked.connect(self._copy_last_result)
        clear_result.clicked.connect(self._clear_last_result)
        result_layout.addLayout(result_header)

        self.last_text = QTextEdit()
        self.last_text.setReadOnly(True)
        self.last_text.setPlainText("Здесь появится распознанный текст.")
        self.last_text.setMinimumHeight(150)
        result_layout.addWidget(self.last_text, 1)
        layout.addWidget(result_card, 1)
        return tab

    def _build_settings_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setStyleSheet("background: transparent;")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(24, 24, 17, 22)
        outer.setSpacing(14)

        heading = QLabel("Настройки")
        heading.setStyleSheet(
            f"color: {TEXT}; font-size: 20pt; font-weight: 750; "
            "letter-spacing: -0.5px;"
        )
        outer.addWidget(heading)
        subtitle = QLabel(
            "Горячая клавиша, микрофон, скорость и обработка текста."
        )
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        outer.addWidget(subtitle)

        def make_section(title: str) -> tuple[QFrame, QVBoxLayout]:
            frame = QFrame()
            frame.setStyleSheet(
                f"QFrame {{ background: {CARD}; border: 1px solid {BORDER}; "
                "border-radius: 14px; }}"
                "QLabel, QCheckBox { border: 0; background: transparent; }"
            )
            section_layout = QVBoxLayout(frame)
            section_layout.setContentsMargins(16, 14, 16, 15)
            section_layout.setSpacing(10)
            label = QLabel(title)
            label.setStyleSheet(
                f"color: {TEXT}; font-size: 10pt; font-weight: 700;"
            )
            section_layout.addWidget(label)
            return frame, section_layout

        def make_form() -> QFormLayout:
            form = QFormLayout()
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(9)
            form.setLabelAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            return form

        controls, controls_layout = make_section("Управление")
        controls_form = make_form()
        self.hotkey_combo = QComboBox()
        self.hotkey_combo.addItems(
            [*HOTKEY_OPTIONS.values(), "Своя комбинация…"]
        )
        controls_form.addRow("Горячая клавиша", self.hotkey_combo)

        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setClearButtonEnabled(True)
        self.hotkey_edit.setToolTip(
            "Нажмите одну комбинацию. Например Ctrl+Alt+R или Ctrl+Shift+F9."
        )
        controls_form.addRow("Своя комбинация", self.hotkey_edit)
        self.hotkey_edit_label = controls_form.labelForField(self.hotkey_edit)
        self.hotkey_combo.currentIndexChanged.connect(
            self._update_custom_hotkey_visibility
        )

        self.insertion_combo = QComboBox()
        self.insertion_combo.addItems(list(INSERTION_OPTIONS.values()))
        controls_form.addRow("Вставка текста", self.insertion_combo)
        controls_layout.addLayout(controls_form)
        controls_hint = QLabel(
            "Повторный запуск ярлыка откроет это окно, даже если «Речка» "
            "уже работает в области уведомлений."
        )
        controls_hint.setWordWrap(True)
        controls_hint.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        controls_layout.addWidget(controls_hint)
        outer.addWidget(controls)

        recognition, recognition_layout = make_section("Распознавание")
        recognition_form = make_form()
        self.model_combo = QComboBox()
        self.model_combo.addItems(list(MODEL_OPTIONS.values()))
        recognition_form.addRow("Модель", self.model_combo)

        self.decoding_combo = QComboBox()
        self.decoding_combo.addItems(list(DECODING_OPTIONS.values()))
        recognition_form.addRow("Скорость", self.decoding_combo)

        self.language_combo = QComboBox()
        self.language_combo.addItems(list(LANGUAGE_OPTIONS.values()))
        recognition_form.addRow("Язык", self.language_combo)

        self.device_combo = QComboBox()
        recognition_form.addRow("Микрофон", self.device_combo)

        self.custom_terms_edit = QLineEdit()
        self.custom_terms_edit.setPlaceholderText(
            "Например: Codex, PostgreSQL, Гастроконсьерж"
        )
        recognition_form.addRow("Слова и названия", self.custom_terms_edit)
        recognition_layout.addLayout(recognition_form)
        refresh = QPushButton("Обновить список микрофонов")
        self._style_secondary_button(refresh)
        refresh.clicked.connect(self._refresh_devices)
        recognition_layout.addWidget(
            refresh,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        outer.addWidget(recognition)

        processing, processing_layout = make_section("Обработка текста")
        processing_form = make_form()
        self.project_context_edit = QLineEdit()
        self.project_context_edit.setPlaceholderText(
            "Например: Windows-приложение, Python, важна приватность"
        )
        processing_form.addRow("Контекст проекта", self.project_context_edit)

        self.ollama_model_edit = QLineEdit()
        self.ollama_model_edit.setPlaceholderText("qwen3:4b")
        processing_form.addRow("AI-модель Ollama", self.ollama_model_edit)
        processing_layout.addLayout(processing_form)

        self.append_space_check = QCheckBox("Добавлять пробел после вставки")
        self.commands_check = QCheckBox(
            "Понимать «новая строка», «поставь точку»"
        )
        self.use_local_ai_check = QCheckBox(
            "Обрабатывать через Ollama — качественнее, но заметно медленнее"
        )
        self.sound_feedback_check = QCheckBox("Мягкий звук начала и остановки")
        for check in (
            self.append_space_check,
            self.commands_check,
            self.use_local_ai_check,
            self.sound_feedback_check,
        ):
            processing_layout.addWidget(check)
        outer.addWidget(processing)

        system, system_layout = make_section("Запуск и обновления")
        self.start_minimized_check = QCheckBox("Запускать свёрнутой")
        self.autostart_check = QCheckBox("Запускать вместе с Windows")
        system_layout.addWidget(self.start_minimized_check)
        system_layout.addWidget(self.autostart_check)
        self.update_button = QPushButton("Проверить обновления")
        self._style_secondary_button(self.update_button)
        self.update_button.clicked.connect(
            lambda: self.check_for_updates(manual=True)
        )
        system_layout.addWidget(
            self.update_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        self.update_status = QLabel("Обновления устанавливаются поверх текущей версии.")
        self.update_status.setWordWrap(True)
        self.update_status.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        system_layout.addWidget(self.update_status)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.hide()
        system_layout.addWidget(self.update_progress)
        outer.addWidget(system)

        actions = QHBoxLayout()
        version_label = QLabel(f"Версия {__version__}")
        version_label.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        actions.addWidget(version_label)
        actions.addStretch()
        save = QPushButton("Сохранить настройки")
        self._style_primary_button(save, ACCENT, compact=True)
        save.clicked.connect(self.save_settings)
        actions.addWidget(save)
        outer.addLayout(actions)

        scroll.setWidget(content)
        return scroll

    def _style_primary_button(
        self,
        button: QPushButton,
        color: str,
        compact: bool = False,
    ) -> None:
        vertical = 7 if compact else 12
        horizontal = 18 if compact else 28
        if color == RECORD:
            hover = "#C93C41"
            text_color = "white"
        elif color == ACID:
            hover = "#B7EE31"
            text_color = TEXT
        else:
            hover = "#343541"
            text_color = "white"
        button.setStyleSheet(
            f"""
            QPushButton {{
                background: {color};
                color: {text_color};
                border: 1px solid {color};
                border-radius: 10px;
                padding: {vertical}px {horizontal}px;
                font-weight: 650;
            }}
            QPushButton:hover {{ background: {hover}; border-color: {hover}; }}
            QPushButton:disabled {{
                background: {CARD_LIGHT};
                border-color: {BORDER};
                color: #9CA3AF;
            }}
            """
        )

    def _style_secondary_button(self, button: QPushButton) -> None:
        button.setStyleSheet(
            f"""
            QPushButton {{
                background: {CARD};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 7px 12px;
            }}
            QPushButton:hover {{ background: {CARD_LIGHT}; }}
            """
        )

    def _build_overlay(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.overlay = QFrame(None, flags)
        self.overlay.setObjectName("voiceOverlay")
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.overlay.setFixedWidth(580)
        self.overlay.setStyleSheet(
            f"""
            QFrame#voiceOverlay {{
                background: {CARD};
                border: 1px solid {BORDER};
                border-radius: 12px;
            }}
            """
        )
        layout = QVBoxLayout(self.overlay)
        layout.setContentsMargins(15, 10, 15, 12)
        layout.setSpacing(7)

        header = QHBoxLayout()
        header.setSpacing(7)
        self.overlay_dot = QLabel("●")
        self.overlay_dot.setStyleSheet(f"color: {RECORD}; font-size: 10pt; border: 0;")
        self.overlay_state_text = QLabel("Слушаю…")
        self.overlay_state_text.setStyleSheet(
            f"color: {TEXT}; font-weight: 500; border: 0;"
        )
        self.overlay_elapsed = QLabel("00:00")
        self.overlay_elapsed.setStyleSheet(
            f"color: {MUTED}; font-size: 9pt; border: 0;"
        )
        header.addWidget(self.overlay_dot)
        header.addWidget(self.overlay_state_text)
        header.addStretch()
        header.addWidget(self.overlay_elapsed)
        layout.addLayout(header)

        self.overlay_audio_state = QLabel("Микрофон подключён · начинайте говорить")
        self.overlay_audio_state.setStyleSheet(
            f"color: {MUTED}; font-size: 8.5pt; border: 0;"
        )
        layout.addWidget(self.overlay_audio_state)

        self.voice_level = VoiceLevelWidget()
        layout.addWidget(self.voice_level)

        self.overlay_preview = QTextEdit()
        self.overlay_preview.setReadOnly(True)
        self.overlay_preview.setAcceptRichText(False)
        self.overlay_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.overlay_preview.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.overlay_preview.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.overlay_preview.setFixedHeight(92)
        self.overlay_preview.setStyleSheet(
            f"color: {TEXT}; font-size: 9.5pt; border: 1px solid {BORDER}; "
            f"background: {CARD_LIGHT}; border-radius: 8px; padding: 7px; "
            "selection-background-color: transparent;"
        )
        layout.addWidget(self.overlay_preview)
        self._set_overlay_preview("Черновик появится через несколько секунд.")
        self.overlay.hide()

    def _position_overlay(self) -> None:
        self.overlay.adjustSize()
        screen = self.application.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            x = area.center().x() - self.overlay.width() // 2
            y = area.bottom() - self.overlay.height() - 36
            self.overlay.move(x, y)

    def _show_overlay(
        self,
        text: str,
        color: str,
        preview: str | None = None,
    ) -> None:
        self.overlay_state_text.setText(text)
        self.overlay_dot.setStyleSheet(f"color: {color}; font-size: 10pt; border: 0;")
        if preview is not None:
            self._set_overlay_preview(preview)
        self._position_overlay()
        self.overlay.show()
        self.overlay.raise_()

    def _set_overlay_preview(self, text: str) -> None:
        self.overlay_preview.setPlainText(text)
        self.overlay_preview.moveCursor(QTextCursor.MoveOperation.End)
        self.overlay_preview.ensureCursorVisible()
        scrollbar = self.overlay_preview.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        QTimer.singleShot(
            0,
            lambda: scrollbar.setValue(scrollbar.maximum()),
        )

    def _make_icon(self, color: str) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#C7FF36"))
        painter.drawRoundedRect(6, 6, 54, 54, 14, 14)
        painter.setBrush(QColor("#F3F1EB"))
        painter.drawRoundedRect(3, 3, 54, 54, 14, 14)
        painter.setBrush(QColor("#171816"))
        painter.drawRoundedRect(11, 11, 40, 40, 11, 11)

        for index, (x, y, width, height, radius) in enumerate(
            (
                (17, 24, 3, 11, 2),
                (23, 20, 3, 19, 2),
                (29, 16, 3, 27, 2),
                (35, 20, 3, 19, 2),
                (41, 24, 3, 11, 2),
            )
        ):
            painter.setBrush(QColor("#C7FF36" if index % 2 == 0 else "#71E5BD"))
            painter.drawRoundedRect(x, y, width, height, radius, radius)

        painter.setBrush(QColor("#F3F1EB"))
        painter.drawRoundedRect(17, 43, 29, 2, 1, 1)
        painter.drawRoundedRect(25, 47, 21, 2, 1, 1)
        painter.drawEllipse(43, 43, 16, 16)
        painter.setBrush(QColor(color))
        painter.drawEllipse(47, 47, 8, 8)
        painter.end()
        return QIcon(pixmap)

    def _start_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._make_icon(ACCENT), self.window)
        self.tray.setToolTip("Речка")
        menu = QMenu()
        toggle_action = QAction("Начать / остановить", menu)
        toggle_action.triggered.connect(lambda: self.events.put(("toggle", None)))
        show_action = QAction("Открыть настройки", menu)
        show_action.triggered.connect(
            lambda: self.events.put(("settings", None))
        )
        exit_action = QAction("Выход", menu)
        exit_action.triggered.connect(lambda: self.events.put(("exit", None)))
        menu.addAction(toggle_action)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def _set_tray_color(self, color: str) -> None:
        self.tray.setIcon(self._make_icon(color))

    def _refresh_devices(self) -> None:
        try:
            self.devices = list_input_devices()
        except Exception as exc:
            QMessageBox.critical(
                self.window,
                "Микрофон",
                f"Не удалось получить список устройств:\n{exc}",
            )
            return

        selected_device = self.config.device_index
        self.device_combo.clear()
        self.device_combo.addItem("Системный микрофон по умолчанию", None)
        for device in self.devices:
            suffix = " — по умолчанию" if device["is_default"] else ""
            label = f'{device["index"]}: {device["name"]}{suffix}'
            self.device_combo.addItem(label, int(device["index"]))

        index = self.device_combo.findData(selected_device)
        self.device_combo.setCurrentIndex(index if index >= 0 else 0)

    def _populate_from_config(self) -> None:
        self.model_combo.setCurrentText(MODEL_OPTIONS[self.config.model])
        self.decoding_combo.setCurrentText(
            DECODING_OPTIONS[self.config.decoding_mode]
        )
        self.mode_combo.setCurrentText(OUTPUT_MODE_OPTIONS[self.config.output_mode])
        self.target_combo.setCurrentText(AI_TARGET_OPTIONS[self.config.ai_target])
        self.language_combo.setCurrentText(LANGUAGE_OPTIONS[self.config.language])
        self.insertion_combo.setCurrentText(INSERTION_OPTIONS[self.config.insertion_mode])
        if self.config.hotkey in HOTKEY_OPTIONS:
            self.hotkey_combo.setCurrentText(HOTKEY_OPTIONS[self.config.hotkey])
        else:
            self.hotkey_combo.setCurrentText("Своя комбинация…")
            self.hotkey_edit.setKeySequence(QKeySequence(self.config.hotkey))
        self._update_custom_hotkey_visibility()
        self.append_space_check.setChecked(self.config.append_space)
        self.commands_check.setChecked(self.config.punctuation_commands)
        self.use_local_ai_check.setChecked(self.config.use_local_ai)
        self.sound_feedback_check.setChecked(self.config.sound_feedback)
        self.start_minimized_check.setChecked(self.config.start_minimized)
        self.autostart_check.setChecked(self.config.autostart)
        self.custom_terms_edit.setText(self.config.custom_terms)
        self.project_context_edit.setText(self.config.project_context)
        self.ollama_model_edit.setText(self.config.ollama_model)
        self._update_mode_description()
        self.hotkey_hint.setText(
            f"Нажмите {hotkey_label(self.config.hotkey)} для начала и остановки"
        )

    def _sync_autostart(self, *, silent: bool = True) -> None:
        if os.environ.get("VOICE_INPUT_DATA_DIR"):
            return
        try:
            main_script = Path(__file__).resolve().parents[1] / "main.py"
            set_autostart(
                self.config.autostart,
                autostart_command(main_script),
            )
        except OSError:
            if not silent:
                raise

    def _update_custom_hotkey_visibility(self, _index: int = -1) -> None:
        visible = self.hotkey_combo.currentText() == "Своя комбинация…"
        self.hotkey_edit.setVisible(visible)
        if self.hotkey_edit_label is not None:
            self.hotkey_edit_label.setVisible(visible)

    def _selected_hotkey(self) -> str:
        reverse_hotkeys = _reverse_map(HOTKEY_OPTIONS)
        selected = reverse_hotkeys.get(self.hotkey_combo.currentText())
        if selected:
            return selected
        portable = self.hotkey_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        )
        if not portable:
            raise ValueError("Нажмите пользовательскую комбинацию клавиш.")
        return parse_hotkey(portable).canonical

    def _update_mode_description(self) -> None:
        is_ai_prompt = self.config.output_mode == "ai_prompt"
        self.target_label.setVisible(is_ai_prompt)
        self.target_combo.setVisible(is_ai_prompt)
        self.target_combo.setEnabled(is_ai_prompt and self.state != "recording")
        if is_ai_prompt:
            text = (
                "Поток мыслей будет превращён в понятную задачу для нейросети. "
                "Выберите ChatGPT, Claude, Gemini или универсальный формат."
            )
        else:
            text = (
                "Текст останется близким к вашей речи: исправятся пунктуация, "
                "явные речевые повторы и случайные оговорки."
            )
        self.mode_description.setText(text)

    def _on_output_mode_changed(self, _index: int = -1) -> None:
        reverse_modes = _reverse_map(OUTPUT_MODE_OPTIONS)
        self.config.output_mode = reverse_modes.get(
            self.mode_combo.currentText(),
            "communication",
        )
        self._update_mode_description()
        save_config(self.config)

    def _on_ai_target_changed(self, _index: int = -1) -> None:
        reverse_targets = _reverse_map(AI_TARGET_OPTIONS)
        self.config.ai_target = reverse_targets.get(
            self.target_combo.currentText(),
            "universal",
        )
        save_config(self.config)

    @staticmethod
    def _mode_short_name(mode: str) -> str:
        return "AI-промпт" if mode == "ai_prompt" else "Общение"

    def _register_hotkey(
        self,
        value: str | None = None,
        *,
        interactive: bool = False,
    ) -> bool:
        hotkey_value = value or self.config.hotkey
        candidate = GlobalHotkey()
        try:
            candidate.start(
                hotkey_value,
                lambda: self.events.put(("toggle", None)),
            )
        except Exception as exc:
            candidate.stop()
            text = f"Не удалось включить {hotkey_label(hotkey_value)}:\n{exc}"
            if interactive:
                QMessageBox.warning(self.window, "Горячая клавиша", text)
            else:
                self.events.put(("hotkey_error", text))
            return False

        previous = self.hotkey
        self.hotkey = candidate
        self._registered_hotkey = hotkey_value
        previous.stop()
        return True

    def _load_model(self, model_name: str) -> None:
        self._model_generation += 1
        generation = self._model_generation
        self.state = "loading"
        self._set_status("Подготовка Whisper…", MUTED)
        self.record_button.setText("Загрузка модели")
        self.record_button.setEnabled(False)
        self._style_primary_button(self.record_button, ACID)
        self.progress.show()

        def worker() -> None:
            try:
                self.engine.load(
                    model_name,
                    status=lambda text: self.events.put(
                        ("model_status", (generation, text))
                    ),
                )
                self.events.put(("model_ready", generation))
            except Exception as exc:
                self.events.put(("model_error", (generation, str(exc))))

        threading.Thread(target=worker, name="model-loader", daemon=True).start()

    def _load_preview_model(self) -> None:
        if self._preview_ready or self._preview_model_loading or self._closing:
            return
        self._preview_model_loading = True

        def worker() -> None:
            try:
                self.preview_engine.load("tiny")
                self.events.put(("preview_model_ready", None))
            except Exception as exc:
                self.events.put(("preview_model_error", str(exc)))

        threading.Thread(
            target=worker,
            name="preview-model-loader",
            daemon=True,
        ).start()

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status_label.setText(text)
        display_color = "#D8D8D2" if color in {MUTED, ACCENT} else color
        self.status_label.setStyleSheet(
            f"color: {display_color}; font-weight: 600;"
        )

    def toggle_recording(self) -> None:
        if self.state == "ready":
            self._start_recording()
        elif self.state == "recording":
            self._stop_recording()
        elif self.state == "loading":
            self._set_status("Подождите: модель ещё загружается", MUTED)
        elif self.state == "transcribing":
            self._set_status("Распознавание уже выполняется", MUTED)

    def _start_recording(self) -> None:
        if self.config.sound_feedback:
            play_feedback("start")
        try:
            sample_rate = self.recorder.start(self.config.device_index)
        except Exception as exc:
            self._handle_error(f"Не удалось начать запись: {exc}")
            return

        self._recording_session += 1
        session = self._recording_session
        self._active_output_mode = self.config.output_mode
        self._active_ai_target = self.config.ai_target
        self._active_project_context = self.config.project_context
        self._preview_stop = threading.Event()
        self._recording_started_at = time.monotonic()
        self._latest_preview_text = ""
        self._recording_warning = ""
        self.state = "recording"
        mode_name = self._mode_short_name(self._active_output_mode)
        self._set_status(
            f"{mode_name}: слушаю… микрофон {sample_rate} Гц",
            RECORD,
        )
        self.record_button.setText("Остановить и вставить")
        self._style_primary_button(self.record_button, RECORD)
        self.mode_combo.setEnabled(False)
        self.target_combo.setEnabled(False)
        self.voice_level.reset()
        self.overlay_elapsed.setText("00:00")
        self.overlay_audio_state.setText("Микрофон подключён · начинайте говорить")
        self.overlay_audio_state.setStyleSheet(
            f"color: {MUTED}; font-size: 8.5pt; border: 0;"
        )
        self._show_overlay(
            f"{mode_name} · идёт запись",
            RECORD,
            (
                "Черновик появится через 2–4 секунды."
                if self._preview_ready
                else "Подготавливаю быстрый черновик — запись уже идёт."
            ),
        )
        self._set_tray_color(RECORD)
        threading.Thread(
            target=self._live_preview_worker,
            args=(session, self._preview_stop),
            name="live-preview",
            daemon=True,
        ).start()

    def _stop_recording(self) -> None:
        if self._preview_stop is not None:
            self._preview_stop.set()
        try:
            clip = self.recorder.stop()
        except Exception as exc:
            self._handle_error(f"Ошибка завершения записи: {exc}")
            return

        if clip.status_messages:
            self._recording_warning = (
                "Во время записи микрофон сообщил о пропуске аудиоданных."
            )

        if self.config.sound_feedback:
            play_feedback("stop")
        if clip.duration_seconds < 0.35 or clip.rms < 0.0015:
            self.state = "ready"
            self.overlay.hide()
            self._set_status("Речь не обнаружена — попробуйте ещё раз", MUTED)
            self.record_button.setText("Начать запись")
            self._style_primary_button(self.record_button, ACID)
            self.mode_combo.setEnabled(True)
            self._update_mode_description()
            self._set_tray_color(ACCENT)
            return

        self.state = "transcribing"
        mode = self._active_output_mode
        session = self._recording_session
        self._set_status("Перерабатываю всю записанную фразу…", ACCENT)
        self.record_button.setText("Распознавание…")
        self.record_button.setEnabled(False)
        self._style_primary_button(self.record_button, ACID)
        self.overlay_audio_state.setText("Запись завершена · обрабатываю результат")
        self._show_overlay(
            "Финальная расшифровка началась…",
            ACCENT,
            self._latest_preview_text or "Обрабатываю записанную фразу…",
        )
        self.voice_level.set_level(0.0)
        self._set_tray_color(ACCENT)
        threading.Thread(
            target=self._transcribe_worker,
            args=(clip, session, mode),
            name="transcriber",
            daemon=True,
        ).start()

    def _live_preview_worker(
        self,
        session: int,
        stop_event: threading.Event,
    ) -> None:
        committed_samples = 0
        stable_text = ""
        last_preview_total = 0
        minimum_first_samples = round(2.8 * 16_000)
        minimum_new_samples = round(1.6 * 16_000)
        overlap_samples = round(1.2 * 16_000)
        maximum_window_samples = round(12.0 * 16_000)
        stability_margin_seconds = 0.85
        if stop_event.wait(0.45):
            return
        while not stop_event.is_set():
            if not self._preview_ready:
                if stop_event.wait(0.25):
                    return
                continue

            total_samples = self.recorder.sample_count
            enough_audio = total_samples >= minimum_first_samples
            enough_new_audio = (
                total_samples - last_preview_total >= minimum_new_samples
            )
            if enough_audio and enough_new_audio:
                start_sample = max(0, committed_samples - overlap_samples)
                start_sample = max(
                    start_sample,
                    total_samples - maximum_window_samples,
                )
                clip = self.recorder.snapshot(start_sample=start_sample)
                if clip.rms < 0.0012:
                    committed_samples = total_samples
                    last_preview_total = total_samples
                    if stop_event.wait(0.2):
                        return
                    continue
                try:
                    segments = self.preview_engine.transcribe_segments(
                        clip.samples,
                        language=self.config.language,
                        beam_size=1,
                        custom_terms=self.config.custom_terms,
                        preview=True,
                    )
                except Exception as exc:
                    self.events.put(("preview_error", (session, str(exc))))
                    return
                if stop_event.is_set():
                    return
                current_text = normalize_transcript(
                    " ".join(segment.text for segment in segments),
                    punctuation_commands=self.config.punctuation_commands,
                )
                if current_text:
                    visible_text = merge_incremental_transcript(
                        stable_text,
                        current_text,
                    )
                    self.events.put(("preview", (session, visible_text)))

                stable_cutoff = max(
                    0.0,
                    clip.duration_seconds - stability_margin_seconds,
                )
                stable_segments = [
                    segment
                    for segment in segments
                    if segment.end <= stable_cutoff
                ]
                if stable_segments:
                    stable_part = normalize_transcript(
                        " ".join(segment.text for segment in stable_segments),
                        punctuation_commands=self.config.punctuation_commands,
                    )
                    stable_text = merge_incremental_transcript(
                        stable_text,
                        stable_part,
                    )
                    committed_samples = min(
                        total_samples,
                        clip.start_sample
                        + round(stable_segments[-1].end * 16_000),
                    )
                last_preview_total = total_samples
            if stop_event.wait(0.3):
                return

    def _transcribe_worker(
        self,
        clip: AudioClip,
        session: int,
        output_mode: str,
    ) -> None:
        try:
            text = self.engine.transcribe(
                clip.samples,
                language=self.config.language,
                beam_size=DECODING_BEAM_SIZES[self.config.decoding_mode],
                custom_terms=self.config.custom_terms,
                punctuation_commands=self.config.punctuation_commands,
            )
            if not text:
                self.events.put(
                    ("transcript", (session, text, ProcessedText(text="")))
                )
                return

            stage_text = (
                "Формирую понятный промпт для AI…"
                if output_mode == "ai_prompt"
                else "Аккуратно уточняю формулировку…"
            )
            self.events.put(
                (
                    "processing_stage",
                    (session, stage_text),
                )
            )
            processed = process_transcript(
                text,
                output_mode,
                use_local_ai=self.config.use_local_ai,
                ollama_model=self.config.ollama_model,
                ai_target=self._active_ai_target,
                project_context=self._active_project_context,
            )
            self.events.put(("transcript", (session, text, processed)))
        except Exception as exc:
            self.events.put(("error", f"Ошибка распознавания: {exc}"))

    def _handle_transcript(
        self,
        payload: tuple[int, str, ProcessedText],
    ) -> None:
        session, raw_text, processed = payload
        if session != self._recording_session:
            return
        text = processed.text
        self.overlay.hide()
        self.state = "ready"
        self.record_button.setText("Начать запись")
        self.record_button.setEnabled(True)
        self._style_primary_button(self.record_button, ACID)
        self.mode_combo.setEnabled(True)
        self._update_mode_description()
        self._set_tray_color(ACCENT)

        if not raw_text or not text:
            self._set_status("Whisper не нашёл речи", MUTED)
            return

        insertion_text = text
        if self.config.append_space and not insertion_text.endswith((" ", "\n", "\t")):
            insertion_text += " "

        inserted = True
        try:
            insert_text(insertion_text, self.config.insertion_mode)
        except Exception as exc:
            inserted = False
            self._handle_error(f"Текст распознан, но не вставлен: {exc}")

        self.last_text.setPlainText(text)
        if inserted:
            insertion_status = (
                "Готово — текст скопирован"
                if self.config.insertion_mode == "clipboard"
                else "Готово — текст вставлен"
            )
            status = (
                f"{insertion_status}. {processed.note}"
                if processed.note
                else insertion_status
            )
            if self._recording_warning:
                status = f"{status}. {self._recording_warning}"
            self._set_status(status, SUCCESS)

    def _handle_error(self, text: str) -> None:
        self.recorder.abort()
        if self._preview_stop is not None:
            self._preview_stop.set()
        self.overlay.hide()
        self.state = "ready" if self.engine.model_name else "error"
        self.record_button.setText(
            "Начать запись" if self.state == "ready" else "Ошибка"
        )
        self.record_button.setEnabled(self.state == "ready")
        self.mode_combo.setEnabled(self.state == "ready")
        self._update_mode_description()
        self._style_primary_button(
            self.record_button,
            ACID if self.state == "ready" else RECORD,
        )
        self._set_status(text, RECORD)
        self._set_tray_color(RECORD)
        if self.config.sound_feedback:
            play_feedback("error")

    def save_settings(self) -> None:
        reverse_models = _reverse_map(MODEL_OPTIONS)
        reverse_decoding = _reverse_map(DECODING_OPTIONS)
        reverse_modes = _reverse_map(OUTPUT_MODE_OPTIONS)
        reverse_targets = _reverse_map(AI_TARGET_OPTIONS)
        reverse_languages = _reverse_map(LANGUAGE_OPTIONS)
        reverse_insertions = _reverse_map(INSERTION_OPTIONS)

        old_model = self.config.model
        old_hotkey = self.config.hotkey
        try:
            selected_hotkey = self._selected_hotkey()
        except ValueError as exc:
            QMessageBox.warning(
                self.window,
                "Горячая клавиша",
                str(exc),
            )
            return
        if (
            selected_hotkey != old_hotkey
            or self._registered_hotkey != selected_hotkey
        ) and not self._register_hotkey(
            selected_hotkey,
            interactive=True,
        ):
            return

        self.config = AppConfig(
            model=reverse_models.get(self.model_combo.currentText(), "base"),
            decoding_mode=reverse_decoding.get(
                self.decoding_combo.currentText(),
                "fast",
            ),
            output_mode=reverse_modes.get(
                self.mode_combo.currentText(),
                "communication",
            ),
            ai_target=reverse_targets.get(
                self.target_combo.currentText(),
                "universal",
            ),
            language=reverse_languages.get(self.language_combo.currentText(), "ru"),
            device_index=self.device_combo.currentData(),
            insertion_mode=reverse_insertions.get(
                self.insertion_combo.currentText(),
                "paste",
            ),
            hotkey=selected_hotkey,
            append_space=self.append_space_check.isChecked(),
            punctuation_commands=self.commands_check.isChecked(),
            start_minimized=self.start_minimized_check.isChecked(),
            autostart=self.autostart_check.isChecked(),
            sound_feedback=self.sound_feedback_check.isChecked(),
            custom_terms=self.custom_terms_edit.text().strip(),
            project_context=self.project_context_edit.text().strip(),
            use_local_ai=self.use_local_ai_check.isChecked(),
            ollama_model=self.ollama_model_edit.text().strip() or "qwen3:4b",
            beam_size=DECODING_BEAM_SIZES[
                reverse_decoding.get(self.decoding_combo.currentText(), "fast")
            ],
        )
        save_config(self.config)

        try:
            self._sync_autostart(silent=False)
        except OSError as exc:
            QMessageBox.warning(
                self.window,
                "Автозапуск",
                f"Настройки сохранены, но автозапуск изменить не удалось:\n{exc}",
            )

        self.hotkey_hint.setText(
            f"Нажмите {hotkey_label(self.config.hotkey)} для начала и остановки"
        )
        self._update_mode_description()
        if self.config.model != old_model:
            self._load_model(self.config.model)
        else:
            self._set_status("Настройки сохранены", SUCCESS)

    def check_for_updates(self, manual: bool = True) -> None:
        if self._update_in_progress or self._closing:
            return
        if not self._update_repository:
            if manual:
                QMessageBox.information(
                    self.window,
                    "Обновления",
                    "Канал обновлений будет подключён при публикации первого "
                    "релиза.",
                )
            return

        self._update_in_progress = True
        self.update_button.setEnabled(False)
        self.update_button.setText("Проверка…")
        self.update_status.setText("Проверяю новую версию…")
        self.update_progress.hide()

        def worker() -> None:
            try:
                update = check_for_update(self._update_repository, __version__)
            except Exception as exc:
                self.events.put(("update_error", (manual, str(exc))))
                return
            self.events.put(("update_result", (manual, update)))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_result(
        self,
        manual: bool,
        update: UpdateInfo | None,
    ) -> None:
        self._update_in_progress = False
        self.update_button.setEnabled(True)
        self.update_button.setText("Проверить обновления")

        if update is None:
            self.update_status.setText(
                f"Установлена актуальная версия {__version__}."
            )
            if manual:
                QMessageBox.information(
                    self.window,
                    "Обновления",
                    f"Установлена актуальная версия {__version__}.",
                )
            return

        notes = update.notes.strip()
        if len(notes) > 700:
            notes = notes[:697].rstrip() + "…"
        details = f"\n\nЧто изменилось:\n{notes}" if notes else ""
        size_mb = update.asset.size / (1024 * 1024)
        answer = QMessageBox.question(
            self.window,
            "Доступно обновление",
            f"Доступна версия {update.version} ({size_mb:.0f} МБ)."
            f"{details}\n\nСкачать и установить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.update_status.setText(
                f"Версия {update.version} доступна — можно установить позже."
            )
            return
        self._download_update(update)

    def _download_update(self, update: UpdateInfo) -> None:
        self._update_in_progress = True
        self.update_button.setEnabled(False)
        self.update_button.setText("Загрузка 0%")
        self.update_status.setText(
            f"Скачиваю «Речку» {update.version} и проверяю файл…"
        )
        self.update_progress.setValue(0)
        self.update_progress.show()

        def progress(downloaded: int, total: int) -> None:
            percent = min(100, round(downloaded * 100 / max(total, 1)))
            self.events.put(("update_progress", percent))

        def worker() -> None:
            try:
                path = download_update(update, progress=progress)
            except Exception as exc:
                self.events.put(("update_download_error", str(exc)))
                return
            self.events.put(("update_downloaded", path))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_update_button(self) -> None:
        self._update_in_progress = False
        self.update_button.setEnabled(True)
        self.update_button.setText("Проверить обновления")

    def _install_downloaded_update(self, path: Path) -> None:
        self.update_button.setEnabled(False)
        self.update_button.setText("Установка…")
        self.update_progress.setValue(100)
        self.update_status.setText(
            "Файл проверен. Закрываю приложение и устанавливаю обновление…"
        )
        try:
            launch_update_installer(path)
        except Exception as exc:
            QMessageBox.warning(
                self.window,
                "Обновление",
                f"Не удалось запустить установщик:\n{exc}",
            )
            self._reset_update_button()
            self.update_status.setText("Автоматическая установка не запустилась.")
            return
        QTimer.singleShot(80, self.exit_app)

    def _copy_last_result(self) -> None:
        text = self.last_text.toPlainText().strip()
        if text and text != "Здесь появится распознанный текст.":
            self.application.clipboard().setText(text)
            self._set_status("Последний результат скопирован", SUCCESS)

    def _clear_last_result(self) -> None:
        self.last_text.setPlainText("Здесь появится распознанный текст.")

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def show_settings(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.show_window()

    def hide_window(self) -> None:
        self.window.hide()

    def exit_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.timer.stop()
        if self._preview_stop is not None:
            self._preview_stop.set()
        self.recorder.abort()
        self.hotkey.stop()
        self.overlay.hide()
        self.tray.hide()
        self.window.allow_close = True
        self.window.close()
        self.application.quit()

    def _process_events(self) -> None:
        if self._closing:
            return
        if (
            self._show_settings_event
            and consume_show_settings_event(self._show_settings_event)
        ):
            self.show_settings()
        if self.state == "recording":
            level = self.recorder.current_level
            self.voice_level.set_level(level)
            elapsed = max(0.0, time.monotonic() - self._recording_started_at)
            minutes, seconds = divmod(int(elapsed), 60)
            self.overlay_elapsed.setText(f"{minutes:02d}:{seconds:02d}")
            if level >= 0.16:
                self.overlay_audio_state.setText(
                    "Голос слышу · звук записывается"
                )
                self.overlay_audio_state.setStyleSheet(
                    f"color: {SUCCESS}; font-size: 8.5pt; "
                    "font-weight: 600; border: 0;"
                )
            elif elapsed >= 1.5:
                self.overlay_audio_state.setText(
                    "Сейчас тихо · скажите что-нибудь или проверьте микрофон"
                )
                self.overlay_audio_state.setStyleSheet(
                    f"color: {MUTED}; font-size: 8.5pt; border: 0;"
                )
            dot_alpha = 255 if int(elapsed * 2) % 2 == 0 else 120
            self.overlay_dot.setStyleSheet(
                f"color: rgba(229, 72, 77, {dot_alpha}); "
                "font-size: 10pt; border: 0;"
            )
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "toggle":
                self.toggle_recording()
            elif event == "show":
                self.show_window()
            elif event == "settings":
                self.show_settings()
            elif event == "exit":
                self.exit_app()
                return
            elif event == "model_status":
                generation, text = payload
                if generation == self._model_generation:
                    self._set_status(text, MUTED)
            elif event == "model_ready":
                if payload == self._model_generation:
                    self.state = "ready"
                    self.progress.hide()
                    self.record_button.setText("Начать запись")
                    self.record_button.setEnabled(True)
                    self._style_primary_button(self.record_button, ACID)
                    self._set_status("Готово к диктовке", SUCCESS)
                    self._set_tray_color(ACCENT)
                    self._load_preview_model()
            elif event == "model_error":
                generation, text = payload
                if generation == self._model_generation:
                    self.progress.hide()
                    self._handle_error(f"Не удалось загрузить модель: {text}")
            elif event == "transcript":
                self._handle_transcript(payload)
            elif event == "preview":
                session, text = payload
                if session == self._recording_session and self.state == "recording":
                    self._latest_preview_text = text
                    self._set_overlay_preview(text)
                    self._position_overlay()
            elif event == "preview_error":
                session, _text = payload
                if session == self._recording_session and self.state == "recording":
                    self._set_overlay_preview(
                        "Запись продолжается. Живой черновик временно недоступен, "
                        "финальная расшифровка всё равно будет выполнена."
                    )
            elif event == "preview_model_ready":
                self._preview_model_loading = False
                self._preview_ready = True
            elif event == "preview_model_error":
                self._preview_model_loading = False
                self._preview_ready = False
            elif event == "hotkey_error":
                self._set_status(payload, RECORD)
            elif event == "processing_stage":
                session, text = payload
                if session == self._recording_session and self.state == "transcribing":
                    self._set_status(text, ACCENT)
                    self._show_overlay(text, ACCENT)
            elif event == "error":
                self._handle_error(payload)
            elif event == "update_result":
                manual, update = payload
                self._handle_update_result(manual, update)
            elif event == "update_error":
                manual, text = payload
                self._reset_update_button()
                self.update_status.setText("Не удалось проверить обновления.")
                if manual:
                    QMessageBox.warning(self.window, "Обновления", text)
            elif event == "update_progress":
                self.update_button.setText(f"Загрузка {payload}%")
                self.update_progress.setValue(payload)
            elif event == "update_download_error":
                self._reset_update_button()
                self.update_progress.hide()
                self.update_status.setText("Не удалось скачать обновление.")
                QMessageBox.warning(
                    self.window,
                    "Обновление",
                    f"Не удалось скачать обновление:\n{payload}",
                )
            elif event == "update_downloaded":
                self._install_downloaded_update(payload)
