# -*- coding: utf-8 -*-
"""GAP-279 (ADR-0048): кнопка «Установить обновление» диспетчера → API хаба →
`releases.apply_installer`, плюс уведомления браузера об обновлениях.

Что здесь проверяется:
1. действия канала `stage_installer`/`apply_installer` — раннер (`run_action`,
   `available_actions`, карточка `installer` в `status()`);
2. эндпоинты хаба `/api/companion/apply-installer` и `/stage-installer` с НАСТОЯЩИМ
   раннером поверх подставного бэкенда: успех (установщик запущен, бинарь MCP не
   тронут), подменённый `kind` → отказ без запуска процесса, нет подготовленного
   установщика → 409 с понятным текстом;
3. WinError 740 (установка «для всех пользователей» из хаба без прав) → typed-отказ
   `elevation_required`, а не «локальная ошибка»;
4. статические якоря веб-интерфейса: кнопка, предпросмотр, оверлей ожидания
   перезапуска, переключатель уведомлений (разрешение — только по действию
   пользователя), фолбэк «(1)» в заголовке вкладки.

Запуск процесса в тестах перехватывается на `subprocess.Popen` — ровно там, где его
делает единая точка `standkit.platform.spawn_hidden` (GAP-138/GAP-412: голого Popen
вне неё нет и здесь).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import standkit_hub.server as server_module
from standkit.platform import ProcessError
from standkit_companion import releases
from standkit_companion.errors import ChannelError
from standkit_companion.runner import CompanionRunner, available_actions
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionSettings
from tests.test_companion_installer_channel import _installer_meta, _installer_sidecar
from tests.test_companion_releases import FakeClient, FakeCtx, _blob
from tests.test_companion_runner import settings_all_on
from tests.test_hub_companion_api import (  # noqa: F401 - фикстура гашения серверов
    _close_hub_servers,
    _install_stub_runner,
    _post,
    _start_hub,
)

WEB_DIR = Path(server_module.__file__).parent / "web"
VERSION = "4.1.0"
FILENAME = f"bpmkit-setup-{VERSION}.exe"


class _FakeProcess:
    pid = 5151


@pytest.fixture()
def popen_calls(monkeypatch):
    calls: list = []

    def _fake_popen(args, **kwargs):
        calls.append(list(args))
        return _FakeProcess()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    return calls


def _make_runner(tmp_path, *, staged: bool = True, kind: str = "installer"):
    binary = tmp_path / "mcp" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stary binar 4.0.0")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    blob = _blob(VERSION)
    meta = _installer_meta(VERSION, blob, filename=FILENAME)
    meta["standkit_version"] = "0.13.0"
    client = FakeClient(meta, blob, sidecar=_installer_sidecar(FILENAME, blob, kind=kind))
    config_path = tmp_path / "standkit-hub.json"
    runner = CompanionRunner(
        config_path,
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: settings_all_on(),
        client_factory=lambda _ctx, _settings: client,
        context_resolver=lambda _settings: ctx,
    )
    if staged:
        runner.run_action("stage_installer", version=VERSION)
    return runner, ctx, client


# ======================================================================================
# 1. Раннер
# ======================================================================================


def test_actions_include_installer_pair():
    from standkit_companion.runner import ACTIONS

    assert "stage_installer" in ACTIONS
    assert "apply_installer" in ACTIONS


def test_available_actions_offer_apply_installer_only_with_staged_file(tmp_path, popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path, staged=False)
    assert available_actions(settings_all_on(), runner._state)["apply_installer"] is False
    assert available_actions(settings_all_on(), runner._state)["stage_installer"] is True

    runner.run_action("stage_installer", version=VERSION)
    allowed = available_actions(settings_all_on(), runner._state)
    assert allowed["apply_installer"] is True

    # Файл унесли (антивирус/уборка) — кнопка гаснет, запись без файла не в счёт.
    Path(runner._state.releases["installer_staged"]["path"]).unlink()
    assert available_actions(settings_all_on(), runner._state)["apply_installer"] is False

    off = available_actions(settings_all_on(enabled=False), runner._state)
    assert off["apply_installer"] is False and off["stage_installer"] is False


def test_status_carries_installer_card_for_preview(tmp_path, popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path)

    card = runner.status()["installer"]

    assert card["staged"]["version"] == VERSION
    assert card["staged"]["standkit_version"] == "0.13.0"
    assert "sidecar" not in card["staged"], "сайдкар подписи наружу не отдаётся"
    assert card["launched"] is None


def test_run_action_apply_installer_launches_silently(tmp_path, popen_calls):
    runner, ctx, _client = _make_runner(tmp_path)

    result = runner.run_action("apply_installer", version=VERSION)

    assert result["launched"] is True
    assert result["version"] == VERSION
    assert len(popen_calls) == 1
    args = popen_calls[0]
    assert args[0].endswith(FILENAME)
    assert "/VERYSILENT" in args and "/SUPPRESSMSGBOXES" in args and "/NORESTART" in args
    # Журнал Inno Setup — единственный внятный след отказа тихой установки.
    log_args = [a for a in args if a.startswith("/LOG=")]
    assert len(log_args) == 1 and log_args[0].endswith("installer_setup.log")
    assert result["log"].endswith("installer_setup.log")
    assert Path(ctx.binary_path).read_bytes() == b"stary binar 4.0.0", (
        "установщик применяется ЗАПУСКОМ, бинарь MCP канал не подменяет")
    launched = runner.status()["installer"]["launched"]
    assert launched["pid"] == _FakeProcess.pid
    assert launched["standkit_version"] == "0.13.0"
    assert "running" in launched


def test_run_action_apply_installer_without_staged_is_clear_error(tmp_path, popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path, staged=False)

    with pytest.raises(ChannelError) as excinfo:
        runner.run_action("apply_installer")

    assert excinfo.value.kind == "nothing_staged"
    assert "установщика нет" in str(excinfo.value)
    assert popen_calls == []


def test_run_action_apply_installer_rejects_kind_mismatch(tmp_path, popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path)
    runner._state.releases["installer_staged"]["sidecar"]["kind"] = "server"
    runner._state.save()

    with pytest.raises(ChannelError) as excinfo:
        runner.run_action("apply_installer")

    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert popen_calls == []


def test_apply_installer_elevation_required_is_typed(tmp_path, monkeypatch):
    runner, _ctx, _client = _make_runner(tmp_path)

    def _refuse(cmd, cwd, log_path):
        cause = OSError("The requested operation requires elevation")
        cause.winerror = releases.INSTALLER_ELEVATION_WINERROR
        raise ProcessError("не удалось запустить") from cause

    monkeypatch.setattr(releases, "spawn_hidden", _refuse)

    with pytest.raises(ChannelError) as excinfo:
        runner.run_action("apply_installer")

    assert excinfo.value.kind == "elevation_required"
    assert "администратора" in str(excinfo.value)
    assert FILENAME in str(excinfo.value), "человеку нужен путь для ручного запуска"


def test_quiet_installer_staging_for_flagged_version(tmp_path, popen_calls):
    """«Проверить обновления» для версии с requires_installer готовит установщик ЭТОЙ
    версии — кнопка «Установить обновление» становится доступна одним нажатием."""
    runner, ctx, _client = _make_runner(tmp_path, staged=False)
    session = runner._session(settings_all_on())

    staged = runner._stage_installer_quietly(
        session, {"available": True, "requires_installer": True, "latest": VERSION,
                  "target": "latest"})

    assert staged["reason"] == "installer_staged"
    assert staged["version"] == VERSION
    assert popen_calls == [], "подготовка установщик не запускает"
    again = runner._stage_installer_quietly(
        session, {"available": True, "requires_installer": True, "latest": VERSION})
    assert again["reason"] == "installer_already_staged"


def test_quiet_installer_staging_never_raises(tmp_path):
    runner, _ctx, client = _make_runner(tmp_path, staged=False)
    client.meta_error = ChannelError("нет установщика", kind="installer_not_available")
    session = runner._session(settings_all_on())

    result = runner._stage_installer_quietly(
        session, {"available": True, "requires_installer": True, "latest": VERSION})

    assert result["reason"] == "installer_not_available"
    assert releases.installer_status(runner._state)["staged"] is None


def test_installer_status_running_window(tmp_path, monkeypatch):
    state = CompanionState(tmp_path / "s.json")
    state.releases["installer_launched"] = {"version": VERSION, "pid": 77,
                                            "launched_at": "2026-09-24T01:00:00Z"}
    monkeypatch.setattr(releases, "is_alive", lambda pid: True)

    fresh = releases.installer_status(state, now_iso="2026-09-24T01:05:00Z")
    assert fresh["launched"]["running"] is True
    # Через час живой pid — уже почти наверняка чужой процесс, не наш установщик.
    stale = releases.installer_status(state, now_iso="2026-09-24T02:05:00Z")
    assert stale["launched"]["running"] is False


# ======================================================================================
# 2. Эндпоинты хаба — настоящий раннер поверх подставного бэкенда
# ======================================================================================


def _hub_with_runner(tmp_path, monkeypatch, runner):
    _install_stub_runner(monkeypatch, runner)
    return _start_hub(tmp_path, companion=CompanionSettings(enabled=True))


def test_hub_apply_installer_success(tmp_path, monkeypatch, popen_calls):
    runner, ctx, _client = _make_runner(tmp_path / "ch")
    base_url, token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, body, _ = _post(base_url, "/api/companion/apply-installer", token,
                            body={"version": VERSION})

    assert status == 200, body
    assert body["ok"] is True
    assert body["result"]["launched"] is True
    assert body["result"]["reason"] == "installer_launched"
    assert len(popen_calls) == 1
    # Свежий статус едет вместе с результатом — со страницей, ждущей перезапуска.
    assert body["status"]["installer"]["launched"]["version"] == VERSION
    assert Path(ctx.binary_path).read_bytes() == b"stary binar 4.0.0"


def test_hub_apply_installer_kind_mismatch_is_refused(tmp_path, monkeypatch, popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path / "ch")
    runner._state.releases["installer_staged"]["sidecar"]["kind"] = "server"
    runner._state.save()
    base_url, token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, body, _ = _post(base_url, "/api/companion/apply-installer", token)

    assert status == 502, body
    assert body["kind"] == "artifact_kind_mismatch"
    assert body["error"]
    assert popen_calls == []


def test_hub_apply_installer_without_staged_is_409_with_text(tmp_path, monkeypatch,
                                                             popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path / "ch", staged=False)
    base_url, token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, body, _ = _post(base_url, "/api/companion/apply-installer", token)

    assert status == 409, body
    assert body["kind"] == "nothing_staged"
    assert "установщика нет" in body["error"]
    assert popen_calls == []


def test_hub_apply_installer_version_mismatch_is_refused(tmp_path, monkeypatch,
                                                         popen_calls):
    runner, _ctx, _client = _make_runner(tmp_path / "ch")
    base_url, token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, body, _ = _post(base_url, "/api/companion/apply-installer", token,
                            body={"version": "9.9.9"})

    assert status == 409, body
    assert body["kind"] == "nothing_staged"
    assert popen_calls == []


def test_hub_stage_installer_route(tmp_path, monkeypatch, popen_calls):
    runner, _ctx, client = _make_runner(tmp_path / "ch", staged=False)
    base_url, token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, body, _ = _post(base_url, "/api/companion/stage-installer", token,
                            body={"version": VERSION})

    assert status == 200, body
    assert body["result"]["reason"] == "installer_staged"
    assert body["status"]["actions"]["apply_installer"] is True
    assert popen_calls == [], "подготовка установщик НЕ запускает"


def test_hub_apply_installer_requires_csrf_token(tmp_path, monkeypatch, popen_calls):
    from tests.test_hub_companion_api import _request

    runner, _ctx, _client = _make_runner(tmp_path / "ch")
    base_url, _token, *_ = _hub_with_runner(tmp_path, monkeypatch, runner)

    status, _body, _ = _request(base_url, "/api/companion/apply-installer",
                                method="POST", body={}, origin=base_url)

    assert status == 403
    assert popen_calls == []


# ======================================================================================
# 3. Веб-интерфейс (статика)
# ======================================================================================


def _html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def test_install_button_and_preview_present():
    html = _html()
    assert re.search(r'<button[^>]*id="upd-installer-btn"[^>]*data-companion-action="apply_installer"', html)
    assert re.search(r'<button[^>]*id="upd-installer-stage-btn"[^>]*data-companion-action="stage_installer"', html)
    assert 'id="upd-installer-preview"' in html
    assert 'id="install-overlay"' in html


def test_install_flow_wiring_in_js():
    js = _js()
    assert 'apply_installer: "/api/companion/apply-installer"' in js
    assert 'stage_installer: "/api/companion/stage-installer"' in js
    # Кнопка идёт через свой сценарий: подтверждение с предпросмотром и ожидание.
    assert "installUpdateFlow(btn)" in js
    flow = js[js.index("async function installUpdateFlow"):]
    flow = flow[:flow.index("\n  }\n") + 4]
    assert "styledConfirm(" in flow, "установка без подтверждения человеком недопустима"
    assert "installerPreviewText(" in flow
    assert "Claude Desktop" in js
    # Предпросмотр «MCP A→B, диспетчер C→D».
    preview = js[js.index("function installerPreviewText"):]
    assert "MCP ${mcpFrom} → ${staged.version}, диспетчер ${hubFrom} → ${hubTo}" in preview
    # Переподключение: опрос /api/version с таймаутом и понятным сообщением.
    wait = js[js.index("async function waitForInstallerRestart"):]
    assert "INSTALL_WAIT_MS" in wait and "/api/version" in js
    assert "window.location.reload()" in wait
    assert "Не дождались перезапуска диспетчера" in wait


def test_notification_toggle_in_updates_settings():
    html = _html()
    pane = html[html.index('id="settings-companion"'):]
    pane = pane[:pane.index("</div>\n\n")] if "</div>\n\n" in pane else pane[:4000]
    toggle = re.search(r'<input[^>]*id="updates-notify-toggle"[^>]*>', html)
    assert toggle, "переключатель уведомлений в «Настройки → Обновления» потерян"
    assert 'id="updates-notify-toggle"' in pane
    assert "name=" not in toggle.group(0), (
        "выбор браузера не должен уезжать в конфиг диспетчера формой настроек")


def test_notification_permission_requested_only_by_user_action():
    js = _js()
    assert js.count("requestPermission(") == 1
    setup = js[js.index("function setupUpdateNotifications"):]
    setup = setup[:setup.index("\n  }\n") + 4]
    assert "requestPermission(" in setup
    assert 'addEventListener("change"' in setup
    # Уведомления: «найдено» и «готово к установке».
    assert "found:${latest}" in js and "ready:${ready}" in js
    assert "new window.Notification(" in js


def test_badge_and_tab_title_fallback():
    js = _js()
    badge = js[js.index("function renderUpdatesBadge"):]
    badge = badge[:badge.index("\n  }\n") + 4]
    assert "renderDocumentTitle(" in badge
    assert "`(1) ${baseDocumentTitle}`" in js
