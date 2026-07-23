"""
Диалог «Настройки» GUI-диспетчера standkit (Qt-форма поверх standkit_gui.config.GuiConfig).

Импорт PySide6 обёрнут в try/except по тому же принципу, что и standkit_gui/app.py:
модуль остаётся импортируемым (для проверки, что он не роняет весь пакет) даже
без установленного extra ``standkit[gui]``, падение — только при попытке
реально создать диалог.

Секреты (токены агентов) НЕ редактируются здесь напрямую — только ссылки
(*_ref) на standkit.secrets; сами значения секретов задаются отдельно (CLI
secretstore/keyring), см. TODO модуля.

TODO(следующая итерация):
  - редактор секретов (создание/ротация ссылок через standkit.secrets прямо
    из диалога, без похода в терминал);
  - валидация путей (TLS-сертификаты, run/log dir) с подсказкой при ошибке;
  - live-проверка доступности удалённого агента (кнопка «Проверить») перед
    сохранением записи в таблице агентов.
"""

from __future__ import annotations

from typing import Optional

try:
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    _HAS_PYSIDE6 = True
except ImportError:
    _HAS_PYSIDE6 = False

from standkit.registry import default_registry_path
from standkit_gui.config import GuiConfig, RemoteAgent

_AGENT_TABLE_COLUMNS = ("name", "url", "token_ref")
_AGENT_TABLE_HEADERS = ("Имя", "URL", "Ссылка на токен (token_ref)")


def _require_pyside6() -> None:
    if not _HAS_PYSIDE6:
        raise ImportError(
            "PySide6 не установлен. standkit_gui — опциональная оболочка, "
            "установите её через: pip install standkit[gui]"
        )


