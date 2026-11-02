# -*- coding: utf-8 -*-
"""
Диагностика активации лицензии — часть диспетчера.

* истёкшая лицензия называет ПРИЧИНУ. Для непройденного окна активации
  (``expired_cause=activation_window``) модалка, баннер и строка состояния пишут
  «не активирована до <activate_by>», а не «закончилась <потолок expires_at>».
  Ключ памяти модалки дополнен причиной; сырой ``URLError`` — только под
  «Подробности».
* баннер warn/crit «BPMkit не может подтвердить лицензию на этом
  компьютере» по ``activation_notice_level`` с остатком срока и кнопками
  «Проверить связь / Текст для ИТ / Повторить активацию».
* проба связи — ``POST /api/license/net-probe`` → ``setup net-probe --json``;
  rc=2 с валидным JSON — штатный исход «связи нет», а не отказ CLI.
* «Собрать диагностику» — ``POST /api/diag/bundle`` → ``setup diag-bundle
  --json``; «Открыть папку» — без пути в теле запроса. Формат hub.log — ISO UTC.

Настоящий CLI не запускается: во все функции инъектируется ``run(argv)``. Тексты UI
проверяются запуском чистых функций app.js в node (как в test_gap782).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import standkit_hub.server as server_module
from standkit.cli_resolve import CLI_ENV_VAR
from standkit_hub import diag_api, hub_logging, license_api
from standkit_hub.config import CompanionSettings
from standkit_hub.logs_browser import OpenFolderResult
from tests.test_gap604_updates_badge import _extract_function, _read_js
from tests.test_hub_license_api import INFO_OK, _request, _start_hub

WEB_DIR = Path(server_module.__file__).parent / "web"
NODE = shutil.which("node")

PROBE_FAIL = {
    "ok": False, "stage": "tls", "cause": "tls_timeout",
    "message_ru": "сервер активации не отвечает: соединение обрывается при установке "
                  "защищённого соединения — вероятно, его блокирует сеть или прокси",
    "it_text_ru": "Откройте исходящий HTTPS (TCP 443) с этой машины до api.example без "
                  "TLS-инспекции.",
    "detail": "URLError: <urlopen error _ssl.c:1059: The handshake operation timed out>",
    "proxy": "direct", "proxy_source": "none", "pac_detected": False,
    "host": "api.example", "port": 443, "elapsed_ms": 8012, "stages": [],
    "http_status": 0,
}
PROBE_OK = dict(PROBE_FAIL, ok=True, stage="ok", cause="ok", message_ru="", it_text_ru="",
                detail="", elapsed_ms=212)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    license_api.invalidate_cache()
    license_api.reset_net_probe()
    diag_api.reset_last_bundle()
    yield
    license_api.invalidate_cache()
    license_api.reset_net_probe()
    diag_api.reset_last_bundle()


@pytest.fixture()
def settings(tmp_path) -> CompanionSettings:
    cli = tmp_path / "bpmkit.exe"
    cli.write_text("", encoding="utf-8")
    return CompanionSettings(mcp_cli=str(cli))


class _Run:
    def __init__(self, rc, stdout, stderr=""):
        self.result = (rc, stdout, stderr)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.result


# ======================================================================================
# проба связи (license_api.net_probe)
# ======================================================================================


def test_net_probe_failure_rc2_is_a_result_not_an_error(settings):
    run = _Run(2, json.dumps(PROBE_FAIL))
    result = license_api.net_probe(settings, run=run)

    assert run.calls[0][-3:] == ["setup", "net-probe", "--json"]
    assert result["ok"] is False
    assert result["cause"] == "tls_timeout"
    assert result["message_ru"].startswith("сервер активации не отвечает")
    assert result["it_text_ru"].startswith("Откройте исходящий HTTPS")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", result["checked_at"])
    # Последний результат помнит процесс хаба — для баннера после перезагрузки вкладки.
    assert license_api.last_net_probe()["cause"] == "tls_timeout"


def test_net_probe_ok(settings):
    result = license_api.net_probe(settings, run=_Run(0, json.dumps(PROBE_OK)))
    assert result["ok"] is True


def test_net_probe_rc_wins_over_ok_flag(settings):
    """`ok: true` при rc=2 — доверяем коду возврата (как у прочих вызовов CLI)."""
    result = license_api.net_probe(settings, run=_Run(2, json.dumps(PROBE_OK)))
    assert result["ok"] is False


def test_net_probe_old_mcp_without_subcommand(settings):
    run = _Run(2, "", "main.py setup: error: argument cmd: invalid choice: 'net-probe'")
    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.net_probe(settings, run=run)
    assert "обновите BPMkit" in exc.value.error
    assert license_api.last_net_probe() is None


def test_net_probe_without_cli_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr(license_api, "_candidate_roots", lambda extra_roots=None: [tmp_path])
    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.net_probe(CompanionSettings(mcp_cli=""), run=_Run(0, "{}"))
    assert exc.value.status == 503


# ======================================================================================
# «Повторить активацию» (license_api.license_refresh)
# ======================================================================================


def test_license_refresh_runs_info_with_refresh_and_caches_snapshot(settings):
    fresh = dict(INFO_OK, activation_state="active")
    run = _Run(0, json.dumps(fresh))
    snapshot = license_api.license_refresh(settings, run=run)

    assert run.calls[0][-4:] == ["setup", "license-info", "--json", "--refresh"]
    assert snapshot["edition"] == "companion"
    assert snapshot["activation_state"] == "active"
    # Следующее чтение сводки — из кэша, тем же снимком (CLI повторно не зовётся).
    broken = _Run(1, "", "не должен вызываться")
    assert license_api.license_info(settings, run=broken)["activation_state"] == "active"
    assert broken.calls == []


def test_license_refresh_old_mcp_without_flag(settings):
    run = _Run(2, "", "main.py: error: unrecognized arguments: --refresh")
    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.license_refresh(settings, run=run)
    assert "обновите BPMkit" in exc.value.error
    assert "раз в час" in exc.value.error


# ======================================================================================
# архив диагностики (diag_api)
# ======================================================================================


def test_diag_bundle_ok_remembers_folder(settings, tmp_path):
    archive = tmp_path / "Desktop" / "bpmkit_diag_20261102.zip"
    run = _Run(0, json.dumps({"ok": True, "path": str(archive), "size": 1234}))
    result = diag_api.diag_bundle(settings, run=run)

    assert run.calls[0][-3:] == ["setup", "diag-bundle", "--json"]
    assert result == {"ok": True, "path": str(archive), "folder": str(archive.parent),
                      "size": 1234}
    opened = []
    ok, _message = diag_api.open_last_bundle_folder(
        opener=lambda p: opened.append(p) or OpenFolderResult(True, f"открыто: {p}"))
    assert ok is True
    assert opened == [archive.parent]


def test_diag_bundle_failure_shows_detail(settings):
    run = _Run(2, json.dumps({"ok": False, "error": "archive_write_failed",
                              "detail": "нет места на диске"}))
    with pytest.raises(license_api.LicenseCliError) as exc:
        diag_api.diag_bundle(settings, run=run)
    assert exc.value.error == "Архив диагностики не создан"
    assert exc.value.detail == "нет места на диске"
    assert diag_api.last_bundle_path() is None


def test_diag_bundle_old_mcp(settings):
    run = _Run(2, "", "error: argument cmd: invalid choice: 'diag-bundle'")
    with pytest.raises(license_api.LicenseCliError) as exc:
        diag_api.diag_bundle(settings, run=run)
    assert "обновите BPMkit" in exc.value.error


def test_open_folder_without_bundle_refuses():
    ok, message = diag_api.open_last_bundle_folder(
        opener=lambda p: pytest.fail("без архива проводник не открывается"))
    assert ok is False
    assert "не собирался" in message


# ======================================================================================
# Маршруты хаба
# ======================================================================================


@pytest.fixture()
def hub(tmp_path):
    base_url, token, httpd = _start_hub(tmp_path)
    yield base_url, token
    for attr in ("status_poller", "companion_runner"):
        worker = getattr(httpd, attr, None)
        if worker is not None:
            try:
                worker.stop(timeout=0.2)
            except Exception:  # noqa: BLE001
                pass
    httpd.shutdown()
    httpd.server_close()


def test_route_net_probe_and_snapshot_carries_last_probe(hub, monkeypatch):
    base_url, token = hub

    def _probe(settings, **kw):
        result = dict(PROBE_FAIL, checked_at="2026-11-02T10:00:00Z")
        license_api._last_probe.update(result)
        return result

    monkeypatch.setattr(license_api, "net_probe", _probe)
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: dict(INFO_OK, edition="companion"))

    status, body, _ = _request(base_url, "/api/license/net-probe", token=token,
                               method="POST", origin=base_url)
    assert status == 200
    assert body["ok"] is True
    assert body["probe"]["ok"] is False
    assert body["probe"]["cause"] == "tls_timeout"

    status, body, _ = _request(base_url, "/api/license", token=token)
    assert status == 200
    assert body["net_probe_last"]["cause"] == "tls_timeout"


def test_route_net_probe_reports_cli_error(hub, monkeypatch):
    base_url, token = hub

    def _fail(settings, **kw):
        raise license_api.LicenseCliError("Эта версия BPMkit не умеет проверять связь")

    monkeypatch.setattr(license_api, "net_probe", _fail)
    status, body, _ = _request(base_url, "/api/license/net-probe", token=token,
                               method="POST", origin=base_url)
    assert status == 400
    assert body["ok"] is False


@pytest.mark.parametrize("path", ["/api/license/net-probe", "/api/license/refresh",
                                  "/api/diag/bundle", "/api/diag/open-folder"])
def test_new_routes_require_local_origin(hub, path):
    base_url, token = hub
    status, _body, _ = _request(base_url, path, token=token, method="POST",
                                origin="http://evil.example")
    assert status == 403


def test_route_refresh_returns_fresh_snapshot(hub, monkeypatch):
    base_url, token = hub
    calls = []
    monkeypatch.setattr(license_api, "license_refresh",
                        lambda settings, **kw: calls.append(1) or {"ok": True})
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: dict(INFO_OK, edition="companion",
                                                    activation_state="active"))
    status, body, _ = _request(base_url, "/api/license/refresh", token=token,
                               method="POST", origin=base_url)
    assert status == 200
    assert calls == [1]
    assert body["ok"] is True
    assert body["activation_state"] == "active"


def test_route_diag_bundle_and_open_folder(hub, monkeypatch):
    base_url, token = hub
    monkeypatch.setattr(diag_api, "diag_bundle",
                        lambda settings, **kw: {"ok": True, "path": "C:/x/d.zip",
                                                "folder": "C:/x", "size": 1})
    status, body, _ = _request(base_url, "/api/diag/bundle", token=token, method="POST",
                               origin=base_url)
    assert status == 200
    assert body["path"] == "C:/x/d.zip"

    monkeypatch.setattr(diag_api, "open_last_bundle_folder",
                        lambda **kw: (True, "открыто: C:/x"))
    # Путь в теле игнорируется: хаб открывает только папку своего архива.
    status, body, _ = _request(base_url, "/api/diag/open-folder", token=token,
                               method="POST", origin=base_url, body={"path": "C:/Windows"})
    assert status == 200
    assert body == {"ok": True, "message": "открыто: C:/x"}


# ======================================================================================
# формат hub.log
# ======================================================================================


def test_hub_log_line_is_iso_utc_level_component(tmp_path):
    path = hub_logging.setup_logging(log_dir=tmp_path, level=logging.INFO, force=True)
    try:
        hub_logging.logger().warning("строка формата")
        for handler in hub_logging.logger().handlers:
            handler.flush()
        line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    finally:
        hub_logging.reset_logging()
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z WARNING \[standkit_hub\] строка формата$",
        line), line


def test_formatter_time_is_utc_not_local():
    record = logging.LogRecord("standkit_hub", logging.INFO, __file__, 0, "x", None, None)
    record.created = 0.0  # 1970-01-01T00:00:00 UTC
    record.msecs = 5
    assert hub_logging.make_formatter().formatTime(record) == "1970-01-01T00:00:00.005Z"


# ======================================================================================
# Разметка и связки app.js
# ======================================================================================


def _html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_markup_has_probe_retry_and_diag_nodes():
    html = _html()
    for node in ("license-net", "license-probe-btn", "license-it-copy-btn",
                 "license-retry-btn", "license-probe-result", "license-probe-details",
                 "lic-details", "license-crit-details", "license-crit-probe-btn",
                 "about-diag-btn", "about-diag-open-btn", "about-diag-status",
                 "help-menu-diag"):
        assert f'id="{node}"' in html, f"узел потерян: {node}"
    for banner in ("license-banner-warn", "license-banner-crit"):
        block = html.split(f'id="{banner}"', 1)[1].split("</div>", 1)[0]
        for action in ("probe", "it-text", "retry"):
            assert f'data-lic-action="{action}"' in block, (banner, action)
    assert "Собрать диагностику" in html
    assert "Проверить связь с сервером активации" in html


def test_app_js_talks_to_new_routes():
    js = _read_js()
    for call in ('apiSend("POST", "/api/license/net-probe")',
                 'apiSend("POST", "/api/license/refresh")',
                 'apiSend("POST", "/api/diag/bundle")',
                 'apiSend("POST", "/api/diag/open-folder")'):
        assert call in js, call
    # Модалка, баннер и строка состояния берут текст истечения из ОДНОЙ функции.
    for fn in ("maybeShowLicenseCritModal", "renderLicenseBanners", "renderLicenseStatusline"):
        assert "licenseExpiredView(" in _extract_function(js, fn), fn
    assert "licenseCritModalKey(snapshot)" in _extract_function(js, "maybeShowLicenseCritModal")
    assert "maybeAutoNetProbe(snapshot)" in _extract_function(js, "applyLicense")
    # Старый безусловный текст «закончилась <expires_at>» ушёл из модалки.
    assert "закончилась ${formatDate(snapshot.expires_at)}" not in js


# ======================================================================================
# Тексты (node)
# ======================================================================================

ACTIVATION_WINDOW = {
    "status": "expired", "licensee": "Лицензиат", "license_id_tail": "7f3a",
    "expired_cause": "activation_window",
    "expires_at": "2026-11-07T14:59:48Z", "expires_effective": "2026-10-31T14:59:48Z",
    "activate_by": "2026-10-31T14:59:48Z", "term_start": "activation",
    "activation_last_error_message_ru": "сервер активации не отвечает: соединение "
                                        "обрывается при установке защищённого соединения",
    "last_online_error": "URLError: <urlopen error _ssl.c:1059 The handshake operation "
                         "timed out> (адрес https://api.example)",
    "activation_attempts_failed": 12,
}

CASES = {
    "window": ACTIVATION_WINDOW,
    "window_no_reason": {k: v for k, v in ACTIVATION_WINDOW.items()
                         if k != "activation_last_error_message_ru"},
    "term": {"status": "expired", "licensee": "Л", "license_id_tail": "7f3a",
             "expired_cause": "term", "expires_at": "2026-11-20T00:00:00Z",
             "expires_effective": "2026-11-09T00:00:00Z"},
    "old": {"status": "expired", "licensee": "Л", "license_id_tail": "7f3a",
            "expires_at": "2026-11-07T00:00:00Z"},
}

NOTICES = {
    "warn": {"status": "valid", "licensee": "Л", "activation_notice_level": "warn",
             "activation_effective_deadline": "2026-11-05T12:00:00Z",
             "activation_hours_left": 50.5,
             "activation_last_error_message_ru": "прокси требует авторизацию (407).",
             "last_online_error": "HTTPError 407"},
    "crit": {"status": "valid", "activation_notice_level": "crit",
             "activation_effective_deadline": "2026-11-03T12:00:00Z",
             "activation_hours_left": 4.2},
    "none": {"status": "valid", "activation_notice_level": ""},
    "old": {"status": "valid"},
}


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    if NODE is None:
        pytest.skip("node недоступен в этой среде")
    js = _read_js()
    script = "\n".join([
        _extract_function(js, "formatDate"),
        _extract_function(js, "pluralDays"),
        _extract_function(js, "licenseExpiredView"),
        _extract_function(js, "activationLeftText"),
        _extract_function(js, "licenseActivationNoticeView"),
        _extract_function(js, "licenseCritModalKey"),
        _extract_function(js, "netProbeView"),
        f"const CASES = {json.dumps(CASES, ensure_ascii=False)};",
        f"const NOTICES = {json.dumps(NOTICES, ensure_ascii=False)};",
        f"const PROBE_FAIL = {json.dumps(PROBE_FAIL, ensure_ascii=False)};",
        f"const PROBE_OK = {json.dumps(PROBE_OK, ensure_ascii=False)};",
        "const out = {expired: {}, keys: {}, notice: {}, probe: {}};",
        "for (const [k, s] of Object.entries(CASES)) {",
        "  out.expired[k] = licenseExpiredView(s); out.keys[k] = licenseCritModalKey(s); }",
        "for (const [k, s] of Object.entries(NOTICES)) out.notice[k] = licenseActivationNoticeView(s, null);",
        "out.notice.warn_probe_fail = licenseActivationNoticeView(NOTICES.warn, PROBE_FAIL);",
        "out.notice.warn_probe_ok = licenseActivationNoticeView(NOTICES.warn, PROBE_OK);",
        "out.probe.fail = netProbeView(PROBE_FAIL); out.probe.ok = netProbeView(PROBE_OK);",
        "console.log(JSON.stringify(out));",
    ])
    path = tmp_path_factory.mktemp("gap1178") / "views.js"
    path.write_text(script, encoding="utf-8")
    env = dict(os.environ, TZ="UTC")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_activation_window_names_cause_and_activate_by(views):
    v = views["expired"]["window"]
    assert v["modalTitle"] == "Лицензия не активирована"
    assert v["modalBody"].startswith(
        "Лицензию Лицензиат не удалось активировать на этом компьютере до 31.10.2026: "
        "сервер активации не отвечает")
    assert "Платные инструменты BPMkit отключены" in v["modalBody"]
    assert "Обратитесь к издателю за новым ключом" in v["modalBody"]
    assert v["bannerTitle"] == "Лицензия не активирована"
    assert "до 31.10.2026" in v["bannerText"]
    assert v["statusBold"] == "не активирована до 31.10.2026"
    assert v["probe"] is True
    for text in (v["modalBody"], v["bannerText"], v["statusBold"]):
        # Потолок expires_at и «закончилась» — ровно тот дефект, ради которого гэп.
        assert "07.11.2026" not in text
        assert "закончилась" not in text
        assert "истёк" not in text
        # Сырой URLError — только под «Подробности».
        assert "URLError" not in text
    assert "URLError" in v["detail"]


def test_activation_window_without_reason_still_reads(views):
    v = views["expired"]["window_no_reason"]
    assert "активировать на этом компьютере до 31.10.2026. Платные" in v["modalBody"]


def test_term_uses_effective_date(views):
    v = views["expired"]["term"]
    assert v["modalTitle"] == "Срок лицензии истёк"
    assert "закончилась 09.11.2026" in v["modalBody"]
    assert "09.11.2026" in v["bannerText"]
    assert v["probe"] is False


def test_old_mcp_keeps_previous_text(views):
    v = views["expired"]["old"]
    assert v["modalTitle"] == "Срок лицензии истёк"
    assert "закончилась 07.11.2026" in v["modalBody"]
    assert v["statusBold"] == "истекла"


def test_modal_memory_key_includes_cause(views):
    assert views["keys"]["window"] == "expired:7f3a:activation_window"
    assert views["keys"]["term"] == "expired:7f3a:term"
    assert views["keys"]["old"] == "expired:7f3a"


def test_notice_warn_banner_text(views):
    v = views["notice"]["warn"]
    assert v["level"] == "warn"
    assert v["title"] == "BPMkit не может подтвердить лицензию на этом компьютере"
    assert v["text"] == ": прокси требует авторизацию (407). Осталось 3 дня (до 05.11.2026)."
    assert "HTTPError" not in v["text"]
    assert v["detail"] == "HTTPError 407"
    assert "Осталось 3 дня (до 05.11.2026)" in v["modalBody"]


def test_notice_crit_last_hours(views):
    v = views["notice"]["crit"]
    assert v["level"] == "crit"
    assert "Осталось 5 ч. (до 03.11.2026)." in v["text"]
    assert "сервер активации не ответил" in v["text"]


def test_no_notice_without_level(views):
    assert views["notice"]["none"] is None
    assert views["notice"]["old"] is None


def test_notice_carries_probe_class_and_it_text(views):
    fail = views["notice"]["warn_probe_fail"]
    assert "Проверка связи: сервер активации не отвечает" in fail["text"]
    assert fail["itText"].startswith("Откройте исходящий HTTPS")
    ok = views["notice"]["warn_probe_ok"]
    assert "Повторить активацию" in ok["text"]
    assert ok["itText"] == ""


def test_probe_view(views):
    fail = views["probe"]["fail"]
    assert fail["text"].startswith("Нет связи с сервером активации api.example: "
                                   "сервер активации не отвечает")
    assert "URLError" not in fail["text"]
    assert "URLError" in fail["detail"]
    assert fail["itText"].startswith("Откройте исходящий HTTPS")
    ok = views["probe"]["ok"]
    assert ok["text"] == "Связь есть: сервер активации api.example отвечает (напрямую, 212 мс)."
    assert ok["itText"] == ""
