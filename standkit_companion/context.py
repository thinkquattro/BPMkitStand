# -*- coding: utf-8 -*-
"""Лицензионный контекст — берётся у CLI самого MCP, а не вычисляется здесь.

Почему так. Companion — ОТДЕЛЬНЫЙ процесс, и соблазн «прочитать конверт из secretstore и
проверить подпись» здесь был бы третьей по счёту копией лицензионной логики: она уже есть
в клиентском MCP (`bpmkit/licensing.py`) и на бэкенде издателя. Три копии расходятся не
«если», а «когда»: одна начинает считать лицензию годной, когда две другие — нет, и
пользователь получает канал, который то работает, то нет. Поэтому канал спрашивает
ГОТОВЫЙ ответ у того, кто за него отвечает::

    <mcp_cli> setup companion-context --json

и получает на stdout один JSON: конверт, статус лицензии, адрес бэкенда, версию MCP и пути
поставки (корни паттернов, цель для файла отзыва, путь бинаря). Отсутствие лицензии — тоже
ШТАТНЫЙ ответ (`{"ok": false, "error": "no_license"}` при rc=0), а не сбой запуска.

Два отказа, которые ОБЯЗАНЫ различаться, потому что чинятся по-разному:

* **нет CLI рядом** (`ContextUnavailable`) — MCP не установлен или путь к нему не задан.
  Чинит человек в настройках хаба (`companion.mcp_cli`) либо переменной окружения
  `BPMKIT_CLI` (полный порядок резолва — в докстринге `find_cli` ниже и в общем
  хелпере `standkit.cli_resolve`, включая фолбэк на запуск из исходников);
* **нет лицензии** (`ChannelError(kind="no_license")`) — MCP на месте и честно ответил, что
  ключа на этой машине нет. Чинится покупкой/установкой ключа, к путям отношения не имеет.

Запуск ИЗ САМОГО CLI. Подкоманды `setup update-check`/`apply-update`/`rollback`
CLI BPMkit поднимают этот же пакет внутри СВОЕГО процесса. Спрашивать контекст у
`BPMkit.exe` ещё одним процессом там бессмысленно и опасно: на VM приёмки холодный старт
onefile-exe — 14–25 с без нагрузки и больше прежнего 30-секундного предела под нагрузкой,
и `apply-update` отвечал `context_unavailable` через 126 с. Поэтому CLI регистрирует
ПРОВАЙДЕРА (`set_inprocess_provider`) — функцию, отдающую тот же JSON-словарь, что
печатает `companion-context`, — и `resolve` берёт ответ у него, не запуская процесс.
Логика лицензии при этом по-прежнему живёт в BPMkit: пакет получает готовый ответ, просто
без промежуточного процесса.

Запуск со стороны (хаб) — через `standkit.platform.run_console_tree`: по тайм-ауту умирает
ВСЁ дерево CLI (у PyInstaller-сборки работающий процесс — ребёнок загрузчика, и
`subprocess.run` после убийства одного загрузчика ждал пайпы ребёнка без предела), а сам
тайм-аут называется в тексте отказа.

Кэш. Контекст спрашивается на КАЖДЫЙ тик трёх циклов, а запуск процесса стоит десятки
миллисекунд и на Windows ещё и рискует мигнуть консольным окном. Поэтому ответ живёт в
памяти `cache_ttl` секунд. На диск он не попадает никогда: в нём лицензионный конверт.
По той же причине ни конверт, ни сырой stdout не попадают в текст ошибок — только stderr
и коды.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from standkit.cli_resolve import (
    CLI_ENV_VAR,
    candidate_roots as _shared_candidate_roots,
    describe_search_targets,
    resolve_command_string,
    search_roots,
)
from standkit.platform import (  # noqa: F401 - run_console: подмена в тестах
    independent_child_env,
    run_console,
    run_console_tree,
)

from .errors import KIND_TITLES, ChannelError, ContextUnavailable

__all__ = [
    "CONTEXT_ARGV_TAIL",
    "LicenseContext",
    "find_cli",
    "resolve",
    "invalidate_cache",
    "set_inprocess_provider",
]

# Подкоманда MCP, отдающая контекст. Держится константой: это контракт с чужим пакетом,
# и менять его «по месту» в трёх строках нельзя.
CONTEXT_ARGV_TAIL = ("setup", "companion-context", "--json")

# Резолв CLI (имена бинаря, подпути, глубина подъёма, фолбэк на запуск из исходников,
# переменная окружения) — общий хелпер `standkit.cli_resolve`: та же логика
# нужна экрану лицензии хаба (`standkit_hub.license_api`), и держать её в двух местах
# «дословно одинаковой» руками — значит рано или поздно её рассинхронизировать.

# Сколько живёт процесс CLI, прежде чем его дерево убивается как зависшее. Прежних 30 с не
# хватило на VM приёмки 13.10.2026 (холодный старт onefile-exe под антивирусом 14–25 с без
# нагрузки), а 90 с — на медленном госте 24.10.2026: `stage-skills` отвечал
# `context_unavailable` «не ответил за 90 с». 240 с — запас на самый медленный гость и всё
# ещё конечный предел: зависший CLI не живёт вечно.
_CLI_TIMEOUT_S = 240.0

# Сколько ЖДЁТ вызывающий (запрос хаба, тик цикла) ответа CLI, прежде чем ответить «идёт
# подготовка, повторите». Процесс при этом НЕ убивается: он доживает свои `_CLI_TIMEOUT_S`
# в фоне и кладёт готовый контекст в кэш, а повторный запрос присоединяется к нему, а не
# запускает второй exe. Меньше минуты: запрос хаба не должен висеть дольше, чем терпит
# клиент HTTP.
_CLI_WAIT_S = 45.0

# Код возврата `_default_run` для тайм-аута — отдельный от «упал/не запустился» (-1):
# тайм-аут чинится иначе (повторить позже, проверить антивирус), и текст отказа его называет.
RC_TIMEOUT = -2

# Штатный рубильник PyInstaller ≥ 6.9: потомок замороженного процесса стартует как
# самостоятельное приложение, а не как «часть» вызывающего (его переменные `_PYI_*`
# не наследуются в смысле загрузчика). Ставится, только когда вызывающий сам frozen.
_PYI_RESET_ENV = "PYINSTALLER_RESET_ENVIRONMENT"

# Провайдер контекста в том же процессе: `() -> dict` той же формы, что JSON
# `companion-context`. Регистрирует CLI BPMkit, когда сам запускает канал.
_inprocess_provider: Optional[Callable[[], dict]] = None
_INPROCESS_CLI_KEY = ("<in-process>",)

_DETAIL_LIMIT = 300

# Кэш: ключ — (argv, backend_url), значение — (момент истечения, контекст). Модульный,
# потому что тик каждого цикла создаёт свои объекты, а платить за запуск процесса трижды
# подряд смысла нет.
_cache: dict = {}
_cache_lock = threading.Lock()


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass(frozen=True, repr=False)
class LicenseContext:
    """Ответ CLI в типизированном виде.

    `repr` переопределён и конверт НЕ печатает. Это не косметика: контекст естественно
    оказывается в отладочном выводе, в тексте `assert`, в трассировке — и любой из этих
    путей утащил бы лицензионный ключ в лог-файл хаба.
    """

    envelope: str
    license_status: str
    backend_url: str
    mcp_version: str
    package_root: str
    shipped_patterns_root: str
    override_patterns_root: str
    patterns_env_registered: bool
    revocations_target: str
    revocations_env_registered: bool
    artifact_pubkey: str
    binary_path: str
    cli: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (f"LicenseContext(license_status={self.license_status!r}, "
                f"backend_url={self.backend_url!r}, mcp_version={self.mcp_version!r}, "
                f"envelope=<скрыт>)")

    @property
    def has_envelope(self) -> bool:
        return bool(self.envelope)


# ------------------------------------------------------------------------------------
# Поиск CLI
# ------------------------------------------------------------------------------------
def _candidate_roots(extra_roots: Optional[Sequence] = None) -> list:
    """Корни, в которых имеет смысл искать CLI: сначала явно переданные (тесты, будущие
    настройки), затем каталоги вверх от самого пакета.

    Тонкая обёртка над `standkit.cli_resolve.candidate_roots`, а не прямой вызов на месте
    использования: имя остаётся стабильным для тестов, которые подменяют именно его
    (`monkeypatch.setattr(context_module, "_candidate_roots", ...)`), не заглядывая внутрь
    общего хелпера.
    """
    return _shared_candidate_roots(__file__, extra_roots=extra_roots)


def find_cli(settings, *, extra_roots: Optional[Sequence] = None) -> Optional[list]:
    """argv-префикс для запуска CLI BPMkit или `None`, если его рядом нет.

    Порядок резолва:

    1. Явная настройка `companion.mcp_cli` — её задал человек, она сильнее любого
       автодетекта и не подменяется ничем.
    2. Переменная окружения `BPMKIT_CLI` (тот же формат, что у настройки) — фолбэк для
       машин, где путь неудобно/нельзя прописать в конфиге хаба (запуск MCP из исходников
       на машине издателя, CI, разовая подмена).
    3. Автодетект бинаря (`bpmkit.exe`/`bpmkit`) рядом с поставкой — как было.
    4. Автодетект запуска из исходников: `<root>/server/main.py` (или
       `<root>/BPMkit/server/main.py`) без бинаря — CLI = тот же python, что у хаба, плюс
       путь к `main.py` (см. `standkit.cli_resolve` — там же обоснование каждого шага).

    Угадывания «а вдруг он в PATH» здесь нет намеренно ни на одном из шагов автодетекта:
    `bpmkit` в PATH может оказаться другой сборкой/другой версией, а канал обязан
    спрашивать лицензию у ТОГО MCP, рядом с которым он установлен.
    """
    configured = str(getattr(settings, "mcp_cli", "") or "").strip()
    if configured:
        return resolve_command_string(configured)

    from_env = str(os.environ.get(CLI_ENV_VAR, "") or "").strip()
    if from_env:
        return resolve_command_string(from_env)

    return search_roots(_candidate_roots(extra_roots))


# ------------------------------------------------------------------------------------
# Резолв контекста
# ------------------------------------------------------------------------------------
def set_inprocess_provider(provider: Optional[Callable[[], dict]]):
    """Зарегистрировать провайдера контекста в ЭТОМ процессе; `None` — снять. Возвращает
    прежнего (для восстановления в тестах).

    Зовёт CLI BPMkit, когда сам поднимает канал (`setup update-check`/`apply-update`/
    `rollback`): процесс, который спрашивает контекст, и есть CLI, и запускать его второй
    копией незачем. `provider()` возвращает словарь ровно той формы, что JSON
    `setup companion-context --json`, включая штатные отказы `{"ok": false, ...}`.
    Кэш сбрасывается: ответ провайдера и ответ внешнего CLI — разные источники.
    """
    global _inprocess_provider
    with _cache_lock:
        previous = _inprocess_provider
        _inprocess_provider = provider
        _cache.clear()
    return previous


def _child_env() -> Optional[dict]:
    """Окружение для запуска CLI: `None` (наследовать как есть), а у frozen-вызывающего —
    копия без переменных загрузчика (`_PYI_*`, `_MEIPASS2`) и с
    `PYINSTALLER_RESET_ENVIRONMENT=1`, чтобы бинарь BPMkit стартовал как самостоятельное
    приложение. Общий хелпер — `standkit.platform.independent_child_env`."""
    return independent_child_env()


def _default_run(argv: list) -> tuple:
    """Запуск CLI через ЕДИНУЮ точку `standkit.platform.run_console_tree`.

    Прямой `subprocess.run` здесь запрещён: канал тикает из процесса без своей
    консоли, и каждый тик мигал бы чёрным окном. `run_console_tree`, а не `run_console`:
    по тайм-ауту убивается ВСЁ дерево — у PyInstaller-сборки работающий
    процесс CLI это ребёнок загрузчика, и `subprocess.run` после убийства одного
    загрузчика ждал его пайпы без предела. Тайм-аут — `rc=RC_TIMEOUT`, любое другое
    исключение (нет файла, отказ в доступе) — `rc=-1`: вызывающий разбирает два вида
    отказа, а не зоопарк исключений `subprocess`.
    """
    try:
        proc = run_console_tree(list(argv), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=_CLI_TIMEOUT_S,
                                env=_child_env())
    except subprocess.TimeoutExpired:
        return RC_TIMEOUT, "", ("процесс CLI и его потомки остановлены после "
                                f"{int(_CLI_TIMEOUT_S)} с ожидания")
    except Exception as exc:  # noqa: BLE001 - см. докстринг: один вид отказа наружу
        return -1, "", str(exc)
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def _parse_stdout(stdout: str) -> dict:
    """JSON из stdout CLI.

    Сырой stdout в сообщение об ошибке НЕ попадает ни при каком исходе — в нём конверт.
    Допускается ведущий/хвостовой шум (баннер, предупреждение рантайма): вырезаем участок
    от первой `{` до последней `}`. Это терпимость к чужому выводу, а не к битому JSON —
    неразобранное всё равно даёт `ContextUnavailable`.
    """
    text = (stdout or "").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ContextUnavailable(
                "CLI BPMkit ответил не в формате JSON — контекст лицензии не получен",
                kind="context_unavailable",
                detail="stdout не содержит JSON-объекта",
            ) from None
        try:
            payload = json.loads(text[start:end + 1])
        except ValueError:
            raise ContextUnavailable(
                "CLI BPMkit ответил не в формате JSON — контекст лицензии не получен",
                kind="context_unavailable",
                detail="stdout не разобран как JSON",
            ) from None
    if not isinstance(payload, dict):
        raise ContextUnavailable(
            "CLI BPMkit вернул JSON неожиданной формы — контекст лицензии не получен",
            kind="context_unavailable",
            detail=f"тип ответа: {type(payload).__name__}",
        )
    return payload


def _raise_not_ok(payload: dict) -> None:
    """`ok: false` — это ответ, а не сбой. Разводим «нет лицензии» и всё остальное."""
    error = str(payload.get("error") or "").strip()
    detail = _clip(str(payload.get("detail") or ""))
    if error == "no_license":
        raise ChannelError(
            "Лицензионный ключ BPMkit не найден на этой машине — "
            "канал обновлений издателя бездействует",
            kind="no_license",
            detail=detail,
        )
    kind = error if error in KIND_TITLES else "unknown"
    raise ChannelError(
        "CLI BPMkit отказал в выдаче лицензионного контекста",
        kind=kind,
        detail=detail or _clip(error),
    )


def resolve(settings, *, run: Optional[Callable] = None,
            cache_ttl: float = 300.0) -> LicenseContext:
    """Спросить лицензионный контекст у CLI MCP (с кэшем на `cache_ttl` секунд).

    `run` — точка инъекции для тестов: `run(argv) -> (rc, stdout, stderr)`. Реальный запуск
    идёт через `standkit.platform.run_console_tree` (см. `_default_run`).

    Порядок источников: явный `run` → провайдер в этом процессе (`set_inprocess_provider`,
    канал запущен ИЗ CLI BPMkit) → внешний CLI.
    """
    override_url = str(getattr(settings, "backend_url", "") or "").strip().rstrip("/")
    provider = _inprocess_provider
    if run is None and provider is not None:
        return _resolve_inprocess(provider, override_url, cache_ttl)

    cli = find_cli(settings)
    if not cli:
        raise ContextUnavailable(
            "Рядом не найден CLI BPMkit — укажите путь в «Настройки → Основные», "
            f"поле «CLI BPMkit», либо переменной окружения {CLI_ENV_VAR}",
            kind="context_unavailable",
            detail=describe_search_targets(_candidate_roots()),
        )

    argv = list(cli) + list(CONTEXT_ARGV_TAIL)
    key = (tuple(argv), override_url)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    if run is not None:
        rc, stdout, stderr = run(argv)
        context = _context_from_run(rc, stdout, stderr, cli=list(cli),
                                    override_url=override_url)
        _cache_put(key, context, cache_ttl)
        return context
    return _resolve_via_background_cli(argv, key, list(cli), override_url, cache_ttl)


def _context_from_run(rc: int, stdout: str, stderr: str, *, cli: list,
                      override_url: str) -> LicenseContext:
    """Исход запуска CLI → контекст или типизированный отказ (общая часть для явного `run`
    и фонового запуска)."""
    if rc == RC_TIMEOUT:
        raise ContextUnavailable(
            f"CLI BPMkit не ответил за {int(_CLI_TIMEOUT_S)} с на запрос лицензионного "
            "контекста — повторите позже (холодный старт BPMkit.exe под антивирусом "
            "бывает долгим)",
            kind="context_unavailable",
            detail=_clip(stderr) or "тайм-аут",
        )
    if rc != 0 or not (stdout or "").strip():
        raise ContextUnavailable(
            "CLI BPMkit не ответил на запрос лицензионного контекста",
            kind="context_unavailable",
            detail=_clip(stderr) or f"код возврата {rc}, пустой вывод",
        )

    payload = _parse_stdout(stdout)
    return _context_from_payload(payload, cli=cli, override_url=override_url)


class _CliJob:
    """Один фоновый запуск CLI за контекстом. Исход — `context` либо `error`."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.started = time.monotonic()
        self.done = threading.Event()
        self.context: Optional[LicenseContext] = None
        self.error: Optional[BaseException] = None


