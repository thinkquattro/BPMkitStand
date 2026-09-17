"""
Тесты фронтенда «прав администратора» (GAP-311, часть с UI).

Серверная часть (elevation.py, /api/hub/elevation, /api/hub/restart-elevated,
/api/hub/elevated-op*) правится параллельно другим исполнителем — контракт
описан в GAP-311 (см. ревью Opus по UI). Эти тесты НЕ поднимают хаб и не ходят
в сеть.

Два слоя:
  - статические — читают web/ и проверяют конкретные ВЕТКИ логики (через
    _extract_function, а не голые "подстрока есть в файле": порядок if'ов,
    что 404/401/дедлайн — терминальные исходы с разблокировкой кнопки), плюс
    обязательную разметку (id элементов, без которых JS не за что зацепиться);
  - поведенческие (если есть ``node``) — реально выполняют РЕАЛЬНЫЙ исходник
    setElevationButtonBusy/runElevatedOp/pollElevatedOp, вырезанный из app.js, в
    минимальном окружении с заглушками document/fetch/Date, и проверяют
    дедлайн pollElevatedOp, терминальность 404 и разблокировку кнопки после
    cancelled — то, что статическим grep'ом по тексту не отличить от "похоже
    на правильный код, но с перепутанным условием".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import standkit_hub.server as server_module

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _extract_function(js: str, name: str) -> str:
    """Вырезает исходник функции ``name`` (async или обычной) по балансу
    скобок — так тесты цепляются за РЕАЛЬНОЕ тело функции, а не за то, что
    где-то в файле встретилась подходящая подстрока."""
    for prefix in (f"async function {name}(", f"function {name}("):
        idx = js.find(prefix)
        if idx != -1:
            break
    else:
        raise AssertionError(f"функция {name} не найдена в app.js")

    brace_start = js.index("{", idx)
    depth = 0
    for i in range(brace_start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[idx : i + 1]
    raise AssertionError(f"не нашли конец функции {name} (незакрытая скобка)")


def _extract_const(js: str, name: str) -> int:
    m = re.search(rf"const {name}\s*=\s*(\d+)\s*;", js)
    assert m, f"константа {name} не найдена"
    return int(m.group(1))


# --------------------------------------------------------------------------
# Щит в шапке
# --------------------------------------------------------------------------


def test_elevation_button_markup_hidden_by_default_and_topbar_style():
    html = _read("index.html")

    assert 'id="elevation-btn"' in html
    # topbar-btn — общий класс всей шапки после редизайна GAP-241; elevation-btn
    # — модификатор поверх него, а не отдельная кнопка со своим стилем с нуля.
    assert 'class="topbar-btn elevation-btn"' in html
    # Скрыт статически до первого ответа /api/hub/elevation (не мигает), но
    # решение владельца 16.09.2026: на Windows виден ВСЕГДА (не только без
    # прав) — видимость решает refreshElevation по supported, а не по
    # elevated.
    assert '<button id="elevation-btn" class="topbar-btn elevation-btn" type="button" hidden' in html
    # Инлайн-SVG, не эмодзи/глиф.
    assert "<svg" in html.split('id="elevation-btn"')[1].split("</button>")[0]


def test_elevation_button_has_no_text_label():
    """Решение владельца 16.09.2026: щит — только иконка, подписи в шапке нет
    (виден он теперь всегда на Windows, а не только в редком случае "нет
    прав", и текстовая подпись рядом с постоянно видимой кнопкой была бы
    шумом на каждый день)."""
    html = _read("index.html")
    css = _read("style.css")

    button_html = html.split('id="elevation-btn"')[1].split("</button>")[0]
    assert "elevation-btn-label" not in button_html
    assert "elevation-btn-label" not in css
    assert "без прав администратора</span>" not in button_html


def test_elevation_button_icon_has_both_ok_and_bad_variants():
    """Внутри щита — две альтернативные фигуры (галочка/восклицательный
    знак), переключаемые CSS по data-elev-state, а не подмена path из JS."""
    html = _read("index.html")
    css = _read("style.css")

    button_html = html.split('id="elevation-btn"')[1].split("</button>")[0]
    assert "elevation-icon-ok" in button_html
    assert "elevation-icon-bad" in button_html
    assert '[data-elev-state="ok"] .elevation-icon-ok' in css
    assert '[data-elev-state="bad"]' in css or '"bad"' in css


def test_elevation_button_is_plain_square_topbar_btn_no_pill_styling():
    """Решение владельца 16.09.2026: щит — обычный квадрат 34×34 (как у
    остальных кнопок шапки), без своей рамки/фона-пилюли и без правила
    ``.topbar-btn.elevation-btn`` с шириной "по содержимому" — только цвет
    иконки несёт состояние."""
    css = _read("style.css")

    assert ".topbar-btn.elevation-btn {" not in css


def test_elevation_button_visibility_condition_matches_contract():
    js = _read("app.js")
    body = _extract_function(js, "refreshElevation")

    # Видимость (supported=false — не Windows — скрывает щит совсем) НЕ
    # зависит от elevated: на Windows щит виден и с правами, и без них.
    assert "btn.hidden = !(data && data.supported)" in body
    assert "data.elevated === false && data.can_restart" not in body


def test_elevation_button_presentation_covers_all_three_states():
    js = _read("app.js")
    body = _extract_function(js, "elevationButtonPresentation")

    assert 'state: "ok"' in body
    assert 'state: "bad"' in body
    assert 'state: "unknown"' in body
    assert "Диспетчер работает с правами администратора" in body
    assert "Нет прав администратора" in body
    assert "Не удалось определить права администратора" in body


def test_elevation_button_click_does_not_restart_when_already_elevated():
    js = _read("app.js")
    body = _extract_function(js, "setupElevation")

    idx = body.index('lastElevationData.elevated === false')
    restart_idx = body.index("restartElevatedFlow(btn)")
    open_settings_idx = body.index('openSettings("about")')
    # restartElevatedFlow вызывается ТОЛЬКО в ветке elevated===false; ветка
    # "иначе" (true/null) ведёт в настройки, не трогая перезапуск.
    assert idx < restart_idx < open_settings_idx


# --------------------------------------------------------------------------
# Подтверждение перед перезапуском — не window.confirm/alert
# --------------------------------------------------------------------------


def test_no_window_confirm_or_alert_in_elevation_functions():
    js = _read("app.js")

    for name in ("restartElevatedFlow", "runElevatedOp", "pollElevatedOp", "waitForHubBack"):
        body = _extract_function(js, name)
        assert "window.confirm(" not in body, name
        assert "window.alert(" not in body, name


def test_restart_confirmation_uses_styled_modal_with_expected_text_and_label():
    js = _read("app.js")
    body = _extract_function(js, "restartElevatedFlow")

    assert "styledConfirm(" in body
    assert "Перезапустить диспетчер с правами администратора?" in body
    assert "Windows покажет окно с запросом прав" in body
    assert "Запущенные стенды не останавливаются." in body
    # Кнопка подтверждения подписана осмысленно, а не общим "Подтвердить".
    assert '"Перезапустить"' in body


def test_confirm_modal_overlay_reused_for_elevation():
    html = _read("index.html")
    assert 'id="confirm-modal-overlay"' in html
    assert 'id="confirm-modal-ok-btn"' in html


# --------------------------------------------------------------------------
# 409 cancelled vs 409 обычная ошибка (requesting/pending — контракт после
# ревью: POST при уже идущем перезапуске/операции отдаёт 409 БЕЗ cancelled)
# --------------------------------------------------------------------------


def test_restart_elevated_flow_distinguishes_cancelled_from_generic_409():
    js = _read("app.js")
    body = _extract_function(js, "restartElevatedFlow")

    cancelled_idx = body.index("e.data.cancelled")
    generic_idx = body.index("describeApiError(e)")
    # Ветка cancelled должна идти РАНЬШЕ общего fallback'а с текстом ошибки —
    # иначе 409 без cancelled (перезапуск уже requesting/pending) тоже
    # получил бы "не подтверждено", хотя это настоящая ошибка сервера.
    assert cancelled_idx < generic_idx
    assert "Повышение прав не подтверждено" in body[:generic_idx]


def test_run_elevated_op_distinguishes_cancelled_from_generic_409():
    js = _read("app.js")
    body = _extract_function(js, "runElevatedOp")

    cancelled_idx = body.index("e.data.cancelled")
    generic_idx = body.index("describeApiError(e)")
    assert cancelled_idx < generic_idx


# --------------------------------------------------------------------------
# Кнопки блокируются на время запроса и разблокируются на любом терминальном
# исходе; двойной клик не должен породить второй запрос.
# --------------------------------------------------------------------------


def test_start_functions_guard_double_click_and_disable_trigger_button():
    js = _read("app.js")

    for name in ("restartElevatedFlow", "runElevatedOp"):
        body = _extract_function(js, name)
        # Повторный клик, пока кнопка уже дизейблена предыдущим — не должен
        # доходить до сети.
        assert "triggerBtn.disabled) return" in body
        assert "setElevationButtonBusy(triggerBtn, true)" in body


def test_wait_for_hub_back_reenables_button_on_every_terminal_branch():
    js = _read("app.js")
    body = _extract_function(js, "waitForHubBack")

    # Терминальные исходы: refused, failed, 401 (дважды — на обоих фазах
    # ожидания) и финальный таймаут. Успешный исход (reload) кнопку не
    # разблокирует — страница всё равно перезагрузится.
    assert body.count("setElevationButtonBusy(triggerBtn, false)") >= 4
    assert body.count('e.status === 401') == 2
    assert body.count("showSessionNotMovedOverlay()") == 2


def test_poll_elevated_op_reenables_button_on_every_terminal_branch():
    js = _read("app.js")
    body = _extract_function(js, "pollElevatedOp")

    # Терминальные исходы: op_id отсутствует, 404, 401, дедлайн, обычное
    # завершение (ok/refused/error/expired).
    assert body.count("setElevationButtonBusy(triggerBtn, false)") >= 5


# --------------------------------------------------------------------------
# pollElevatedOp: клиентский дедлайн, 404/401 как терминальные исходы
# (а не бесконечный continue), сетевые сбои — retry в пределах дедлайна.
# --------------------------------------------------------------------------


def test_poll_elevated_op_has_client_deadline_using_wait_constant():
    js = _read("app.js")
    body = _extract_function(js, "pollElevatedOp")

    assert "ELEVATED_OP_WAIT_MS" in body
    assert re.search(r"const deadline = Date\.now\(\)\s*\+\s*ELEVATED_OP_WAIT_MS", body)
    assert "while (Date.now() < deadline)" in body
    # 200с — тот самый клиентский дедлайн, о котором просили в ревью.
    assert _extract_const(js, "ELEVATED_OP_WAIT_MS") >= 200000


def test_poll_elevated_op_treats_404_and_401_as_terminal_not_infinite_continue():
    js = _read("app.js")
    body = _extract_function(js, "pollElevatedOp")

    for status in ("404", "401"):
        branch_idx = body.index(f"e.status === {status}")
        # После условия до следующего `return;` не должно встретиться
        # голого `continue;` — иначе статус не терминален, а зациклен.
        return_idx = body.index("return;", branch_idx)
        branch = body[branch_idx:return_idx]
        assert "continue;" not in branch, status
        assert "setElevationButtonBusy(triggerBtn, false)" in branch, status

    # А вот сетевой/прочий сбой (после обеих веток 404/401) — наоборот,
    # обязан продолжать опрос до дедлайна, а не завершаться терминально.
    last_catch_tail = body[body.rindex("e.status === 401") :]
    assert "continue;" in last_catch_tail


def test_wait_for_hub_back_deadline_covers_server_ttl():
    js = _read("app.js")

    assert _extract_const(js, "RESTART_WAIT_MS") >= 200000


def test_wait_for_hub_back_first_phase_treats_requesting_and_pending_as_non_terminal():
    js = _read("app.js")
    body = _extract_function(js, "waitForHubBack")

    assert 'restart.status === "refused"' in body
    assert 'restart.status === "failed"' in body
    # "requesting"/"pending" не упомянуты как отдельные if — они должны
    # падать в тот же `continue`, что и "ответ ещё не в курсе".
    refused_idx = body.index('restart.status === "refused"')
    failed_idx = body.index('restart.status === "failed"')
    after_failed = body[failed_idx:]
    assert "continue;" in after_failed[: after_failed.index("} catch")]


# --------------------------------------------------------------------------
# Оверлеи ожидания
# --------------------------------------------------------------------------


def test_restart_overlay_has_closable_footer_for_terminal_outcomes():
    html = _read("index.html")

    assert 'id="restart-overlay-footer"' in html
    assert 'id="restart-overlay-close-btn"' in html


def test_elevated_op_overlay_exists_separately_from_restart_overlay():
    html = _read("index.html")

    assert 'id="elevated-op-overlay"' in html
    assert 'id="elevated-op-overlay-text"' in html
    assert 'id="elevated-op-overlay-footer"' in html
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


def test_stand_elevation_error_is_a_separate_persistent_block_not_the_toast():
    js = _read("app.js")
    css = _read("style.css")

    # Не переиспользует таймер #action-status (ACTION_STATUS_TTL_*) — своя
    # функция закрытия, вызываемая только по клику (см. setupStandElevationError).
    show_body = _extract_function(js, "showStandElevationError")
    assert "ACTION_STATUS_TTL" not in show_body
    assert "setTimeout" not in show_body
    assert ".stand-elevation-error {" in css


def test_on_stand_action_routes_elevation_required_to_persistent_block():
    js = _read("app.js")
    body = _extract_function(js, "onStandAction")

    idx = body.index("e.data.elevation_required")
    tail_line = body[idx : idx + 200]
    assert "showStandElevationError(" in tail_line


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
    body = _extract_function(js, "updateAboutElevation")

    assert 'id="about-elevated-once"' in html
    assert 'id="about-elevated-once-stand"' in html
    assert 'id="about-elevated-once-action"' in html
    assert 'id="about-elevated-once-run-btn"' in html
    assert "onceBlock.hidden = !data.supported" in body


def test_about_elevation_state_labels_cover_true_false_null():
    js = _read("app.js")
    body = _extract_function(js, "updateAboutElevation")

    assert '"есть"' in body
    assert '"нет"' in body
    assert '"неизвестно"' in body


def test_one_shot_stand_select_filters_by_iis_host_kind():
    js = _read("app.js")
    body = _extract_function(js, "populateElevatedOnceStandSelect")

    assert 's.host_kind === "iis"' in body


def test_about_run_button_is_threaded_as_trigger_for_double_click_guard():
    js = _read("app.js")
    body = _extract_function(js, "setupAboutElevation")

    # Кнопка "Выполнить" видна и активна всё время ожидания (не прячется,
    # в отличие от кнопок в баннере ошибки стенда) — если её не передать как
    # triggerBtn, двойной клик отправит вторую операцию.
    assert "runElevatedOp(stand, action, runBtn)" in body


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


# --------------------------------------------------------------------------
# Поведенческие тесты через node: реальный код setElevationButtonBusy/runElevatedOp/
# pollElevatedOp, вырезанный из app.js, выполняется в минимальном окружении.
# --------------------------------------------------------------------------

NODE = shutil.which("node")


def _build_node_harness(js: str) -> str:
    set_button_busy = _extract_function(js, "setElevationButtonBusy")
    run_elevated_op = _extract_function(js, "runElevatedOp")
    poll_elevated_op = _extract_function(js, "pollElevatedOp")
    poll_ms = _extract_const(js, "ELEVATED_OP_POLL_MS")
    wait_ms = _extract_const(js, "ELEVATED_OP_WAIT_MS")

    # Заглушки — минимум, нужный трём вырезанным функциям. sleep и Date.now
    # управляются вручную (fakeNow), чтобы 200-секундный дедлайн проверялся
    # мгновенно, а не реальным ожиданием в тесте.
    return f"""
