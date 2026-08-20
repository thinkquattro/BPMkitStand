# -*- coding: utf-8 -*-
"""Канал паттернов: приём дельты с бэкенда издателя и укладка её в базу паттернов MCP.

Зачем модуль. Клиентский MCP читает библиотеку паттернов из markdown-файлов на диске
(`BPMkit/server/bpmkit/patterns.py`), кэша у него нет — новый файл виден сразу, без
перезапуска. Значит доставка обновлений сводится к аккуратной записи файлов в тот корень,
который читатель считает своим. Вся сложность — в слове «аккуратной»; ниже перечислены
решения, каждое из которых закрывает конкретный способ тихо сломать базу паттернов.

**1. Курсор — ПАРА `(since, since_id)`.** Курсор только по времени теряет строки с
одинаковой меткой на границе страницы, поэтому сервер отдаёт `next_since` + `next_since_id`
и ждёт их обратно вместе. `since_id` без `since` он игнорирует ЦЕЛИКОМ — то есть «послать
половину» хуже, чем не посылать ничего: клиент молча начнёт качать базу с начала. Значения
возвращаются дословно как пришли: любая пере-сборка datetime (нормализация зоны, обрезка
микросекунд) сдвигает границу страницы и теряет строки.

**2. Пагинация — до `has_more == false` В ОДНОМ проходе.** Растянуть страницы на разные
тики нельзя: между тиками база меняется, и дельта склеится из несогласованных срезов.

**3. `count == 0` при `has_more == true` — штатная ситуация**, а не конец данных: страницу
целиком отфильтровал сервер по `mcp_version`. Курсор при этом всё равно едет вперёд.
Трактовка `count == 0` как «конец» — самый дешёвый способ навсегда застрять на месте.

**4. Пустая дельта — успех, а не ошибка.** `count: 0`, `has_more: false`, курсор эхом —
это «у вас всё актуально»; статус тика `ok`.

**5. Tombstone применяется БЕЗУСЛОВНО.** Запись `{"deleted": true}` приезжает независимо
от `mcp_version` — паттерн мог быть применён раньше, на другой версии MCP, и отзыв обязан
его достать. Клиентские фильтры к отзыву не применяются вовсе.

**6. `bundle_sha256` — ЦЕЛОСТНОСТЬ, НЕ ПОДЛИННОСТЬ.** Он считается по тому же массиву,
что и приехал, поэтому ловит только порчу в канале (обрыв, кривой прокси, битая склейка),
но никак не подмену злоумышленником — тот пересчитает сумму вместе с телом. Ни в логах,
ни в UI этот механизм не называется подписью: подпись паттернов — отдельная опция
(`require_pattern_signature`), и путать их значит обещать пользователю защиту, которой нет.

**7. Порча одной записи не валит канал.** Несовпадение `content_sha256` — причина
выбросить ОДИН паттерн (с явной причиной в отчёте), а не всю страницу; несовпадение
`bundle_sha256` — наоборот, повод отбросить страницу целиком и НЕ двигать курсор, чтобы
следующий тик перезапросил её же.

**8. Файлы — проекция состояния.** `state.patterns["applied"]` хранит полные записи, а
файлы рисуются из них целиком при каждом применении (`render`). Поэтому отзыв паттерна —
это удаление записи из состояния плюс перерисовка, а не хирургия по markdown, которая
ломается на любом нестандартном оформлении тела.

**9. Скачанное НИКОГДА не дописывается в поставочные файлы.** Обновления едут в отдельные
`dev/patterns_<area>_updates.md`, а в индексе занимают управляемый блок между маркерами;
всё, что вне маркеров, — рукописный текст издателя, он не трогается никогда. Причина
простая: дописывание в чужой файл делает отзыв и откат неразрешимой задачей.

**10. Override-корень заменяет поставочный ЦЕЛИКОМ.** У читателя приоритет такой:
env `BPMKIT_PATTERNS_PATH` → автодетект `<package_root>/skills/bpmsoft-dev/references`;
merge двух корней он не делает. Значит первое же применение обязано СНАЧАЛА скопировать
всё поставочное дерево в override (`seed_override_root`) — иначе половина базы паттернов
исчезнет молча, и заметят это не сегодня, а когда паттерн понадобится.

Правила читателя, под которые здесь генерируется markdown (вычитаны из его кода):

* корень валиден, только если в нём есть `dev/patterns_index.md`;
* файлы библиотеки — `<root>/dev/patterns_*.md`, кроме самого индекса;
* заголовок раздела — `^#{2,4}\\s+(.*\\S)\\s*$` (уровни 2..4; ровно один `#` не считается);
* описание раздела — первая непустая строка после заголовка, если она не начинается с `#`;
* индекс: секции по `^##\\s+(?!#)(.*)$`, файл-подсказка — первое `` `имя.md` `` в заголовке
  секции (приоритет) либо в теле, строки паттернов — `^\\|\\s*\\*\\*(.+?)\\*\\*\\s*\\|...`;
* фильтр `area` срабатывает, если `area.lower()` — подстрока файла-подсказки либо токен её
  basename (разбиение по не-`[a-z0-9]`). Поэтому `patterns_js_ui_updates.md` находится по
  `area="js_ui"` ровно так же, как поставочный `patterns_js_ui.md`.

Модуль stdlib-only и НЕ импортирует `backend`/`context` в рантайме: от клиента ему нужен
единственный метод `get_json`, от контекста — набор атрибутов. Это же делает его
тестируемым подставным клиентом без сети.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import fsutil
from .errors import ChannelError

if TYPE_CHECKING:  # только для аннотаций — в рантайме связь duck-typing'овая
    from .backend import BackendClient
    from .context import LicenseContext
    from .state import CompanionState

__all__ = [
    "SYNC_PATH",
    "MANAGED_BEGIN",
    "MANAGED_END",
    "UPDATES_SUFFIX",
    "PAGE_LIMIT",
    "MAX_LIMIT",
    "parse_version",
    "compare_versions",
    "sanitize_area",
    "bundle_sha256",
    "content_sha256",
    "seed_override_root",
    "render",
    "sync",
    "snapshot",
    "restore",
]

# --------------------------------------------------------------------------------------
# Контракт эндпоинта
# --------------------------------------------------------------------------------------
SYNC_PATH = "/v1/content/patterns/sync"

# Размер страницы. Дефолт сервера — 200, потолок — 500; берём дефолт: страницы с телами
# паттернов весят сотни килобайт, гнаться за потолком незачем.
PAGE_LIMIT = 200
MAX_LIMIT = 500

# --------------------------------------------------------------------------------------
# Раскладка на диске
# --------------------------------------------------------------------------------------
MANAGED_BEGIN = "<!-- BPMKIT-COMPANION-BEGIN -->"
MANAGED_END = "<!-- BPMKIT-COMPANION-END -->"

# Суффикс имени файла области. Он НАРОЧНО отличает наши файлы от поставочных
# (`patterns_js_ui.md` против `patterns_js_ui_updates.md`): по имени всегда видно, что
# файл сгенерирован каналом и его можно удалить целиком, а фильтр `area` у читателя
# срабатывает одинаково на обоих.
UPDATES_SUFFIX = "_updates"
UPDATES_PREFIX = "patterns_"

DEV_SUBDIR = "dev"
INDEX_NAME = "patterns_index.md"

# Заголовок файла области и индекса. Обычный HTML-комментарий: регексы читателя его не
# видят, а человек, открывший файл руками, сразу понимает, почему его правки пропадут.
_GENERATED_NOTE = ("<!-- Файл сгенерирован каналом обновлений BPMkitStand Companion. "
                   "Правки будут потеряны при следующем применении. -->")

_EMPTY_INDEX = (
    "# Индекс паттернов\n"
    "\n"
    f"{_GENERATED_NOTE}\n"
    "\n"
    "Поставочный индекс не найден — файл создан каналом обновлений, чтобы корень базы\n"
    "паттернов был валиден для клиентского MCP.\n"
)

# Имя области, под которым едет всё, что не удалось привести к безопасному имени файла.
_FALLBACK_AREA = "other"
# Потолок длины имени области. Имя приходит ИЗ СЕТИ и становится именем файла — длину
# ограничиваем так же жёстко, как набор символов.
_MAX_AREA_LEN = 48

# Описание паттерна в индексе. Длинная строка ломает читаемость таблицы, поэтому режется.
_SUMMARY_MAX = 200
_DEFAULT_SUMMARY = "Паттерн из канала обновлений издателя"

# Поля записи паттерна, которые сохраняются в состоянии. Всё лишнее из ответа
# отбрасывается: состояние не должно расти от каждого нового поля сервера.
_RECORD_KEYS = (
    "id", "title", "body_markdown", "version", "min_mcp_version", "area", "proof",
    "pattern_type", "published_at", "updated_at", "status", "content_sha256",
    "signature", "sig_key_id",
)

# Статусы, при которых запись считается отозванной даже без флага `deleted`.
_REVOKED_STATUSES = frozenset({"revoked", "deleted"})

_HEADING_IN_BODY_RE = re.compile(r"^(#{1,6})(\s+)(.*)$")
# Ограда блока кода: до трёх пробелов отступа, затем 3+ символа ` или ~.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_AREA_BAD_RE = re.compile(r"[^a-z0-9_]+")


# --------------------------------------------------------------------------------------
# Версии
# --------------------------------------------------------------------------------------
def parse_version(value: Any) -> tuple:
    """Версия → кортеж целых для сравнения.

    Своя реализация, повторяющая правила бэкенда, а не `packaging.version`: пакет
    stdlib-only, тянуть зависимость ради трёх чисел нельзя. Нечисловой сегмент = 0
    (`1.2.0-rc1` → `(1, 2, 0)` невозможно отличить от `(1, 2, 0)` — и не надо: канал
    сравнивает опубликованные версии, а не пре-релизы).
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return ()
    out = []
    for part in text.split("."):
        chunk = part.strip()
        out.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(out)


