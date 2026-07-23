"""
Tray-приложение и главное окно диспетчера стендов (PySide6/Qt).

Импорт PySide6 — единственная точка во всём репозитории, где он используется,
и обёрнута в try/except с понятным сообщением: ядро и агент никогда не должны
требовать Qt, поэтому модуль остаётся импортируемым (объект-заглушка) даже
без установленного extra ``standkit[gui]`` — падение происходит только при
попытке реально создать окно (``main()``), а не при простом ``import``.

TODO(следующая итерация) — это скелет минимальной таблицы, ниже конкретика:
  - панель живого лога (сейчас — заглушка QPlainTextEdit без подписки на
    standkit.logs.follow / long-poll к агенту);
  - кнопка "Снос" (удаление стенда из реестра / деактивация) — сейчас не
    реализована, только Start/Stop/Restart/Логи по ТЗ каркаса;
  - запуск/остановка ЛОКАЛЬНОГО агента прямо из GUI (standkit_agent как
    дочерний процесс с параметрами GuiConfig.agent_* — сейчас конфиг только
    хранит дефолты, реального спавна ещё нет);
  - редактор секретов (создание/ротация token_ref через standkit.secrets).
"""

from __future__ import annotations

import importlib.resources
from typing import Optional

try:
    from PySide6.QtCore import QThread, QTimer, Signal
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QHeaderView,
        QMainWindow,
        QMenu,
        QPlainTextEdit,
        QPushButton,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    _HAS_PYSIDE6 = True
except ImportError:
    _HAS_PYSIDE6 = False

from standkit.models import ProbeState, StandStatus
from standkit.registry import Registry
from standkit_gui.client import FederatedClient
from standkit_gui.config import GuiConfig
from standkit_gui.settings_dialog import SettingsDialog

_ICON_SVG_NAME = "bpmkit-icon.svg"
_ICON_PNG_NAME = "icon.png"

_STATUS_COLUMN_ORDER = ("process", "http", "db", "redis")

# Цветовые индикаторы статуса (см. ProbeState) — для QTableWidgetItem.setBackground.
_STATE_COLORS = {
    ProbeState.OK: "#2e7d32",  # зелёный
    ProbeState.DOWN: "#c62828",  # красный
    ProbeState.UNKNOWN: "#9e9e9e",  # серый
    ProbeState.SKIPPED: "#f9a825",  # жёлтый
}


def _require_pyside6() -> None:
    if not _HAS_PYSIDE6:
        raise ImportError(
            "PySide6 не установлен. standkit_gui — опциональная оболочка, "
            "установите её через: pip install standkit[gui]"
        )