'use strict';
let fakeNow = 0;
Date.now = () => fakeNow;
function sleep(ms) {{ fakeNow += ms; return Promise.resolve(); }}

const ELEVATED_OP_POLL_MS = {poll_ms};
const ELEVATED_OP_WAIT_MS = {wait_ms};

const calls = {{ apiGet: [], showActionStatus: [], showElevatedOpOverlay: [], hideElevatedOpOverlay: [] }};
let apiGetImpl = () => Promise.resolve({{ status: "pending" }});
let apiSendImpl = () => Promise.resolve({{ op_id: "op-1" }});
function apiGet(path) {{ calls.apiGet.push(path); return apiGetImpl(path); }}
function apiSend(method, path, body) {{ return apiSendImpl(method, path, body); }}
function describeApiError(e) {{ return (e && e.message) || String(e); }}
function showActionStatus(message, isError) {{ calls.showActionStatus.push({{ message, isError }}); }}
function showElevatedOpOverlay(text, closable) {{ calls.showElevatedOpOverlay.push({{ text, closable }}); }}
function hideElevatedOpOverlay() {{ calls.hideElevatedOpOverlay.push(true); }}
let selectedStand = null;
async function refreshStands() {{}}
function refreshState() {{}}

{set_button_busy}

{run_elevated_op}

