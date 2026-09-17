# -*- coding: utf-8 -*-
"""Раздел «Данные и телеметрия» диспетчера: ``standkit_hub/consent_api.py`` и
маршрут ``/api/consent`` (GAP-332).

Предмет — та же граница «хаб ↔ CLI BPMkit», что у ``test_hub_license_api.py``:
согласия живут в MCP, диспетчер их только показывает и меняет ЧЕРЕЗ CLI.
Проверяется:

* сводка отдаётся даже когда CLI рядом нет — `available: false` и причина
  текстом, а НЕ 503 (в отличие от лицензии: раздел приватности обязан быть
  виден всегда, в т.ч. в свободной редакции без CLI, см. docstring модуля);
* `POST /api/consent` принимает ТОЛЬКО четыре известных флага — любое другое
  поле в теле — 400, значение обязано быть булевым;
* кэш сводки сбрасывается КАЖДОЙ мутацией — «переключил, а ничего не
  изменилось» здесь так же недопустимо, как у лицензии;
* разметка раздела: четыре переключателя со справкой (`.field-help`), кнопка
  предпросмотра, строки состояния.

Настоящий CLI не запускается: во все функции инъектируется
``run(argv) -> (rc, stdout, stderr)``.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from standkit.cli_resolve import CLI_ENV_VAR
from standkit.models import Stand
from standkit.registry import Registry
from standkit_hub import consent_api
from standkit_hub.config import CompanionSettings, HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub import server as server_module
from standkit_hub.server import create_hub_server

WEB_DIR = Path(server_module.__file__).parent / "web"

INFO_OK = {
    "ok": True,
    "analytics": {"value": True, "decided": True, "decided_at": "2026-08-01T00:00:00+00:00"},
    "attach_logs": {"value": False, "decided": True, "decided_at": "2026-08-01T00:00:00+00:00"},
    "pattern_submission": {"value": True, "decided": False, "decided_at": None},
    "candidate_submission": {"value": False, "decided": False, "decided_at": None},
    "eula_accepted_at": "2026-08-01T00:00:00+00:00",
    "install_id_tail": "a1b2",
    "defaults_source": "defaults",
    "consent_file": "/home/user/.bpmkit/consent.json",
    # `has_pending` (bool) — контракт CLI, ревью Opus В5: у CLI по факту
    # бинарное «есть/нет неотправленного» (глубже периода истории нет),
    # старое поле `pending_days` было недостижимым числом.
    "telemetry": {"last_sent_at": "2026-09-10T00:00:00+00:00", "has_pending": True,
                 "backend_host": "telemetry.bpmkit.example"},
    "preview": "analytics: включена\nattach_logs: выключена\n",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    consent_api.invalidate_cache()
    yield
    consent_api.invalidate_cache()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)


@pytest.fixture()
def cli_path(tmp_path) -> Path:
    path = tmp_path / "bpmkit.exe"
    path.write_text("", encoding="utf-8")
    return path


def _settings(cli_path) -> CompanionSettings:
    return CompanionSettings(mcp_cli=str(cli_path))


class _Runs:
    def __init__(self, *responses):
        self.responses = list(responses) or [(0, json.dumps(INFO_OK), "")]
        self.calls: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


# ======================================================================================
# consent_api: сводка
# ======================================================================================


def test_consent_info_returns_available_snapshot(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))

    payload = consent_api.consent_info(_settings(cli_path), run=runs)

    assert payload["available"] is True
    assert payload["analytics"]["value"] is True
    assert payload["install_id_tail"] == "a1b2"
    assert payload["preview"].startswith("analytics")
    assert runs.calls[0] == [str(cli_path), "setup", "consent-info", "--json"]
    assert payload["cli"] == str(cli_path)
    assert payload["cli_source"] == "settings"


def test_consent_info_without_cli_is_unavailable(tmp_path, monkeypatch):
    """Диспетчер без MCP — раздел обязан показать причину, а не упасть."""
    monkeypatch.setattr(consent_api, "_candidate_roots", lambda extra_roots=None: [tmp_path])

    payload = consent_api.consent_info(CompanionSettings())

    assert payload["available"] is False
    assert payload["reason"]
    assert payload["cli"] is None
    assert payload["cli_source"] is None


def test_consent_info_survives_broken_cli_output(cli_path):
    payload = consent_api.consent_info(_settings(cli_path),
                                       run=_Runs((1, "не JSON вовсе", "boom")))
    assert payload["available"] is False
    assert "boom" in payload["reason"]
    assert payload["cli"] == str(cli_path)


def test_consent_info_survives_module_missing(cli_path):
    """Контракт: `{"ok": false, "error": …}`, когда в старом MCP нет модуля
    `consent` — тоже «недоступно», а не 500."""
    payload = consent_api.consent_info(
        _settings(cli_path),
        run=_Runs((0, json.dumps({"ok": False, "error": "модуль consent не найден"}), "")),
    )
    assert payload["available"] is False
    assert "consent" in payload["reason"]


def test_consent_info_tolerates_banner_around_json(cli_path):
    noisy = "WARNING: баннер\n" + json.dumps(INFO_OK) + "\n"
    payload = consent_api.consent_info(_settings(cli_path), run=_Runs((0, noisy, "")))
    assert payload["available"] is True


def test_consent_info_is_cached(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))
    consent_api.consent_info(_settings(cli_path), run=runs, cache_ttl=60.0)
    consent_api.consent_info(_settings(cli_path), run=runs, cache_ttl=60.0)
    assert len(runs.calls) == 1


def test_failed_snapshot_is_not_cached(cli_path):
    runs = _Runs((1, "boom", "err"), (0, json.dumps(INFO_OK), ""))
    first = consent_api.consent_info(_settings(cli_path), run=runs, cache_ttl=60.0)
    second = consent_api.consent_info(_settings(cli_path), run=runs, cache_ttl=60.0)
    assert first["available"] is False
    assert second["available"] is True
    assert len(runs.calls) == 2


# ======================================================================================
# consent_api: изменение флагов
# ======================================================================================


def test_consent_set_builds_argv_from_flags(cli_path):
    runs = _Runs((0, json.dumps({"ok": True}), ""), (0, json.dumps(INFO_OK), ""))

    consent_api.consent_set(_settings(cli_path), {"analytics": False, "attach_logs": True},
                            run=runs)

    set_call = runs.calls[0]
    assert set_call[:3] == [str(cli_path), "setup", "consent-set"]
    tail = set_call[3:]
    assert "--analytics" in tail and tail[tail.index("--analytics") + 1] == "false"
    assert "--attach-logs" in tail and tail[tail.index("--attach-logs") + 1] == "true"


def test_consent_set_invalidates_cache(cli_path):
    """`consent_set` обязан сбросить старую сводку ДО того, как прочитает
    свежую: иначе «переключил, а ничего не изменилось» на экране."""
    stale = dict(INFO_OK)
    stale["analytics"] = {"value": True, "decided": True, "decided_at": None}
    fresh = dict(INFO_OK)
    fresh["analytics"] = {"value": False, "decided": True, "decided_at": None}

    consent_api.consent_info(_settings(cli_path), run=_Runs((0, json.dumps(stale), "")),
                             cache_ttl=60.0)

    result = consent_api.consent_set(
        _settings(cli_path), {"analytics": False},
        run=_Runs((0, json.dumps({"ok": True}), ""), (0, json.dumps(fresh), "")),
    )

    # Ответ мутации — СВЕЖАЯ сводка (сброшенный кэш + новый вызов CLI внутри
    # consent_set), а не заново отданная устаревшая.
    assert result["analytics"]["value"] is False


def test_consent_set_with_no_flags_skips_cli_call(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))
    result = consent_api.consent_set(_settings(cli_path), {}, run=runs)
    assert result["available"] is True
    assert len(runs.calls) == 1  # только сводка, без consent-set


def test_consent_set_rejects_unknown_flag(cli_path):
    with pytest.raises(AssertionError):
        consent_api.consent_set(_settings(cli_path), {"bogus": True}, run=_Runs())


# ======================================================================================
# consent_set НИКОГДА не бросает ConsentCliError (ревью Opus, Б3-блокер):
# ======================================================================================
#
# Воспроизведено ревьюером — до фикса `consent-set`, отказавший CLI (старый MCP
# без модуля consent, `ok:false`, ненулевой rc, таймаут) роняет обработчик
# необработанным исключением: соединение рвётся без ответа, фронт показывает
# «нет связи с хабом», хотя диспетчер жив.


def test_consent_set_cli_failure_returns_unavailable_not_raises(cli_path):
    """CLI отказал на самом `consent-set` (например, `ok:false` от старого
    MCP) — результат ОБЯЗАН быть обычным словарём `available:false`, а не
    брошенным исключением."""
    runs = _Runs((0, json.dumps({"ok": False, "error": "модуль consent не найден"}), ""))

    result = consent_api.consent_set(_settings(cli_path), {"analytics": False}, run=runs)

    assert result["available"] is False
    assert "consent" in result["reason"]


def test_consent_set_cli_timeout_returns_unavailable_not_raises(cli_path):
    """Таймаут/сбой запуска (`rc=-1`, вывод не JSON) — тот же честный отказ."""
    runs = _Runs((-1, "", "процесс не ответил"))

    result = consent_api.consent_set(_settings(cli_path), {"analytics": True}, run=runs)

    assert result["available"] is False
    assert "процесс не ответил" in result["reason"]


def test_consent_set_cli_failure_still_invalidates_cache(cli_path):
    """Кэш обязан сброситься даже при отказе самого `consent-set»: следующее
    чтение не должно тихо вернуть устаревшую (уже неверную) сводку."""
    consent_api.consent_info(_settings(cli_path), run=_Runs((0, json.dumps(INFO_OK), "")),
                             cache_ttl=60.0)

    consent_api.consent_set(
        _settings(cli_path), {"analytics": False},
        run=_Runs((1, "boom", "не принял")),
    )

    fresh_runs = _Runs((0, json.dumps(INFO_OK), ""))
    consent_api.consent_info(_settings(cli_path), run=fresh_runs, cache_ttl=60.0)
    assert len(fresh_runs.calls) == 1  # кэш был сброшен — снова пошли в CLI


# ======================================================================================
# rc != 0 обязан побеждать разбор `ok:false` (ревью Opus, М8): иначе на CLI,
# ответившем ненулевым кодом при формально валидном `ok:true`-JSON, теряется
# `detail` из stderr.
# ======================================================================================


def test_run_json_prefers_nonzero_rc_over_ok_field(cli_path):
    runs = _Runs((7, json.dumps({"ok": True}), "упал с кодом 7, подробности в stderr"))

    with pytest.raises(consent_api.ConsentCliError) as exc:
        consent_api._run_json(_settings(cli_path), consent_api.CONSENT_INFO_TAIL,
                              run=runs, failure="не ответил")

    assert "код возврата 7" in exc.value.detail or "упал с кодом 7" in exc.value.detail


# ======================================================================================
# Маршруты хаба
# ======================================================================================


def _start_hub(tmp_path):
    registry_path = tmp_path / "projects.json"
    Registry(path=registry_path, default="demo",
             stands={"demo": Stand(name="demo", stand_dir=str(tmp_path / "demo"))}).save()
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path)).save(config_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}", token, httpd


@pytest.fixture()
def hub(tmp_path):
    base_url, token, httpd = _start_hub(tmp_path)
    yield base_url, token
    for attr in ("status_poller", "companion_runner"):
        worker = getattr(httpd, attr, None)
        if worker is not None:
            try:
                worker.stop(timeout=0.2)
            except Exception:  # noqa: BLE001 - уборка не роняет тест
                pass
    stopper = threading.Thread(target=httpd.shutdown, daemon=True)
    stopper.start()
    stopper.join(timeout=1.0)
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001 - уборка не роняет тест
        pass


def _request(base_url, path, *, token=None, method="GET", body=None, origin=None):
    req = urllib.request.Request(base_url + path, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
    if origin is not None:
        req.add_header("Origin", origin)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {}), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else {}), raw


def test_get_consent_returns_snapshot(hub, monkeypatch):
    base_url, token = hub
    monkeypatch.setattr(consent_api, "consent_info",
                        lambda settings, **kw: dict(INFO_OK, available=True))

    status, body, _ = _request(base_url, "/api/consent", token=token)

    assert status == 200
    assert body["available"] is True
    assert body["analytics"]["value"] is True


def test_get_consent_without_cli_is_200_not_503(hub, monkeypatch):
    """Отличие от лицензии: раздел приватности обязан показать причину при
    200, а не спрятаться за 503 — иначе фронт без спец-обработки статуса
    показал бы просто пустой экран."""
    base_url, token = hub
    monkeypatch.setattr(consent_api, "consent_info",
                        lambda settings, **kw: {"ok": True, "available": False,
                                                "reason": "CLI не найден", "cli": None,
                                                "cli_source": None})

    status, body, _ = _request(base_url, "/api/consent", token=token)

    assert status == 200
    assert body["available"] is False
    assert body["reason"] == "CLI не найден"


def test_get_consent_requires_token(hub):
    base_url, _token = hub
    status, _body, _ = _request(base_url, "/api/consent")
    assert status == 401


def test_post_consent_requires_local_origin(hub):
    base_url, token = hub
    status, _body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                                body={"analytics": True}, origin="http://evil.example")
    assert status == 403


def test_post_consent_updates_flag_and_returns_fresh_snapshot(hub, monkeypatch):
    base_url, token = hub
    seen: list = []
    monkeypatch.setattr(consent_api, "consent_set",
                        lambda settings, flags, **kw: seen.append(flags) or dict(INFO_OK, available=True))

    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={"analytics": False}, origin=base_url)

    assert status == 200
    assert seen == [{"analytics": False}]
    assert body["available"] is True


def test_post_consent_accepts_multiple_known_flags(hub, monkeypatch):
    base_url, token = hub
    seen: list = []
    monkeypatch.setattr(consent_api, "consent_set",
                        lambda settings, flags, **kw: seen.append(flags) or dict(INFO_OK, available=True))

    status, _body, _ = _request(
        base_url, "/api/consent", token=token, method="POST",
        body={"pattern_submission": False, "candidate_submission": True}, origin=base_url,
    )

    assert status == 200
    assert seen == [{"pattern_submission": False, "candidate_submission": True}]


def test_post_consent_rejects_unknown_field(hub):
    base_url, token = hub
    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={"bogus_field": True}, origin=base_url)
    assert status == 400
    assert body["ok"] is False
    assert "bogus_field" in body["error"]


def test_post_consent_rejects_non_bool_value(hub):
    base_url, token = hub
    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={"analytics": "true"}, origin=base_url)
    assert status == 400
    assert body["ok"] is False
    assert "analytics" in body["error"]


def test_post_consent_empty_body_is_a_noop_ok(hub, monkeypatch):
    """Пустое тело — валидный (пустой) набор флагов, не 400: фронт может
    захотеть просто перечитать сводку через мутационный маршрут в будущем."""
    base_url, token = hub
    monkeypatch.setattr(consent_api, "consent_set",
                        lambda settings, flags, **kw: dict(INFO_OK, available=True))
    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={}, origin=base_url)
    assert status == 200
    assert body["available"] is True


def test_post_consent_cli_error_from_consent_set_is_200_not_500(hub, monkeypatch):
    """Ревью Opus, Б3-блокер, воспроизведено ревьюером: если бы `consent_set`
    (или что-то неожиданное внутри него) бросило `ConsentCliError` наружу,
    хендлер обязан отдать 200 `available:false`, а НЕ уронить соединение без
    ответа. `consent_set` сам уже не бросает (см. его тесты выше) — здесь
    проверяется defense-in-depth обёртка самого маршрута."""
    base_url, token = hub

    def _raise(settings, flags, **kw):
        raise consent_api.ConsentCliError("CLI BPMkit не принял изменение согласий",
                                          detail="код возврата 3")

    monkeypatch.setattr(consent_api, "consent_set", _raise)

    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={"analytics": False}, origin=base_url)

    assert status == 200
    assert body["ok"] is True
    assert body["available"] is False
    assert "код возврата 3" in body["reason"]


def test_post_consent_cli_ok_false_flows_through_as_unavailable(hub, monkeypatch):
    """Сквозной путь без monkeypatch внутренностей `consent_set`: настоящий
    (подставной) CLI отвечает `ok:false` на `consent-set` — маршрут обязан
    вернуть 200 `available:false`, а не 500."""
    base_url, token = hub
    calls: list = []

    def _run(argv):
        calls.append(list(argv))
        if "consent-set" in argv:
            return (0, json.dumps({"ok": False, "error": "модуль consent не найден"}), "")
        return (0, json.dumps(INFO_OK), "")

    monkeypatch.setattr(consent_api, "find_cli", lambda settings, **kw: ["fake-cli"])
    monkeypatch.setattr(consent_api, "_default_run", lambda argv, **kw: _run(argv))

    status, body, _ = _request(base_url, "/api/consent", token=token, method="POST",
                               body={"analytics": False}, origin=base_url)

    assert status == 200
    assert body["ok"] is True
    assert body["available"] is False
    assert calls  # дошли до CLI, а не срезали путь моком


# ======================================================================================
# Файл согласий повреждён (ревью Opus В4): `consent_file_corrupted` доезжает
# до снимка и рендерится предупреждением ДО переключателей.
# ======================================================================================


def test_consent_info_passes_through_corrupted_flag(cli_path):
    """`consent_file_corrupted`/`detail` — поля CLI, хаб их не трактует и не
    отфильтровывает (см. docstring consent_info: «хаб не выдумывает поля»)."""
    corrupted = dict(INFO_OK)
    corrupted["consent_file_corrupted"] = True
    corrupted["detail"] = "не удалось разобрать JSON"

    payload = consent_api.consent_info(_settings(cli_path),
                                       run=_Runs((0, json.dumps(corrupted), "")))

    assert payload["consent_file_corrupted"] is True
    assert payload["detail"] == "не удалось разобрать JSON"


def test_get_consent_corrupted_flag_reaches_http_response(hub, monkeypatch):
    base_url, token = hub
    corrupted = dict(INFO_OK, available=True, consent_file_corrupted=True,
                     detail="файл повреждён")
    monkeypatch.setattr(consent_api, "consent_info", lambda settings, **kw: corrupted)

    status, body, _ = _request(base_url, "/api/consent", token=token)

    assert status == 200
    assert body["consent_file_corrupted"] is True
    assert body["detail"] == "файл повреждён"


def test_consent_corrupted_warning_node_exists():
    """Разметка узла предупреждения — без него JS не за что зацепиться."""
    html = _web("index.html")
    assert 'id="consent-corrupted-warning"' in html


def test_render_consent_pane_shows_corrupted_warning_when_flagged():
    """`renderConsentPane` обязан читать `consent_file_corrupted` и выставлять
    текст предупреждения ДО переключателей, а не молча рисовать умолчания как
    настоящие значения."""
    js = _web("app.js")
    body = _extract_function(js, "renderConsentPane")
    assert "renderConsentCorruptedWarning" in body

    warn_fn = _extract_function(js, "renderConsentCorruptedWarning")
    assert "consent_file_corrupted" in warn_fn
    assert "consent-corrupted-warning" in warn_fn
    # textContent, не innerHTML — detail от CLI такой же чужой текст, как preview.
    assert "textContent" in warn_fn
    assert "innerHTML" not in warn_fn


# ======================================================================================
# `has_pending` вместо недостижимого `pending_days` (ревью Opus В5); на время
# рассинхронизации веток принимаются ОБА варианта.
# ======================================================================================


def test_render_consent_telemetry_status_reads_has_pending_and_falls_back():
    js = _web("app.js")
    fn = _extract_function(js, "consentHasPending")
    assert "has_pending" in fn
    assert "pending_days" in fn  # запасной путь на время рассинхронизации веток

    status_fn = _extract_function(js, "renderConsentTelemetryStatus")
    assert "consentHasPending" in status_fn
    assert "есть неотправленные данные" in status_fn
    assert "нет неотправленных данных" in status_fn
    # Старая формулировка "сут" (дни) больше не печатается вовсе.
    assert " сут" not in status_fn


# ======================================================================================
# Фронтенд: статические гарды раздела «Данные и телеметрия»
# ======================================================================================


def _web(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def test_consent_rail_item_exists():
    html = _web("index.html")
    assert 'data-pane="consent"' in html
    assert "Данные и телеметрия" in html


def test_consent_pane_markup_exists():
    html = _web("index.html")
    for node in ("consent-error", "consent-unavailable", "consent-content",
                 "consent-analytics", "consent-attach-logs",
                 "consent-pattern-submission", "consent-candidate-submission",
                 "consent-telemetry-status", "consent-eula-status",
                 "consent-preview-btn", "consent-preview-overlay",
                 "consent-preview-text"):
        assert f'id="{node}"' in html, f"узел раздела согласий потерян: {node}"


def _consent_pane_html(html: str) -> str:
    start = html.index('<div class="settings-pane" data-pane="consent">')
    end = html.index('<!-- ── Обновления', start)
    return html[start:end]


def test_every_consent_toggle_has_field_help_icon():
    pane = _consent_pane_html(_web("index.html"))
    for node in ("consent-analytics", "consent-attach-logs",
                 "consent-pattern-submission", "consent-candidate-submission"):
        idx = pane.index(f'id="{node}"')
        before = pane[:idx]
        label_start = before.rindex('<label class="checkbox-label">')
        between = pane[label_start:idx]
        after_idx = pane.index("</label>", idx)
        whole_label = pane[label_start:after_idx]
        assert 'class="field-help"' in whole_label, (
            f"переключатель {node} без иконки-подсказки"
        )


def test_consent_preview_uses_textcontent_not_innerhtml():
    """`preview` — чужой текст (ответ CLI), не HTML: innerHTML тут дефект."""
    js = _web("app.js")
    assert 'byId("consent-preview-text").textContent = text' in js
    assert 'consent-preview-text").innerHTML' not in js


def test_consent_ui_talks_to_consent_route():
    js = _web("app.js")
    assert '"/api/consent"' in js
    assert 'apiSend("POST", "/api/consent"' in js
    assert 'apiGet("/api/consent")' in js


# ======================================================================================
# Поведенческий тест: реальный `sendConsentFlag`, вырезанный из app.js, в
# минимальном node-окружении (см. приём test_hub_elevation_ui.py) — статическим
# grep'ом не отличить «откатывает чекбокс при отказе» от «оставляет как есть».
# ======================================================================================

NODE = shutil.which("node")


def _extract_function(js: str, name: str) -> str:
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


def _build_node_harness(js: str) -> str:
    send_flag = _extract_function(js, "sendConsentFlag")
    apply_toggles = _extract_function(js, "applyConsentToggles")

    return f"""
'use strict';
const elements = {{
  "consent-analytics": {{ checked: true }},
  "consent-attach-logs": {{ checked: false }},
  "consent-pattern-submission": {{ checked: false }},
  "consent-candidate-submission": {{ checked: false }},
  "consent-status": {{ textContent: "" }},
  "consent-error": {{ textContent: "" }},
}};
function byId(id) {{ return elements[id]; }}

const CONSENT_FIELDS = [
  ["analytics", "consent-analytics"],
  ["attach_logs", "consent-attach-logs"],
  ["pattern_submission", "consent-pattern-submission"],
  ["candidate_submission", "consent-candidate-submission"],
];

let lastConsent = {{
  analytics: {{ value: true }}, attach_logs: {{ value: false }},
  pattern_submission: {{ value: false }}, candidate_submission: {{ value: false }},
}};

let apiSendImpl = null;
function apiSend(method, path, body) {{ return apiSendImpl(method, path, body); }}
function describeApiError(e) {{ return (e && e.message) || String(e); }}
function applyConsent(snapshot) {{ lastConsent = snapshot; renderConsentPaneStub(snapshot); }}
function renderConsentPaneStub(snapshot) {{ applyConsentToggles(snapshot); }}

{apply_toggles}

{send_flag}

async function scenarioSuccess() {{
  elements["consent-analytics"].checked = false;
  apiSendImpl = () => Promise.resolve({{
    available: true,
    analytics: {{ value: false }}, attach_logs: {{ value: false }},
    pattern_submission: {{ value: false }}, candidate_submission: {{ value: false }},
  }});
  await sendConsentFlag("analytics", false);
  return {{
    statusText: elements["consent-status"].textContent,
    errorText: elements["consent-error"].textContent,
    checkedAfter: elements["consent-analytics"].checked,
  }};
}}

async function scenarioFailureRollsBack() {{
  lastConsent = {{
    analytics: {{ value: true }}, attach_logs: {{ value: false }},
    pattern_submission: {{ value: false }}, candidate_submission: {{ value: false }},
  }};
  elements["consent-analytics"].checked = false; // человек кликнул — выключил
  apiSendImpl = () => Promise.reject(new Error("CLI недоступен"));
  await sendConsentFlag("analytics", false);
  return {{
    statusText: elements["consent-status"].textContent,
    errorText: elements["consent-error"].textContent,
    // Откат: чекбокс должен вернуться к последнему ПОДТВЕРЖДЁННЫМ сервером значению (true).
    checkedAfterRollback: elements["consent-analytics"].checked,
  }};
}}

(async () => {{
  const results = {{
    success: await scenarioSuccess(),
    failure: await scenarioFailureRollsBack(),
  }};
  console.log(JSON.stringify(results));
}})().catch((e) => {{
  console.error(e && e.stack || String(e));
  process.exit(1);
}});
"""


def _run_node_harness(tmp_path) -> dict:
    js = _web("app.js")
    script = _build_node_harness(js)
    script_path = tmp_path / "consent_behavior.js"
    script_path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(script_path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_send_consent_flag_success_updates_status_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    success = results["success"]
    assert success["statusText"] == "Сохранено"
    assert success["errorText"] == ""
    assert success["checkedAfter"] is False


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_send_consent_flag_failure_rolls_back_checkbox_via_node(tmp_path):
    results = _run_node_harness(tmp_path)
    failure = results["failure"]
    # Сервер отказал — чекбокс обязан вернуться к тому, что реально подтверждено
    # (true), а не остаться в положении, которое сервер не принял.
    assert failure["checkedAfterRollback"] is True
    assert "CLI недоступен" in failure["errorText"]