def compare_versions(a: Any, b: Any) -> int:
    """-1 / 0 / 1 для `a` относительно `b`.

    Сравнение ПОЭЛЕМЕНТНОЕ по int-кортежам, никогда строковое: `"0.10.0" < "0.9.0"` как
    строки, но `0.10.0` новее — на этой ошибке канал перестал бы отдавать паттерны всем,
    кто перешагнул десятку в минорной версии. Короткий кортеж дополняется нулями справа
    (`1.2` == `1.2.0`).
    """
    ta, tb = parse_version(a), parse_version(b)
    size = max(len(ta), len(tb))
    ta = ta + (0,) * (size - len(ta))
    tb = tb + (0,) * (size - len(tb))
    return (ta > tb) - (ta < tb)


# --------------------------------------------------------------------------------------
# Имена файлов
# --------------------------------------------------------------------------------------
def sanitize_area(area: Any) -> str:
    """Имя области → безопасный кусок имени файла (`[a-z0-9_]`).

    Это ЕДИНСТВЕННЫЙ барьер между строкой из сети и путём на диске: `area` подставляется
    в имя файла, и `"../../evil"` без санации записал бы файл за пределами корня. Поэтому
    здесь не «нормализация для красоты», а фильтр по белому списку символов — точки и
    разделители пути не выживают в принципе. Пустой/вырожденный результат — `other`,
    молча терять паттерн из-за кривой области нельзя.
    """
    text = str(area if area is not None else "").strip().lower()
    cleaned = _AREA_BAD_RE.sub("_", text).strip("_")
    if not cleaned:
        return _FALLBACK_AREA
    return cleaned[:_MAX_AREA_LEN].strip("_") or _FALLBACK_AREA