{poll_elevated_op}

function makeBtn() {{ return {{ disabled: false }}; }}

async function scenarioDeadline() {{
  fakeNow = 0;
  calls.apiGet.length = 0;
  calls.showElevatedOpOverlay.length = 0;
  apiGetImpl = () => Promise.resolve({{ status: "pending" }});
  const btn = makeBtn();
  btn.disabled = true; // как после вызова через runElevatedOp
  await pollElevatedOp("op-1", "alpha", btn);
  const last = calls.showElevatedOpOverlay[calls.showElevatedOpOverlay.length - 1];
  return {{
    pollCount: calls.apiGet.length,
    lastOverlay: last,
    btnDisabled: btn.disabled,
    expectedPolls: Math.floor(ELEVATED_OP_WAIT_MS / ELEVATED_OP_POLL_MS),
  }};
}}

async function scenario404() {{
  fakeNow = 0;
  calls.apiGet.length = 0;
  calls.showElevatedOpOverlay.length = 0;
  apiGetImpl = () => {{
    const e = new Error("not found");
    e.status = 404;
    return Promise.reject(e);
  }};
  const btn = makeBtn();
  btn.disabled = true;
  await pollElevatedOp("op-1", "alpha", btn);
  const last = calls.showElevatedOpOverlay[calls.showElevatedOpOverlay.length - 1];
  return {{
    pollCount: calls.apiGet.length,
    lastOverlay: last,
    btnDisabled: btn.disabled,
  }};
}}

