# -*- coding: utf-8 -*-
"""Канал паттернов: метаданные межрелизных паттернов с бэкенда издателя — индекс без тел.

**Онлайн-выдача.** Тела межрелизных паттернов на диск клиента НЕ пишутся. Компаньон кладёт
в override-корень только индекс метаданных `dev/patterns_updates_index.json` — в том же
формате, что и поставочный `dev/patterns_index.json` (заголовок раздела, краткое описание,
ключ `pat:<id>`), а тело раздела клиентский MCP берёт с бэкенда издателя по лицензии
(`pattern_get`). Отсутствие тела в ответе `sync` — норма, а не ошибка формата; если старый
бэкенд всё же прислал `body_markdown`, тело игнорируется и на диск не попадает.

Файлы прежней раскладки — `dev/patterns_<area>_updates.md` и управляемый блок
«Канал обновлений» в `dev/patterns_index.md` — удаляются `migrate_legacy` при первом же
проходе новой версии. Это штатная часть перехода, а не потеря данных: те же разделы
доступны онлайн по ключу из индекса.

Остальная механика канала (курсор, пагинация, отзыв) не изменилась; ниже — решения,
каждое из которых закрывает конкретный способ тихо сломать индекс.

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
ни в UI этот механизм не называется подписью: путать их значит обещать пользователю
защиту, которой нет.

**7. Тела не хранятся и не проверяются.** `content_sha256` и подпись относятся к телу,
которого у компаньона больше нет: их проверяет сторона, выдающая тело (MCP при онлайн-
запросе). Несовпадение `bundle_sha256` по-прежнему отбрасывает страницу целиком и НЕ двигает
курсор, чтобы следующий тик перезапросил её же.

**8. Индекс — проекция состояния.** `state.patterns["applied"]` хранит только метаданные
записей, а индекс рисуется из них целиком при каждом применении (`render`). Отзыв паттерна —
удаление записи из состояния плюс перерисовка.

**9. Поставочные файлы не трогаются.** Межрелизный индекс — отдельный файл
`dev/patterns_updates_index.json`; поставочный `patterns_index.json` и рукописный
`patterns_index.md` компаньон не меняет (единственное исключение — однократная вычистка
управляемого блока прежней версии, см. `migrate_legacy`).

**10. Override-корень заменяет поставочный ЦЕЛИКОМ.** У читателя приоритет такой:
env `BPMKIT_PATTERNS_PATH` → автодетект `<package_root>/skills/bpmsoft-dev/references`;
merge двух корней он не делает. Значит первое же применение обязано СНАЧАЛА скопировать
всё поставочное дерево в override (`seed_override_root`) — иначе половина базы паттернов
исчезнет молча, и заметят это не сегодня, а когда паттерн понадобится.

Формат индекса (совпадает с поставочным `patterns_index.json`, который читает MCP):

    {"format": 1, "note": "...", "files": [{"file": "dev/patterns_<area>_updates.md",
     "area": "<area>_updates", "sections": [{"heading": "...", "level": 3,
     "snippet": "...", "key": "pat:<id>"}]}]}

`file` — виртуальное имя для группировки и фильтра `area` у читателя; файла с таким именем
на диске нет. `snippet` — однострочное описание из метаданных (≤160 символов, без кода).

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
from .errors import ChannelError, NotModified

if TYPE_CHECKING:  # только для аннотаций — в рантайме связь duck-typing'овая
    from .backend import BackendClient
    from .context import LicenseContext
    from .state import CompanionState

__all__ = [
    "SYNC_PATH",
    "STATS_PATH",
    "STATS_TIMEOUT_SEC",
    "MANAGED_BEGIN",
    "MANAGED_END",
    "UPDATES_SUFFIX",
    "UPDATES_INDEX_NAME",
    "PAGE_LIMIT",
    "MAX_LIMIT",
    "parse_version",
    "compare_versions",
    "sanitize_area",
    "bundle_sha256",
    "content_sha256",
    "seed_override_root",
    "migrate_legacy",
    "build_index",
    "render",
    "sync",
    "fetch_stats",
    "stats_from_error",
    "stats_line",
    "INDEX_PATH",
    "INDEX_TIMEOUT_SEC",
    "SERVER_INDEX_NAME",
    "index_sha256",
    "local_index_sha",
    "fetch_index",
    "write_server_index",
    "reconcile_index",
    "index_status_from_error",
    "INDEX_STATUS_TITLES",
    "snapshot",
    "restore",
]

# --------------------------------------------------------------------------------------
# Контракт эндпоинтов
# --------------------------------------------------------------------------------------
SYNC_PATH = "/v1/content/patterns/sync"
# Счётчик библиотеки на сервере для окна «Обновления»: тот же конверт лицензии, что у
# `sync`, ответ `{"sections": int, "updates": int, "updated_at": "ISO"}`.
STATS_PATH = "/v1/content/patterns/stats"
# Окно «Обновления» не должно ждать сеть: счётчик — справочная строка, а не операция.
STATS_TIMEOUT_SEC = 5.0
# С 0.12.21 ответ `stats` несёт ещё `index_sha256` (отпечаток опубликованного индекса
# паттернов, `null` — индекс ещё не опубликован) и `patterns` (число паттернов в индексе).

# Полный индекс паттернов, опубликованный издателем: конверт лицензии, ответ
# `{"sha256","generated_at","patterns","index":{…}}` + `ETag: "<sha>"`; `If-None-Match`
# с тем же отпечатком → 304; индекса нет → 404 `pattern_index_not_published`.
INDEX_PATH = "/v1/content/patterns/index"
# Индекс — десятки-сотни КБ; качается в фоне, но ждать его вечно незачем.
INDEX_TIMEOUT_SEC = 15.0
# Скачанный индекс сервера в override-корне. Клиентский MCP предпочитает его поставочному
# `dev/patterns_index.json`; формат файла: `{"sha256","generated_at","fetched_at","index"}`.
SERVER_INDEX_NAME = "patterns_index.server.json"
# Поставочный индекс, с которым сверяемся, пока серверного файла нет.
SHIPPED_INDEX_JSON = "patterns_index.json"
# Отпечаток сервера считается по индексу формата 2; поставочный формата 1 заведомо не
# совпадает с сервером (старая поставка) — его отпечаток не считается вовсе.
SHIPPED_INDEX_FORMAT = 2

# Размер страницы. Дефолт сервера — 200, потолок — 500; страницы теперь без тел и лёгкие,
# но дефолт сервера менять незачем.
PAGE_LIMIT = 200
MAX_LIMIT = 500

# --------------------------------------------------------------------------------------
# Раскладка на диске
# --------------------------------------------------------------------------------------
# Маркеры управляемого блока ПРЕЖНЕЙ раскладки (индекс-markdown). Новая версия блок не
# пишет — маркеры нужны только миграции, чтобы его вычистить.
MANAGED_BEGIN = "<!-- BPMKIT-COMPANION-BEGIN -->"
MANAGED_END = "<!-- BPMKIT-COMPANION-END -->"

# Суффикс области. В индексе он отличает межрелизные разделы от поставочных
# (`patterns_js_ui.md` против `patterns_js_ui_updates.md`), а фильтр `area` у читателя
# срабатывает одинаково на обоих. Те же имена прежняя раскладка использовала для файлов
# с телами — по ним миграция и находит, что удалить.
UPDATES_SUFFIX = "_updates"
UPDATES_PREFIX = "patterns_"

DEV_SUBDIR = "dev"
INDEX_NAME = "patterns_index.md"
UPDATES_INDEX_NAME = "patterns_updates_index.json"
INDEX_FORMAT = 1
INDEX_NOTE = "Generated by companion: metadata of inter-release patterns (no bodies)"
# Уровень заголовка раздела в индексе — как у разделов поставочной библиотеки.
SECTION_LEVEL = 3
KEY_PREFIX = "pat:"

# Заголовок созданного с нуля индекса-markdown. Обычный HTML-комментарий: регексы
# читателя его не видят, а человек, открывший файл руками, понимает, откуда он взялся.
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

# Имя области, под которым едет всё, что не удалось привести к безопасному имени.
_FALLBACK_AREA = "other"
# Потолок длины имени области. Имя приходит ИЗ СЕТИ и становится частью имени в индексе
# (и в прежней раскладке — именем файла), поэтому длина ограничена так же жёстко, как набор
# символов.
_MAX_AREA_LEN = 48

# Краткое описание раздела в индексе: одна строка, без кода.
_SNIPPET_MAX = 160
_SNIPPET_FIELDS = ("snippet", "summary", "description")

# Поля записи, которые сохраняются в состоянии. Тела (`body_markdown`) и подписи сюда
# НЕ входят: компаньон их больше не хранит, даже если старый бэкенд их прислал.
_RECORD_KEYS = (
    "id", "title", "version", "min_mcp_version", "area", "proof",
    "pattern_type", "published_at", "updated_at", "status", "section_key", "snippet",
)

# Статусы, при которых запись считается отозванной даже без флага `deleted`.
_REVOKED_STATUSES = frozenset({"revoked", "deleted"})

# Отказы, означающие «нет действующей лицензии» (а не «нет связи»), — для строки окна.
_LICENSE_KINDS = frozenset({"no_license", "invalid_envelope", "signature_invalid",
                            "not_yet_valid", "expired", "revoked"})

# Ограда блока кода: до трёх пробелов отступа, затем 3+ символа ` или ~.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_AREA_BAD_RE = re.compile(r"[^a-z0-9_]+")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


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
def _count_shipped_patterns(shipped_root: Any) -> Optional[int]:
    """Сколько файлов лежит в поставочной базе паттернов — честный, но
    ПРИБЛИЖЁННЫЙ счётчик: считает файлы дерева, а не «паттерны» по строгому определению
    читателя (обычно это одно и то же, но не гарантия — среди файлов есть и служебные,
    вроде самого индекса). `None` — корень не задан или не читается: счётчик тогда просто
    НЕ показывается (см. `state.py::CompanionState.summary`), а не врёт нулём — ноль
    читался бы как «поставочная база пуста», а не как «не смогли посчитать».
    """
    text = str(shipped_root or "").strip()
    if not text:
        return None
    src = Path(text)
    if not src.is_dir():
        return None
    try:
        return sum(1 for item in src.rglob("*") if item.is_file())
    except OSError:
        return None


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

    Ключ `shipped_count` результата — размер поставочной базы, посчитанный ЗДЕСЬ
    и ВСЕГДА, независимо от `skipped`: пропуск seed'а (обычный тик, индекс уже валиден) не
    должен означать «размер неизвестен» — иначе состояние канала теряло бы счётчик на
    каждом тике, кроме самого первого.
    """
    override = Path(override_root)
    index = override / DEV_SUBDIR / INDEX_NAME
    shipped_count = _count_shipped_patterns(shipped_root)
    if index.is_file() and not force:
        return {"copied": 0, "skipped": True, "root": str(override),
                "shipped_count": shipped_count}

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

    return {"copied": copied, "skipped": False, "root": str(override),
            "shipped_count": shipped_count}


