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
  Чинит человек в настройках хаба (`companion.mcp_cli`);
* **нет лицензии** (`ChannelError(kind="no_license")`) — MCP на месте и честно ответил, что
  ключа на этой машине нет. Чинится покупкой/установкой ключа, к путям отношения не имеет.

Кэш. Контекст спрашивается на КАЖДЫЙ тик трёх циклов, а запуск процесса стоит десятки
миллисекунд и на Windows ещё и рискует мигнуть консольным окном. Поэтому ответ живёт в
памяти `cache_ttl` секунд. На диск он не попадает никогда: в нём лицензионный конверт.
По той же причине ни конверт, ни сырой stdout не попадают в текст ошибок — только stderr
и коды.
"""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from standkit.platform import run_console

from .errors import KIND_TITLES, ChannelError, ContextUnavailable

__all__ = [
    "CONTEXT_ARGV_TAIL",
    "LicenseContext",
    "find_cli",
    "resolve",
    "invalidate_cache",
]

# Подкоманда MCP, отдающая контекст. Держится константой: это контракт с чужим пакетом,
# и менять его «по месту» в трёх строках нельзя.
CONTEXT_ARGV_TAIL = ("setup", "companion-context", "--json")

# Имена исполняемого файла CLI. `.exe` первым — поставка клиента всегда Windows-овая;
# второе имя нужно, чтобы канал был отлаживаем на Linux, где расширения нет.
_CLI_NAMES = ("bpmkit.exe", "bpmkit")

# Куда смотреть от корня-кандидата. `server/` — штатное место бинаря в поставке MCP,
# корень — сборка «всё рядом».
_CLI_SUBPATHS = (("server",), ())

# На сколько уровней вверх от пакета поднимаемся в поисках корня MCP. Поставка кладёт
# BPMkitStand ВНУТРЬ пакета MCP (`build/pack/BPMkitStand`), то есть корень пакета — на два
# уровня выше каталога `standkit_companion`; берём с запасом на нестандартную распаковку.
_CLI_MAX_UP = 4

# Сколько ждём ответа CLI. 30 с — с запасом на холодный старт `.exe` под антивирусом и
# заведомо меньше интервала любого цикла: зависший CLI не должен копить тики.
_CLI_TIMEOUT_S = 30.0

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
def _split_command(value: str) -> list:
    """Разбор строки запуска из настроек (`python -m bpmkit`, `"C:\\...\\bpmkit.exe"`).

    `posix=False` на Windows обязателен: в posix-режиме `shlex` съедает обратные слэши как
    экранирование и `C:\\Program Files\\bpmkit.exe` превращается в `C:Program Filesbpmkit.exe`.
    Плата за это — сохранённые кавычки вокруг токенов, их снимаем сами.
    """
    posix = os.name != "nt"
    parts = shlex.split(value, posix=posix)
    if not posix:
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return [p for p in parts if p]


def _candidate_roots(extra_roots: Optional[Sequence] = None) -> list:
    """Корни, в которых имеет смысл искать CLI: сначала явно переданные (тесты, будущие
    настройки), затем каталоги вверх от самого пакета."""
    roots: list = [Path(r) for r in (extra_roots or [])]
    here = Path(__file__).resolve().parent
    node = here
    for _ in range(_CLI_MAX_UP):
        node = node.parent
        roots.append(node)
    return roots


def find_cli(settings, *, extra_roots: Optional[Sequence] = None) -> Optional[list]:
    """argv-префикс для запуска CLI BPMkit или `None`, если его рядом нет.

    Порядок: явная настройка `companion.mcp_cli` (её задал человек — она сильнее любого
    автодетекта), затем поиск бинаря рядом с поставкой. Угадывания «а вдруг он в PATH»
    здесь нет намеренно: `bpmkit` в PATH может оказаться другой сборкой/другой версией,
    а канал обязан спрашивать лицензию у ТОГО MCP, рядом с которым он установлен.
    """
    configured = str(getattr(settings, "mcp_cli", "") or "").strip()
    if configured:
        as_path = Path(configured)
        if as_path.is_file():
            # Путь к файлу берём целиком: в нём могут быть пробелы, и дробить его нельзя.
            return [str(as_path)]
        parts = _split_command(configured)
        return parts or None

    for root in _candidate_roots(extra_roots):
        for subpath in _CLI_SUBPATHS:
            for name in _CLI_NAMES:
                candidate = root.joinpath(*subpath, name)
                if candidate.is_file():
                    return [str(candidate)]
    return None


# ------------------------------------------------------------------------------------
# Резолв контекста
# ------------------------------------------------------------------------------------
def _default_run(argv: list) -> tuple:
    """Запуск CLI через ЕДИНУЮ точку `standkit.platform.run_console`.

    Прямой `subprocess.run` здесь запрещён (GAP-138): канал тикает из процесса без своей
    консоли, и каждый тик мигал бы чёрным окном. Любое исключение (нет файла, таймаут,
    отказ в доступе) превращается в `rc=-1`: вызывающий разбирает ОДИН вид отказа, а не
    зоопарк исключений `subprocess`.
    """
    try:
        proc = run_console(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=_CLI_TIMEOUT_S)
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
    идёт через `standkit.platform.run_console` (см. `_default_run`).
    """
    cli = find_cli(settings)
    if not cli:
        raise ContextUnavailable(
            "Рядом не найден CLI BPMkit — укажите путь к нему в настройках хаба "
            "(раздел «Канал обновлений», поле companion.mcp_cli)",
            kind="context_unavailable",
            detail="автодетект bpmkit.exe рядом с поставкой не дал результата",
        )

    override_url = str(getattr(settings, "backend_url", "") or "").strip().rstrip("/")
    argv = list(cli) + list(CONTEXT_ARGV_TAIL)
    key = (tuple(argv), override_url)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    runner = run if run is not None else _default_run
    rc, stdout, stderr = runner(argv)
    if rc != 0 or not (stdout or "").strip():
        raise ContextUnavailable(
            "CLI BPMkit не ответил на запрос лицензионного контекста",
            kind="context_unavailable",
            detail=_clip(stderr) or f"код возврата {rc}, пустой вывод",
        )

    payload = _parse_stdout(stdout)
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
    _cache_put(key, context, cache_ttl)
    return context


def invalidate_cache() -> None:
    """Сбросить кэш контекста.

    Зовётся после действий, которые меняют лицензионную картину: пользователь ввёл ключ,
    сменил адрес бэкенда, применил обновление MCP. Без явного сброса канал до пяти минут
    жил бы с устаревшим ответом — то есть «ключ ввёл, а ничего не поменялось».
    """
    with _cache_lock:
        _cache.clear()


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