# Запущенные, ещё не завершённые запуски CLI: ключ кэша → задание. Повторный запрос,
# пришедший пока exe поднимается, присоединяется к нему, а не запускает второй процесс.
_inflight: dict = {}
# Поколение кэша: `invalidate_cache` увеличивает его, и результат задания прежнего
# поколения (ключ введён/сменён, пока exe стартовал) в кэш не ложится.
_generation = 0


def _resolve_via_background_cli(argv: list, key, cli: list, override_url: str,
                                cache_ttl: float) -> LicenseContext:
    """Запуск внешнего CLI в фоновом потоке с ограниченным ожиданием.

    Ждём `_CLI_WAIT_S`. Ответил — обычный путь. Не ответил (холодный старт на медленном
    госте) — `ContextUnavailable(kind="context_starting")`: процесс продолжает работать до
    `_CLI_TIMEOUT_S`, готовый контекст сам ляжет в кэш, и следующий запрос его получит.
    """
    with _cache_lock:
        job = _inflight.get(key)
        if job is None or job.done.is_set():
            job = _CliJob(_generation)
            _inflight[key] = job
            thread = threading.Thread(
                target=_run_cli_job, name="standkit-context-cli", daemon=True,
                args=(job, argv, key, cli, override_url, cache_ttl))
            thread.start()
    if not job.done.wait(_CLI_WAIT_S):
        waited = int(time.monotonic() - job.started)
        raise ContextUnavailable(
            f"BPMkit запускается — идёт подготовка (уже {waited} с, холодный старт "
            "BPMkit.exe на медленной машине бывает долгим); повторите через минуту",
            kind="context_starting",
            detail=f"CLI не ответил за {int(_CLI_WAIT_S)} с, процесс продолжает работу "
                   f"до {int(_CLI_TIMEOUT_S)} с",
        )
    if job.error is not None:
        raise job.error
    return job.context


