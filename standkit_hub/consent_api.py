# -*- coding: utf-8 -*-
"""Согласия MCP («Данные и телеметрия») глазами диспетчера — GAP-332.

Тот же принцип, что у ``standkit_hub/license_api.py`` (см. докстринг там —
здесь не повторяется целиком): своей логики согласий у диспетчера НЕТ и быть
не может. Согласия (телеметрия, приложение журнала к обращениям, отправка
кандидатов-паттернов и кандидатов) живут в MCP BPMkit — в клиентском CLI и в
файле согласий пользователя. Хаб умеет ровно два вызова CLI::

    <cli> setup consent-info --json
    <cli> setup consent-set --analytics true|false --attach-logs … \
                            --pattern-submission … --candidate-submission …

Почему отдельный модуль, а не расширение license_api.py. Формально устройство
идентичное (резолв CLI, кэш, честный ответ без CLI), но предметы разные:
лицензия — конверт издателя, согласия — локальный файл согласий MCP. Смешивать
их в одном модуле означало бы, что отказ CLI по одной причине (например,
`consent` module отсутствует в старой версии MCP) выглядит как отказ лицензии
— два разных «недоступно» с разными подсказками пользователю обязаны остаться
разными функциями.

Почему модуль лежит в MIT-ядре хаба, а не в платном пакете канала. Раздел
«Данные и телеметрия» обязан быть виден и в свободной редакции — согласия не
зависят от лицензии вовсе (даже без лицензии MCP собирает обезличенную
телеметрию, если пользователь её не отключил). `find_cli` здесь — та же
тонкая обёртка над общим `standkit.cli_resolve` (GAP-273), что у license_api.py
(см. его докстринг про порядок резолва: настройка → env → автодетект бинаря →
запуск из исходников).

Отличия от license_api.py, которые ЕСТЬ:

* нет понятия «редакция» (`edition`) — согласия видны независимо от лицензии,
  поле в ответе называется `available` (булево), а не `edition`;
* `consent-info` не бывает `ok: false` по существу (CLI отвечает `rc=0` всегда
  — контракт GAP-332), но старая версия MCP без модуля `consent` вернёт
  `{"ok": false, "error": …}` — это тоже «недоступно», а не 500;
* мутация — ОДНА функция `consent_set` с набором флагов (не три разных, как у
  лицензии): контракт CLI принимает любое подмножество из четырёх флагов за
  один вызов, тело `POST /api/consent` — тоже подмножество.

ЗАПРЕТЫ (как у license_api.py):

* сеть/ключ здесь ни при чём — но то же правило «наружу уходит только stderr
  CLI (обрезанный) и коды возврата» соблюдается: сырой stdout в ошибки не
  попадает;
* кэш только для чтения, любая мутация сбрасывает его немедленно — «нажал
  переключатель, а ничего не изменилось» здесь так же недопустимо, как и у
  лицензии.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

from standkit.cli_resolve import (
    CLI_ENV_VAR,
    candidate_roots as _shared_candidate_roots,
    describe_search_targets,
    resolve_command_string,
    search_roots,
)
from standkit.platform import run_console

__all__ = [
    "CACHE_TTL_SEC",
    "CONSENT_INFO_TAIL",
    "CONSENT_SET_TAIL",
    "CONSENT_FLAGS",
    "ConsentCliError",
    "find_cli",
    "consent_info",
    "consent_set",
    "invalidate_cache",
]

#: Хвосты argv — контракт с CLI (см. докстринг модуля).
CONSENT_INFO_TAIL = ("setup", "consent-info", "--json")
CONSENT_SET_TAIL = ("setup", "consent-set")

#: Флаг → имя опции CLI. Порядок — порядок отображения в UI (GAP-332: сначала
#: телеметрия использования, затем журнал обращений, затем два вида кандидатов).
CONSENT_FLAGS: dict = {
    "analytics": "--analytics",
    "attach_logs": "--attach-logs",
    "pattern_submission": "--pattern-submission",
    "candidate_submission": "--candidate-submission",
}

#: Сколько живёт сводка. Та же минута, что у лицензии (см. license_api.py) —
#: экран согласий опрашивается вместе с прочими вкладками настроек.
CACHE_TTL_SEC = 60.0

_CLI_TIMEOUT_S = 30.0
_CLI_WRITE_TIMEOUT_S = 30.0

_DETAIL_LIMIT = 300

_cache_lock = threading.Lock()
_cache: dict = {}


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class ConsentCliError(Exception):
    """Отказ CLI согласий, уже приведённый к форме ответа хаба.

    В отличие от `LicenseCliError` здесь нет отдельного `status` для
    «CLI не найден»: `GET /api/consent` и `POST /api/consent` отдают 200
    со сводкой `available: false` в ЛЮБОМ отказе CLI (см. докстринг модуля —
    раздел обязан показать причину текстом, а не HTTP-ошибкой), а валидация
    тела `POST` — отдельная, до похода в CLI (см. server.py::_api_consent_post).
    """

    def __init__(self, error: str, *, detail: str = "") -> None:
        super().__init__(error)
        self.error = error
        self.detail = _clip(detail)


# ------------------------------------------------------------------------------------
# Поиск CLI — дословно та же схема, что у license_api.py (см. его докстринг).
# ------------------------------------------------------------------------------------
def _candidate_roots(extra_roots: Optional[Sequence] = None) -> list:
    """Тонкая обёртка над `standkit.cli_resolve.candidate_roots` — имя стабильно
    для тестов (`monkeypatch.setattr(consent_api, "_candidate_roots", ...)`)."""
    return _shared_candidate_roots(__file__, extra_roots=extra_roots)


def _resolve_cli(settings, *, extra_roots: Optional[Sequence] = None) -> tuple:
    """(argv, source) — резолв CLI, дословно тот же порядок, что у
    `license_api._resolve_cli`: явная настройка → env → автодетект бинаря →
    запуск из исходников."""
    configured = str(getattr(settings, "mcp_cli", "") or "").strip()
    if configured:
        return resolve_command_string(configured), "settings"

    from_env = str(os.environ.get(CLI_ENV_VAR, "") or "").strip()
    if from_env:
        return resolve_command_string(from_env), "env"

    found = search_roots(_candidate_roots(extra_roots))
    if found:
        return found, "auto"
    return None, None


def find_cli(settings, *, extra_roots: Optional[Sequence] = None) -> Optional[list]:
    """argv-префикс запуска CLI BPMkit или `None`, если рядом его нет.

    `settings` — та же секция `companion` конфига хаба, что у license_api.py:
    путь к CLI один на весь диспетчер, второго поля не заводим.
    """
    argv, _source = _resolve_cli(settings, extra_roots=extra_roots)
    return argv


# ------------------------------------------------------------------------------------
# Запуск CLI
# ------------------------------------------------------------------------------------
def _default_run(argv: list, *, timeout: float = _CLI_TIMEOUT_S) -> tuple:
    """Запуск через ЕДИНУЮ точку `standkit.platform.run_console` (GAP-138)."""
    try:
        proc = run_console(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - см. license_api.py::_default_run
        return -1, "", str(exc)
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def _parse_json(stdout: str) -> Optional[dict]:
    """JSON из stdout CLI; `None`, если разобрать нечего (см. license_api.py —
    та же терпимость к баннеру рантайма перед/после `{...}`)."""
    text = (stdout or "").strip()
    if not text:
        return None
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _cli_or_raise(settings) -> list:
    cli = find_cli(settings)
    if not cli:
        raise ConsentCliError(
            "Рядом не найден CLI BPMkit — укажите путь к нему в «Настройки → "
            f"Основные», поле «CLI BPMkit», либо переменной окружения {CLI_ENV_VAR}",
            detail=describe_search_targets(_candidate_roots()),
        )
    return cli


def _run_json(settings, tail: Sequence[str], *, run: Optional[Callable] = None,
              timeout: float = _CLI_TIMEOUT_S, failure: str) -> dict:
    """Один вызов CLI, отдающий JSON. Отказ — типизированный `ConsentCliError`."""
    argv = _cli_or_raise(settings) + list(tail)
    if run is not None:
        rc, stdout, stderr = run(argv)
    else:
        rc, stdout, stderr = _default_run(argv, timeout=timeout)
    payload = _parse_json(stdout)
    if payload is None:
        raise ConsentCliError(
            failure,
            detail=_clip(stderr) or f"код возврата {rc}, вывод не разобран как JSON",
        )
    if rc != 0:
        # ПРОВЕРКА rc — ДО `ok:false`: CLI мог отдать валидный JSON с
        # `ok:true`, но ненулевым кодом возврата (ревью, М8) — в этом случае
        # `payload.get("detail")` пуст (успешная по форме сводка его не
        # несёт), и текст отказа обязан остаться в stderr/rc, а не потеряться
        # за пустым `detail` из ветки `ok:false` ниже.
        raise ConsentCliError(failure, detail=_clip(stderr) or f"код возврата {rc}")
    if not payload.get("ok", True):
        # Контракт GAP-332: `consent-info` отвечает `ok:false` только когда в
        # MCP нет модуля согласий вовсе (старая версия) — это «недоступно»,
        # а не «согласий нет» (см. докстринг модуля).
        raise ConsentCliError(
            str(payload.get("error") or failure),
            detail=_clip(str(payload.get("detail") or "")) or _clip(stderr),
        )
    return payload


# ------------------------------------------------------------------------------------
# Сводка (GET /api/consent)
# ------------------------------------------------------------------------------------
def _cli_display(cli: Optional[list]) -> Optional[str]:
    """argv → команда одной строкой для UI, тот же приём, что у license_api.py."""
    if not cli:
        return None
    return subprocess.list2cmdline(list(cli))


def _unavailable_snapshot(detail: str, *, cli: Optional[list] = None,
                          cli_source: Optional[str] = None) -> dict:
    """Ответ, когда CLI рядом нет или отказал: не 500 и не 503 — честная
    сводка `available: false` с причиной (см. докстринг модуля: раздел
    обязан показать её текстом с подсказкой про поле «CLI BPMkit»)."""
    return {
        "ok": True,
        "available": False,
        "reason": _clip(detail),
        "cli": _cli_display(cli),
        "cli_source": cli_source,
    }


def consent_info(settings, *, run: Optional[Callable] = None,
                 cache_ttl: float = CACHE_TTL_SEC) -> dict:
    """Сводка согласий для `GET /api/consent` (с кэшем на `cache_ttl` секунд).

    Форма успешного ответа — то, что вернул CLI (`analytics`, `attach_logs`,
    `pattern_submission`, `candidate_submission`, `eula_accepted_at`,
    `install_id_tail`, `defaults_source`, `consent_file`, `telemetry`,
    `preview`, …), плюс `available: true` и `cli`/`cli_source` — хаб не
    выдумывает поля, которых не дал CLI.
    """
    cli, source = _resolve_cli(settings)
    if not cli:
        return _unavailable_snapshot(describe_search_targets(_candidate_roots()))

    key = tuple(cli) + CONSENT_INFO_TAIL
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        payload = _run_json(settings, CONSENT_INFO_TAIL, run=run,
                            failure="CLI BPMkit не ответил на запрос сведений о согласиях")
    except ConsentCliError as exc:
        # Не кэшируется: починка (правка пути к CLI, обновление MCP) обязана
        # быть видна немедленно, как и у license_api.py::license_info.
        return _unavailable_snapshot(exc.detail or exc.error, cli=cli, cli_source=source)

    snapshot = dict(payload)
    snapshot["ok"] = True
    snapshot["available"] = True
    snapshot["cli"] = _cli_display(cli)
    snapshot["cli_source"] = source
    _cache_put(key, snapshot, cache_ttl)
    return snapshot


# ------------------------------------------------------------------------------------
# Изменение флагов (POST /api/consent)
# ------------------------------------------------------------------------------------
def consent_set(settings, flags: dict, *, run: Optional[Callable] = None) -> dict:
    """Изменить одно или несколько согласий одним вызовом CLI.

    `flags` — подмножество `{"analytics", "attach_logs", "pattern_submission",
    "candidate_submission"}` со значениями `bool`; валидация состава и типов —
    забота вызывающей стороны (`server.py::_api_consent_post`), здесь только
    сборка argv и сброс кэша. Пустой `flags` — не ошибка (нет-op), но такой
    вызов CLI не делает вовсе — незачем гонять процесс без единого флага.

    НИКОГДА не бросает `ConsentCliError` наружу (ревью Opus, Б3): отказ CLI на
    самом `consent-set` (старый MCP без модуля `consent`, `ok:false`,
    ненулевой rc, таймаут) — та же «сводка недоступна», что у `consent_info`,
    а не необработанное исключение, которое рвёт соединение раньше, чем
    хендлер успеет ответить (фронт в этом случае показывал бы «нет связи с
    хабом», хотя диспетчер жив и просто не смог поговорить с CLI). Кэш ВСЁ
    РАВНО сбрасывается при любом исходе — сохранение переключателя обязано
    быть отражено следующим чтением, даже если сам вызов CLI не удался.
    """
    tail = list(CONSENT_SET_TAIL)
    for name, value in flags.items():
        option = CONSENT_FLAGS.get(name)
        if option is None:
            # Защита от опечатки внутри самого хаба — а не от чужого ввода
            # (тот отфильтрован раньше, в server.py). AssertionError, а не
            # тихий пропуск: иначе баг «флаг не долетел до CLI» замечается
            # только глазами на экране согласий.
            raise AssertionError(f"неизвестный флаг согласия: {name}")
        tail.append(option)
        tail.append("true" if value else "false")

    try:
        if len(tail) > len(CONSENT_SET_TAIL):
            _run_json(settings, tail, run=run, timeout=_CLI_WRITE_TIMEOUT_S,
                     failure="CLI BPMkit не принял изменение согласий")
    except ConsentCliError as exc:
        invalidate_cache()
        cli, source = _resolve_cli(settings)
        return _unavailable_snapshot(exc.detail or exc.error, cli=cli, cli_source=source)

    invalidate_cache()
    return consent_info(settings, run=run)


# ------------------------------------------------------------------------------------
# Кэш — дословно та же схема, что у license_api.py.
# ------------------------------------------------------------------------------------
def invalidate_cache() -> None:
    """Сбросить кэш сводки. Зовётся после КАЖДОЙ мутации согласий."""
    with _cache_lock:
        _cache.clear()


def _cache_get(key) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at, payload = entry
        if expires_at <= time.monotonic():
            _cache.pop(key, None)
            return None
        return dict(payload)


def _cache_put(key, payload: dict, ttl: float) -> None:
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, dict(payload))
