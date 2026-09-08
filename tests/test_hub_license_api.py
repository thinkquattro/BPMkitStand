# -*- coding: utf-8 -*-
"""Экран лицензии диспетчера: ``standkit_hub/license_api.py`` и маршруты ``/api/license``.

Предмет — ГРАНИЦА «хаб ↔ CLI BPMkit», а не лицензионная логика: она живёт в самом
MCP и на бэкенде издателя, у хаба её нет вовсе. Проверяется ровно то, за что хаб
отвечает:

* сводка отдаётся даже когда CLI рядом нет — ``edition: "free"`` и причина текстом,
  а не 500 на экране лицензии;
* КЛЮЧ НЕ ПОПАДАЕТ В КОМАНДНУЮ СТРОКУ. Командная строка чужого процесса видна всей
  машине (``tasklist``/``ps``), поэтому ключ уезжает временным файлом с правами
  только владельца — и файл удаляется при любом исходе, включая отказ CLI;
* кэш сводки живёт минуту, но КАЖДАЯ мутация сбрасывает его немедленно: «ввёл ключ,
  а ничего не изменилось» — это дефект, а не кэш;
* отказ CLI превращается в 400 с человеческим текстом, а отсутствие CLI — в 503
  (чинится путём в настройках, а не обращением к издателю).

Настоящий CLI не запускается ни разу: во все функции модуля инъектируется
``run(argv) -> (rc, stdout, stderr)`` — та же точка инъекции, что у
``standkit_companion.context.resolve``.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from standkit.models import Stand
from standkit.registry import Registry
from standkit_hub import license_api
from standkit_hub.config import CompanionSettings, HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server

# Текст, изображающий лицензионный ключ. Ни в одном argv он появиться не имеет права.
FAKE_KEY = "BPMKIT-KEY-c0ffee-НЕ-ДОЛЖЕН-ПОПАСТЬ-В-ARGV"

INFO_OK = {
    "ok": True,
    "status": "valid",
    "licensee": "ООО Ромашка",
    "tier": "pro",
    "tier_label": "Pro",
    "license_id_tail": "7f3a",
    "expires_at": "2027-03-12T00:00:00+00:00",
    "days_left": 185,
    "source": "keyring",
    "activated": True,
    "fingerprint_label": "t14r7",
    "reason": None,
    "backend_url": "https://updates.example",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш модульный — соседние тесты не имеют права видеть чужую сводку."""
    license_api.invalidate_cache()
    yield
    license_api.invalidate_cache()


@pytest.fixture()
def cli_path(tmp_path) -> Path:
    """Файл, изображающий ``bpmkit.exe``: резолв смотрит на существование файла."""
    path = tmp_path / "bpmkit.exe"
    path.write_text("", encoding="utf-8")
    return path


def _settings(cli_path) -> CompanionSettings:
    return CompanionSettings(mcp_cli=str(cli_path))


class _Runs:
    """Записывающий подставной запуск CLI: ``run(argv) -> (rc, stdout, stderr)``.

    Помимо кодов и вывода фиксирует СОДЕРЖИМОЕ файлов-аргументов на момент вызова:
    после возврата файл ключа удаляется, и проверить его иначе уже нечем.
    """

    def __init__(self, *responses):
        self.responses = list(responses) or [(0, json.dumps(INFO_OK), "")]
        self.calls: list = []
        self.file_args: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        for item in argv:
            candidate = Path(str(item))
            try:
                if candidate.is_file():
                    self.file_args.append((str(candidate),
                                           candidate.read_text(encoding="utf-8"),
                                           os.stat(candidate).st_mode))
            except OSError:
                continue
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


# ======================================================================================
# Резолв CLI
# ======================================================================================


def test_find_cli_prefers_explicit_setting(cli_path):
    assert license_api.find_cli(_settings(cli_path)) == [str(cli_path)]


def test_find_cli_splits_command_line_setting():
    """`python -m bpmkit` — это команда, а не путь: её нужно разобрать на argv."""
    result = license_api.find_cli(CompanionSettings(mcp_cli="python -m bpmkit"))
    assert result == ["python", "-m", "bpmkit"]