# --------------------------------------------------------------------------------------
# Миграция прежней раскладки
# --------------------------------------------------------------------------------------
def _strip_managed_block(text: str) -> str:
    """Убрать управляемый блок прежней версии, не тронув НИЧЕГО за маркерами.

    Текст вне маркеров — рукописный индекс издателя: голова и хвост берутся срезами
    исходной строки. Пустые строки, которыми прежняя версия отделяла блок от текста,
    подрезаются, чтобы файл вернулся к виду «до канала», а не копил переводы строк.
    """
    begin = text.find(MANAGED_BEGIN)
    end = text.find(MANAGED_END)
    if begin == -1 or end == -1 or end <= begin:
        return text
    head = text[:begin]
    tail = text[end + len(MANAGED_END):]
    # Файл мог быть сохранён с CRLF (правка руками на Windows) — переводы строк
    # подрезаются в обоих видах, а дописывается тот, что уже есть в файле.
    newline = "\r\n" if "\r\n" in text else "\n"
    if not tail.strip("\r\n"):
        stripped = head.rstrip("\r\n")
        return stripped + newline if stripped else ""
    return head + tail.lstrip("\r\n")


def migrate_legacy(override_root: Any) -> dict:
    """Убрать из override-корня всё, что писала прежняя версия канала.

    Прежняя раскладка клала тела межрелизных паттернов в `dev/patterns_<area>_updates.md`
    и вживляла управляемый блок в `dev/patterns_index.md`. Теперь тела выдаются онлайн,
    и эти файлы — устаревшие копии, которые к тому же расходились бы с сервером после
    первого же отзыва. Удаление — штатная часть перехода.

    Маска ловит ТОЛЬКО имена канала (`patterns_*_updates.md`): поставочный
    `patterns_js_ui.md` под неё не попадает. Операция идемпотентна — на чистом корне ничего
    не делает и файлы не трогает.
    """
    dev = Path(override_root) / DEV_SUBDIR
    removed: list = []
    if dev.is_dir():
        for path in sorted(dev.glob(f"{UPDATES_PREFIX}*{UPDATES_SUFFIX}.md")):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ChannelError(f"Не удалось удалить {path}", kind="local_io",
                                   detail=str(exc)) from None
            removed.append(str(path))

    index_cleaned = False
    index = dev / INDEX_NAME
    if index.is_file():
        text = _read_text(index)
        if MANAGED_BEGIN in text:
            cleaned = _strip_managed_block(text)
            if not cleaned.strip():
                # Индекс состоял из одного блока (корень создан каналом с нуля): пустым
                # оставлять нельзя — читатель признаёт корень только при живом индексе.
                cleaned = _EMPTY_INDEX
            if cleaned != text:
                _write_text(index, cleaned)
                index_cleaned = True
    return {"files_removed": removed, "index_cleaned": index_cleaned}