if _HAS_PYSIDE6:

    def _load_app_icon() -> QIcon:
        """
        Грузит иконку BPMkit из ресурсов пакета (``standkit_gui/assets``), а
        не с произвольного пути на диске — работает и из установленного
        пакета (``importlib.resources``), не только из исходников репозитория.

        PySide6 умеет рендерить SVG (плагин QtSvg) — пробуем его первым; если
        по какой-то причине SVG не отрисовался (пустая иконка), откатываемся
        на растровый ``icon.png``.
        """
        assets = importlib.resources.files("standkit_gui") / "assets"

        svg_path = assets / _ICON_SVG_NAME
        try:
            with importlib.resources.as_file(svg_path) as svg_file:
                icon = QIcon(str(svg_file))
                if not icon.isNull():
                    return icon
        except (FileNotFoundError, OSError):
            pass

        png_path = assets / _ICON_PNG_NAME
        try:
            with importlib.resources.as_file(png_path) as png_file:
                return QIcon(str(png_file))
        except (FileNotFoundError, OSError):
            return QIcon()

    class _StatusFetchWorker(QThread):
        """
        Фоновый опрос всех стендов (сетевые вызовы к агентам + локальные
        пробы) в отдельном потоке, чтобы таймер автообновления и кнопка
        "Обновить" не замораживали UI-поток. Результат прилетает обратно в
        главный поток через сигнал ``done`` (Qt сам маршалит его через
        event loop, никакого ручного лока не нужно).
        """

        done = Signal(dict)
        failed = Signal(str)

        def __init__(self, client: FederatedClient, parent: Optional[QWidget] = None):
            super().__init__(parent)
            self.client = client

        def run(self) -> None:  # pragma: no cover - исполняется в QThread, не в pytest
            try:
                statuses = self.client.status_all()
            except Exception as exc:  # noqa: BLE001 - любой сбой не должен убивать поток
                self.failed.emit(str(exc))
                return
            self.done.emit(statuses)

    class MainWindow(QMainWindow):
        """
        Главное окно диспетчера: таблица стендов со статусами + панель кнопок
        + панель живого лога выбранного стенда (заглушка, см. TODO модуля).
        """

        def __init__(self, client: FederatedClient, config: Optional[GuiConfig] = None):
            super().__init__()
            self.client = client
            self.config = config or GuiConfig()
            self._worker: Optional[_StatusFetchWorker] = None
            self.setWindowTitle("standkit — диспетчер стендов")
            self.resize(900, 600)
            self.setWindowIcon(_load_app_icon())

            central = QWidget(self)
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)

            self.table = QTableWidget(0, 1 + len(_STATUS_COLUMN_ORDER), central)
            self.table.setHorizontalHeaderLabels(["Стенд", "Процесс", "HTTP", "БД", "Redis"])
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            layout.addWidget(self.table)

            buttons = QHBoxLayout()
            self.btn_start = QPushButton("Start")
            self.btn_stop = QPushButton("Stop")
            self.btn_restart = QPushButton("Restart")
            self.btn_logs = QPushButton("Логи")
            self.btn_refresh = QPushButton("Обновить")
            self.btn_settings = QPushButton("Настройки…")
            for b in (
                self.btn_start,
                self.btn_stop,
                self.btn_restart,
                self.btn_logs,
                self.btn_refresh,
                self.btn_settings,
            ):
                buttons.addWidget(b)
            layout.addLayout(buttons)

            self.log_view = QPlainTextEdit(central)
            self.log_view.setReadOnly(True)
            self.log_view.setPlaceholderText("Лог выбранного стенда появится здесь (TODO: live follow)")
            layout.addWidget(self.log_view)

            self.btn_refresh.clicked.connect(self.refresh)
            self.btn_start.clicked.connect(lambda: self._act("start"))
            self.btn_stop.clicked.connect(lambda: self._act("stop"))
            self.btn_restart.clicked.connect(lambda: self._act("restart"))
            self.btn_logs.clicked.connect(self._show_logs)
            self.btn_settings.clicked.connect(self.open_settings)

            # Таймер автообновления — сам refresh() запускает опрос в фоновом
            # потоке (см. _StatusFetchWorker), таймер только его планирует.
            self.refresh_timer = QTimer(self)
            self.refresh_timer.timeout.connect(self.refresh)
            self._apply_refresh_interval()

            self.refresh()

        def _apply_refresh_interval(self) -> None:
            interval_ms = max(1, self.config.refresh_interval_sec) * 1000
            self.refresh_timer.start(interval_ms)

        def _selected_name(self) -> Optional[str]:
            row = self.table.currentRow()
            if row < 0:
                return None
            item = self.table.item(row, 0)
            return item.text() if item else None

        def refresh(self) -> None:
            """
            Запускает опрос всех стендов в фоновом потоке (не блокируя UI).
            Если предыдущий опрос ещё не завершился (например, при коротком
            refresh_interval_sec и медленном агенте) — новый не запускается,
            дожидаемся текущего.
            """
            if self._worker is not None and self._worker.isRunning():
                return
            self._worker = _StatusFetchWorker(self.client, self)
            self._worker.done.connect(self._on_statuses_ready)
            self._worker.failed.connect(self._on_refresh_failed)
            self._worker.start()

        def _on_statuses_ready(self, statuses: dict[str, StandStatus]) -> None:
            self.table.setRowCount(len(statuses))
            for row, (name, status) in enumerate(statuses.items()):
                self.table.setItem(row, 0, QTableWidgetItem(name))
                for col, field in enumerate(_STATUS_COLUMN_ORDER, start=1):
                    state: ProbeState = getattr(status, field)
                    cell = QTableWidgetItem(state.value)
                    color = _STATE_COLORS.get(state)
                    if color:
                        from PySide6.QtGui import QColor

                        cell.setBackground(QColor(color))
                    self.table.setItem(row, col, cell)

        def _on_refresh_failed(self, detail: str) -> None:  # pragma: no cover - см. _StatusFetchWorker
            self.log_view.setPlainText(f"Ошибка обновления: {detail}")

        def _act(self, action: str) -> None:
            name = self._selected_name()
            if not name:
                return
            getattr(self.client, action)(name)
            self.refresh()

        def _show_logs(self) -> None:
            name = self._selected_name()
            if not name:
                return
            lines = self.client.logs(name, n=200)
            self.log_view.setPlainText("\n".join(lines))

        def open_settings(self) -> None:
            """
            Открывает диалог настроек. При ОК — конфиг уже сохранён диалогом
            на диск (SettingsDialog._on_accept), здесь применяем его "вживую":
            пересобираем реестр/клиент, если путь реестра изменился, и
            перезапускаем таймер автообновления с новым интервалом.
            """
            dialog = SettingsDialog(self.config, self)
            if dialog.exec() != QDialog.Accepted:
                return
            if dialog.result_config is None:
                return

            old_registry_path = self.config.registry_path
            self.config = dialog.result_config

            if self.config.registry_path != old_registry_path:
                registry = Registry.load(self.config.registry_path)
                self.client = FederatedClient(registry)

            self._apply_refresh_interval()
            self.refresh()

    class TrayIcon(QSystemTrayIcon):
        """
        Иконка BPMkit в системном трее с минимальным меню (показать окно /
        выход). Сворачивание в трей по закрытию окна — TODO модуля.
        """

        def __init__(self, window: MainWindow, parent: Optional[QWidget] = None):
            super().__init__(_load_app_icon(), parent)
            self.window = window
            self.setToolTip("standkit — диспетчер стендов BPMkit")

            menu = QMenu()
            show_action = menu.addAction("Показать окно")
            show_action.triggered.connect(self._show_window)
            settings_action = menu.addAction("Настройки…")
            settings_action.triggered.connect(window.open_settings)
            menu.addSeparator()
            quit_action = menu.addAction("Выход")
            quit_action.triggered.connect(QApplication.quit)
            self.setContextMenu(menu)

            self.activated.connect(self._on_activated)

        def _show_window(self) -> None:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

        def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self._show_window()

else:
    MainWindow = None  # type: ignore[assignment]
    TrayIcon = None  # type: ignore[assignment]


def main(registry_path: Optional[str] = None) -> int:
    """
    Точка входа GUI (см. также standkit_gui/__main__.py).

    ``registry_path`` — необязательное явное переопределение пути реестра
    (например, флаг ``--registry``); если не задан, используется
    ``GuiConfig.load().registry_path`` (который сам по умолчанию резолвится
    через ``standkit.registry.default_registry_path`` — тот же реестр, что и
    у BPMkit MCP).
    """
    _require_pyside6()

    config = GuiConfig.load()
    if registry_path:
        config.registry_path = registry_path

    registry = Registry.load(config.registry_path)
    client = FederatedClient(registry)

    app = QApplication([])
    app.setWindowIcon(_load_app_icon())

    window = MainWindow(client, config)
    window.show()

    tray = TrayIcon(window)
    tray.show()

    return app.exec()
