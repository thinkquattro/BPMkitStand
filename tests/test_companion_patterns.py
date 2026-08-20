# -*- coding: utf-8 -*-
"""Тесты standkit_companion.patterns — канала доставки паттернов на диск клиента.

Зачем файл. Здесь проверяется не «код не падает», а набор правил, каждое из которых
ломается ТИХО: база паттернов исчезает наполовину, курсор не двигается, отозванный
паттерн остаётся лежать, файл уезжает за пределы каталога. Ни одно из этих последствий
не видно ни в логе, ни в UI — только по факту «паттерн не нашёлся». Поэтому каждое
правило закрыто отдельным регресс-тестом:

* seed поставочного дерева в override-корень (override заменяет поставку ЦЕЛИКОМ,
  merge читатель не делает — без seed половина базы пропадает);
* курсор — ПАРА `(since, since_id)`, уезжает в запрос вместе или никак;
* пагинация до `has_more == false` в ОДНОМ проходе, `count == 0` проход не прерывает;
* пустая дельта — штатный `ok`, а не ошибка;
* tombstone применяется безусловно, включая паттерны «не по версии»;
* `bundle_sha256` рвёт страницу целиком и НЕ двигает курсор, `content_sha256` — одну запись;
* имя области из сети не становится путём (`../../evil`);
* поставочный текст индекса вне маркеров не трогается никогда;
* сгенерированный markdown читается регексами клиентского MCP (копия его правил — ниже).

Сеть не поднимается: клиенту нужен единственный метод `get_json`, и подставной клиент
здесь заодно журналирует параметры запросов — два теста проверяют именно их.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from standkit_companion import patterns as pm
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionSettings

MCP_VERSION = "0.305.0"

# --------------------------------------------------------------------------------------
# Копия правил РЕЗОЛВА клиентского MCP (BPMkit/server/bpmkit/patterns.py), урезанная до
# того, что нужно тесту совместимости. Копия сознательная: если читатель поменяет свои
# регексы, этот тест обязан упасть здесь, а не у пользователя в виде «паттерн не найден».
# --------------------------------------------------------------------------------------
READER_HEADING_RE = re.compile(r"^#{2,4}\s+(.*\S)\s*$")
READER_INDEX_SECTION_RE = re.compile(r"^##\s+(?!#)(.*)$")
READER_INDEX_ROW_RE = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*$")
READER_FILE_HINT_RE = re.compile(r"`([^`]+\.md)`")


def reader_sections(text: str) -> list:
    """Разделы файла библиотеки: (заголовок, описание) по правилам читателя."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = READER_HEADING_RE.match(line)
        if not m:
            continue
        description = ""
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            description = "" if nxt.lstrip().startswith("#") else nxt.strip().replace("**", "")
            break
        out.append((m.group(1), description))
    return out


def reader_index(text: str) -> list:
    """Секции индекса: (заголовок, файл-подсказка, [(паттерн, описание)])."""
    sections = []
    current = None
    for line in text.splitlines():
        m = READER_INDEX_SECTION_RE.match(line)
        if m:
            title = m.group(1)
            hint = READER_FILE_HINT_RE.search(title)
            current = {"title": title, "hint": hint.group(1) if hint else "", "rows": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if not current["hint"]:
            hint = READER_FILE_HINT_RE.search(line)
            if hint:
                current["hint"] = hint.group(1)
        row = READER_INDEX_ROW_RE.match(line)
        if row:
            current["rows"].append((row.group(1), row.group(2)))
    return [(s["title"], s["hint"], s["rows"]) for s in sections]


def reader_area_matches(area: str, hint: str) -> bool:
    """Фильтр `area` читателя: подстрока подсказки либо токен её basename."""
    needle = area.lower()
    if needle in hint.lower():
        return True
    tokens = set(re.split(r"[^a-z0-9]+", Path(hint).name.lower()))
    return needle in tokens


# --------------------------------------------------------------------------------------
# Подставные соседи
# --------------------------------------------------------------------------------------
class FakeClient:
    """Клиент бэкенда: отдаёт заранее записанные страницы и журналирует запросы.

    Журнал — не украшение: тесты 7 и 10 проверяют ИМЕННО параметры запроса (пара курсора
    и то, что после отброшенной страницы запрашивается тот же самый `since`).
    """

    def __init__(self, pages) -> None:
        self.pages = list(pages)
        self.calls: list = []

    def get_json(self, path, *, params=None, authorized=True, etag=None):
        self.calls.append({"path": path, "params": dict(params or {}),
                           "authorized": authorized, "etag": etag})
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
    """Контрольная сумма страницы, посчитанная ПО ФОРМУЛЕ КОНТРАКТА, а не вызовом
    проверяемого модуля: иначе тест подтверждал бы сам себя."""
    blob = json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pattern(pid, *, title, body, area="js_ui", min_mcp="0.0.0", signature=None,
            content_sha=None, status="published"):
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
        "content_sha256": (content_sha if content_sha is not None
                           else hashlib.sha256(body.encode("utf-8")).hexdigest()),
        "signature": signature,
        "sig_key_id": None,
        "deleted": False,
    }