def _run_cli_job(job: _CliJob, argv: list, key, cli: list, override_url: str,
                 cache_ttl: float) -> None:
    try:
        rc, stdout, stderr = _default_run(argv)
        job.context = _context_from_run(rc, stdout, stderr, cli=cli,
                                        override_url=override_url)
        with _cache_lock:
            fresh = job.generation == _generation
        if fresh:
            _cache_put(key, job.context, cache_ttl)
    except BaseException as exc:  # noqa: BLE001 - исход задания, поток не должен упасть молча
        job.error = exc
    finally:
        with _cache_lock:
            if _inflight.get(key) is job:
                _inflight.pop(key, None)
        job.done.set()


def _resolve_inprocess(provider: Callable[[], dict], override_url: str,
                       cache_ttl: float) -> LicenseContext:
    """Контекст от провайдера в этом процессе. Текст исключения провайдера в
    ошибку не попадает — только тип: рядом с ним мог оказаться конверт."""
    key = (_INPROCESS_CLI_KEY, override_url)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        payload = provider()
    except Exception as exc:  # noqa: BLE001 - один вид отказа наружу, как у `_default_run`
        raise ContextUnavailable(
            "CLI BPMkit не выдал лицензионный контекст (запрос внутри процесса)",
            kind="context_unavailable",
            detail=f"исключение {type(exc).__name__}",
        ) from None
    if not isinstance(payload, dict):
        raise ContextUnavailable(
            "CLI BPMkit вернул контекст неожиданной формы — контекст лицензии не получен",
            kind="context_unavailable",
            detail=f"тип ответа: {type(payload).__name__}",
        )
    context = _context_from_payload(payload, cli=[], override_url=override_url)
    _cache_put(key, context, cache_ttl)
    return context


