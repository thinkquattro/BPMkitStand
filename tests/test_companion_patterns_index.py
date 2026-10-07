# -*- coding: utf-8 -*-
"""Тесты автообновления индекса паттернов (standkit 0.12.21).

Требование владельца: индекс паттернов у клиента обновляется САМ, когда издатель
опубликовал на бэкенде новый; пункт «Паттерны» окна «Обновления» показывает «актуален»
ТОЛЬКО когда индекс у клиента совпадает с индексом на сервере. Сверка — на плановом тике
и по «Проверить обновления».

Контракт (общий с бэкендом и клиентом MCP):

* `GET /v1/content/patterns/stats` → `index_sha256` (`null` — не опубликован), `patterns`;
* `GET /v1/content/patterns/index` → `{"sha256","generated_at","patterns","index"}` +
  `ETag: "<sha>"`; `If-None-Match: "<sha>"` → 304; нет индекса → 404
  `pattern_index_not_published`;
* отпечаток — `sha256(json.dumps(index, ensure_ascii=False, sort_keys=True,
  separators=(",", ":")).encode("utf-8"))`;
* компаньон пишет `<override>/dev/patterns_index.server.json` атомарно, ПЕРЕСЧИТАВ
  отпечаток; не совпало — не пишет.

Сеть не поднимается: подставной клиент отдаёт записанные ответы и журналирует запросы.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from standkit_companion import fsutil
from standkit_companion import patterns as pm
from standkit_companion.errors import ChannelError, NotModified
from standkit_hub.config import CompanionSettings
from tests.test_companion_patterns import FakeContext, make_shipped

INDEX_V1 = {"format": 2, "note": "поставка", "files": [
    {"file": "dev/patterns_js_ui.md", "area": "js_ui",
     "sections": [{"heading": "Кнопка на карточке", "level": 3, "snippet": "",
                   "key": "pat:1"}]}]}
INDEX_V2 = {"format": 2, "note": "сервер", "files": [
    {"file": "dev/patterns_js_ui.md", "area": "js_ui",
     "sections": [{"heading": "Кнопка на карточке", "level": 3, "snippet": "",
                   "key": "pat:1"},
                  {"heading": "Деталь со связью", "level": 3, "snippet": "новое",
                   "key": "pat:2"}]}]}


def sha_of(index) -> str:
    """Отпечаток ПО ФОРМУЛЕ КОНТРАКТА, а не вызовом проверяемого модуля."""
    blob = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class IndexClient:
    """Бэкенд: `stats` и `index` из сценария; журналирует запросы."""

    def __init__(self, *, stats=None, index=None, has_envelope=True) -> None:
        self.stats = stats
        self.index = index
        self.has_envelope = has_envelope
        self.calls: list = []

    def get_json(self, path, *, params=None, authorized=True, etag=None, timeout=None):
        self.calls.append({"path": path, "etag": etag, "timeout": timeout})
        value = self.stats if path == pm.STATS_PATH else self.index
        if path not in (pm.STATS_PATH, pm.INDEX_PATH):
            raise AssertionError(f"неожиданный запрос {path}")
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(etag)
        return value

    def index_calls(self) -> list:
        return [c for c in self.calls if c["path"] == pm.INDEX_PATH]


def stats_of(index_sha, *, patterns=2, updates=1, sections=5) -> dict:
    return {"sections": sections, "updates": updates, "patterns": patterns,
            "index_sha256": index_sha, "updated_at": "2026-10-07T08:00:00Z"}


def index_response(index, *, sha=None, etag="auto"):
    payload = {"sha256": sha if sha is not None else sha_of(index),
               "generated_at": "2026-10-07T07:59:00Z", "patterns": 2, "index": index}
    headers = {} if etag is None else {
        "etag": f'"{sha_of(index)}"' if etag == "auto" else etag}
    return payload, headers


def env(tmp_path, *, shipped_index=INDEX_V1):
    shipped = make_shipped(tmp_path)
    if shipped_index is not None:
        (shipped / "dev" / "patterns_index.json").write_text(
            json.dumps(shipped_index, ensure_ascii=False, indent=2), encoding="utf-8")
    override = tmp_path / "appdata" / "patterns" / "references"
    return FakeContext(shipped, override), override


def server_file(override) -> Path:
    return Path(override) / "dev" / pm.SERVER_INDEX_NAME


def reconcile(client, ctx, previous=None):
    try:
        stats = pm.fetch_stats(client)
    except Exception as exc:  # noqa: BLE001 - как в раннере
        stats = pm.stats_from_error(exc)
    return pm.reconcile_index(client, ctx, stats, previous=previous)


# --------------------------------------------------------------------------------------
# Отпечаток и локальный индекс
# --------------------------------------------------------------------------------------
def test_index_sha256_is_contract_formula():
    assert pm.index_sha256(INDEX_V2) == sha_of(INDEX_V2)
    # Кириллица — как есть, не \\uXXXX; порядок ключей не важен.
    reordered = json.loads(json.dumps(INDEX_V2), object_pairs_hook=lambda kv: dict(kv[::-1]))
    assert pm.index_sha256(reordered) == sha_of(INDEX_V2)


def test_local_sha_prefers_server_file_then_shipped(tmp_path):
    ctx, override = env(tmp_path)
    sha, source = pm.local_index_sha(override, ctx.shipped_patterns_root)
    assert (sha, source) == (sha_of(INDEX_V1), "shipped")
    server_file(override).parent.mkdir(parents=True)
    server_file(override).write_text(json.dumps(
        {"sha256": sha_of(INDEX_V2), "index": INDEX_V2}, ensure_ascii=False), "utf-8")
    assert pm.local_index_sha(override, ctx.shipped_patterns_root) == (
        sha_of(INDEX_V2), "server")


def test_broken_server_file_falls_back_to_shipped(tmp_path):
    ctx, override = env(tmp_path)
    server_file(override).parent.mkdir(parents=True)
    # Записанный отпечаток не совпадает с содержимым (правка руками) — файл битый.
    server_file(override).write_text(json.dumps(
        {"sha256": "0" * 64, "index": INDEX_V2}), "utf-8")
    assert pm.local_index_sha(override, ctx.shipped_patterns_root) == (
        sha_of(INDEX_V1), "shipped")
    server_file(override).write_text("{не json", "utf-8")
    assert pm.local_index_sha(override, ctx.shipped_patterns_root)[1] == "shipped"


def test_shipped_format1_never_matches(tmp_path):
    ctx, override = env(tmp_path, shipped_index={"format": 1, "files": []})
    assert pm.local_index_sha(override, ctx.shipped_patterns_root) == (
        None, "shipped_format1")
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2))
    block = reconcile(client, ctx)
    assert block["status"] == "ok"
    assert client.index_calls()[0]["etag"] is None, "сверять нечем — без If-None-Match"
    assert json.loads(server_file(override).read_text("utf-8"))["sha256"] == sha_of(INDEX_V2)


# --------------------------------------------------------------------------------------
# Сверка
# --------------------------------------------------------------------------------------
def test_match_is_ok_without_download(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V1)), {}),
                         index=AssertionError("качать не нужно"))
    block = reconcile(client, ctx)
    assert block["status"] == "ok"
    assert block["local_sha"] == block["server_sha"] == sha_of(INDEX_V1)
    assert client.index_calls() == []
    assert not server_file(override).exists()


def test_mismatch_downloads_writes_and_is_ok(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2))
    block = reconcile(client, ctx)

    assert block["status"] == "ok"
    assert block["local_sha"] == block["server_sha"] == sha_of(INDEX_V2)
    assert block["local_source"] == "server"
    assert block["last_fetch_at"] and block["last_check_at"]
    call = client.index_calls()[0]
    assert call["etag"] == f'"{sha_of(INDEX_V1)}"', "If-None-Match — локальный, в кавычках"
    assert call["timeout"] == pm.INDEX_TIMEOUT_SEC
    doc = json.loads(server_file(override).read_text(encoding="utf-8"))
    assert set(doc) == {"sha256", "generated_at", "fetched_at", "index"}
    assert doc["sha256"] == sha_of(INDEX_V2) and doc["index"] == INDEX_V2
    assert doc["fetched_at"] == block["last_fetch_at"]
    assert "Деталь со связью" in server_file(override).read_text(encoding="utf-8"), (
        "UTF-8 без \\uXXXX")
    # Корень override засеян: без поставки рядом серверный индекс заменил бы базу пустотой.
    assert (override / "dev" / "patterns_index.md").is_file()

    # Следующая сверка — уже совпадение, без скачивания.
    again = reconcile(client, ctx, previous=block)
    assert again["status"] == "ok" and len(client.index_calls()) == 1


def test_not_modified_is_ok(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=NotModified("не изменилось"))
    block = reconcile(client, ctx)
    assert block["status"] == "ok"
    assert not server_file(override).exists()


def test_sha_mismatch_in_response_is_not_written(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2, sha="ab" * 32, etag=None))
    block = reconcile(client, ctx)
    assert block["status"] == "stale"
    assert block["kind"] == "integrity_mismatch"
    assert not server_file(override).exists(), "непроверенный индекс не пишется"


def test_etag_mismatch_is_not_written(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2, etag=f'"{"cd" * 32}"'))
    block = reconcile(client, ctx)
    assert block["status"] == "stale" and block["kind"] == "integrity_mismatch"
    assert not server_file(override).exists()


def test_server_without_index_is_not_published(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(None, patterns=None), {}),
                         index=AssertionError("качать нечего"))
    block = reconcile(client, ctx)
    assert block["status"] == "not_published", "не «актуален» — сверять не с чем"
    assert client.index_calls() == []
    # Старый бэкенд (без новых полей) — тоже «не опубликован».
    old = IndexClient(stats=({"sections": 3, "updates": 0}, {}))
    assert reconcile(old, ctx)["status"] == "not_published"


def test_index_404_is_not_published(tmp_path):
    ctx, override = env(tmp_path)
    err = ChannelError("Бэкенд издателя ответил 404", kind="http_error", http_status=404,
                       detail="pattern_index_not_published")
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}), index=err)
    assert reconcile(client, ctx)["status"] == "not_published"


def test_offline(tmp_path):
    ctx, override = env(tmp_path)
    off = ChannelError("Бэкенд издателя недоступен", kind="offline")
    assert reconcile(IndexClient(stats=off), ctx)["status"] == "offline"
    # Счётчик ответил, а индекс — уже нет.
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}), index=off)
    assert reconcile(client, ctx)["status"] == "offline"
    assert not server_file(override).exists()


def test_license(tmp_path):
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}), has_envelope=False)
    block = reconcile(client, ctx)
    assert block["status"] == "no_license"
    assert client.calls == [], "без конверта лицензии в сеть не ходим"
    expired = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                          index=ChannelError("истекла", kind="expired", http_status=401))
    assert reconcile(expired, ctx)["status"] == "no_license"


def test_write_is_atomic_and_failure_keeps_previous(tmp_path, monkeypatch):
    ctx, override = env(tmp_path)
    good = {"sha256": sha_of(INDEX_V1), "generated_at": None, "index": INDEX_V1}
    pm.write_server_index(override, good, fetched_at="2026-10-07T00:00:00Z")
    before = server_file(override).read_bytes()

    used: list = []
    real = fsutil.atomic_write_text

    def spy(path, text, encoding="utf-8"):
        used.append(Path(path))
        real(path, text, encoding)
    monkeypatch.setattr(fsutil, "atomic_write_text", spy)
    pm.write_server_index(override, {"sha256": sha_of(INDEX_V2), "index": INDEX_V2},
                          fetched_at="2026-10-07T00:01:00Z")
    assert used == [server_file(override)], "запись только через tmp+replace"

    # Сбой на замене — прежний файл цел, статус «устарел».
    server_file(override).write_bytes(before)
    pm.seed_override_root(ctx.shipped_patterns_root, override)  # seed уже был

    def broken_write(path, data):
        Path(str(path) + ".tmp").write_bytes(data)
        raise OSError("файл занят")  # os.replace не случился
    monkeypatch.setattr(fsutil, "atomic_write_bytes", broken_write)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2))
    block = reconcile(client, ctx)
    assert block["status"] == "stale" and block["kind"] == "local_io"
    assert server_file(override).read_bytes() == before

    # Подложенный индекс с чужим отпечатком не пишется вовсе.
    with pytest.raises(ChannelError):
        pm.write_server_index(override, {"sha256": "0" * 64, "index": INDEX_V2},
                              fetched_at="x")


def test_window_counter_uses_patterns_plus_updates():
    stats = pm.fetch_stats(IndexClient(stats=(stats_of("a" * 64, patterns=40, updates=2,
                                                       sections=999), {})))
    assert stats["total"] == 42 and stats["index_sha256"] == "a" * 64
    assert pm.stats_line(stats).startswith("Паттерны: 42 паттерна на сервере")
    legacy = pm.fetch_stats(IndexClient(stats=({"sections": 10, "updates": 1}, {})))
    assert legacy["total"] == 11 and legacy["patterns"] is None


# --------------------------------------------------------------------------------------
# Раннер: тик и кнопка «Проверить обновления»
# --------------------------------------------------------------------------------------
def _runner(tmp_path, client, ctx):
    from standkit_companion.runner import CompanionRunner

    settings = CompanionSettings(enabled=True)
    return CompanionRunner(
        tmp_path / "standkit-hub.json",
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: settings,
        client_factory=lambda c, s: client,
        context_resolver=lambda s: ctx,
    )


def test_check_update_button_starts_background_reconcile(tmp_path, monkeypatch):
    from standkit_companion import releases

    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2))
    runner = _runner(tmp_path, client, ctx)
    monkeypatch.setattr(releases, "check", lambda *a, **k: {"available": False})
    monkeypatch.setattr(runner, "_sync_cookbook", lambda session: None)

    result = runner.run_action("check_update")
    assert result["patterns"]["refreshing"] is True, "кнопка не ждёт докачку индекса"
    runner._stats_thread.join(15.0)

    snap = runner.patterns_stats(refresh=False)
    assert snap["index"]["status"] == "ok" and snap["index"]["title"] == "актуален"
    assert server_file(override).is_file()
    saved = json.loads((tmp_path / "companion-state.json").read_text(encoding="utf-8"))
    sync = saved["patterns"]["index_sync"]
    assert sync["status"] == "ok" and sync["server_sha"] == sha_of(INDEX_V2)
    for key in ("local_sha", "server_sha", "last_check_at", "last_fetch_at", "status",
                "detail"):
        assert key in sync
    assert runner.status()["state"]["patterns"]["server"]["index"]["status"] == "ok"


def test_scheduled_tick_reconciles_index(tmp_path, monkeypatch):
    ctx, override = env(tmp_path)

    class TickClient(IndexClient):
        def get_json(self, path, **kwargs):
            if path == pm.SYNC_PATH:
                self.calls.append({"path": path})
                return {"patterns": [], "has_more": False, "count": 0,
                        "next_since": None, "next_since_id": None}, {}
            return super().get_json(path, **kwargs)

    client = TickClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                        index=index_response(INDEX_V2))
    runner = _runner(tmp_path, client, ctx)
    monkeypatch.setattr(runner, "_sync_cookbook", lambda session: None)
    monkeypatch.setattr(runner, "_sync_candidates", lambda settings: None)

    report = runner.run_cycle("patterns", force=True)
    assert report["status"] == "ok"
    assert len(client.index_calls()) == 1
    assert runner.patterns_stats(refresh=False)["index"]["status"] == "ok"
    assert json.loads(server_file(override).read_text("utf-8"))["index"] == INDEX_V2


def test_stale_until_match(tmp_path):
    """Пока сверка не удалась (порча отпечатка) — не «актуален», причина в detail."""
    ctx, override = env(tmp_path)
    client = IndexClient(stats=(stats_of(sha_of(INDEX_V2)), {}),
                         index=index_response(INDEX_V2, sha="ab" * 32, etag=None))
    runner = _runner(tmp_path, client, ctx)
    snap = runner.patterns_stats(wait=10.0)
    assert snap["refreshing"] is False
    assert snap["index"]["status"] == "stale" and snap["index"]["title"] == "устарел"
    assert snap["index"]["detail"]


# --------------------------------------------------------------------------------------
# Окно: чип пункта «Паттерны»
# --------------------------------------------------------------------------------------
def test_window_chip_texts():
    import standkit_hub.server as server_module

    js = (Path(server_module.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    start = js.index("function renderPatternsServer(")
    body = js[start:js.index("function renderPatternsDetail(", start)]
    # «актуален» — только ветка совпадения индекса, и она первая.
    assert body.index('ix.status === "ok"') < body.index('"актуален"') < body.index(
        "s.refreshing")
    for text in ('"обновляется…"', '"устарел"', '"лицензия не активна"', '"нет связи"',
                 '"не опубликован"'):
        assert text in body, text
    assert 'action === "check_update"' in js and "refreshPatternsStats();" in js