def tombstone(pid, **extra):
    item = {"id": pid, "status": "revoked", "deleted": True,
            "updated_at": "2026-08-02T10:00:00Z"}
    item.update(extra)
    return item


def page(items, *, has_more=False, next_since="2026-08-01T10:00:00Z", next_since_id=1,
         since=None, since_id=None, bundle=None):
    """Конверт ответа — ровно 9 ключей верхнего уровня, как у сервера."""
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

## Серверный код (`dev/patterns_csharp.md`)

| Паттерн | Когда использовать |
|---------|--------------------|
| **Листенер сущности** | Нужна реакция на сохранение записи |
"""

SHIPPED_JS = """## Кнопка в реестре

Нужна кнопка на панели раздела.

### Решение
Код схемы.
"""

SHIPPED_CS = """## Листенер сущности

Нужна реакция на сохранение записи.
"""

BODY_TASK = """## Задача
Добавить поле на карточку.

### Решение
Правка схемы.
"""

BODY_FENCED = """## Задача
Показать пример кода.

```python
## Задача
print("# это не заголовок")
```

### Решение
Готово.
"""


def make_shipped(tmp_path) -> Path:
    """Поставочный корень: индекс, два файла библиотеки и файл во вложенном каталоге
    (последний — чтобы seed проверялся на ДЕРЕВЕ, а не на плоском списке)."""
    root = tmp_path / "package" / "skills" / "bpmsoft-dev" / "references"
    (root / "dev").mkdir(parents=True)
    (root / "dev" / "patterns_index.md").write_text(SHIPPED_INDEX, encoding="utf-8")
    (root / "dev" / "patterns_js_ui.md").write_text(SHIPPED_JS, encoding="utf-8")
    (root / "dev" / "patterns_csharp.md").write_text(SHIPPED_CS, encoding="utf-8")
    (root / "dev" / "snippets").mkdir()
    (root / "dev" / "snippets" / "esq.md").write_text("Пример ESQ\n", encoding="utf-8")
    return root


def make_env(tmp_path, *, mcp_version=MCP_VERSION):
    """Готовая тройка (контекст, состояние, override-корень) для одного теста."""
    shipped = make_shipped(tmp_path)
    override = tmp_path / "appdata" / "patterns" / "references"
    ctx = FakeContext(shipped, override, mcp_version=mcp_version)
    state = CompanionState(tmp_path / "companion-state.json")
    return ctx, state, override


def files_snapshot(root) -> dict:
    """Побайтный слепок дерева — для сравнений «ничего не изменилось»."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def index_text(override) -> str:
    return (Path(override) / "dev" / "patterns_index.md").read_text(encoding="utf-8")


def managed_parts(text):
    """(до маркера, блок, после маркера) — чтобы сравнивать поставочный текст побайтно."""
    begin = text.index(pm.MANAGED_BEGIN)
    end = text.index(pm.MANAGED_END) + len(pm.MANAGED_END)
    return text[:begin], text[begin:end], text[end:]