def _updates_name(area: str) -> str:
    return f"{UPDATES_PREFIX}{area}{UPDATES_SUFFIX}.md"


def _updates_rel(area: str) -> str:
    """Путь файла области ОТНОСИТЕЛЬНО корня базы, с прямыми слэшами.

    Именно в таком виде он попадает в индекс: файл-подсказку читатель ищет как
    `` `dev/patterns_js_ui_updates.md` `` и сравнивает с ней `area` — обратный слэш
    Windows сломал бы и поиск подсказки, и совпадение области.
    """
    return f"{DEV_SUBDIR}/{_updates_name(area)}"


def _area_from_name(name: str) -> str:
    """Обратное преобразование имени файла в область — чтобы понять, чей это файл."""
    stem = name[:-3] if name.endswith(".md") else name
    if stem.startswith(UPDATES_PREFIX):
        stem = stem[len(UPDATES_PREFIX):]
    if stem.endswith(UPDATES_SUFFIX):
        stem = stem[:-len(UPDATES_SUFFIX)]
    return stem


# --------------------------------------------------------------------------------------
# Хеши
# --------------------------------------------------------------------------------------
def bundle_sha256(patterns: Any) -> str:
    """Контрольная сумма страницы — по массиву `patterns`, а НЕ по всему конверту.

    Форма сериализации зафиксирована контрактом и повторяется здесь дословно
    (`sort_keys=True`, `ensure_ascii=False`, `separators=(",", ":")`): любое отличие —
    лишний пробел, порядок ключей, экранирование кириллицы — даёт другую сумму, и канал
    будет вечно отбрасывать корректные страницы.

    Массив хешируется УЖЕ ОТФИЛЬТРОВАННЫЙ сервером и в том порядке, в котором приехал.
    """
    blob = json.dumps(patterns, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def content_sha256(body: Any) -> str:
    """sha256 тела паттерна (utf-8). Ловит порчу ОДНОЙ записи внутри целой страницы."""
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Ввод-вывод
# --------------------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """Текст файла или `""`, если файла нет.

    Отсутствие файла — штатно (первый запуск), а вот нечитаемый или недекодируемый файл —
    `local_io`: молча принять его за пустой значило бы затереть рукописный индекс.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ChannelError(f"Не удалось прочитать {path}", kind="local_io",
                           detail=str(exc)) from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChannelError(f"Файл {path} не в UTF-8 — перезапись отменена",
                           kind="local_io", detail=str(exc)) from None


def _write_text(path: Path, text: str) -> None:
    try:
        fsutil.atomic_write_text(path, text)
    except OSError as exc:
        raise ChannelError(f"Не удалось записать {path}", kind="local_io",
                           detail=str(exc)) from None


def _write_if_changed(path: Path, text: str) -> bool:
    """Запись только при РЕАЛЬНОМ изменении содержимого.

    Пустая дельта не должна трогать файлы: перезапись меняет mtime, а по нему читатель
    (и человек, и будущие проверки) судит о том, менялась ли база. `True` — файл записан.
    """
    if path.is_file() and _read_text(path) == text:
        return False
    _write_text(path, text)
    return True


# --------------------------------------------------------------------------------------
# Seed: перенос поставочного дерева в override-корень
# --------------------------------------------------------------------------------------
def seed_override_root(shipped_root: Any, override_root: Any, *,
                       force: bool = False) -> dict:
    """Скопировать поставочную базу паттернов в override-корень.

    Ключевой шаг всей задачи. Читатель выбирает ОДИН корень (env `BPMKIT_PATTERNS_PATH`
    сильнее автодетекта) и не сливает его с поставочным. Поэтому, как только канал
    зарегистрировал свой override-корень, поставочные паттерны обязаны в нём оказаться —
    иначе пользователь потеряет всю библиотеку и увидит только то, что успело приехать
    из сети.

    Идемпотентность определяется по признаку валидности корня у читателя — наличию
    `dev/patterns_index.md`. Если он есть, повторный seed НЕ выполняется: файлы могли быть
    правлены руками, и затирать их каждым тиком нельзя. `force=True` — осознанное
    восстановление из поставки.
    """
    override = Path(override_root)
    index = override / DEV_SUBDIR / INDEX_NAME
    if index.is_file() and not force:
        return {"copied": 0, "skipped": True, "root": str(override)}

    copied = 0
    src = Path(shipped_root) if str(shipped_root or "").strip() else None
    if src is not None and src.is_dir():
        try:
            items = sorted(p for p in src.rglob("*") if p.is_file())
        except OSError as exc:
            raise ChannelError(f"Не удалось прочитать поставочный корень {src}",
                               kind="local_io", detail=str(exc)) from None
        for item in items:
            dst = override / item.relative_to(src)
            # Уже лежащий файл при обычном seed не трогаем: он либо из прошлого seed'а,
            # либо правлен человеком — в обоих случаях его версия не хуже поставочной.
            if dst.is_file() and not force:
                continue
            try:
                payload = item.read_bytes()
            except OSError as exc:
                raise ChannelError(f"Не удалось прочитать {item}", kind="local_io",
                                   detail=str(exc)) from None
            try:
                fsutil.atomic_write_bytes(dst, payload)
            except OSError as exc:
                raise ChannelError(f"Не удалось записать {dst}", kind="local_io",
                                   detail=str(exc)) from None
            copied += 1

    # Корень обязан быть валидным для читателя даже если поставочного дерева рядом нет
    # (сборка без скиллов, битая установка): без индекса он отвергнет корень целиком.
    if not index.is_file():
        _write_text(index, _EMPTY_INDEX)

    return {"copied": copied, "skipped": False, "root": str(override)}


# --------------------------------------------------------------------------------------
# Рендер markdown
# --------------------------------------------------------------------------------------
def _text_of(value: Any) -> str:
    """Однострочная нормализация текста из сети: без переводов строк и лишних пробелов."""
    return " ".join(str(value if value is not None else "").split())


def _escape_cell(value: str) -> str:
    """Экранирование для ячейки таблицы. `|` внутри значения разорвал бы строку на две
    ячейки, и строка перестала бы соответствовать regex читателя."""
    return value.replace("|", r"\|")


def _row_title(rec: dict) -> str:
    """Заголовок для строки индекса: без `**` и без `|`.

    `**` снимается потому, что regex читателя нежадный (`\\*\\*(.+?)\\*\\*`) и на вложенном
    выделении обрезал бы название на полуслове.
    """
    title = _text_of(rec.get("title")).replace("**", "")
    title = _escape_cell(title)
    if not title:
        title = f"Паттерн #{rec.get('id')}"
    return title


def _shift_headings(body: str) -> str:
    """Сдвинуть заголовки тела на уровень глубже, НЕ трогая содержимое блоков кода.

    Тело паттерна почти всегда начинается с `## Задача` — вставленное как есть, оно
    оказалось бы на одном уровне с заголовком самого паттерна, и раздел «поехал» бы:
    читатель считает разделом каждый `^#{2,4}`. Сдвиг гарантирует, что единственный
    заголовок второго уровня в файле — название паттерна.

    Внутри ограждённых блоков (``` / ~~~) строки не трогаются: там `#` — это комментарий
    shell/python или markdown-пример, и «починка» уровня испортила бы работающий код.
    Уровень результата не опускается ниже третьего (иначе `# Заголовок` из тела снова
    стал бы разделом верхнего уровня) и не превышает шестого.
    """
    lines = str(body or "").splitlines()
    out = []
    fence = ""  # символ активной ограды: "" = мы вне блока кода
    for line in lines:
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)[0]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        heading = _HEADING_IN_BODY_RE.match(line)
        if heading:
            level = min(6, max(3, len(heading.group(1)) + 1))
            out.append("#" * level + heading.group(2) + heading.group(3))
            continue
        out.append(line)
    return "\n".join(out)