# --------------------------------------------------------------------------------------
# Индекс метаданных
# --------------------------------------------------------------------------------------
def _text_of(value: Any) -> str:
    """Однострочная нормализация текста из сети: без переводов строк и лишних пробелов."""
    return " ".join(str(value if value is not None else "").split())


def _heading(rec: dict) -> str:
    return _text_of(rec.get("title")) or f"Паттерн #{rec.get('id')}"


def _snippet_of(item: dict) -> str:
    """Краткое описание раздела из МЕТАДАННЫХ записи: одна строка, без кода, ≤160 символов.

    Тело паттерна для описания не используется намеренно — даже если старый бэкенд его
    прислал: тело компаньону больше не принадлежит. Нет описания в метаданных — пустая
    строка (читатель индекса это допускает).
    """
    for field in _SNIPPET_FIELDS:
        raw = item.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        lines = []
        for line in raw.splitlines():
            if _FENCE_RE.match(line):
                break  # всё, что ниже ограды кода, в описание не идёт
            lines.append(line)
        text = _text_of(" ".join(lines).replace("**", "").replace("`", ""))
        if not text:
            continue
        if len(text) > _SNIPPET_MAX:
            text = text[:_SNIPPET_MAX - 1].rstrip() + "…"
        return text
    return ""


def _section_key(rec: dict) -> str:
    key = _text_of(rec.get("section_key"))
    return key if key.startswith(KEY_PREFIX) else f"{KEY_PREFIX}{rec.get('id')}"