def test_find_cli_returns_none_without_cli(tmp_path, monkeypatch):
    """Автодетект ничего не нашёл — это `None`, а не догадка про PATH.

    `bpmkit` в PATH может оказаться другой сборкой; экран лицензии обязан спрашивать
    ТОТ MCP, рядом с которым установлен диспетчер (та же политика, что в context.py).
    """
    monkeypatch.setattr(license_api, "_candidate_roots", lambda extra_roots=None: [tmp_path])
    assert license_api.find_cli(CompanionSettings()) is None


# ======================================================================================
# Сводка
# ======================================================================================


def test_license_info_returns_companion_edition_snapshot(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))

    payload = license_api.license_info(_settings(cli_path), run=runs)

    assert payload["edition"] == "companion"
    assert payload["status"] == "valid"
    assert payload["licensee"] == "ООО Ромашка"
    assert payload["license_id_tail"] == "7f3a"
    assert runs.calls[0] == [str(cli_path), "setup", "license-info", "--json"]


def test_license_info_without_cli_is_free_edition(tmp_path, monkeypatch):
    """Диспетчер без MCP — штатная ситуация, а не ошибка экрана."""
    monkeypatch.setattr(license_api, "_candidate_roots", lambda extra_roots=None: [tmp_path])

    payload = license_api.license_info(CompanionSettings())

    assert payload["edition"] == "free"
    assert payload["status"] == "unavailable"
    assert payload["detail"]


def test_license_info_survives_broken_cli_output(cli_path):
    """CLI ответил мусором — «сведений нет», а не исключение наружу."""
    payload = license_api.license_info(_settings(cli_path),
                                       run=_Runs((1, "не JSON вовсе", "boom")))

    assert payload["edition"] == "free"
    assert payload["status"] == "unavailable"
    assert "boom" in payload["detail"]


def test_license_info_tolerates_banner_around_json(cli_path):
    """Баннер рантайма вокруг JSON — не повод считать ответ битым."""
    noisy = "WARNING: тут баннер\n" + json.dumps(INFO_OK) + "\n"
    payload = license_api.license_info(_settings(cli_path), run=_Runs((0, noisy, "")))

    assert payload["status"] == "valid"


def test_license_info_is_cached_for_a_minute(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))

    license_api.license_info(_settings(cli_path), run=runs)
    license_api.license_info(_settings(cli_path), run=runs)

    assert len(runs.calls) == 1, "вторая сводка обязана прийти из кэша"


def test_failed_snapshot_is_not_cached(cli_path):
    """Отказ не кэшируется: починку (установку MCP) обязано быть видно сразу."""
    runs = _Runs((1, "", "нет ответа"))

    license_api.license_info(_settings(cli_path), run=runs)
    license_api.license_info(_settings(cli_path), run=runs)

    assert len(runs.calls) == 2


def test_ttl_zero_disables_cache(cli_path):
    runs = _Runs((0, json.dumps(INFO_OK), ""))

    license_api.license_info(_settings(cli_path), run=runs, cache_ttl=0)
    license_api.license_info(_settings(cli_path), run=runs, cache_ttl=0)

    assert len(runs.calls) == 2


# ======================================================================================
# Запись ключа: ключ не в argv, файл с правами владельца, уборка
# ======================================================================================


def test_store_token_never_puts_key_into_argv(cli_path, tmp_path, monkeypatch):
    monkeypatch.setattr(license_api, "bpmkit_config_dir", lambda: tmp_path / "cfg")
    runs = _Runs((0, json.dumps({"ok": True, "status": "valid"}), ""))

    result = license_api.license_store_token(_settings(cli_path), FAKE_KEY, run=runs)

    assert result["ok"] is True
    argv = runs.calls[0]
    assert FAKE_KEY not in " ".join(argv), "ключ виден в командной строке процесса"
    assert argv[:3] == [str(cli_path), "setup", "license-store"]
    assert argv[-1] == "--json"
    # Ключ уехал ФАЙЛОМ — и на момент вызова файл содержал именно его.
    assert runs.file_args, "CLI не получил файла с ключом"
    _path, content, mode = runs.file_args[-1]
    assert content == FAKE_KEY
    if os.name != "nt":
        # Права только у владельца: на общей машине сосед не должен успеть прочитать.
        assert stat.S_IMODE(mode) == 0o600