def _summary(rec: dict) -> str:
    """Описание паттерна одной строкой — для колонки «Когда использовать» в индексе.

    Берётся первая содержательная строка тела (не заголовок, не ограда кода) — ровно то,
    что читатель считает описанием раздела. `**` снимаются по его же правилу.
    """
    for raw in str(rec.get("body_markdown") or "").splitlines():
        if _FENCE_RE.match(raw):
            break
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip(">").strip()
        line = _text_of(line.replace("**", ""))
        if not line:
            continue
        if len(line) > _SUMMARY_MAX:
            line = line[:_SUMMARY_MAX].rstrip() + "…"
        return _escape_cell(line)
    return _DEFAULT_SUMMARY


def _render_area_file(area: str, records: list) -> str:
    """Файл одной области: шапка-комментарий и по разделу второго уровня на паттерн."""
    parts = [_GENERATED_NOTE, "",
             f"<!-- Область: {area}. Источник: канал обновлений издателя BPMkit. -->", ""]
    for rec in records:
        title = _text_of(rec.get("title")) or f"Паттерн #{rec.get('id')}"
        parts.append(f"## {title}")
        parts.append("")
        body = _shift_headings(rec.get("body_markdown"))
        if body.strip():
            parts.append(body.strip("\n"))
            parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def _managed_block(groups: dict) -> str:
    """Управляемый блок индекса: по секции на область. Пустые группы → пустая строка,
    то есть блок из индекса исчезает целиком (а не остаётся пустым огрызком)."""
    if not groups:
        return ""
    lines = [MANAGED_BEGIN, "", _GENERATED_NOTE, ""]
    for area in sorted(groups):
        lines.append(f"## Канал обновлений: {area} (`{_updates_rel(area)}`)")
        lines.append("")
        lines.append("| Паттерн | Когда использовать |")
        lines.append("|---------|--------------------|")
        for rec in groups[area]:
            lines.append(f"| **{_row_title(rec)}** | {_summary(rec)} |")
        lines.append("")
    lines.append(MANAGED_END)
    return "\n".join(lines)


