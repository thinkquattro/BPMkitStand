# -*- coding: utf-8 -*-
"""Тесты standkit_companion.patterns — канала метаданных межрелизных паттернов.

Зачем файл. Тела межрелизных паттернов выдаются онлайн по лицензии (MCP берёт раздел с
бэкенда издателя), а компаньон пишет на диск клиента только индекс метаданных
`dev/patterns_updates_index.json`. Правила, которые здесь закрыты регресс-тестами, ломаются
ТИХО — раздел пропадает из поиска, курсор не двигается, отозванный паттерн остаётся в
индексе, тело всё-таки оседает на диске:

* seed поставочного дерева в override-корень (override заменяет поставку ЦЕЛИКОМ);
* курсор — ПАРА `(since, since_id)`, уезжает в запрос вместе или никак;
* пагинация до `has_more == false` в ОДНОМ проходе, `count == 0` проход не прерывает;
* пустая дельта — штатный `ok`, файлы не трогаются;
* `sync` без тел → индекс в формате поставочного `patterns_index.json`;
* tombstone убирает раздел из индекса безусловно;
* тело от старого бэкенда игнорируется — ни в индекс, ни в состояние, ни на диск;
* миграция удаляет прежние `patterns_*_updates.md` и управляемый блок индекса-markdown;
* `bundle_sha256` рвёт страницу целиком и НЕ двигает курсор;
* счётчик библиотеки на сервере (`stats`) → строка окна «Обновления»; нет сети/лицензии.

Сеть не поднимается: клиенту нужен единственный метод `get_json`, подставной клиент
заодно журналирует параметры запросов.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from standkit_companion import patterns as pm
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionSettings

MCP_VERSION = "0.305.0"


# --------------------------------------------------------------------------------------
# Подставные соседи
# --------------------------------------------------------------------------------------
class FakeClient:
    """Клиент бэкенда: отдаёт заранее записанные ответы и журналирует запросы."""

    def __init__(self, pages, *, stats=None, has_envelope=True) -> None:
        self.pages = list(pages)
        self.stats = stats
        self.has_envelope = has_envelope
        self.calls: list = []

    def get_json(self, path, *, params=None, authorized=True, etag=None, timeout=None):
        self.calls.append({"path": path, "params": dict(params or {}),
                           "authorized": authorized, "etag": etag, "timeout": timeout})
        if path == pm.STATS_PATH:
            if isinstance(self.stats, BaseException):
                raise self.stats
            return self.stats, {}
        if not self.pages:
            raise AssertionError(
                "Канал запросил больше страниц, чем предусмотрено сценарием теста: "
                "значит пагинация не остановилась на has_more=false")
        return self.pages.pop(0), {}


class FakeContext:
    """Лицензионный контекст: каналу нужны только пути и версия MCP (duck-typing)."""

    def __init__(self, shipped_root, override_root, mcp_version=MCP_VERSION) -> None:
        self.envelope = "BPMKIT1.payload.sig"
        self.license_status = "active"
        self.backend_url = "https://backend.example"
        self.mcp_version = mcp_version
        self.package_root = str(Path(shipped_root).parent)
        self.shipped_patterns_root = str(shipped_root)
        self.override_patterns_root = str(override_root)
        self.patterns_env_registered = True
        self.revocations_target = ""
        self.revocations_env_registered = False
        self.artifact_pubkey = ""
        self.binary_path = ""
        self.cli = []
        self.raw = {}


# --------------------------------------------------------------------------------------
# Конструкторы данных
# --------------------------------------------------------------------------------------
def bundle_of(items) -> str:
    """Контрольная сумма страницы ПО ФОРМУЛЕ КОНТРАКТА, а не вызовом проверяемого модуля."""
    blob = json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pattern(pid, *, title, area="js_ui", min_mcp="0.0.0", snippet=None, body=None,
            status="published", section_key="auto"):
    """Запись нового бэкенда: тела и подписи нет (null), есть `section_key`."""
    return {
        "id": pid,
        "title": title,
        "body_markdown": body,
        "version": "1",
        "min_mcp_version": min_mcp,
        "area": area,
        "proof": "L",
        "pattern_type": "dev",
        "published_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-01T10:00:00Z",
        "status": status,
        "content_sha256": (hashlib.sha256(body.encode("utf-8")).hexdigest()
                           if body else None),
        "signature": None,
        "sig_key_id": None,
        "section_key": f"pat:{pid}" if section_key == "auto" else section_key,
        "snippet": snippet,
        "deleted": False,
    }


def tombstone(pid, **extra):
    item = {"id": pid, "status": "revoked", "deleted": True,
            "updated_at": "2026-08-02T10:00:00Z"}
    item.update(extra)
    return item


def page(items, *, has_more=False, next_since="2026-08-01T10:00:00Z", next_since_id=1,
         since=None, since_id=None, bundle=None):
    return {
        "generated_at": "2026-08-20T12:00:00Z",
        "since": since,
        "since_id": since_id,
        "next_since": next_since,
        "next_since_id": next_since_id,
        "has_more": has_more,
        "count": len(items),
        "bundle_sha256": bundle if bundle is not None else bundle_of(items),
        "patterns": items,
    }


SHIPPED_INDEX = """# Индекс паттернов BPMSoft

