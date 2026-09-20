# -*- coding: utf-8 -*-
"""Обратный проход канала: разгрузка локальной очереди находок вендору
(GAP-260, GAP-248 п.2) — попутчик обычного тика, рядом с кукбуком.

**Что здесь чинится.** Канал Companion до сих пор работал только НА ПРИЁМ:
тянул паттерны, релизы, отзывы и кукбук. Локально накопленные находки
(`~/.bpmkit/outbox/` — письма обратной связи и кандидаты паттернов, которые
пишут `session_note`/`pattern_submit(submit=True)`) не уезжали НИКОГДА, пока
пользователь не вызывал `feedback_flush(confirm=True)` руками. Очередь на
бэкенде пуста не потому, что нечего слать, а потому что путь непроходим для
обычного пользователя (GAP-248) — при этом ежедневный триаж издателя
рассчитан на эту очередь как на источник.

**Почему мы НЕ читаем `~/.bpmkit/outbox/` сами.** Соблазн очевиден: каталог
известен, формат — JSON. Но очередь принадлежит MCP, и у неё есть правила,
которые здесь пришлось бы повторить: два подкаталога с разными капами,
счётчик `attempts` с атомарной перезаписью, перенос в `dead/` после N
отказов, маскирование улик, гейты согласия и генерализации, формат письма.
Вторая копия этой логики в другом репозитории разъедется на первом же
изменении формата, и разъедется молча — находки начнут теряться или
дублироваться. Поэтому Companion зовёт ту же поставку, которой очередь и
принадлежит: `bpmkit setup outbox-flush`, ровно так же, как зовёт
`companion-context`. Транспорт (`feedback.flush_outbox`) уже существует,
отлажен и НИКОГДА не теряет файл — ему нужен был только тот, кто его
запускает.

**Офлайн, квота и отсутствие лицензии — не ошибка по построению.** Требование
строки GAP-260 дословно. `flush_outbox` возвращает системный отказ полем
`stopped_reason` (`offline` / `rate_limited` / `no_license` /
`feature_disabled` / `insecure_transport`) и не бросает исключений: «сейчас не
уехало» — штатный исход фонового прохода. Такой исход помечается в состоянии
как `skipped`, а не `error`: красный цикл, который чинится сам при следующем
тике, обесценивает индикацию.

**Согласие не спрашивается здесь и не может быть обойдено отсюда.** Флаги
(`analytics` / `attach_logs` / `pattern_submission` / `candidate_submission`)
независимы (ADR-0024) и проверяются транспортом на стороне MCP. Companion не
знает о них ничего и не вправе знать: единственный способ отправить что-либо
отсюда — попросить поставку разгрузить очередь.

**Как именно отказывает поставка (по факту, GAP-413).** До 20.09.2026 фраза
«она откажет сама» была неточной: согласие проверялось только в момент, когда
письмо КЛАДЁТСЯ в очередь, а `feedback._transport_send` отправлял уже стоящее
без единой проверки — отзыв согласия очередь не останавливал (строка L-31
`docs/legal_backlog.md`, 152-ФЗ). Теперь гейт стоит ПЕРЕД КАЖДЫМ письмом: при
снятом флаге письмо получает статус `no_consent`, НЕ отправляется, остаётся в
очереди нетронутым и считается отдельно — поставка печатает их число полем
`held_no_consent`. Для канала это не отказ и не системная остановка: проход
идёт дальше по остальным письмам, а число задержанных попадает в состояние и в
текст для пользователя. Молчать о них нельзя — иначе снявший галку человек
видит «очередь пуста» и не понимает, почему находки не уезжают.

Поле может отсутствовать: Companion и MCP обновляются РАЗНЫМИ артефактами и
разными каналами, поэтому старая поставка его не печатает — это норма, а не
повод объявить проход неудачным (читается через `int(... or 0)`).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from standkit.platform import run_console

from .context import find_cli
from .errors import ChannelError
from .state import utc_now_iso

__all__ = [
    "FLUSH_ARGV_TAIL",
    "SYSTEM_STOP_REASONS",
    "flush",
]

#: Хвост командной строки поставки. Тот же приём, что `CONTEXT_ARGV_TAIL` у
#: лицензионного контекста: подкоманда фиксирована здесь, а не собирается по
#: месту вызова.
FLUSH_ARGV_TAIL = ("setup", "outbox-flush", "--json")

#: Системные причины остановки прохода — это «сейчас не уехало», а не поломка.
#: Список ДОСЛОВНО повторяет контракт `feedback._flush_pass`; незнакомое
#: значение сюда не попадает и честно трактуется как ошибка.
SYSTEM_STOP_REASONS = frozenset({
    "offline",
    "rate_limited",
    "no_license",
    "feature_disabled",
    "insecure_transport",
})

#: Потолок ожидания. Проход идёт по письмам ПО ОДНОМУ с коротким сетевым
#: таймаутом на каждое, поэтому здесь не «сколько угодно»: попутчик не имеет
#: права держать тик канала дольше, чем несущая операция.
FLUSH_TIMEOUT_S = 120

#: Максимум записей за проход КАЖДОГО типа. Ограничение осознанное: фоновый
#: проход не должен превращаться в многоминутную выгрузку накопленного за
#: месяц — остаток уедет следующим тиком, очередь никуда не денется.
DEFAULT_LIMIT = 25

_DETAIL_LIMIT = 400


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _default_run(argv: list) -> tuple:
    """Запуск поставки через ЕДИНУЮ точку `standkit.platform.run_console` —
    той же формы, что `context._default_run`.

    Прямой `subprocess.run` здесь ЗАПРЕЩЁН (GAP-138): канал тикает из процесса
    без своей консоли, и каждый тик мигал бы чёрным окном — ровно тот дефект,
    из-за которого у владельца раз в ~12 с всплывали окна поллера. Любое
    исключение (нет файла, таймаут, отказ в доступе) превращается в `rc=-1`:
    вызывающий разбирает ОДИН вид отказа, а не зоопарк исключений
    `subprocess`.
    """
    try:
        proc = run_console(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=FLUSH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - см. докстринг: один вид отказа наружу
        return -1, "", str(exc)
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def _parse_stdout(stdout: str) -> dict:
    """Единственная строка JSON из вывода подкоманды.

    Читается ПОСЛЕДНЯЯ непустая строка, а не весь вывод: поставка может
    напечатать перед ответом предупреждение (например, о нерезолвнутом
    корне пакета), и жёсткий `json.loads(stdout)` ломался бы об него.
    """
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ChannelError(
        "Ответ подкоманды разгрузки очереди не разобран как JSON — очередь не тронута",
        kind="bad_response",
        detail=_clip(stdout),
    )


def flush(state, settings, *, run: Optional[Callable] = None,
          limit: Optional[int] = DEFAULT_LIMIT) -> dict:
    """Один обратный проход: попросить поставку разгрузить очередь.

    Возвращает словарь исхода (в состояние он же и оседает); исключение
    наружу не поднимается НИКОГДА, кроме неразобранного ответа — вызывающий
    (`runner._sync_candidates`) ловит и это, но попутчик обязан быть
    безопасным сам по себе.
    """
    block = state.data.setdefault("candidates", {})
    runner = run or _default_run

    cli = find_cli(settings)
    if not cli:
        # CLI поставки не найден — это НЕ отказ канала: точно так же
        # ведёт себя лицензионный контекст (GAP-273), и чинит это человек
        # в настройках хаба.
        block["last_sync_at"] = utc_now_iso()
        state.mark("candidates", "skipped",
                   "CLI BPMkit не найден — очередь находок не разгружалась")
        state.save()
        return {"flushed": False, "reason": "cli_not_found",
                "sent": 0, "failed": 0, "held_no_consent": 0, "remaining": None}

    argv = list(cli) + list(FLUSH_ARGV_TAIL)
    if limit is not None:
        argv += ["--limit", str(int(limit))]

    # `_default_run` не выпускает исключений наружу (таймаут/нет файла → rc=-1),
    # но двойник в тестах вправе бросить — попутчик обязан пережить и это.
    try:
        rc, stdout, stderr = runner(argv)
    except Exception as exc:  # noqa: BLE001
        block["last_sync_at"] = utc_now_iso()
        state.mark("candidates", "error", f"CLI BPMkit не запустился: {_clip(str(exc))}")
        state.save()
        return {"flushed": False, "reason": "spawn_error",
                "sent": 0, "failed": 0, "held_no_consent": 0, "remaining": None}

    if rc != 0:
        block["last_sync_at"] = utc_now_iso()
        detail = _clip(stderr) or f"код возврата {rc}"
        state.mark("candidates", "error", f"разгрузка очереди отказала: {detail}")
        state.save()
        return {"flushed": False, "reason": "cli_error", "detail": detail,
                "sent": 0, "failed": 0, "held_no_consent": 0, "remaining": None}

    payload = _parse_stdout(stdout)

    if not payload.get("ok"):
        block["last_sync_at"] = utc_now_iso()
        detail = _clip(payload.get("detail") or payload.get("reason") or "без пояснений")
        state.mark("candidates", "error", f"очередь не разгружена: {detail}")
        state.save()
        return {"flushed": False, "reason": payload.get("reason") or "refused",
                "detail": detail, "sent": 0, "failed": 0, "held_no_consent": 0, "remaining": None}

    sent = int(payload.get("sent") or 0)
    failed = int(payload.get("failed") or 0)
    # Письма, задержанные гейтом согласия (GAP-413). Старая поставка поля не
    # печатает — см. докстринг модуля, это обратная совместимость, а не ошибка.
    held = int(payload.get("held_no_consent") or 0)
    remaining = payload.get("remaining")
    stopped = payload.get("stopped_reason") or None

    block["last_sync_at"] = utc_now_iso()
    block["last_sent"] = sent
    block["last_held_no_consent"] = held
    block["last_remaining"] = remaining

    if stopped in SYSTEM_STOP_REASONS:
        # Ровно требование строки GAP-260: офлайн/квота/нет лицензии не
        # должны быть ошибкой ПО ПОСТРОЕНИЮ. Отправленное до остановки при
        # этом отправлено — об этом и говорим.
        state.mark("candidates", "skipped",
                   _with_held(_stop_text(stopped, sent, remaining), held))
        state.save()
        return {"flushed": sent > 0, "reason": stopped, "sent": sent,
                "failed": failed, "held_no_consent": held, "remaining": remaining}

    if stopped:
        state.mark("candidates", "error",
                   _with_held(f"проход очереди остановлен: {_clip(stopped)}", held))
        state.save()
        return {"flushed": sent > 0, "reason": stopped, "sent": sent,
                "failed": failed, "held_no_consent": held, "remaining": remaining}

    state.mark("candidates", "ok", _ok_text(sent, failed, remaining, held))
    state.save()
    return {"flushed": sent > 0, "reason": "flushed", "sent": sent,
            "failed": failed, "held_no_consent": held, "remaining": remaining}


def _stop_text(reason: str, sent: int, remaining) -> str:
    """Человеческий текст системного отказа — его увидит пользователь в UI."""
    texts = {
        "offline": "нет связи с сервисом издателя",
        "rate_limited": "суточная квота приёмника исчерпана",
        "no_license": "нет лицензии — отправка не выполняется",
        "feature_disabled": "издатель временно не принимает этот тип находок",
        "insecure_transport": "адрес сервиса издателя запрещён политикой транспорта",
    }
    head = texts.get(reason, reason)
    if sent:
        return f"Отправлено находок: {sent}; дальше — {head}"
    left = f", в очереди {remaining}" if isinstance(remaining, int) and remaining else ""
    return f"Находки не отправлены: {head}{left}"


def _with_held(text: str, held: int) -> str:
    """Дописывает к тексту исхода число писем, задержанных гейтом согласия.

    Отдельной строкой, а не заменой текста: «отправлено 3, задержано 2» и
    «задержано 2» — разные сообщения, и первое не имеет права потеряться.
    """
    if not held:
        return text
    return f"{text}; задержано без согласия: {held} (проверьте флаги в «Данные и телеметрия»)"


def _ok_text(sent: int, failed: int, remaining, held: int = 0) -> str:
    if not sent and not failed and not held:
        return "Очередь находок пуста — отправлять нечего"
    if not sent and not failed and held:
        # Очередь НЕ пуста, но ничего не уехало и уехать не могло: писать
        # «пуста» здесь значило бы соврать ровно тому, кто снял галку.
        return _with_held(f"Находки не отправлены: согласие снято, писем в очереди {held}",
                          0)
    parts = [f"Отправлено находок: {sent}"]
    if failed:
        parts.append(f"отклонено: {failed}")
    if isinstance(remaining, int) and remaining:
        parts.append(f"в очереди: {remaining}")
    return _with_held("; ".join(parts), held)