# --------------------------------------------------------------------------------------
# 0. Юнит-правила: версии и имена областей
# --------------------------------------------------------------------------------------
def test_compare_versions_numeric_not_lexicographic():
    assert pm.compare_versions("0.10.0", "0.9.0") == 1, (
        "Сравнение версий обязано быть поэлементным по числам: как строки «0.10.0» "
        "меньше «0.9.0», и клиент отфильтровал бы паттерны у всех, кто перешагнул "
        "десятку в минорной версии")
    assert pm.compare_versions("1.2", "1.2.0") == 0, (
        "Короткий кортеж дополняется нулями справа: 1.2 и 1.2.0 — одна и та же версия")
    assert pm.compare_versions("0.305.0", "0.400.0") == -1
    assert pm.parse_version("1.x.3") == (1, 0, 3), (
        "Нечисловой сегмент трактуется как 0 — те же правила, что у бэкенда")


def test_sanitize_area_strips_path_and_case():
    assert pm.sanitize_area("JS_UI") == "js_ui"
    assert pm.sanitize_area("") == "other", (
        "Пустая область не должна терять паттерн — он едет в файл `other`")
    assert pm.sanitize_area(None) == "other"
    cleaned = pm.sanitize_area("../../evil")
    assert "/" not in cleaned and "\\" not in cleaned and ".." not in cleaned, (
        "Имя области приходит ИЗ СЕТИ и становится именем файла: разделители пути и "
        "точки обязаны быть вычищены, иначе это готовый path traversal")


# --------------------------------------------------------------------------------------
# 1. Seed: поставочная база не исчезает
# --------------------------------------------------------------------------------------
def test_first_sync_seeds_shipped_tree_into_override(tmp_path):
    ctx, state, override = make_env(tmp_path)
    shipped = Path(ctx.shipped_patterns_root)
    client = FakeClient([page([pattern(1, title="Поле на карточке", body=BODY_TASK)])])

    result = pm.sync(client, state, ctx, CompanionSettings())

    for rel in ("dev/patterns_js_ui.md", "dev/patterns_csharp.md", "dev/snippets/esq.md"):
        src = shipped / rel
        dst = override / rel
        assert dst.is_file(), (
            f"Поставочный файл {rel} не доехал в override-корень. Читатель выбирает ОДИН "
            "корень и не сливает его с поставочным — без копирования дерева половина "
            "библиотеки паттернов исчезает у пользователя молча")
        assert dst.read_bytes() == src.read_bytes(), (
            f"Поставочный файл {rel} доехал изменённым — seed обязан копировать как есть")

    text = index_text(override)
    assert "Рукописный поставочный индекс" in text, (
        "Поставочный текст индекса обязан сохраниться: канал только ДОПИСЫВАЕТ свой блок")
    assert "## Клиентские схемы (`dev/patterns_js_ui.md`)" in text, (
        "Поставочные секции индекса обязаны остаться на месте")
    assert pm.MANAGED_BEGIN in text and pm.MANAGED_END in text, (
        "Управляемый блок канала обязан появиться в индексе override-корня")
    assert result["applied"] == 1 and result["seed"]["skipped"] is False