def _with_managed_block(text: str, block: str) -> str:
    """Вживить/обновить/убрать управляемый блок, не тронув НИЧЕГО за маркерами.

    Текст вне маркеров — рукописный индекс издателя. Он переносится посимвольно: и голова,
    и хвост берутся срезами исходной строки. Единственная вольность — при ПЕРВОЙ вставке
    подрезаются пустые строки в конце файла, иначе каждый цикл «вставили-убрали» добавлял
    бы по переводу строки.
    """
    begin = text.find(MANAGED_BEGIN)
    end = text.find(MANAGED_END)
    if begin != -1 and end != -1 and end > begin:
        head = text[:begin]
        tail = text[end + len(MANAGED_END):]
        if not block:
            return head + tail
        return head + block + tail
    if not block:
        return text
    if not text.strip():
        return block + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def render(applied: list, override_root: Any) -> dict:
    """Перерисовать файлы канала из состояния.

    Полная перерисовка, а не инкремент: только так отзыв паттерна и откат к снимку
    получаются корректными по построению. Файлы, чья область опустела, удаляются —
    пустой `patterns_<area>_updates.md` иначе остался бы висеть в выдаче читателя.
    """
    root = Path(override_root)
    dev = root / DEV_SUBDIR
    try:
        dev.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(f"Не удалось создать каталог {dev}", kind="local_io",
                           detail=str(exc)) from None

    groups: dict = {}
    for rec in applied or []:
        groups.setdefault(sanitize_area(rec.get("area")), []).append(rec)
    for records in groups.values():
        # Порядок разделов в файле не должен зависеть от порядка приезда страниц —
        # иначе дифф файла «взрывается» на ровном месте.
        records.sort(key=lambda r: (_as_int(r.get("id")) or 0, _text_of(r.get("title"))))

    written: list = []
    removed: list = []

    for area in sorted(groups):
        path = dev / _updates_name(area)
        if _write_if_changed(path, _render_area_file(area, groups[area])):
            written.append(str(path))

    # Осиротевшие файлы канала. Маска ловит ТОЛЬКО наши имена — поставочный
    # `patterns_js_ui.md` под неё не попадает и удалён быть не может.
    for path in sorted(dev.glob(f"{UPDATES_PREFIX}*{UPDATES_SUFFIX}.md")):
        if _area_from_name(path.name) in groups:
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise ChannelError(f"Не удалось удалить {path}", kind="local_io",
                               detail=str(exc)) from None
        removed.append(str(path))

    index = dev / INDEX_NAME
    updated_index = _with_managed_block(_read_text(index), _managed_block(groups))
    if not updated_index.strip():
        updated_index = _EMPTY_INDEX
    if _write_if_changed(index, updated_index):
        written.append(str(index))

    return {
        "files_written": written,
        "files_removed": removed,
        "areas": {area: len(recs) for area, recs in sorted(groups.items())},
    }