async function scenarioCancelled() {{
  calls.showActionStatus.length = 0;
  calls.showElevatedOpOverlay.length = 0;
  apiSendImpl = () => {{
    const e = new Error("refused");
    e.status = 409;
    e.data = {{ cancelled: true }};
    return Promise.reject(e);
  }};
  const btn = makeBtn();
  await runElevatedOp("alpha", "start", btn);
  return {{
    btnDisabled: btn.disabled,
    overlayShown: calls.showElevatedOpOverlay.length > 0,
    lastStatus: calls.showActionStatus[calls.showActionStatus.length - 1],
  }};
}}

async function scenarioGeneric409() {{
  calls.showActionStatus.length = 0;
  calls.showElevatedOpOverlay.length = 0;
  apiSendImpl = () => {{
    const e = new Error("операция уже выполняется");
    e.status = 409;
    e.data = {{}};
    return Promise.reject(e);
  }};
  const btn = makeBtn();
  await runElevatedOp("alpha", "start", btn);
  return {{
    btnDisabled: btn.disabled,
    lastStatus: calls.showActionStatus[calls.showActionStatus.length - 1],
  }};
}}

(async () => {{
  const results = {{
    deadline: await scenarioDeadline(),
    notFound: await scenario404(),
    cancelled: await scenarioCancelled(),
    generic409: await scenarioGeneric409(),
  }};
  console.log(JSON.stringify(results));
}})().catch((e) => {{
  console.error(e && e.stack || String(e));
  process.exit(1);
}});
"""


def _run_node_harness(tmp_path) -> dict:
    js = _read("app.js")
    script = _build_node_harness(js)
    script_path = tmp_path / "elevation_behavior.js"
    script_path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        # encoding="utf-8" ОБЯЗАТЕЛЕН: node печатает JSON в UTF-8 всегда, а
        # text=True без явной кодировки декодирует вывод локалью процесса — на
        # русской Windows это cp1251, и весь кириллический текст в ответе
        # превращается в мусор («РџРѕРІС‹С€РµРЅРёРµ…»). Тесты, сверяющие
        # русские сообщения, падали именно на этом, а не на коде страницы
        # (живьём 17.09.2026, хост издателя).
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_poll_elevated_op_deadline_behavior_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    d = results["deadline"]

    # Дедлайн — не "ждём чуть дольше и как-нибудь выйдем": ровно
    # ELEVATED_OP_WAIT_MS / ELEVATED_OP_POLL_MS опросов, не больше и не меньше
    # (иначе либо ждём вечно, либо сдаёмся раньше срока).
    assert d["pollCount"] == d["expectedPolls"]
    assert d["lastOverlay"]["closable"] is True
    assert "Не дождались" in d["lastOverlay"]["text"]
    # Кнопка разблокирована по истечении дедлайна.
    assert d["btnDisabled"] is False


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_poll_elevated_op_404_is_terminal_not_polled_forever_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    nf = results["notFound"]

    # Один опрос — не двести: 404 обрывает ожидание немедленно.
    assert nf["pollCount"] == 1
    assert nf["lastOverlay"]["closable"] is True
    assert nf["btnDisabled"] is False


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_run_elevated_op_cancelled_reenables_button_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    c = results["cancelled"]

    assert c["btnDisabled"] is False
    assert c["overlayShown"] is False  # диспетчер не пытался поднимать права
    assert "не подтверждено" in c["lastStatus"]["message"]
    assert c["lastStatus"]["isError"] is True


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_run_elevated_op_generic_409_reenables_button_and_shows_server_text_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    g = results["generic409"]

    # 409 БЕЗ cancelled (операция уже requesting/pending) — настоящая
    # ошибка, текст от сервера, но кнопка всё равно разблокирована.
    assert g["btnDisabled"] is False
    assert "операция уже выполняется" in g["lastStatus"]["message"]
    assert g["lastStatus"]["isError"] is True


def test_shield_has_no_label_to_squeeze_into_square_topbar_button():
    """Живьём 16.09.2026: подпись щита переносилась в две строки внутри
    квадратного .topbar-btn 34×34 — решение владельца от того же дня убрало
    подпись целиком, а не расширило кнопку: теперь щит — обычный квадрат,
    как все прочие кнопки шапки, без отдельного правила ширины."""
    css = (Path(__file__).resolve().parents[1] / "standkit_hub" / "web" / "style.css").read_text(encoding="utf-8")
    assert ".topbar-btn.elevation-btn {" not in css


def _run_wait_for_hub_back(tmp_path, responses) -> dict:
    """Выполняет РЕАЛЬНЫЙ waitForHubBack из app.js: ``responses`` — ответы
    GET /api/hub/elevation по очереди (последний повторяется)."""
    js = _read("app.js")
    wait_fn = _extract_function(js, "waitForHubBack")
    script = f"""
