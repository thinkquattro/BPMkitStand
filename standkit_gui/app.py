"""
Tray-приложение и главное окно диспетчера стендов (PySide6/Qt).

Импорт PySide6 — единственная точка во всём репозитории, где он используется,
и обёрнута в try/except с понятным сообщением: ядро и агент никогда не должны
требовать Qt, поэтому модуль остаётся импортируемым (объект-заглушка) даже
без установленного extra ``standkit[gui]`` — падение происходит только при
попытке реально создать окно (``main()``), а не при простом ``import``.

TODO(следующая итерация) — это скелет минимальной таблицы, ниже конкретика:
  - реальный tray-icon с меню (сейчас закомментирован пример, полноценная
    реализация — предмет отдельной итерации, включая иконки для разных ОС);
  - панель живого лога (сейчас — заглушка QPlainTextEdit без подписки на
    standkit.logs.follow / long-poll к агенту);
  - кнопка "Снос" (удаление стенда из реестра / деактивация) — сейчас не
    реализована, только Start/Stop/Restart/Логи по ТЗ каркаса;
  - фоновый опрос статусов по таймеру (сейчас — обновление только по кнопке),
    чтобы не блокировать UI-поток сетевыми вызовами к агентам напрямую
    (нужен QThread/QRunnable-воркер вокруг FederatedClient.status_all()).
"""

from __future__ import annotations

from typing import Optional

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QHeaderView,
        QMainWindow,
        QPlainTextEdit,
        QPushButton,
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

    class MainWindow(QMainWindow):
        """
        Главное окно диспетчера: таблица стендов со статусами + панель кнопок
        + панель живого лога выбранного стенда (заглушка, см. TODO модуля).
        """

        def __init__(self, client: FederatedClient):
            super().__init__()
            self.client = client
            self.setWindowTitle("standkit — диспетчер стендов")
            self.resize(900, 600)

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
            for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_logs, self.btn_refresh):
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

            self.refresh()

        def _selected_name(self) -> Optional[str]:
            row = self.table.currentRow()
            if row < 0:
                return None
            item = self.table.item(row, 0)
            return item.text() if item else None

        def refresh(self) -> None:
            """
            Полный синхронный опрос всех стендов и перерисовка таблицы.

            TODO: см. модульный TODO — вынести в фоновый поток, чтобы не
            замораживать UI при недоступном агенте (таймаут по умолчанию 5с
            на каждый агент в client.py).
            """
            statuses = self.client.status_all()
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

else:
    MainWindow = None  # type: ignore[assignment]


def main(registry_path: str = "projects.json") -> int:
    """Точка входа GUI (см. также standkit_gui/__main__.py)."""
    _require_pyside6()

    registry = Registry.load(registry_path)
    client = FederatedClient(registry)

    app = QApplication([])
    window = MainWindow(client)
    window.show()

    # TODO: полноценная реализация системного трея (QSystemTrayIcon + меню
    # Start/Stop/Restart на каждый стенд, сворачивание в трей по закрытию
    # окна) — намеренно оставлено как задел следующей итерации.

    return app.exec()