Рукописный поставочный индекс. Его текст канал не трогает.

## Клиентские схемы (`dev/patterns_js_ui.md`)

| Паттерн | Когда использовать |
|---------|--------------------|
| **Кнопка в реестре** | Нужна кнопка на панели раздела |
"""

SHIPPED_JS = """## Кнопка в реестре

Нужна кнопка на панели раздела.
"""

SHIPPED_CS = """## Листенер сущности

Нужна реакция на сохранение записи.
"""

BODY_SECRET = """## Задача
Тело паттерна, которое НЕ должно попасть на диск клиента: UsrSecretMarker.

```js
var x = 1;
```
"""


def make_shipped(tmp_path) -> Path:
    root = tmp_path / "package" / "skills" / "bpmsoft-dev" / "references"
    (root / "dev").mkdir(parents=True)
    (root / "dev" / "patterns_index.md").write_text(SHIPPED_INDEX, encoding="utf-8")
    (root / "dev" / "patterns_js_ui.md").write_text(SHIPPED_JS, encoding="utf-8")
    (root / "dev" / "patterns_csharp.md").write_text(SHIPPED_CS, encoding="utf-8")
    (root / "dev" / "snippets").mkdir()
    (root / "dev" / "snippets" / "esq.md").write_text("Пример ESQ\n", encoding="utf-8")
    return root


def make_env(tmp_path, *, mcp_version=MCP_VERSION):
    shipped = make_shipped(tmp_path)
    override = tmp_path / "appdata" / "patterns" / "references"
    ctx = FakeContext(shipped, override, mcp_version=mcp_version)
    state = CompanionState(tmp_path / "companion-state.json")
    return ctx, state, override


def files_snapshot(root) -> dict:
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def index_path(override) -> Path:
    return Path(override) / "dev" / pm.UPDATES_INDEX_NAME


def read_index(override) -> dict:
    return json.loads(index_path(override).read_text(encoding="utf-8"))


def sections_of(override, area="js_ui") -> list:
    for entry in read_index(override)["files"]:
        if entry["area"] == f"{area}_updates":
            return entry["sections"]
    return []


def headings_of(override, area="js_ui") -> list:
    return [s["heading"] for s in sections_of(override, area)]


# --------------------------------------------------------------------------------------
# 0. Юнит-правила
# --------------------------------------------------------------------------------------
def test_compare_versions_numeric_not_lexicographic():
    assert pm.compare_versions("0.10.0", "0.9.0") == 1
    assert pm.compare_versions("1.2", "1.2.0") == 0
    assert pm.compare_versions("0.305.0", "0.400.0") == -1
    assert pm.parse_version("1.x.3") == (1, 0, 3)


def test_sanitize_area_strips_path_and_case():
    assert pm.sanitize_area("JS_UI") == "js_ui"
    assert pm.sanitize_area("") == "other"
    assert pm.sanitize_area(None) == "other"
    cleaned = pm.sanitize_area("../../evil")
    assert "/" not in cleaned and "\\" not in cleaned and ".." not in cleaned


# --------------------------------------------------------------------------------------
# 1. Seed: поставочная база не исчезает
# --------------------------------------------------------------------------------------
def test_first_sync_seeds_shipped_tree_into_override(tmp_path):
    ctx, state, override = make_env(tmp_path)
    shipped = Path(ctx.shipped_patterns_root)
    result = pm.sync(FakeClient([page([pattern(1, title="Поле на карточке")])]),
                     state, ctx, CompanionSettings())

    for rel in ("dev/patterns_index.md", "dev/patterns_js_ui.md", "dev/snippets/esq.md"):
        assert (override / rel).read_bytes() == (shipped / rel).read_bytes(), (
            f"Поставочный файл {rel} обязан доехать в override-корень как есть")
    assert result["applied"] == 1 and result["seed"]["skipped"] is False


def test_second_sync_does_not_reseed_and_keeps_manual_edits(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Поле")])]), state, ctx, CompanionSettings())
    manual = override / "dev" / "patterns_js_ui.md"
    manual.write_text(SHIPPED_JS + "\n## Правка руками\n", encoding="utf-8")
    manual_bytes = manual.read_bytes()

    result = pm.sync(FakeClient([page([], since="2026-08-01T10:00:00Z", since_id=1)]),
                     state, ctx, CompanionSettings())

    assert result["seed"]["skipped"] is True
    assert manual.read_bytes() == manual_bytes


# --------------------------------------------------------------------------------------
# 2. Индекс метаданных без тел
# --------------------------------------------------------------------------------------
def test_sync_without_bodies_writes_metadata_index(tmp_path):
    ctx, state, override = make_env(tmp_path)
    items = [
        pattern(2, title="Листенер", area="csharp", snippet="Реакция на сохранение UsrOrder"),
        pattern(1, title="Кнопка в реестре", snippet="Кнопка на панели раздела"),
    ]
    result = pm.sync(FakeClient([page(items)]), state, ctx, CompanionSettings())

    assert result["applied"] == 2 and result["skipped"] == [], (
        "Отсутствие тела — норма онлайн-выдачи, а не повод пропустить запись")
    index = read_index(override)
    assert index["format"] == 1
    assert index["note"] == pm.INDEX_NOTE
    assert index["files"] == [
        {"file": "dev/patterns_csharp_updates.md", "area": "csharp_updates",
         "sections": [{"heading": "Листенер", "level": 3,
                       "snippet": "Реакция на сохранение UsrOrder", "key": "pat:2"}]},
        {"file": "dev/patterns_js_ui_updates.md", "area": "js_ui_updates",
         "sections": [{"heading": "Кнопка в реестре", "level": 3,
                       "snippet": "Кнопка на панели раздела", "key": "pat:1"}]},
    ]
    assert list((override / "dev").glob("patterns_*_updates.md")) == [], (
        "Файлов с телами на диске быть не должно — `file` в индексе виртуальный")
    assert index_path(override).read_bytes().decode("utf-8").endswith("\n")
    assert str(index_path(override)) in result["files_written"]


def test_section_key_falls_back_to_pattern_id(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(7, title="Без ключа", section_key=None)])]),
            state, ctx, CompanionSettings())
    assert sections_of(override)[0]["key"] == "pat:7"


def test_snippet_is_one_line_without_code_and_capped(tmp_path):
    ctx, state, override = make_env(tmp_path)
    long_text = "**Описание** раздела " + "слово " * 60 + "\n```js\nvar x = 1;\n```"
    pm.sync(FakeClient([page([pattern(1, title="Длинный", snippet=long_text)])]),
            state, ctx, CompanionSettings())
    snippet = sections_of(override)[0]["snippet"]
    assert len(snippet) <= 160
    assert "\n" not in snippet and "```" not in snippet and "var x" not in snippet
    assert "**" not in snippet and snippet.startswith("Описание раздела")


def test_old_backend_body_is_ignored_not_written(tmp_path):
    """Старый бэкенд ещё присылает `body_markdown`: тело игнорируется — ни в индекс, ни
    в состояние, ни в файлы; описание берётся только из метаданных."""
    ctx, state, override = make_env(tmp_path)
    item = pattern(1, title="Со старым телом", body=BODY_SECRET, section_key=None)
    result = pm.sync(FakeClient([page([item])]), state, ctx, CompanionSettings())

    assert result["applied"] == 1
    assert sections_of(override)[0] == {"heading": "Со старым телом", "level": 3,
                                        "snippet": "", "key": "pat:1"}
    for rel, data in files_snapshot(override).items():
        assert b"UsrSecretMarker" not in data, f"тело паттерна оказалось на диске: {rel}"
    state.save()
    assert "UsrSecretMarker" not in state.path.read_text(encoding="utf-8"), (
        "тело паттерна не должно оседать и в файле состояния")
    assert "body_markdown" not in state.patterns["applied"][0]


def test_index_order_is_stable_regardless_of_page_order(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(3, title="Третий"), pattern(1, title="Первый"),
                              pattern(2, title="Второй")])]),
            state, ctx, CompanionSettings())
    assert headings_of(override) == ["Первый", "Второй", "Третий"]


# --------------------------------------------------------------------------------------
# 3. Пагинация и курсор
# --------------------------------------------------------------------------------------
def test_pagination_walks_all_pages_in_single_sync(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([pattern(1, title="Первый")], has_more=True,
             next_since="2026-08-01T10:00:00Z", next_since_id=1),
        page([pattern(2, title="Второй")], has_more=True,
             since="2026-08-01T10:00:00Z", since_id=1,
             next_since="2026-08-02T10:00:00Z", next_since_id=2),
        page([pattern(3, title="Третий")], has_more=False,
             since="2026-08-02T10:00:00Z", since_id=2,
             next_since="2026-08-03T10:00:00Z", next_since_id=3),
    ])
    result = pm.sync(client, state, ctx, CompanionSettings())

    assert result["pages"] == 3 and result["applied"] == 3
    assert result["cursor"] == {"since": "2026-08-03T10:00:00Z", "since_id": 3}
    assert state.patterns["since_id"] == 3
    assert headings_of(override) == ["Первый", "Второй", "Третий"]


def test_empty_page_with_has_more_does_not_stop_pagination(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([], has_more=True, next_since="2026-08-01T10:00:00Z", next_since_id=7),
        page([pattern(9, title="После пустой страницы")], has_more=False,
             since="2026-08-01T10:00:00Z", since_id=7,
             next_since="2026-08-02T10:00:00Z", next_since_id=9),
    ])
    result = pm.sync(client, state, ctx, CompanionSettings())
    assert result["pages"] == 2 and result["applied"] == 1
    assert state.patterns["since_id"] == 9


def test_cursor_is_a_pair_and_goes_back_to_server(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([pattern(1, title="Первый")], has_more=True,
             next_since="2026-08-01T10:00:00Z", next_since_id=42),
        page([], has_more=False, since="2026-08-01T10:00:00Z", since_id=42,
             next_since="2026-08-01T10:00:00Z", next_since_id=42),
    ])
    pm.sync(client, state, ctx, CompanionSettings())
    first, second = client.calls[0]["params"], client.calls[1]["params"]
    assert "since" not in first and "since_id" not in first
    assert second["since"] == "2026-08-01T10:00:00Z" and second["since_id"] == 42
    assert second["mcp_version"] == MCP_VERSION


def test_empty_delta_is_ok_and_touches_nothing(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый")])]), state, ctx, CompanionSettings())
    before = files_snapshot(override)
    mtimes = {p: p.stat().st_mtime_ns for p in override.rglob("*") if p.is_file()}

    result = pm.sync(FakeClient([page([], since="2026-08-01T10:00:00Z", since_id=1)]),
                     state, ctx, CompanionSettings())

    assert state.patterns["last_status"] == "ok"
    assert result["files_written"] == [] and result["files_removed"] == []
    assert files_snapshot(override) == before
    assert {p: p.stat().st_mtime_ns for p in override.rglob("*") if p.is_file()} == mtimes


# --------------------------------------------------------------------------------------
# 4. Отзыв (tombstone)
# --------------------------------------------------------------------------------------
def test_tombstone_removes_section_and_finally_the_group(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([
        pattern(1, title="Первый"), pattern(2, title="Второй"),
        pattern(3, title="Серверный", area="csharp"),
    ], next_since_id=3)]), state, ctx, CompanionSettings())

    result = pm.sync(FakeClient([page([tombstone(1)], since="2026-08-01T10:00:00Z",
                                      since_id=3, next_since_id=4)]),
                     state, ctx, CompanionSettings())
    assert result["removed"] == 1
    assert headings_of(override) == ["Второй"]
    assert state.patterns["had_new_last_run"] is True

    pm.sync(FakeClient([page([tombstone(2)], since="2026-08-01T10:00:00Z", since_id=4,
                             next_since_id=5)]),
            state, ctx, CompanionSettings())
    areas = [entry["area"] for entry in read_index(override)["files"]]
    assert areas == ["csharp_updates"], "опустевшая группа уходит из индекса целиком"


def test_tombstone_applies_even_when_version_filter_would_reject(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(5, title="Старый")], next_since_id=5)]),
            state, ctx, CompanionSettings())
    result = pm.sync(FakeClient([page([tombstone(5, min_mcp_version="99.0.0")],
                                      since="2026-08-01T10:00:00Z", since_id=5,
                                      next_since_id=6)]),
                     state, ctx, CompanionSettings())
    assert result["removed"] == 1 and result["skipped"] == []
    assert read_index(override)["files"] == []


# --------------------------------------------------------------------------------------
# 5. Миграция прежней раскладки
# --------------------------------------------------------------------------------------
LEGACY_BLOCK = (f"{pm.MANAGED_BEGIN}\n\n<!-- служебно -->\n\n"
                "## Канал обновлений: js_ui (`dev/patterns_js_ui_updates.md`)\n\n"
                "| Паттерн | Когда использовать |\n|---|---|\n| **Старый** | Тело |\n\n"
                f"{pm.MANAGED_END}")


def make_legacy_root(override) -> None:
    dev = Path(override) / "dev"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "patterns_index.md").write_text(SHIPPED_INDEX + "\n" + LEGACY_BLOCK + "\n",
                                           encoding="utf-8")
    (dev / "patterns_js_ui.md").write_text(SHIPPED_JS, encoding="utf-8")
    (dev / "patterns_js_ui_updates.md").write_text("## Старый\n\nUsrSecretMarker\n",
                                                   encoding="utf-8")
    (dev / "patterns_csharp_updates.md").write_text("## Ещё\n", encoding="utf-8")


def test_migration_removes_legacy_bodies_and_managed_block(tmp_path):
    ctx, state, override = make_env(tmp_path)
    make_legacy_root(override)
    state.patterns["applied"] = [{"id": 1, "title": "Старый", "area": "js_ui",
                                  "body_markdown": "UsrSecretMarker", "version": "1"}]
    state.patterns["seeded"] = True

    result = pm.sync(FakeClient([page([])]), state, ctx, CompanionSettings())

    dev = override / "dev"
    assert not (dev / "patterns_js_ui_updates.md").exists()
    assert not (dev / "patterns_csharp_updates.md").exists()
    assert (dev / "patterns_js_ui.md").read_text(encoding="utf-8") == SHIPPED_JS, (
        "поставочный файл под маску миграции не попадает")
    assert (dev / "patterns_index.md").read_text(encoding="utf-8") == SHIPPED_INDEX, (
        "из индекса вычищается только управляемый блок, рукописный текст — побайтно")
    assert result["legacy_index_cleaned"] is True
    assert {Path(p).name for p in result["files_removed"]} == {
        "patterns_js_ui_updates.md", "patterns_csharp_updates.md"}
    # Записи прежнего состояния переживают миграцию — уже без тел.
    assert headings_of(override) == ["Старый"]
    assert "body_markdown" not in state.patterns["applied"][0]
    assert sections_of(override)[0]["key"] == "pat:1"


def test_migration_runs_before_network_and_is_idempotent(tmp_path):
    ctx, state, override = make_env(tmp_path)
    make_legacy_root(override)

    class Offline(FakeClient):
        def get_json(self, path, **kwargs):
            raise ChannelError("Бэкенд издателя недоступен", kind="offline")

    with pytest.raises(ChannelError):
        pm.sync(Offline([]), state, ctx, CompanionSettings())
    assert not (override / "dev" / "patterns_js_ui_updates.md").exists(), (
        "переход на онлайн-выдачу не ждёт первого удачного ответа сервера")
    assert pm.MANAGED_BEGIN not in (override / "dev" / "patterns_index.md").read_text(
        encoding="utf-8")

    before = files_snapshot(override)
    again = pm.migrate_legacy(override)
    assert again == {"files_removed": [], "index_cleaned": False}
    assert files_snapshot(override) == before


def test_migration_keeps_crlf_index_text_byte_for_byte(tmp_path):
    override = tmp_path / "root"
    (override / "dev").mkdir(parents=True)
    shipped = SHIPPED_INDEX.replace("\n", "\r\n").encode("utf-8")
    block = ("\r\n" + LEGACY_BLOCK.replace("\n", "\r\n") + "\r\n").encode("utf-8")
    (override / "dev" / "patterns_index.md").write_bytes(shipped + block)
    pm.migrate_legacy(override)
    assert (override / "dev" / "patterns_index.md").read_bytes() == shipped


def test_migration_of_index_made_only_of_block_keeps_root_valid(tmp_path):
    override = tmp_path / "root"
    (override / "dev").mkdir(parents=True)
    (override / "dev" / "patterns_index.md").write_text(LEGACY_BLOCK + "\n", encoding="utf-8")
    pm.migrate_legacy(override)
    text = (override / "dev" / "patterns_index.md").read_text(encoding="utf-8")
    assert text.strip() and pm.MANAGED_BEGIN not in text, (
        "индекс не должен остаться пустым — без него читатель отвергнет корень")


# --------------------------------------------------------------------------------------
# 6. Целостность и фильтры
# --------------------------------------------------------------------------------------
def test_bundle_mismatch_drops_page_and_keeps_cursor(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый")])]), state, ctx, CompanionSettings())
    before = files_snapshot(override)
    broken = page([pattern(2, title="Второй")], since="2026-08-01T10:00:00Z", since_id=1,
                  next_since="2026-08-05T10:00:00Z", next_since_id=5, bundle="0" * 64)
    with pytest.raises(ChannelError) as info:
        pm.sync(FakeClient([broken]), state, ctx, CompanionSettings())
    assert info.value.kind == "integrity_mismatch"
    assert "подпис" not in str(info.value).lower()
    assert files_snapshot(override) == before
    assert (state.patterns["since"], state.patterns["since_id"]) == ("2026-08-01T10:00:00Z", 1)


def test_min_mcp_version_newer_than_client_is_skipped(tmp_path):
    ctx, state, override = make_env(tmp_path)
    result = pm.sync(FakeClient([page([
        pattern(1, title="Подходит", min_mcp="0.300.0"),
        pattern(2, title="Слишком новый", min_mcp="0.400.0"),
    ])]), state, ctx, CompanionSettings())
    assert result["applied"] == 1
    assert result["skipped"][0]["reason"] == "min_mcp_version"
    assert headings_of(override) == ["Подходит"]


def test_strict_signature_setting_does_not_block_metadata(tmp_path):
    """Подпись относится к телу, которого у компаньона больше нет: строгий режим не
    должен выключать индекс (тело и его подпись проверяет сторона онлайн-выдачи)."""
    ctx, state, override = make_env(tmp_path)
    result = pm.sync(FakeClient([page([pattern(1, title="Без подписи")])]), state, ctx,
                     CompanionSettings(require_pattern_signature=True))
    assert result["applied"] == 1


def test_hostile_area_never_escapes_dev_directory(tmp_path):
    ctx, state, override = make_env(tmp_path)
    result = pm.sync(FakeClient([page([pattern(1, title="Злой", area="../../evil")])]),
                     state, ctx, CompanionSettings())
    assert result["applied"] == 1
    for path in map(Path, result["files_written"]):
        assert path.parent == override / "dev"
    entry = read_index(override)["files"][0]
    assert "/" not in entry["area"] and ".." not in entry["file"]


# --------------------------------------------------------------------------------------
# 7. Откат
# --------------------------------------------------------------------------------------
def test_restore_returns_exactly_previous_files(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый")])]), state, ctx, CompanionSettings())
    snap = pm.snapshot(state)
    before = files_snapshot(override)
    pm.sync(FakeClient([page([pattern(2, title="Новый", area="csharp")],
                             since="2026-08-01T10:00:00Z", since_id=1, next_since_id=2)]),
            state, ctx, CompanionSettings())
    assert files_snapshot(override) != before

    pm.restore(state, snap, override)

    assert files_snapshot(override) == before
    assert [r["id"] for r in state.patterns["applied"]] == [1]
    assert state.patterns["since_id"] == 2


def test_snapshot_is_deep_copy(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый")])]), state, ctx, CompanionSettings())
    snap = pm.snapshot(state)
    state.patterns["applied"][0]["title"] = "Испорчено на месте"
    assert snap[0]["title"] == "Первый"


# --------------------------------------------------------------------------------------
# 8. Счётчик библиотеки на сервере → строка окна «Обновления»
# --------------------------------------------------------------------------------------
def test_stats_to_window_line():
    client = FakeClient([], stats={"sections": 120, "updates": 3,
                                   "updated_at": "2026-10-05T08:30:00Z"})
    stats = pm.fetch_stats(client)
    assert client.calls[0]["path"] == "/v1/content/patterns/stats"
    assert client.calls[0]["authorized"] is True
    assert client.calls[0]["timeout"] == pm.STATS_TIMEOUT_SEC, "таймаут короткий"
    assert stats["status"] == "ok" and stats["total"] == 123
    assert pm.stats_line(stats) == (
        "Паттерны: 123 паттерна на сервере, библиотека обновлена 05.10.2026, "
        "доступ по лицензии")


@pytest.mark.parametrize("total,word", [(1, "паттерн"), (2, "паттерна"), (5, "паттернов"),
                                        (11, "паттернов"), (14, "паттернов"), (21, "паттерн"),
                                        (22, "паттерна"), (112, "паттернов")])
def test_stats_line_plural(total, word):
    line = pm.stats_line({"status": "ok", "sections": total, "updates": 0,
                          "updated_at": None})
    assert line == f"Паттерны: {total} {word} на сервере, доступ по лицензии"


def test_stats_network_unavailable_is_no_connection_line():
    client = FakeClient([], stats=ChannelError("Бэкенд издателя недоступен", kind="offline"))
    with pytest.raises(ChannelError) as info:
        pm.fetch_stats(client)
    stats = pm.stats_from_error(info.value)
    assert stats["status"] == "offline"
    assert pm.stats_line(stats) == "Паттерны: нет связи с сервером"
    # Непонятный ответ сервера для человека — тоже «нет связи».
    bad = FakeClient([], stats={"sections": "много"})
    with pytest.raises(ChannelError) as info2:
        pm.fetch_stats(bad)
    assert pm.stats_line(pm.stats_from_error(info2.value)) == "Паттерны: нет связи с сервером"
    assert pm.stats_line(pm.stats_from_error(OSError("timed out"))) == (
        "Паттерны: нет связи с сервером")


def test_stats_without_license():
    client = FakeClient([], stats={"sections": 1, "updates": 0}, has_envelope=False)
    with pytest.raises(ChannelError) as info:
        pm.fetch_stats(client)
    assert client.calls == [], "без конверта лицензии в сеть не ходим"
    assert pm.stats_line(pm.stats_from_error(info.value)) == (
        "Паттерны: доступ по лицензии: лицензия не активна")
    expired = pm.stats_from_error(ChannelError("истекла", kind="expired"))
    assert expired["status"] == "no_license"


def test_stats_line_before_first_check():
    assert pm.stats_line(None) == "Паттерны: проверяем сервер…"
    assert pm.stats_line({}) == "Паттерны: проверяем сервер…"


# --------------------------------------------------------------------------------------
# 9. Раннер: счётчик в фоне, без ожидания сети
# --------------------------------------------------------------------------------------
def _runner(tmp_path, client):
    from standkit_companion.runner import CompanionRunner

    settings = CompanionSettings(enabled=True)
    return CompanionRunner(
        tmp_path / "standkit-hub.json",
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: settings,
        client_factory=lambda ctx, s: client,
        context_resolver=lambda s: FakeContext(tmp_path / "shipped", tmp_path / "override"),
    )


def test_runner_patterns_stats_refreshes_in_background(tmp_path):
    client = FakeClient([], stats={"sections": 10, "updates": 1,
                                   "updated_at": "2026-10-01T00:00:00Z"})
    runner = _runner(tmp_path, client)

    first = runner.patterns_stats(wait=5.0)
    assert first["refreshing"] is False
    assert first["line"] == ("Паттерны: 11 паттернов на сервере, библиотека обновлена "
                             "01.10.2026, доступ по лицензии")
    assert json.loads((tmp_path / "companion-state.json").read_text(encoding="utf-8"))[
        "patterns"]["server"]["total"] == 11, "результат сохранён в состояние"

    # Свежий результат не перезапрашивается на каждое открытие окна.
    runner.patterns_stats(wait=1.0)
    assert len([c for c in client.calls if c["path"] == pm.STATS_PATH]) == 1


def test_runner_patterns_stats_does_not_block_on_slow_network(tmp_path):
    class Slow(FakeClient):
        def get_json(self, path, **kwargs):
            time.sleep(0.5)
            raise ChannelError("Бэкенд издателя недоступен", kind="offline")

    runner = _runner(tmp_path, Slow([]))
    started = time.monotonic()
    snap = runner.patterns_stats()
    assert time.monotonic() - started < 0.4, "ответ окну — без ожидания сети"
    assert snap["refreshing"] is True
    assert snap["line"] == "Паттерны: проверяем сервер…"
    done = runner.patterns_stats(refresh=False, wait=0.0)
    runner._stats_thread.join(5.0)
    done = runner.patterns_stats(refresh=False)
    assert done["status"] == "offline" and done["line"] == "Паттерны: нет связи с сервером"


def test_runner_check_update_refreshes_stats_instead_of_pending(tmp_path, monkeypatch):
    from standkit_companion import releases

    client = FakeClient([], stats={"sections": 4, "updates": 0, "updated_at": None})
    runner = _runner(tmp_path, client)
    monkeypatch.setattr(releases, "check", lambda *a, **k: {"available": False})
    monkeypatch.setattr(runner, "_sync_cookbook", lambda session: None)
    result = runner.run_action("check_update")
    # С 0.12.21 счётчик и сверка индекса идут в фоне — кнопка не ждёт сеть.
    assert "refreshing" in result["patterns"]
    runner._stats_thread.join(15.0)
    assert runner.patterns_stats(refresh=False)["status"] == "ok"
    assert runner.status()["state"]["patterns"]["server"]["line"] == (
        "Паттерны: 4 паттерна на сервере, доступ по лицензии")