# --------------------------------------------------------------------------------------
# Синхронизация
# --------------------------------------------------------------------------------------
def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize(item: dict) -> dict:
    """Запись сервера → запись состояния: только известные поля, id — целым.

    Лишние поля отбрасываются намеренно: состояние читает и пишет каждый тик, и
    неограниченный рост от новых полей сервера — это рост файла на диске у пользователя.
    """
    rec = {key: item.get(key) for key in _RECORD_KEYS}
    rec["id"] = _as_int(item.get("id"))
    rec["title"] = str(item.get("title") or "")
    rec["body_markdown"] = str(item.get("body_markdown") or "")
    rec["area"] = str(item.get("area") or "")
    return rec


def _is_tombstone(item: dict) -> bool:
    """Признак отзыва. Флаг `deleted` — основной; статус разбирается дополнительно, потому
    что запись, снятая с публикации, тоже обязана исчезнуть у клиента."""
    if bool(item.get("deleted")):
        return True
    return str(item.get("status") or "").strip().lower() in _REVOKED_STATUSES


def _check_page(payload: Any) -> list:
    """Разбор конверта страницы + проверка целостности.

    Несовпадение `bundle_sha256` отбрасывает страницу ЦЕЛИКОМ и не двигает курсор:
    следующий тик перезапросит ровно её же. Отсутствующая сумма (старый сервер) проверку
    не проваливает — иначе клиент перестал бы работать с любой версией бэкенда, кроме
    последней; порчу тел в этом случае ловит `content_sha256` каждой записи.
    """
    if not isinstance(payload, dict):
        raise ChannelError("Ответ дельты паттернов не похож на объект",
                           kind="bad_response",
                           detail=f"тип тела: {type(payload).__name__}")
    items = payload.get("patterns")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ChannelError("Поле patterns в ответе — не массив", kind="bad_response",
                           detail=f"тип: {type(items).__name__}")
    expected = payload.get("bundle_sha256")
    if isinstance(expected, str) and expected.strip():
        actual = bundle_sha256(items)
        if actual != expected.strip().lower():
            raise ChannelError(
                "Контрольная сумма страницы не сошлась — страница отброшена целиком",
                kind="integrity_mismatch",
                detail=f"ожидалось {expected[:16]}…, посчитано {actual[:16]}…")
    return [it for it in items if isinstance(it, dict)]