def test_store_token_removes_temp_file_even_on_failure(cli_path, tmp_path, monkeypatch):
    key_dir = tmp_path / "cfg"
    monkeypatch.setattr(license_api, "bpmkit_config_dir", lambda: key_dir)
    runs = _Runs((1, json.dumps({"ok": False, "error": "Ключ не распознан",
                                 "detail": "подпись не сошлась"}), ""))

    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.license_store_token(_settings(cli_path), FAKE_KEY, run=runs)

    assert exc.value.status == 400
    assert exc.value.error == "Ключ не распознан"
    assert exc.value.detail == "подпись не сошлась"
    assert list(key_dir.glob("license-input-*")) == [], "временный файл ключа остался на диске"


def test_store_token_rejects_empty_key(cli_path):
    with pytest.raises(license_api.LicenseCliError):
        license_api.license_store_token(_settings(cli_path), "   ", run=_Runs())


def test_store_token_invalidates_snapshot_cache(cli_path, tmp_path, monkeypatch):
    monkeypatch.setattr(license_api, "bpmkit_config_dir", lambda: tmp_path / "cfg")
    info_runs = _Runs((0, json.dumps(INFO_OK), ""))
    license_api.license_info(_settings(cli_path), run=info_runs)

    license_api.license_store_token(_settings(cli_path), FAKE_KEY,
                                    run=_Runs((0, json.dumps({"ok": True}), "")))
    license_api.license_info(_settings(cli_path), run=info_runs)

    assert len(info_runs.calls) == 2, "после записи ключа сводка обязана перечитаться"


def test_store_file_reads_key_from_path(cli_path, tmp_path, monkeypatch):
    monkeypatch.setattr(license_api, "bpmkit_config_dir", lambda: tmp_path / "cfg")
    source = tmp_path / "license.lic"
    source.write_text(FAKE_KEY, encoding="utf-8")
    runs = _Runs((0, json.dumps({"ok": True}), ""))

    license_api.license_store_file(_settings(cli_path), str(source), run=runs)

    _path, content, _mode = runs.file_args[-1]
    assert content == FAKE_KEY
    # Путь, пришедший из браузера, самому CLI не передаётся — только своя копия.
    assert str(source) not in runs.calls[0]


def test_store_file_reports_unreadable_path(cli_path, tmp_path):
    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.license_store_file(_settings(cli_path),
                                       str(tmp_path / "нет-такого.lic"), run=_Runs())

    assert exc.value.status == 400
    assert "не прочитан" in exc.value.error


def test_missing_cli_is_503_not_400(tmp_path, monkeypatch):
    """Нет CLI — «возможность недоступна» (503), чинится путём в настройках."""
    monkeypatch.setattr(license_api, "_candidate_roots", lambda extra_roots=None: [tmp_path])

    with pytest.raises(license_api.LicenseCliError) as exc:
        license_api.license_deactivate(CompanionSettings())

    assert exc.value.status == 503


# ======================================================================================
# Снятие активации
# ======================================================================================


def test_deactivate_returns_cli_outcome_as_is(cli_path):
    payload = {"ok": True, "remote": "unreachable", "remote_detail": "нет сети",
               "removed": ["keyring", "state-cache"], "left": ["env-file"]}
    runs = _Runs((0, json.dumps(payload), ""))

    result = license_api.license_deactivate(_settings(cli_path), run=runs)

    assert result["remote"] == "unreachable"
    assert result["removed"] == ["keyring", "state-cache"]
    assert result["left"] == ["env-file"]
    assert runs.calls[0][-3:] == ["setup", "license-deactivate", "--json"]


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


def test_get_license_returns_snapshot(hub, monkeypatch):
    base_url, token = hub
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: dict(INFO_OK, edition="companion"))

    status, body, _ = _request(base_url, "/api/license", token=token)

    assert status == 200
    assert body["edition"] == "companion"
    assert body["status"] == "valid"


def test_get_license_requires_token(hub):
    base_url, _token = hub
    status, _body, _ = _request(base_url, "/api/license")
    assert status == 401


@pytest.mark.parametrize("method,body", [("PUT", {"token": FAKE_KEY}),
                                         ("DELETE", None)])
def test_license_mutations_require_local_origin(hub, method, body):
    """CSRF-контур мутаций распространяется на лицензию без послаблений."""
    base_url, token = hub
    status, _body, _ = _request(base_url, "/api/license", token=token, method=method,
                                body=body, origin="http://evil.example")
    assert status == 403