def build_index(applied: list) -> dict:
    """Записи состояния → объект индекса в формате поставочного `patterns_index.json`.

    Порядок групп и разделов не зависит от порядка приезда страниц — иначе файл
    «дёргался» бы на ровном месте и перезаписывался без изменения содержания.
    """
    groups: dict = {}
    for rec in applied or []:
        groups.setdefault(sanitize_area(rec.get("area")), []).append(rec)
    files = []
    for area in sorted(groups):
        records = sorted(groups[area],
                         key=lambda r: (_as_int(r.get("id")) or 0, _heading(r)))
        files.append({
            "file": _updates_rel(area),
            "area": f"{area}{UPDATES_SUFFIX}",
            "sections": [{"heading": _heading(rec), "level": SECTION_LEVEL,
                          "snippet": str(rec.get("snippet") or ""),
                          "key": _section_key(rec)} for rec in records],
        })
    return {"format": INDEX_FORMAT, "note": INDEX_NOTE, "files": files}


def render(applied: list, override_root: Any) -> dict:
    """Перерисовать индекс метаданных из состояния (и вычистить прежнюю раскладку).

    Полная перерисовка, а не инкремент: только так отзыв паттерна и откат к снимку
    получаются корректными по построению. Файл пишется атомарно и только при реальном
    изменении содержимого.
    """
    root = Path(override_root)
    dev = root / DEV_SUBDIR
    try:
        dev.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(f"Не удалось создать каталог {dev}", kind="local_io",
                           detail=str(exc)) from None

    legacy = migrate_legacy(root)
    index = build_index(applied)
    written: list = []
    path = dev / UPDATES_INDEX_NAME
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    if _write_if_changed(path, text):
        written.append(str(path))
    if legacy["index_cleaned"]:
        written.append(str(dev / INDEX_NAME))
    return {
        "files_written": written,
        "files_removed": list(legacy["files_removed"]),
        "areas": {entry["area"][:-len(UPDATES_SUFFIX)]: len(entry["sections"])
                  for entry in index["files"]},
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
    """Запись сервера → запись состояния: только метаданные, id — целым.

    Тело (`body_markdown`), подписи и прочие поля отбрасываются намеренно: тело выдаётся
    онлайн и компаньону не принадлежит, даже если его прислал старый бэкенд; а
    неограниченный рост состояния от новых полей сервера — это рост файла на диске.
    """
    rec = {key: item.get(key) for key in _RECORD_KEYS}
    rec["id"] = _as_int(item.get("id"))
    rec["title"] = str(item.get("title") or "")
    rec["area"] = str(item.get("area") or "")
    rec["snippet"] = _snippet_of(item)
    rec["section_key"] = _section_key(rec)
    return rec


def _metadata_only(rec: dict) -> dict:
    """Запись из состояния прежней версии (с телом) → только метаданные.

    Прежняя версия хранила в состоянии полные тела; при первом проходе новой они
    вычищаются и из состояния, а не только с диска.
    """
    if not isinstance(rec, dict):
        return rec
    clean = {key: rec.get(key) for key in _RECORD_KEYS}
    if not clean.get("snippet"):
        clean["snippet"] = _snippet_of(rec)
    clean["section_key"] = _section_key(clean)
    return clean


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
    последней.
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


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChannelError("Счётчик паттернов на сервере не разобран", kind="bad_response",
                           detail=f"поле {key}: {value!r}")
    return value


def fetch_stats(client: "BackendClient", *, timeout: float = STATS_TIMEOUT_SEC) -> dict:
    """Сколько паттернов в библиотеке на сервере и когда она обновлялась.

    Справочный запрос для окна «Обновления»: курсор, индекс и состояние НЕ трогаются.
    Таймаут короткий — строка в окне не стоит того, чтобы ждать сеть. Отказ поднимается
    наверх типизированным: что из него показать, решает `stats_from_error`.
    """
    if hasattr(client, "has_envelope") and not client.has_envelope:
        raise ChannelError("Лицензионный ключ не найден на этой машине", kind="no_license")
    try:
        payload, _headers = client.get_json(STATS_PATH, timeout=timeout)
    except TypeError:
        # Подставной клиент без параметра таймаута (тесты, старые обёртки).
        payload, _headers = client.get_json(STATS_PATH)
    if not isinstance(payload, dict):
        raise ChannelError("Ответ счётчика паттернов не похож на объект",
                           kind="bad_response",
                           detail=f"тип тела: {type(payload).__name__}")
    sections = _int_field(payload, "sections")
    updates = _int_field(payload, "updates")
    updated_at = payload.get("updated_at")
    # Новые поля (с бэкенда, опубликовавшего индекс): число паттернов в индексе и его
    # отпечаток. Старый бэкенд их не присылает — счётчик тогда по прежней формуле, а
    # сверка индекса считает его «не опубликованным».
    count = payload.get("patterns")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    server_sha = payload.get("index_sha256")
    server_sha = (server_sha.strip().lower()
                  if isinstance(server_sha, str) and server_sha.strip() else None)
    from .state import utc_now_iso  # локально: state.py сам импортирует patterns
    return {
        "status": "ok",
        "sections": sections,
        "updates": updates,
        "patterns": count,
        # Счётчик окна — число паттернов индекса плюс межрелизные (`updates`); без
        # `patterns` — прежняя формула `sections + updates`.
        "total": (count + updates) if count is not None else sections + updates,
        "index_sha256": server_sha,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        "checked_at": utc_now_iso(),
        "detail": "",
    }


def stats_from_error(exc: BaseException) -> dict:
    """Отказ запроса счётчика → запись для окна: «лицензия не активна» или «нет связи».

    Всё, что не про лицензию, для человека означает одно — сервер сейчас не ответил
    (сеть, таймаут, ошибка бэкенда, непонятный ответ); подробность остаётся в `detail`.
    """
    from .state import utc_now_iso  # локально: state.py сам импортирует patterns
    kind = str(getattr(exc, "kind", "") or "")
    status = "no_license" if kind in _LICENSE_KINDS else "offline"
    detail = str(exc)[:200]
    return {"status": status, "kind": kind or "unknown", "detail": detail,
            "checked_at": utc_now_iso()}


def _plural_patterns(count: int) -> str:
    n = abs(int(count))
    tens = n % 100
    ones = n % 10
    if 11 <= tens <= 14:
        word = "паттернов"
    elif ones == 1:
        word = "паттерн"
    elif 2 <= ones <= 4:
        word = "паттерна"
    else:
        word = "паттернов"
    return f"{n} {word}"


def _date_ru(value: Any) -> str:
    """ISO-метка → `ДД.ММ.ГГГГ` (дата как есть в метке, без пересчёта зоны)."""
    match = _ISO_DATE_RE.match(str(value or "").strip())
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


def stats_line(stats: Optional[dict]) -> str:
    """Строка пункта «Паттерны» в окне «Обновления».

    `ok` — «Паттерны: N паттернов на сервере, библиотека обновлена ДД.ММ.ГГГГ, доступ по
    лицензии» (N = `patterns` индекса + `updates`; у старого бэкенда без `patterns` —
    sections + updates; одно число); нет лицензии — «доступ по
    лицензии: лицензия не активна»; сеть/ошибка — «нет связи с сервером».
    """
    stats = stats if isinstance(stats, dict) else {}
    status = stats.get("status")
    if status == "ok":
        total = int(stats.get("total") if stats.get("total") is not None
                    else int(stats.get("sections") or 0) + int(stats.get("updates") or 0))
        parts = [f"Паттерны: {_plural_patterns(total)} на сервере"]
        date = _date_ru(stats.get("updated_at"))
        if date:
            parts.append(f"библиотека обновлена {date}")
        parts.append("доступ по лицензии")
        return ", ".join(parts)
    if status == "no_license":
        return "Паттерны: доступ по лицензии: лицензия не активна"
    if status == "offline":
        return "Паттерны: нет связи с сервером"
    if status == "disabled":
        return "Паттерны: канал обновлений выключен в настройках"
    return "Паттерны: проверяем сервер…"


# --------------------------------------------------------------------------------------
# Сверка индекса паттернов с сервером (автообновление)
# --------------------------------------------------------------------------------------
# Статусы сверки — машинные значения `state.patterns["index_sync"]["status"]`, окно
# «Обновления» рисует по ним чип пункта «Паттерны». «Актуален» (`ok`) — ТОЛЬКО когда
# отпечаток индекса у клиента совпал с отпечатком на сервере.
INDEX_STATUS_TITLES = {
    "ok": "актуален",
    "stale": "устарел",
    "offline": "нет связи с сервером",
    "no_license": "лицензия не активна",
    "not_published": "индекс на сервере не опубликован",
    "disabled": "канал обновлений выключен",
    "never": "ещё не проверялся",
}


def index_sha256(index: Any) -> str:
    """Отпечаток индекса паттернов — ФОРМУЛА КОНТРАКТА, общая с бэкендом и клиентом MCP.

    `sha256(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    .encode("utf-8"))`. Любое отклонение (пробел, порядок ключей, `\\uXXXX` вместо
    кириллицы) дало бы «не совпадает» навсегда и бесконечную перекачку индекса.
    """
    blob = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_json_file(path: Path) -> Any:
    """JSON-файл или `None` (нет файла, не читается, не JSON) — без исключений: битый
    локальный индекс означает лишь «не совпадает», и лечится скачиванием с сервера."""
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _server_index_sha(override_root: Any) -> Optional[str]:
    """Отпечаток `dev/patterns_index.server.json`, ПЕРЕСЧИТАННЫЙ по его `index`.

    Записанному в файле `sha256` не верим: файл могли поправить руками или он мог
    обрезаться. Если записанный и пересчитанный расходятся — файл считается битым
    (`None`), и сверка скачает индекс заново.
    """
    text = str(override_root or "").strip()
    if not text:
        return None
    data = _load_json_file(Path(text) / DEV_SUBDIR / SERVER_INDEX_NAME)
    if not isinstance(data, dict) or not isinstance(data.get("index"), dict):
        return None
    actual = index_sha256(data["index"])
    stored = data.get("sha256")
    if isinstance(stored, str) and stored.strip() and stored.strip().lower() != actual:
        return None
    return actual


def _shipped_index_sha(shipped_root: Any) -> tuple:
    """Отпечаток поставочного `dev/patterns_index.json` → `(sha|None, source)`.

    Только формат 2: формат 1 — старая поставка, с сервером он не совпадёт по
    определению, поэтому и не считается (`source = "shipped_format1"`).
    """
    text = str(shipped_root or "").strip()
    if not text:
        return None, "none"
    data = _load_json_file(Path(text) / DEV_SUBDIR / SHIPPED_INDEX_JSON)
    if not isinstance(data, dict):
        return None, "none"
    if data.get("format") != SHIPPED_INDEX_FORMAT:
        return None, "shipped_format1"
    return index_sha256(data), "shipped"


def local_index_sha(override_root: Any, shipped_root: Any) -> tuple:
    """Локальный отпечаток индекса клиента → `(sha|None, source)`.

    Порядок — тот же, в котором индекс выбирает клиентский MCP: сначала скачанный индекс
    сервера (`source="server"`), если его нет или он битый — поставочный формата 2
    (`"shipped"`); поставочный формата 1 и отсутствие индекса — `None` («не совпадает»).
    """
    sha = _server_index_sha(override_root)
    if sha:
        return sha, "server"
    return _shipped_index_sha(shipped_root)


def _etag_sha(value: Any) -> Optional[str]:
    """`ETag: "<sha>"` → sha (без `W/` и кавычек); всё, что не похоже на hex-64, — None."""
    text = str(value or "").strip()
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip().strip('"').strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def fetch_index(client: "BackendClient", *, if_none_match: Optional[str] = None,
                timeout: float = INDEX_TIMEOUT_SEC) -> Optional[dict]:
    """Скачать опубликованный индекс паттернов и ПРОВЕРИТЬ его отпечаток.

    `if_none_match` — локальный отпечаток (без кавычек — их добавит функция): совпадение на
    сервере даёт `304`, и функция возвращает `None` («у вас уже этот индекс»). Иначе —
    `{"sha256","generated_at","patterns","index"}`, где `sha256` ПЕРЕСЧИТАН по `index`.

    Пересчитанный отпечаток обязан совпасть с `sha256` ответа (и с `ETag`, если он похож на
    отпечаток); расхождение — `integrity_mismatch`: файл НЕ пишется, это порча в канале, а
    не новый индекс. Как и `bundle_sha256`, это ЦЕЛОСТНОСТЬ, а не подлинность.
    404 — `pattern_index_not_published` (издатель ещё не выложил индекс).
    """
    if hasattr(client, "has_envelope") and not client.has_envelope:
        raise ChannelError("Лицензионный ключ не найден на этой машине", kind="no_license")
    etag = None
    if if_none_match:
        tag = str(if_none_match).strip()
        etag = tag if tag.startswith('"') else f'"{tag}"'
    try:
        payload, headers = client.get_json(INDEX_PATH, etag=etag, timeout=timeout)
    except NotModified:
        return None
    except ChannelError as exc:
        if exc.http_status == 404 or exc.kind == "pattern_index_not_published":
            raise ChannelError("Индекс паттернов на сервере не опубликован",
                               kind="pattern_index_not_published", http_status=404,
                               detail=exc.detail) from None
        raise
    if not isinstance(payload, dict) or not isinstance(payload.get("index"), dict):
        raise ChannelError("Ответ индекса паттернов не похож на ожидаемый",
                           kind="bad_response",
                           detail="нет объекта index в ответе")
    actual = index_sha256(payload["index"])
    claimed = payload.get("sha256")
    claimed = claimed.strip().lower() if isinstance(claimed, str) else ""
    if claimed != actual:
        raise ChannelError(
            "Отпечаток индекса паттернов не сошёлся с содержимым — индекс не записан",
            kind="integrity_mismatch",
            detail=f"в ответе {claimed[:16] or '—'}…, посчитано {actual[:16]}…")
    etag_sha = _etag_sha((headers or {}).get("etag") if isinstance(headers, dict) else None)
    if etag_sha is not None and etag_sha != actual:
        raise ChannelError(
            "ETag индекса паттернов не совпал с содержимым — индекс не записан",
            kind="integrity_mismatch",
            detail=f"ETag {etag_sha[:16]}…, посчитано {actual[:16]}…")
    count = payload.get("patterns")
    generated_at = payload.get("generated_at")
    return {
        "sha256": actual,
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "patterns": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "index": payload["index"],
    }


def write_server_index(override_root: Any, fetched: dict, *, fetched_at: str) -> Path:
    """Записать индекс сервера в `<override>/dev/patterns_index.server.json` атомарно.

    Отпечаток пересчитывается ЕЩЁ РАЗ прямо перед записью: файл, который клиентский MCP
    предпочтёт поставочному индексу, не имеет права разойтись со своим `sha256`. Запись —
    tmp + replace (`fsutil.atomic_write_text`), UTF-8, `ensure_ascii=False`: читатель
    никогда не увидит половину файла.
    """
    index = fetched.get("index") if isinstance(fetched, dict) else None
    if not isinstance(index, dict):
        raise ChannelError("Нечего записывать: индекс не получен", kind="bad_response")
    actual = index_sha256(index)
    if actual != str(fetched.get("sha256") or "").strip().lower():
        raise ChannelError("Отпечаток индекса не сошёлся перед записью — индекс не записан",
                           kind="integrity_mismatch",
                           detail=f"посчитано {actual[:16]}…")
    path = Path(override_root) / DEV_SUBDIR / SERVER_INDEX_NAME
    doc = {"sha256": actual, "generated_at": fetched.get("generated_at"),
           "fetched_at": fetched_at, "index": index}
    _write_text(path, json.dumps(doc, ensure_ascii=False) + "\n")
    return path


def _index_status_of_kind(kind: str) -> str:
    if kind in _LICENSE_KINDS:
        return "no_license"
    if kind == "offline":
        return "offline"
    if kind == "pattern_index_not_published":
        return "not_published"
    if kind == "disabled":
        return "disabled"
    return "stale"


def index_status_from_error(exc: BaseException, previous: Optional[dict] = None) -> dict:
    """Отказ сверки → блок статуса: лицензия / нет связи / не опубликован / устарел.

    Всё, что не про лицензию, сеть и неопубликованный индекс (порча отпечатка, файловая
    ошибка, непонятный ответ), — «устарел» с причиной в `detail`: локальный индекс мог
    остаться прежним, и честно сказать «актуален» уже нельзя.
    """
    from .state import utc_now_iso  # локально: state.py сам импортирует patterns
    prev = previous if isinstance(previous, dict) else {}
    kind = str(getattr(exc, "kind", "") or "")
    return {
        "status": _index_status_of_kind(kind),
        "kind": kind or "unknown",
        "detail": str(exc)[:200],
        "local_sha": prev.get("local_sha"),
        "local_source": prev.get("local_source"),
        "server_sha": prev.get("server_sha"),
        "last_check_at": utc_now_iso(),
        "last_fetch_at": prev.get("last_fetch_at"),
        "generated_at": prev.get("generated_at"),
    }


def reconcile_index(client: "BackendClient", ctx: "LicenseContext", stats: Optional[dict],
                    *, previous: Optional[dict] = None) -> dict:
    """Сверка индекса паттернов клиента с сервером и докачка при расхождении.

    Вход — уже полученный ответ `fetch_stats` (или запись отказа `stats_from_error`):
    сверка не делает второго запроса счётчика. Логика:

    * отказ счётчика → тот же статус (`offline`/`no_license`/`disabled`);
    * `index_sha256 == null` → `not_published` (НЕ «актуален»: сверять не с чем);
    * отпечаток сервера == локальному → `ok` без скачивания;
    * иначе `fetch_index(if_none_match=<локальный>)` → проверка → атомарная запись
      `dev/patterns_index.server.json` → `ok`; `304` — тоже `ok` (у нас тот же индекс).

    Исключения наружу НЕ уходят: это фоновая справка окна и попутный шаг тика, результат —
    блок для `state.patterns["index_sync"]` с полями `status`, `detail`, `local_sha`,
    `local_source`, `server_sha`, `last_check_at`, `last_fetch_at`, `generated_at`.
    """
    from .state import utc_now_iso  # локально: state.py сам импортирует patterns
    prev = previous if isinstance(previous, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    override_root = str(getattr(ctx, "override_patterns_root", "") or "").strip()
    shipped_root = str(getattr(ctx, "shipped_patterns_root", "") or "").strip()
    now = utc_now_iso()
    try:
        local_sha, local_source = local_index_sha(override_root, shipped_root)
    except Exception:  # noqa: BLE001 - локальный отпечаток best-effort
        local_sha, local_source = None, "none"
    block = {
        "status": "never",
        "kind": "",
        "detail": "",
        "local_sha": local_sha,
        "local_source": local_source,
        "server_sha": prev.get("server_sha"),
        "last_check_at": now,
        "last_fetch_at": prev.get("last_fetch_at"),
        "generated_at": prev.get("generated_at"),
    }
    stats_status = stats.get("status")
    if stats_status != "ok":
        block["status"] = (stats_status if stats_status in ("offline", "no_license",
                                                            "disabled") else "offline")
        block["kind"] = str(stats.get("kind") or stats_status or "")
        block["detail"] = str(stats.get("detail") or "")
        return block

    server_sha = stats.get("index_sha256")
    block["server_sha"] = server_sha
    if not server_sha:
        block["status"] = "not_published"
        block["detail"] = "издатель ещё не опубликовал индекс паттернов на сервере"
        return block
    if local_sha == server_sha:
        block["status"] = "ok"
        return block

    try:
        if not override_root:
            raise ChannelError("Клиентский MCP не сообщил override-корень базы паттернов",
                               kind="local_io",
                               detail="пустой override_patterns_root в лицензионном контексте")
        fetched = fetch_index(client, if_none_match=local_sha)
        if fetched is None:
            # 304: на сервере ровно наш индекс (счётчик успел устареть).
            block["status"] = "ok"
            block["server_sha"] = local_sha
            return block
        # Корень обязан быть валидным для читателя: без seed'а override-корень, в котором
        # лежит только индекс сервера, заменил бы поставку пустотой.
        seed_override_root(shipped_root, override_root)
        write_server_index(override_root, fetched, fetched_at=now)
    except Exception as exc:  # noqa: BLE001 - сверка не роняет ни тик, ни окно
        failed = index_status_from_error(exc, block)
        failed["last_check_at"] = now
        if failed["status"] == "stale" and not failed["detail"]:
            failed["detail"] = "индекс у клиента не совпадает с сервером"
        return failed
    block.update({"status": "ok", "local_sha": fetched["sha256"], "local_source": "server",
                  "server_sha": fetched["sha256"], "last_fetch_at": now,
                  "generated_at": fetched.get("generated_at")})
    return block


def sync(client: "BackendClient", state: "CompanionState", ctx: "LicenseContext",
         settings: Any, *, max_pages: int = 200) -> dict:
    """Полный проход канала паттернов: seed → миграция → пагинация → индекс → состояние.

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
    seed = seed_override_root(shipped_root, override_root)
    # Прежняя раскладка (тела на диске) вычищается ДО сети: переход на онлайн-выдачу не
    # должен ждать первого удачного ответа сервера.
    legacy = migrate_legacy(override_root)
    # сохраняем размер поставочной базы, только если он посчитан честно —
    # `None` (корень не задан/не читается) не должен затирать ранее известное значение.
    if seed.get("shipped_count") is not None:
        block["shipped_count"] = seed["shipped_count"]

    # Курсор берём из состояния и двигаем ЛОКАЛЬНО: в состояние он попадёт только после
    # успешного прохода — иначе отброшенная по целостности страница «съела» бы дельту.
    cursor_since = block.get("since")
    cursor_id = block.get("since_id")

    current: dict = {}
    for rec in block.get("applied") or []:
        rid = _as_int(rec.get("id")) if isinstance(rec, dict) else None
        if rid is not None:
            current[rid] = _metadata_only(rec)

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

            # Тело (если старый бэкенд его прислал) отбрасывается здесь же: в состояние и
            # на диск попадают только метаданные. Пустое тело — норма онлайн-выдачи.
            rec = _normalize(item)
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
    # «последний тик реально что-то поменял в индексе»; пустая дельта сбрасывает флаг.
    block["had_new_last_run"] = bool(applied_count or removed_count)
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
        "files_removed": list(legacy["files_removed"]) + files["files_removed"],
        "legacy_index_cleaned": bool(legacy["index_cleaned"]),
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