def _skip(rid: Any, rec: dict, reason: str, note: str) -> dict:
    return {"id": rid, "title": _text_of(rec.get("title")),
            "area": str(rec.get("area") or ""), "reason": reason, "note": note}


def sync(client: "BackendClient", state: "CompanionState", ctx: "LicenseContext",
         settings: Any, *, max_pages: int = 200) -> dict:
    """Полный проход канала паттернов: seed → пагинация → применение → рендер → состояние.

    `ChannelError` наружу НЕ ловится: решение «повторить, промолчать или показать
    пользователю» принимает планировщик по полям `retriable`/`user_visible`, и глушить
    ошибку здесь значило бы отнять у него это решение. Зато на успешном пути метка тика и
    сохранение состояния — забота этой функции: разнести их по вызывающим означало бы
    рано или поздно применить файлы и не сохранить курсор.
    """
    block = state.patterns
    override_root = str(getattr(ctx, "override_patterns_root", "") or "").strip()
    if not override_root:
        raise ChannelError(
            "Клиентский MCP не сообщил override-корень базы паттернов",
            kind="local_io",
            detail="пустой override_patterns_root в лицензионном контексте")
    shipped_root = str(getattr(ctx, "shipped_patterns_root", "") or "").strip()
    mcp_version = str(getattr(ctx, "mcp_version", "") or "").strip()
    require_signature = bool(getattr(settings, "require_pattern_signature", False))

    seed = seed_override_root(shipped_root, override_root)

    # Курсор берём из состояния и двигаем ЛОКАЛЬНО: в состояние он попадёт только после
    # успешного прохода — иначе отброшенная по целостности страница «съела» бы дельту.
    cursor_since = block.get("since")
    cursor_id = block.get("since_id")

    current: dict = {}
    for rec in block.get("applied") or []:
        rid = _as_int(rec.get("id"))
        if rid is not None:
            current[rid] = rec

    fetched = 0
    applied_count = 0
    removed_count = 0
    pages = 0
    skipped: list = []
    last_bundle = ""

    while pages < max(1, int(max_pages)):
        params: dict = {"limit": min(PAGE_LIMIT, MAX_LIMIT)}
        if mcp_version:
            params["mcp_version"] = mcp_version
        # ОБА элемента курсора или ни одного: `since_id` без `since` сервер игнорирует
        # целиком, и клиент незаметно скачивал бы базу с самого начала каждый тик.
        if cursor_since is not None and cursor_id is not None:
            params["since"] = cursor_since
            params["since_id"] = cursor_id

        payload, _headers = client.get_json(SYNC_PATH, params=params)
        items = _check_page(payload)
        pages += 1
        fetched += len(items)
        if isinstance(payload.get("bundle_sha256"), str):
            last_bundle = payload["bundle_sha256"]

        for item in items:
            rid = _as_int(item.get("id"))
            if rid is None:
                skipped.append(_skip(item.get("id"), item, "bad_id",
                                     "в записи нет целочисленного id"))
                continue

            # Отзыв применяется ДО любых фильтров: паттерн мог быть применён раньше, на
            # другой версии MCP, и «не положен по версии» не повод оставить его на диске.
            if _is_tombstone(item):
                if current.pop(rid, None) is not None:
                    removed_count += 1
                continue

            rec = _normalize(item)
            expected = str(rec.get("content_sha256") or "").strip().lower()
            if expected and content_sha256(rec["body_markdown"]) != expected:
                skipped.append(_skip(rid, rec, "content_mismatch",
                                     "контрольная сумма тела паттерна не сошлась"))
                continue
            if not rec["body_markdown"].strip():
                skipped.append(_skip(rid, rec, "empty_body",
                                     "пустое тело паттерна — записывать нечего"))
                continue
            if require_signature and not str(rec.get("signature") or "").strip():
                skipped.append(_skip(rid, rec, "signature_required",
                                     "включён строгий режим, а подписи у паттерна нет"))
                continue
            min_version = str(rec.get("min_mcp_version") or "").strip()
            # Пустая версия MCP в контексте — не повод отфильтровать всё: сравнивать
            # не с чем, и серверный фильтр остаётся единственным. Иначе неизвестная
            # версия молча обнулила бы канал.
            if min_version and mcp_version and compare_versions(min_version,
                                                                mcp_version) > 0:
                skipped.append(_skip(rid, rec, "min_mcp_version",
                                     f"требуется MCP {min_version}, установлен "
                                     f"{mcp_version}"))
                continue

            current[rid] = rec
            applied_count += 1

        next_since = payload.get("next_since")
        next_id = payload.get("next_since_id")
        has_more = bool(payload.get("has_more"))
        # `count == 0` НЕ означает конец: страницу мог целиком отфильтровать сервер по
        # mcp_version. Единственный признак конца — has_more.
        if has_more and next_since == cursor_since and next_id == cursor_id:
            raise ChannelError(
                "Сервер просит продолжить, но курсор не сдвинулся — проход прерван",
                kind="bad_response",
                detail=f"since={next_since!r}, since_id={next_id!r}")
        cursor_since, cursor_id = next_since, next_id
        if not has_more:
            break

    applied_records = [current[key] for key in sorted(current)]
    files = render(applied_records, override_root)

    block["applied"] = applied_records
    block["since"] = cursor_since
    block["since_id"] = cursor_id
    block["seeded"] = True
    block["root"] = str(Path(override_root))
    if last_bundle:
        block["last_bundle_sha256"] = last_bundle
    detail = (f"страниц {pages}, получено {fetched}, применено {applied_count}, "
              f"отозвано {removed_count}, пропущено {len(skipped)}")
    # Пустая дельта — это `ok`: «у вас всё актуально» не ошибка и не должна светиться в
    # UI красным.
    state.mark("patterns", "ok", detail)
    state.save()

    return {
        "fetched": fetched,
        "applied": applied_count,
        "removed": removed_count,
        "skipped": skipped,
        "pages": pages,
        "cursor": {"since": cursor_since, "since_id": cursor_id},
        "files_written": files["files_written"],
        "files_removed": files["files_removed"],
        "seed": seed,
    }


# --------------------------------------------------------------------------------------
# Снимок и откат
# --------------------------------------------------------------------------------------
def snapshot(state: "CompanionState") -> list:
    """Глубокая копия применённых записей — точка отката перед рискованным применением.

    Копия именно глубокая: мелкая отдала бы те же самые словари, и `sync` менял бы
    «снимок» вместе с состоянием, обесценив откат.
    """
    return copy.deepcopy(list(state.patterns.get("applied") or []))


def restore(state: "CompanionState", snapshot_list: list, override_root: Any) -> dict:
    """Вернуть состояние и файлы к снимку.

    Курсор сознательно НЕ откатывается: он отражает, что уже ПОЛУЧЕНО с сервера, а не что
    применено. Сдвиг курсора назад заставил бы клиента перекачивать дельту, которая и так
    у него есть; если нужен именно повторный приезд паттернов, курсор сбрасывает
    вызывающий явно.
    """
    records = copy.deepcopy(list(snapshot_list or []))
    files = render(records, override_root)
    state.patterns["applied"] = records
    state.save()
    return files