'use strict';
let fakeNow = 0;
Date.now = () => fakeNow;
function sleep(ms) {{ fakeNow += ms; return Promise.resolve(); }}
const RESTART_WAIT_MS = 200000;
const RESTART_POLL_MS = 1000;
const responses = {json.dumps(responses)};
let i = 0;
const calls = {{ reload: 0, overlays: [], apiGet: [] }};
const window = {{ location: {{ reload: () => {{ calls.reload += 1; }} }} }};
function apiGet(path) {{
  calls.apiGet.push(path);
  const r = responses[Math.min(i, responses.length - 1)]; i += 1;
  return Promise.resolve(r);
}}
function isNetworkError(e) {{ return false; }}
function setElevationButtonBusy() {{}}
function showRestartOverlay(text, hint, closable) {{ calls.overlays.push({{ text, closable }}); }}
function showSessionNotMovedOverlay() {{ calls.overlays.push({{ text: "session" }}); }}
{wait_fn}
(async () => {{
  await waitForHubBack({{ disabled: true }});
  console.log(JSON.stringify({{ reload: calls.reload, polls: calls.apiGet.length, overlays: calls.overlays }}));
}})().catch((e) => {{ console.error(e && e.stack || String(e)); process.exit(1); }});
"""
    path = tmp_path / "wait_for_hub_back.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_wait_for_hub_back_reloads_when_new_elevated_hub_answers_without_gap_via_node(tmp_path):
    """Живьём 16.09.2026: новый хаб занял порт между опросами, обрыва связи не
    было — оверлей висел, щит оставался красным. Ответ elevated=true от того же
    адреса — это уже новый процесс: перезагрузка страницы."""
    out = _run_wait_for_hub_back(tmp_path, [
        {"elevated": False, "restart": {"status": "pending"}},
        {"elevated": True, "restart": None},
    ])
    assert out["reload"] == 1
    assert out["polls"] == 2


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_wait_for_hub_back_reloads_when_pending_state_disappears_via_node(tmp_path):
    out = _run_wait_for_hub_back(tmp_path, [
        {"elevated": False, "restart": {"status": "pending"}},
        {"elevated": None, "restart": None},
    ])
    assert out["reload"] == 1


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_wait_for_hub_back_keeps_waiting_while_pending_via_node(tmp_path):
    out = _run_wait_for_hub_back(tmp_path, [{"elevated": False, "restart": {"status": "pending"}}])
    assert out["reload"] == 0
    assert out["polls"] >= 190  # до клиентского дедлайна, без ложной перезагрузки
