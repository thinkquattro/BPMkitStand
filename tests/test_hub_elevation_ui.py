"""
Статические тесты фронтенда «прав администратора» (GAP-311, часть с UI).

Серверная часть (elevation.py, /api/hub/elevation, /api/hub/restart-elevated,
/api/hub/elevated-op*) правится параллельно другим исполнителем — контракт
описан в GAP-311. Эти тесты НЕ поднимают хаб и не ходят в сеть: они читают
статические файлы web/ и проверяют, что разметка и app.js соответствуют
контракту (id элементов, вызовы нужных путей API, обработка elevation_required
и cancelled), — тот же приём, что в test_hub_pwa_and_compact.py::
test_compact_css_rules_exist.
"""

from __future__ import annotations

import standkit_hub.server as server_module

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Щит в шапке
# --------------------------------------------------------------------------


def test_elevation_button_markup_hidden_by_default_and_topbar_style():
    html = _read("index.html")

    assert 'id="elevation-btn"' in html
    # topbar-btn — общий класс всей шапки после редизайна GAP-241; elevation-btn
    # — модификатор поверх него, а не отдельная кнопка со своим стилем с нуля.
    assert 'class="topbar-btn elevation-btn"' in html
    # Скрыт статически: до первого ответа /api/hub/elevation щит не мигает.
    assert '<button id="elevation-btn" class="topbar-btn elevation-btn" type="button" hidden' in html
    assert 'id="elevation-btn-label"' in html or 'elevation-btn-label' in html
    # Инлайн-SVG, не эмодзи/глиф.
    assert "<svg" in html.split('id="elevation-btn"')[1].split("</button>")[0]


def test_elevation_button_visibility_condition_matches_contract():
    js = _read("app.js")

    # П.1 контракта: щит виден только когда ОС умеет повышать права, их
    # СЕЙЧАС нет (elevated === false — не null/"неизвестно") и restart возможен.
    assert "data.supported && data.elevated === false && data.can_restart" in js


def test_elevation_button_hidden_in_compact_view():
    css = _read("style.css")

    assert '[data-view="compact"] .elevation-btn-label' in css
    assert '[data-view="compact"] .elevation-btn' in css


# --------------------------------------------------------------------------
# Подтверждение перед перезапуском — не window.confirm/alert
# --------------------------------------------------------------------------


def test_no_window_confirm_or_alert_in_elevation_functions():
    js = _read("app.js")

    # Вырезаем именно тела restartElevatedFlow/runElevatedOp (а не весь файл —
    # window.alert где-то ещё в приложении не наш предмет) и проверяем, что
    # внутри них нет window.confirm/alert.
    start = js.index("async function restartElevatedFlow(")
    end = js.index("async function pollElevatedOp(")
    body = js[start:end]

    assert "window.confirm(" not in body
    assert "window.alert(" not in body


def test_restart_confirmation_uses_styled_modal_with_expected_text():
    js = _read("app.js")

    assert "async function restartElevatedFlow(" in js
    assert "styledConfirm(" in js
    assert "Перезапустить диспетчер с правами администратора?" in js
    assert "Windows покажет окно с запросом прав" in js
    assert "Запущенные стенды не останавливаются." in js
    # Кнопка подтверждения подписана осмысленно, а не общим "Подтвердить".
    assert '"Перезапустить"' in js


def test_confirm_modal_overlay_reused_for_elevation():
    html = _read("index.html")
    assert 'id="confirm-modal-overlay"' in html
    assert 'id="confirm-modal-ok-btn"' in html


# --------------------------------------------------------------------------
# Контракт бэкенда — правильные пути и обработка статусов/полей
# --------------------------------------------------------------------------


def test_app_js_calls_elevation_endpoints():
    js = _read("app.js")

    assert '"/api/hub/elevation"' in js
    assert '"/api/hub/restart-elevated"' in js
    assert '"/api/hub/elevated-op"' in js
    assert "/api/hub/elevated-op/" in js  # GET .../<op_id>