if _HAS_PYSIDE6:

    class SettingsDialog(QDialog):
        """
        Форма по всем полям GuiConfig. ОК сохраняет через config.save() и
        возвращает применённый GuiConfig (self.result_config); Отмена не
        трогает исходный объект/файл — откат «бесплатный», т.к. запись
        происходит только по нажатию ОК.
        """

        def __init__(self, config: GuiConfig, parent: Optional[QWidget] = None):
            super().__init__(parent)
            self.setWindowTitle("Настройки standkit")
            self.resize(640, 560)
            self._config = config
            self.result_config: Optional[GuiConfig] = None

            root = QVBoxLayout(self)
            tabs = QTabWidget(self)
            root.addWidget(tabs)

            tabs.addTab(self._build_general_tab(config), "Общие")
            tabs.addTab(self._build_agents_tab(config), "Удалённые агенты")
            tabs.addTab(self._build_agent_defaults_tab(config), "Агент по умолчанию")

            buttons = QDialogButtonBox(
                QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
            )
            buttons.accepted.connect(self._on_accept)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)

        # --- вкладка "Общие" ---

        def _build_general_tab(self, config: GuiConfig) -> QWidget:
            widget = QWidget(self)
            form = QFormLayout(widget)

            self.registry_edit = QLineEdit(config.registry_path, widget)
            registry_row = QHBoxLayout()
            registry_row.addWidget(self.registry_edit)
            browse_registry = QPushButton("Обзор…", widget)
            browse_registry.clicked.connect(self._browse_registry)
            registry_row.addWidget(browse_registry)
            form.addRow("Реестр стендов:", registry_row)

            hint = QLabel(
                f"Резолвленный путь по умолчанию (BPMkit): {default_registry_path()}",
                widget,
            )
            hint.setWordWrap(True)
            form.addRow("", hint)

            self.run_dir_edit = QLineEdit(config.run_dir, widget)
            run_dir_row = QHBoxLayout()
            run_dir_row.addWidget(self.run_dir_edit)
            browse_run = QPushButton("Обзор…", widget)
            browse_run.clicked.connect(lambda: self._browse_dir(self.run_dir_edit))
            run_dir_row.addWidget(browse_run)
            form.addRow("Каталог pid-файлов (run_dir):", run_dir_row)

            self.log_dir_edit = QLineEdit(config.log_dir, widget)
            log_dir_row = QHBoxLayout()
            log_dir_row.addWidget(self.log_dir_edit)
            browse_log = QPushButton("Обзор…", widget)
            browse_log.clicked.connect(lambda: self._browse_dir(self.log_dir_edit))
            log_dir_row.addWidget(browse_log)
            form.addRow("Каталог логов (log_dir):", log_dir_row)

            self.refresh_spin = QSpinBox(widget)
            self.refresh_spin.setRange(1, 3600)
            self.refresh_spin.setSuffix(" сек")
            self.refresh_spin.setValue(config.refresh_interval_sec)
            form.addRow("Интервал автообновления:", self.refresh_spin)

            return widget

        def _browse_registry(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Выбрать реестр стендов", self.registry_edit.text(), "JSON (*.json)"
            )
            if path:
                self.registry_edit.setText(path)

        def _browse_dir(self, target: QLineEdit) -> None:
            path = QFileDialog.getExistingDirectory(self, "Выбрать каталог", target.text())
            if path:
                target.setText(path)

        # --- вкладка "Удалённые агенты" ---

        def _build_agents_tab(self, config: GuiConfig) -> QWidget:
            widget = QWidget(self)
            layout = QVBoxLayout(widget)

            self.agents_table = QTableWidget(len(config.agents), len(_AGENT_TABLE_COLUMNS), widget)
            self.agents_table.setHorizontalHeaderLabels(list(_AGENT_TABLE_HEADERS))
            self.agents_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            for row, agent in enumerate(config.agents):
                self.agents_table.setItem(row, 0, QTableWidgetItem(agent.name))
                self.agents_table.setItem(row, 1, QTableWidgetItem(agent.url))
                self.agents_table.setItem(row, 2, QTableWidgetItem(agent.token_ref))
            layout.addWidget(self.agents_table)

            buttons = QHBoxLayout()
            add_btn = QPushButton("Добавить", widget)
            add_btn.clicked.connect(self._add_agent_row)
            remove_btn = QPushButton("Удалить", widget)
            remove_btn.clicked.connect(self._remove_agent_row)
            buttons.addWidget(add_btn)
            buttons.addWidget(remove_btn)
            buttons.addStretch(1)
            layout.addLayout(buttons)

            note = QLabel(
                "token_ref — ссылка на секрет (standkit.secrets), НЕ сам токен. "
                "Значение секрета задаётся отдельно (keyring/env).",
                widget,
            )
            note.setWordWrap(True)
            layout.addWidget(note)

            return widget

        def _add_agent_row(self) -> None:
            row = self.agents_table.rowCount()
            self.agents_table.insertRow(row)
            for col in range(len(_AGENT_TABLE_COLUMNS)):
                self.agents_table.setItem(row, col, QTableWidgetItem(""))

        def _remove_agent_row(self) -> None:
            row = self.agents_table.currentRow()
            if row >= 0:
                self.agents_table.removeRow(row)

        # --- вкладка "Агент по умолчанию" ---

        def _build_agent_defaults_tab(self, config: GuiConfig) -> QWidget:
            widget = QWidget(self)
            layout = QVBoxLayout(widget)

            conn_group = QGroupBox("Подключение", widget)
            conn_form = QFormLayout(conn_group)

            self.agent_host_edit = QLineEdit(config.agent_host, widget)
            conn_form.addRow("Host:", self.agent_host_edit)

            self.agent_port_spin = QSpinBox(widget)
            self.agent_port_spin.setRange(1, 65535)
            self.agent_port_spin.setValue(config.agent_port)
            conn_form.addRow("Port:", self.agent_port_spin)

            self.token_ref_edit = QLineEdit(config.token_ref, widget)
            conn_form.addRow("token_ref (control):", self.token_ref_edit)

            self.readonly_token_ref_edit = QLineEdit(config.readonly_token_ref, widget)
            conn_form.addRow("readonly_token_ref:", self.readonly_token_ref_edit)

            self.insecure_check = QCheckBox(
                "--insecure (осознанный обход fail-closed на non-loopback без TLS)", widget
            )
            self.insecure_check.setChecked(config.insecure)
            conn_form.addRow("", self.insecure_check)

            layout.addWidget(conn_group)

            tls_group = QGroupBox("TLS / mTLS", widget)
            tls_form = QFormLayout(tls_group)

            self.tls_cert_edit, tls_cert_row = self._path_row(widget, config.tls_cert, is_file=True)
            tls_form.addRow("tls_cert:", tls_cert_row)

            self.tls_key_edit, tls_key_row = self._path_row(widget, config.tls_key, is_file=True)
            tls_form.addRow("tls_key:", tls_key_row)

            self.tls_client_ca_edit, tls_ca_row = self._path_row(widget, config.tls_client_ca, is_file=True)
            tls_form.addRow("tls_client_ca:", tls_ca_row)

            layout.addWidget(tls_group)

            audit_group = QGroupBox("Аудит и lockout", widget)
            audit_form = QFormLayout(audit_group)

            self.audit_log_edit, audit_log_row = self._path_row(widget, config.audit_log, is_file=True)
            audit_form.addRow("audit_log:", audit_log_row)

            self.lockout_max_spin = QSpinBox(widget)
            self.lockout_max_spin.setRange(1, 1000)
            self.lockout_max_spin.setValue(config.lockout_max_failures)
            audit_form.addRow("lockout_max_failures:", self.lockout_max_spin)

            self.lockout_window_spin = QDoubleSpinBox(widget)
            self.lockout_window_spin.setRange(1.0, 86400.0)
            self.lockout_window_spin.setSuffix(" сек")
            self.lockout_window_spin.setValue(config.lockout_window_sec)
            audit_form.addRow("lockout_window_sec:", self.lockout_window_spin)

            layout.addWidget(audit_group)
            layout.addStretch(1)

            return widget

        def _path_row(self, widget: QWidget, value: str, *, is_file: bool) -> tuple[QLineEdit, QHBoxLayout]:
            edit = QLineEdit(value, widget)
            row = QHBoxLayout()
            row.addWidget(edit)
            browse = QPushButton("Обзор…", widget)
            if is_file:
                browse.clicked.connect(lambda: self._browse_file(edit))
            else:
                browse.clicked.connect(lambda: self._browse_dir(edit))
            row.addWidget(browse)
            return edit, row

        def _browse_file(self, target: QLineEdit) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Выбрать файл", target.text())
            if path:
                target.setText(path)

        # --- сохранение ---

        def _on_accept(self) -> None:
            agents: list[RemoteAgent] = []
            for row in range(self.agents_table.rowCount()):
                name_item = self.agents_table.item(row, 0)
                url_item = self.agents_table.item(row, 1)
                ref_item = self.agents_table.item(row, 2)
                name = name_item.text().strip() if name_item else ""
                url = url_item.text().strip() if url_item else ""
                token_ref = ref_item.text().strip() if ref_item else ""
                if not (name or url or token_ref):
                    continue
                agents.append(RemoteAgent(name=name, url=url, token_ref=token_ref))

            updated = GuiConfig(
                registry_path=self.registry_edit.text().strip() or str(default_registry_path()),
                run_dir=self.run_dir_edit.text().strip(),
                log_dir=self.log_dir_edit.text().strip(),
                refresh_interval_sec=self.refresh_spin.value(),
                agents=agents,
                agent_host=self.agent_host_edit.text().strip() or "127.0.0.1",
                agent_port=self.agent_port_spin.value(),
                token_ref=self.token_ref_edit.text().strip(),
                readonly_token_ref=self.readonly_token_ref_edit.text().strip(),
                tls_cert=self.tls_cert_edit.text().strip(),
                tls_key=self.tls_key_edit.text().strip(),
                tls_client_ca=self.tls_client_ca_edit.text().strip(),
                insecure=self.insecure_check.isChecked(),
                audit_log=self.audit_log_edit.text().strip(),
                lockout_max_failures=self.lockout_max_spin.value(),
                lockout_window_sec=self.lockout_window_spin.value(),
            )
            updated.save()
            self.result_config = updated
            self.accept()

else:
    SettingsDialog = None  # type: ignore[assignment]