def test_second_sync_does_not_reseed_and_keeps_manual_edits(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([page([pattern(1, title="Поле на карточке", body=BODY_TASK)])])
    pm.sync(client, state, ctx, CompanionSettings())

    manual = override / "dev" / "patterns_js_ui.md"
    manual.write_text(SHIPPED_JS + "\n## Правка руками\n\nЖивёт своей жизнью.\n",
                      encoding="utf-8")
    manual_bytes = manual.read_bytes()

    client2 = FakeClient([page([], next_since="2026-08-01T10:00:00Z", next_since_id=1,
                               since="2026-08-01T10:00:00Z", since_id=1)])
    result = pm.sync(client2, state, ctx, CompanionSettings())

    assert result["seed"]["skipped"] is True, (
        "Корень уже валиден (есть dev/patterns_index.md) — повторный seed запрещён")
    assert manual.read_bytes() == manual_bytes, (
        "Повторный seed затёр бы правки пользователя в поставочном файле — именно поэтому "
        "он выполняется ровно один раз")


# --------------------------------------------------------------------------------------
# 2. Управляемый блок индекса
# --------------------------------------------------------------------------------------
def test_managed_block_rewritten_wholesale_outside_text_untouched(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый", body=BODY_TASK)])]),
            state, ctx, CompanionSettings())

    # Человек дописал свою секцию в поставочную часть индекса — она обязана пережить всё.
    text = index_text(override)
    head, block, tail = managed_parts(text)
    head_edited = head + "## Моя секция (`dev/patterns_my.md`)\n\nРучная запись.\n\n"
    (override / "dev" / "patterns_index.md").write_text(head_edited + block + tail,
                                                        encoding="utf-8")

    pm.sync(FakeClient([page([pattern(2, title="Второй", body=BODY_TASK)],
                             since="2026-08-01T10:00:00Z", since_id=1,
                             next_since="2026-08-03T10:00:00Z", next_since_id=2)]),
            state, ctx, CompanionSettings())

    new_head, new_block, new_tail = managed_parts(index_text(override))
    assert new_head == head_edited and new_tail == tail, (
        "Текст ВНЕ маркеров — поставочный и рукописный — не трогается никогда: он "
        "переносится посимвольно срезами исходной строки")
    assert "Первый" in new_block and "Второй" in new_block, (
        "Управляемый блок перерисовывается целиком из состояния, а не дописывается")
    assert new_block != block


# --------------------------------------------------------------------------------------
# 3. Пагинация и курсор
# --------------------------------------------------------------------------------------
def test_pagination_walks_all_pages_in_single_sync(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([pattern(1, title="Первый", body=BODY_TASK)], has_more=True,
             next_since="2026-08-01T10:00:00Z", next_since_id=1),
        page([pattern(2, title="Второй", body=BODY_TASK)], has_more=True,
             since="2026-08-01T10:00:00Z", since_id=1,
             next_since="2026-08-02T10:00:00Z", next_since_id=2),
        page([pattern(3, title="Третий", body=BODY_TASK)], has_more=False,
             since="2026-08-02T10:00:00Z", since_id=2,
             next_since="2026-08-03T10:00:00Z", next_since_id=3),
    ])

    result = pm.sync(client, state, ctx, CompanionSettings())

    assert result["pages"] == 3 and result["applied"] == 3, (
        "Пагинация обязана дойти до has_more=false В ОДНОМ проходе: страницы, растянутые "
        "на разные тики, склеиваются из несогласованных срезов базы")
    assert result["cursor"] == {"since": "2026-08-03T10:00:00Z", "since_id": 3}
    assert state.patterns["since"] == "2026-08-03T10:00:00Z"
    assert state.patterns["since_id"] == 3
    text = (override / "dev" / "patterns_js_ui_updates.md").read_text(encoding="utf-8")
    for title in ("Первый", "Второй", "Третий"):
        assert f"## {title}" in text


def test_empty_page_with_has_more_does_not_stop_pagination(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([], has_more=True, next_since="2026-08-01T10:00:00Z", next_since_id=7),
        page([pattern(9, title="После пустой страницы", body=BODY_TASK)], has_more=False,
             since="2026-08-01T10:00:00Z", since_id=7,
             next_since="2026-08-02T10:00:00Z", next_since_id=9),
    ])

    result = pm.sync(client, state, ctx, CompanionSettings())

    assert result["pages"] == 2 and result["applied"] == 1, (
        "count==0 при has_more=true — это страница, целиком отфильтрованная сервером по "
        "mcp_version, а не конец данных; остановка на ней навсегда обрезала бы дельту")
    assert state.patterns["since_id"] == 9


def test_cursor_is_a_pair_and_goes_back_to_server(tmp_path):
    ctx, state, override = make_env(tmp_path)
    client = FakeClient([
        page([pattern(1, title="Первый", body=BODY_TASK)], has_more=True,
             next_since="2026-08-01T10:00:00Z", next_since_id=42),
        page([], has_more=False, since="2026-08-01T10:00:00Z", since_id=42,
             next_since="2026-08-01T10:00:00Z", next_since_id=42),
    ])

    pm.sync(client, state, ctx, CompanionSettings())

    first, second = client.calls[0]["params"], client.calls[1]["params"]
    assert "since" not in first and "since_id" not in first, (
        "Первый запрос идёт без курсора вовсе — половина пары сервером игнорируется")
    assert second["since"] == "2026-08-01T10:00:00Z" and second["since_id"] == 42, (
        "Курсор — ПАРА: `since_id` без `since` сервер игнорирует целиком, и клиент молча "
        "качал бы базу с начала; строки с одинаковой меткой на границе страницы теряются, "
        "если слать только время")
    assert second["mcp_version"] == MCP_VERSION, (
        "Версия MCP уходит в каждый запрос — на ней строится серверный фильтр")