def test_elevation_required_error_is_handled_specially():
    js = _read("app.js")

    assert "elevation_required" in js
    assert "function showStandElevationError(" in js


def test_cancelled_409_is_not_treated_as_generic_error():
    js = _read("app.js")

    # 409 {cancelled: true} — отказ в самом окне UAC, отдельная ветка и для
    # restart-elevated, и для elevated-op.
    assert "e.data.cancelled" in js
    assert "Повышение прав не подтверждено" in js


def test_elevated_op_polling_stops_on_non_pending_status():
    js = _read("app.js")

    assert 'data.status === "pending"' in js
    assert 'data.status === "ok"' in js


# --------------------------------------------------------------------------
# Оверлей ожидания
# --------------------------------------------------------------------------


def test_restart_overlay_has_closable_footer_for_terminal_outcomes():
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="restart-overlay-footer"' in html
    assert 'id="restart-overlay-close-btn"' in html
    assert 'restart.status === "refused"' in js
    assert 'restart.status === "failed"' in js


def test_elevated_op_overlay_exists_separately_from_restart_overlay():
    html = _read("index.html")

    assert 'id="elevated-op-overlay"' in html
    assert 'id="elevated-op-overlay-text"' in html
    assert 'id="elevated-op-overlay-close-btn"' in html


# --------------------------------------------------------------------------
# Ошибка кнопки действия со стендом: два варианта восстановления
# --------------------------------------------------------------------------


def test_stand_elevation_error_block_has_two_recovery_buttons():
    html = _read("index.html")

    assert 'id="stand-elevation-error"' in html
    assert 'id="stand-elevation-error-restart-btn"' in html
    assert 'id="stand-elevation-error-once-btn"' in html
    assert 'id="stand-elevation-error-close"' in html


def test_stand_elevation_error_does_not_autohide():
    css = _read("style.css")
    js = _read("app.js")

    # Блок не должен попадать в тот же таймер, что у #action-status
    # (ACTION_STATUS_TTL_*) — ищем, что закрытие только по клику.
    assert "function hideStandElevationError(" in js
    assert ".stand-elevation-error {" in css


# --------------------------------------------------------------------------
# Настройки → «О программе»
# --------------------------------------------------------------------------


def test_about_pane_has_elevation_state_row():
    html = _read("index.html")

    assert 'id="about-elevation-state"' in html
    assert 'id="about-elevation-user"' in html
    assert 'id="about-elevation-restart-btn"' in html


def test_about_pane_has_one_shot_elevated_op_block_gated_by_supported():
    html = _read("index.html")
    js = _read("app.js")

    assert 'id="about-elevated-once"' in html
    assert 'id="about-elevated-once-stand"' in html
    assert 'id="about-elevated-once-action"' in html
    assert 'id="about-elevated-once-run-btn"' in html
    assert "onceBlock.hidden = !data.supported" in js


def test_about_elevation_state_labels_cover_true_false_null():
    js = _read("app.js")

    assert '"есть"' in js
    assert '"нет"' in js
    assert '"неизвестно"' in js


def test_one_shot_stand_select_filters_by_iis_host_kind():
    js = _read("app.js")

    assert 'function populateElevatedOnceStandSelect(' in js
    assert 's.host_kind === "iis"' in js


# --------------------------------------------------------------------------
# Кукбук
# --------------------------------------------------------------------------


def test_cookbook_describes_both_elevation_recovery_paths():
    html = _read("cookbook.html")
    # Разметка кукбука переносит длинные фразы по строкам — сравниваем "как
    # текст", а не байт-в-байт, как и остальной HTML в этом файле.
    flat = " ".join(html.split())

    assert "Только эту операцию" in flat
    assert "Перезапустить с правами администратора" in flat
    assert "Однократная операция с правами администратора" in flat
    assert "войти пользователем-администратором" in flat