def _context_from_payload(payload: dict, *, cli: list, override_url: str) -> LicenseContext:
    """Разбор ответа (из stdout CLI или от провайдера) в `LicenseContext`."""
    if not payload.get("ok"):
        _raise_not_ok(payload)

    # Явная настройка сильнее дефолта из поставки: адрес в конфиге хаба задал человек
    # (стенд издателя, прокси предприятия), и подменять его тем, что «зашито» в MCP,
    # значит молча игнорировать настройку.
    backend_url = override_url or str(payload.get("backend_url") or "").strip().rstrip("/")

    context = LicenseContext(
        envelope=str(payload.get("envelope") or ""),
        license_status=str(payload.get("license_status") or ""),
        backend_url=backend_url,
        mcp_version=str(payload.get("mcp_version") or ""),
        package_root=str(payload.get("package_root") or ""),
        shipped_patterns_root=str(payload.get("shipped_patterns_root") or ""),
        override_patterns_root=str(payload.get("override_patterns_root") or ""),
        patterns_env_registered=bool(payload.get("patterns_env_registered")),
        revocations_target=str(payload.get("revocations_target") or ""),
        revocations_env_registered=bool(payload.get("revocations_env_registered")),
        artifact_pubkey=str(payload.get("artifact_pubkey") or ""),
        binary_path=str(payload.get("binary_path") or ""),
        cli=list(cli),
        raw=payload,
    )
    return context


def invalidate_cache() -> None:
    """Сбросить кэш контекста.

    Зовётся после действий, которые меняют лицензионную картину: пользователь ввёл ключ,
    сменил адрес бэкенда, применил обновление MCP. Без явного сброса канал до пяти минут
    жил бы с устаревшим ответом — то есть «ключ ввёл, а ничего не поменялось».
    """
    global _generation
    with _cache_lock:
        _cache.clear()
        _generation += 1
        _inflight.clear()


def _cache_get(key) -> Optional[LicenseContext]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at, context = entry
        if expires_at <= time.monotonic():
            _cache.pop(key, None)
            return None
        return context


def _cache_put(key, context: LicenseContext, ttl: float) -> None:
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0:
        # Нулевой TTL — осознанное «не кэшировать» (тесты, диагностика).
        return
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, context)