def test_empty_delta_is_ok_and_touches_nothing(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый", body=BODY_TASK)],
                             next_since="2026-08-01T10:00:00Z", next_since_id=1)]),
            state, ctx, CompanionSettings())
    before = files_snapshot(override)
    mtimes = {p: p.stat().st_mtime_ns for p in override.rglob("*") if p.is_file()}

    result = pm.sync(FakeClient([page([], since="2026-08-01T10:00:00Z", since_id=1,
                                      next_since="2026-08-01T10:00:00Z",
                                      next_since_id=1)]),
                     state, ctx, CompanionSettings())

    assert state.patterns["last_status"] == "ok", (
        "Пустая дельта — это «у вас всё актуально», штатный успех; статус error поднял бы "
        "в UI несуществующую проблему")
    assert result["cursor"] == {"since": "2026-08-01T10:00:00Z", "since_id": 1}
    assert result["files_written"] == [] and result["files_removed"] == [], (
        "Без изменений файлы не переписываются — перезапись меняет mtime, по которому "
        "судят о свежести базы")
    assert files_snapshot(override) == before
    assert {p: p.stat().st_mtime_ns for p in override.rglob("*") if p.is_file()} == mtimes


# --------------------------------------------------------------------------------------
# 4. Отзыв (tombstone)
# --------------------------------------------------------------------------------------
def test_tombstone_removes_section_row_and_finally_the_file(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([
        pattern(1, title="Первый", body=BODY_TASK),
        pattern(2, title="Второй", body=BODY_TASK),
        pattern(3, title="Серверный", body=BODY_TASK, area="csharp"),
    ], next_since="2026-08-01T10:00:00Z", next_since_id=3)]),
        state, ctx, CompanionSettings())
    area_file = override / "dev" / "patterns_js_ui_updates.md"
    assert "## Первый" in area_file.read_text(encoding="utf-8")

    result = pm.sync(FakeClient([page([tombstone(1)], since="2026-08-01T10:00:00Z",
                                      since_id=3, next_since="2026-08-02T10:00:00Z",
                                      next_since_id=4)]),
                     state, ctx, CompanionSettings())

    assert result["removed"] == 1
    text = area_file.read_text(encoding="utf-8")
    assert "## Первый" not in text and "## Второй" in text, (
        "Отзыв — это удаление записи из состояния и ПОЛНАЯ перерисовка файла; хирургия по "
        "markdown ломалась бы на любом нестандартном оформлении тела")
    assert "**Первый**" not in index_text(override), (
        "Строка отозванного паттерна обязана исчезнуть и из индекса")

    result2 = pm.sync(FakeClient([page([tombstone(2)], since="2026-08-02T10:00:00Z",
                                       since_id=4, next_since="2026-08-03T10:00:00Z",
                                       next_since_id=5)]),
                      state, ctx, CompanionSettings())

    assert not area_file.exists(), (
        "Область опустела — файл удаляется целиком, иначе пустой файл остался бы висеть "
        "в выдаче читателя")
    assert str(area_file) in result2["files_removed"]
    assert (override / "dev" / "patterns_csharp_updates.md").is_file(), (
        "Соседняя область не должна пострадать")
    assert "Канал обновлений: js_ui" not in index_text(override)


def test_tombstone_applies_even_when_version_filter_would_reject(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(5, title="Старый", body=BODY_TASK)],
                             next_since="2026-08-01T10:00:00Z", next_since_id=5)]),
            state, ctx, CompanionSettings())

    result = pm.sync(FakeClient([page([tombstone(5, min_mcp_version="99.0.0",
                                                 area="js_ui")],
                                      since="2026-08-01T10:00:00Z", since_id=5,
                                      next_since="2026-08-02T10:00:00Z",
                                      next_since_id=6)]),
                     state, ctx, CompanionSettings())

    assert result["removed"] == 1 and result["skipped"] == [], (
        "Tombstone приезжает независимо от mcp_version и применяется БЕЗУСЛОВНО: паттерн "
        "мог быть применён раньше, на другой версии MCP, и отзыв обязан его достать")
    assert not (override / "dev" / "patterns_js_ui_updates.md").exists()