def test_put_license_stores_key_and_returns_fresh_snapshot(hub, monkeypatch):
    base_url, token = hub
    stored: list = []
    monkeypatch.setattr(license_api, "license_store_token",
                        lambda settings, value, **kw: stored.append(value) or {"ok": True})
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: dict(INFO_OK, edition="companion"))

    status, body, raw = _request(base_url, "/api/license", token=token, method="PUT",
                                 body={"token": FAKE_KEY}, origin=base_url)

    assert status == 200
    assert stored == [FAKE_KEY]
    assert body["ok"] is True
    # Ответ — снимок, а не эхо запроса: ключ обратно в браузер не возвращается.
    assert body["status"] == "valid"
    assert FAKE_KEY not in raw


def test_put_license_rejects_empty_token(hub):
    base_url, token = hub
    status, body, _ = _request(base_url, "/api/license", token=token, method="PUT",
                               body={"token": "  "}, origin=base_url)
    assert status == 400
    assert body["ok"] is False
    assert "token" in body["error"]


def test_put_license_reports_cli_failure_as_400(hub, monkeypatch):
    base_url, token = hub

    def _fail(settings, value, **kw):
        raise license_api.LicenseCliError("Ключ не распознан", detail="подпись не сошлась")

    monkeypatch.setattr(license_api, "license_store_token", _fail)

    status, body, _ = _request(base_url, "/api/license", token=token, method="PUT",
                               body={"token": FAKE_KEY}, origin=base_url)

    assert status == 400
    assert body == {"ok": False, "error": "Ключ не распознан", "detail": "подпись не сошлась"}


def test_put_license_reports_missing_cli_as_503(hub, monkeypatch):
    base_url, token = hub

    def _fail(settings, value, **kw):
        raise license_api.LicenseCliError("Рядом не найден CLI BPMkit", status=503)

    monkeypatch.setattr(license_api, "license_store_token", _fail)

    status, body, _ = _request(base_url, "/api/license", token=token, method="PUT",
                               body={"token": FAKE_KEY}, origin=base_url)

    assert status == 503
    assert body["ok"] is False


def test_post_license_file_uses_path(hub, monkeypatch, tmp_path):
    base_url, token = hub
    seen: list = []
    monkeypatch.setattr(license_api, "license_store_file",
                        lambda settings, path, **kw: seen.append(path) or {"ok": True})
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: dict(INFO_OK, edition="companion"))

    target = str(tmp_path / "license.lic")
    status, body, _ = _request(base_url, "/api/license/file", token=token, method="POST",
                               body={"path": target}, origin=base_url)

    assert status == 200
    assert seen == [target]
    assert body["ok"] is True
    assert body["status"] == "valid"


def test_post_license_file_requires_path(hub):
    base_url, token = hub
    status, body, _ = _request(base_url, "/api/license/file", token=token, method="POST",
                               body={}, origin=base_url)
    assert status == 400
    assert "path" in body["error"]


def test_delete_license_returns_outcome_and_snapshot(hub, monkeypatch):
    base_url, token = hub
    monkeypatch.setattr(license_api, "license_deactivate",
                        lambda settings, **kw: {"ok": True, "remote": "released",
                                                "removed": ["keyring"], "left": []})
    monkeypatch.setattr(license_api, "license_info",
                        lambda settings, **kw: {"ok": True, "edition": "companion",
                                                "status": "none"})

    status, body, _ = _request(base_url, "/api/license", token=token, method="DELETE",
                               origin=base_url)

    assert status == 200
    assert body["ok"] is True
    assert body["remote"] == "released"
    assert body["removed"] == ["keyring"]
    # И сразу свежий снимок: ключа больше нет.
    assert body["status"] == "none"


def test_patch_license_is_405(hub):
    """PUT завёлся только для лицензии — прочие методы по-прежнему отвергаются."""
    base_url, token = hub
    status, _body, _ = _request(base_url, "/api/license", token=token, method="PATCH",
                                body={}, origin=base_url)
    assert status == 405


def test_put_unknown_path_is_405(hub):
    base_url, token = hub
    status, _body, _ = _request(base_url, "/api/settings", token=token, method="PUT",
                                body={}, origin=base_url)
    assert status == 405