# --------------------------------------------------------------------------------------
# 5. Целостность
# --------------------------------------------------------------------------------------
def test_bundle_mismatch_drops_page_and_keeps_cursor(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый", body=BODY_TASK)],
                             next_since="2026-08-01T10:00:00Z", next_since_id=1)]),
            state, ctx, CompanionSettings())
    before = files_snapshot(override)

    broken = page([pattern(2, title="Второй", body=BODY_TASK)],
                  since="2026-08-01T10:00:00Z", since_id=1,
                  next_since="2026-08-05T10:00:00Z", next_since_id=5,
                  bundle="0" * 64)
    with pytest.raises(ChannelError) as info:
        pm.sync(FakeClient([broken]), state, ctx, CompanionSettings())

    assert info.value.kind == "integrity_mismatch"
    assert "подпис" not in str(info.value).lower(), (
        "bundle_sha256 — ЦЕЛОСТНОСТЬ, а не подлинность: злоумышленник пересчитает сумму "
        "вместе с телом. Называть её подписью значит обещать защиту, которой нет")
    assert files_snapshot(override) == before, (
        "Страница отбрасывается ЦЕЛИКОМ — ни один файл не должен измениться")
    assert state.patterns["since"] == "2026-08-01T10:00:00Z"
    assert state.patterns["since_id"] == 1, (
        "Курсор не двигается, иначе битая страница была бы потеряна навсегда")

    retry = FakeClient([page([], since="2026-08-01T10:00:00Z", since_id=1,
                             next_since="2026-08-01T10:00:00Z", next_since_id=1)])
    pm.sync(retry, state, ctx, CompanionSettings())
    assert retry.calls[0]["params"]["since"] == "2026-08-01T10:00:00Z"
    assert retry.calls[0]["params"]["since_id"] == 1, (
        "Следующий тик обязан перезапросить ту же самую страницу")


def test_content_mismatch_skips_single_pattern_only(tmp_path):
    ctx, state, override = make_env(tmp_path)
    items = [
        pattern(1, title="Целый", body=BODY_TASK),
        pattern(2, title="Испорченный", body=BODY_TASK, content_sha="f" * 64),
        pattern(3, title="Тоже целый", body=BODY_TASK),
    ]

    result = pm.sync(FakeClient([page(items)]), state, ctx, CompanionSettings())

    assert result["applied"] == 2
    assert [s["id"] for s in result["skipped"]] == [2]
    assert result["skipped"][0]["reason"] == "content_mismatch"
    text = (override / "dev" / "patterns_js_ui_updates.md").read_text(encoding="utf-8")
    assert "## Целый" in text and "## Тоже целый" in text and "## Испорченный" not in text, (
        "Порча ОДНОЙ записи не должна валить весь канал: остальная страница применяется")


def test_legacy_pattern_without_content_sha_is_applied(tmp_path):
    ctx, state, override = make_env(tmp_path)
    legacy = pattern(1, title="Легаси", body=BODY_TASK, content_sha="")

    result = pm.sync(FakeClient([page([legacy])]), state, ctx, CompanionSettings())

    assert result["applied"] == 1 and result["skipped"] == [], (
        "Пустой content_sha256 — легаси-строка старой базы, а не порча: считать её "
        "ошибкой значит выбросить исторические паттерны")


# --------------------------------------------------------------------------------------
# 6. Клиентские фильтры
# --------------------------------------------------------------------------------------
def test_min_mcp_version_newer_than_client_is_skipped(tmp_path):
    ctx, state, override = make_env(tmp_path)
    items = [
        pattern(1, title="Подходит", body=BODY_TASK, min_mcp="0.300.0"),
        pattern(2, title="Слишком новый", body=BODY_TASK, min_mcp="0.400.0"),
    ]

    result = pm.sync(FakeClient([page(items)]), state, ctx, CompanionSettings())

    assert result["applied"] == 1
    assert result["skipped"][0]["reason"] == "min_mcp_version", (
        "Клиентский фильтр по версии — ДОПОЛНИТЕЛЬНЫЙ к серверному: сервер мог отдать "
        "страницу без фильтра (например, при пустом mcp_version в запросе)")
    text = (override / "dev" / "patterns_js_ui_updates.md").read_text(encoding="utf-8")
    assert "## Слишком новый" not in text


def test_strict_signature_mode_skips_unsigned(tmp_path):
    ctx, state, override = make_env(tmp_path)
    item = pattern(1, title="Без подписи", body=BODY_TASK, signature=None)

    strict = pm.sync(FakeClient([page([item])]), state, ctx,
                     CompanionSettings(require_pattern_signature=True))
    assert strict["applied"] == 0
    assert strict["skipped"][0]["reason"] == "signature_required", (
        "В строгом режиме паттерн без подписи не применяется — это осознанное ужесточение")
    assert not (override / "dev" / "patterns_js_ui_updates.md").exists()

    ctx2, state2, override2 = make_env(tmp_path / "second")
    relaxed = pm.sync(FakeClient([page([item])]), state2, ctx2,
                      CompanionSettings(require_pattern_signature=False))
    assert relaxed["applied"] == 1, (
        "Дефолт — не требовать подпись: механизма подписи markdown у издателя ещё нет, и "
        "строгий режим по умолчанию просто выключил бы канал целиком")


# --------------------------------------------------------------------------------------
# 7. Безопасность имени файла
# --------------------------------------------------------------------------------------
def test_hostile_area_never_escapes_dev_directory(tmp_path):
    ctx, state, override = make_env(tmp_path)
    item = pattern(1, title="Злой", body=BODY_TASK, area="../../evil")

    result = pm.sync(FakeClient([page([item])]), state, ctx, CompanionSettings())

    assert result["applied"] == 1, "Кривая область не повод терять паттерн"
    created = [Path(p) for p in result["files_written"]]
    for path in created:
        assert path.parent == override / "dev", (
            f"Файл {path} создан вне <override>/dev — имя области приходит из сети, и "
            "любой её символ, пригодный для навигации по путям, это path traversal")
    outside = [p for p in tmp_path.rglob("*")
               if p.is_file() and "evil" in p.name and override not in p.parents]
    assert outside == [], f"За пределами override-корня появились файлы: {outside}"


# --------------------------------------------------------------------------------------
# 8. Рендер тела
# --------------------------------------------------------------------------------------
def test_body_headings_shifted_but_fenced_code_intact(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Пример с кодом", body=BODY_FENCED)])]),
            state, ctx, CompanionSettings())

    lines = (override / "dev" / "patterns_js_ui_updates.md").read_text(
        encoding="utf-8").splitlines()
    fence_positions = [i for i, line in enumerate(lines) if line.startswith("```")]
    assert len(fence_positions) == 2
    inside = lines[fence_positions[0] + 1:fence_positions[1]]
    outside = lines[:fence_positions[0]] + lines[fence_positions[1] + 1:]

    assert "### Задача" in outside, (
        "Заголовок тела обязан уехать на уровень глубже, чтобы название паттерна "
        "оставалось единственным разделом второго уровня в файле")
    assert "#### Решение" in outside
    assert "## Задача" in inside, (
        "Строка ВНУТРИ ограждённого блока не трогается: там `#` — это комментарий или "
        "пример разметки, и «починка» уровня испортила бы работающий код")
    assert [line for line in outside if line.startswith("## ")] == ["## Пример с кодом"], (
        "Единственный заголовок второго уровня в файле — название паттерна")


# --------------------------------------------------------------------------------------
# 9. Совместимость с читателем (клиентский MCP)
# --------------------------------------------------------------------------------------
def test_generated_markdown_is_readable_by_mcp_rules(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([
        pattern(1, title="Кнопка в реестре", body=BODY_TASK),
        pattern(2, title="Листенер", body=BODY_TASK, area="csharp"),
    ])]), state, ctx, CompanionSettings())

    area_path = override / "dev" / "patterns_js_ui_updates.md"
    assert area_path.name.startswith("patterns_") and area_path.name.endswith(".md"), (
        "Файл обязан попадать под маску `<root>/dev/patterns_*.md`, иначе читатель его "
        "просто не увидит")
    sections = reader_sections(area_path.read_text(encoding="utf-8"))
    assert sections and sections[0][0] == "Кнопка в реестре", (
        "Заголовок паттерна обязан находиться регексом читателя `^#{2,4}\\s+(.*\\S)\\s*$`")

    index_sections = reader_index(index_text(override))
    managed = [s for s in index_sections if s[0].startswith("Канал обновлений")]
    assert len(managed) == 2, (
        "В индексе обязана появиться секция на каждую область — читатель разбирает индекс "
        "регексом `^##\\s+(?!#)(.*)$`")
    js = [s for s in managed if "js_ui" in s[1]][0]
    assert js[1] == "dev/patterns_js_ui_updates.md", (
        "Файл-подсказка обязана резолвиться из ЗАГОЛОВКА секции (у него приоритет)")
    assert js[2] and js[2][0][0] == "Кнопка в реестре", (
        "Строка индекса обязана парситься регексом "
        "`^\\|\\s*\\*\\*(.+?)\\*\\*\\s*\\|\\s*(.+?)\\s*\\|\\s*$` — первая ячейка **bold**")
    assert js[2][0][1].strip(), "Колонка «Когда использовать» не должна быть пустой"
    assert reader_area_matches("js_ui", js[1]), (
        "Фильтр area обязан срабатывать на `patterns_js_ui_updates.md` так же, как на "
        "поставочном `patterns_js_ui.md` — ради этого суффикс и добавляется в КОНЕЦ имени")
    assert not reader_area_matches("csharp", js[1])

    # Поставочные секции индекса остались разбираемыми — блок канала их не сломал.
    assert any(s[1] == "dev/patterns_js_ui.md" for s in index_sections), (
        "Поставочные секции индекса обязаны продолжать резолвиться после вставки блока")


def test_index_row_escapes_pipe_in_title(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Кнопка | реестр", body=BODY_TASK)])]),
            state, ctx, CompanionSettings())

    rows = [row for _, _, section_rows in reader_index(index_text(override))
            for row in section_rows]
    assert ("Кнопка \\| реестр", "Добавить поле на карточку.") in rows, (
        "Неэкранированный `|` в названии разорвал бы строку таблицы на лишние ячейки, и "
        "regex читателя перестал бы её видеть")


# --------------------------------------------------------------------------------------
# 10. Откат
# --------------------------------------------------------------------------------------
def test_restore_returns_exactly_previous_files(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый", body=BODY_TASK)],
                             next_since="2026-08-01T10:00:00Z", next_since_id=1)]),
            state, ctx, CompanionSettings())
    snap = pm.snapshot(state)
    before = files_snapshot(override)

    pm.sync(FakeClient([page([pattern(2, title="Новый", body=BODY_TASK, area="csharp")],
                             since="2026-08-01T10:00:00Z", since_id=1,
                             next_since="2026-08-02T10:00:00Z", next_since_id=2)]),
            state, ctx, CompanionSettings())
    assert files_snapshot(override) != before

    pm.restore(state, snap, override)

    assert files_snapshot(override) == before, (
        "Откат обязан вернуть ПОБАЙТНО прежний набор файлов: файлы — проекция состояния, "
        "поэтому откат состояния плюс перерисовка эквивалентны отмене применения")
    assert [r["id"] for r in state.patterns["applied"]] == [1]
    assert state.patterns["since_id"] == 2, (
        "Курсор откатом не сдвигается: он отражает, что уже ПОЛУЧЕНО с сервера, а не что "
        "применено")


def test_snapshot_is_deep_copy(tmp_path):
    ctx, state, override = make_env(tmp_path)
    pm.sync(FakeClient([page([pattern(1, title="Первый", body=BODY_TASK)])]),
            state, ctx, CompanionSettings())

    snap = pm.snapshot(state)
    state.patterns["applied"][0]["title"] = "Испорчено на месте"

    assert snap[0]["title"] == "Первый", (
        "Мелкая копия отдала бы те же словари, и sync правил бы «снимок» вместе с "
        "состоянием — откат стал бы бессмысленным")
