# -*- coding: utf-8 -*-
"""CLI канала обновлений: `python -m standkit_companion <команда>`.

Зачем он нужен, если канал живёт внутри хаба. Ровно затем, что канал живёт внутри хаба:
когда «обновления не приезжают», разбираться приходится на машине клиента, где UI уже
показал невнятное «ошибка канала», а логов службы под рукой нет. Эта точка входа даёт
человеку тот же самый код, но с русским текстом причины и понятным кодом возврата — и не
требует поднимать хаб.

Три кода возврата, и они означают РАЗНОЕ:

* `0` — сделано;
* `1` — отказ по существу: канал отработал и сообщает, что не может (нет подготовленного
  обновления, подпись не подтверждена, лицензия истекла). Чинится решением про предмет;
* `2` — ошибка использования или окружения: неизвестная команда, битый конфиг, рядом нет
  CLI BPMkit. Чинится до того, как предмет вообще станет обсуждаем.

Разводить их обязательно: в скриптах установки `1` и `2` ведут к разным действиям, а
слипшись в «ненулевой код» они превращают диагностику в гадание.

Стек-трейс наружу не летит никогда: пользователю он ничего не объясняет, зато прячет
единственную полезную строку. Все отказы канала типизированы (`errors.CompanionError`) и
печатаются одинаково: заголовок, причина, что делать. Лицензионный конверт не печатается
ни в каком режиме и ни при какой ошибке — он не выходит за пределы secretstore MCP.

Консольного скрипта в `pyproject.toml` для этого модуля НЕТ намеренно: поставка ставит хаб,
а не отдельный бинарь канала, и лишнее имя в `PATH` пришлось бы поддерживать вечно.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Optional

from . import __version__
from .errors import CompanionError, ContextUnavailable
from .releases import RESTART_MESSAGE
from .runner import build_runner, status_snapshot

__all__ = ["main"]

# Что делать человеку — по машинному `kind` отказа. Таблица живёт здесь, а не в
# `errors.py`, потому что это ПРЕДСТАВЛЕНИЕ: в UI хаба у тех же причин будут кнопки, а не
# строки «выполните команду». Заполнены только те виды, где совет действительно есть;
# выдумывать «обратитесь в поддержку» на каждый kind — способ обесценить подсказку.
_HINTS = {
    "context_unavailable": ("укажите путь к CLI BPMkit в настройках хаба "
                            "(раздел «Канал обновлений», поле companion.mcp_cli) "
                            "или запустите команду рядом с установленным BPMkit"),
    "no_license": "установите лицензионный ключ BPMkit — без него канал бездействует",
    "expired": "продлите лицензию у издателя и обновите ключ на этой машине",
    "not_yet_valid": "проверьте системную дату: лицензия ещё не вступила в силу",
    "revoked": "лицензия отозвана издателем — обратитесь к нему за новой",
    "invalid_envelope": "переустановите лицензионный ключ: сохранённый конверт не читается",
    "signature_invalid": "переустановите лицензионный ключ: его подпись не подтверждается",
    "offline": "проверьте доступ к бэкенду издателя (сеть, прокси, адрес в настройках)",
    "server_misconfigured": "это отказ на стороне издателя — повторите позже",
    "signature_not_available": ("обновление не применяется: издатель не подтвердил подпись "
                                "файла. Дождитесь исправленного релиза"),
    "artifact_signature_invalid": ("подпись скачанного файла недействительна — файл "
                                   "отброшен. Повторите подготовку позже"),
    "pubkey_missing": ("в поставке нет публичного ключа подписи артефактов — обновите "
                       "BPMkit вручную"),
    "revocations_signature_invalid": ("подпись списка отзыва не подтверждена — локальная "
                                      "копия не тронута"),
    "integrity_mismatch": "данные повреждены в канале — повторите команду",
    "nothing_staged": "сначала подготовьте обновление: python -m standkit_companion stage-update",
    "nothing_to_rollback": "откат станет доступен после первого применённого обновления",
    "local_io": ("проверьте права на каталоги BPMkit и закройте Claude Desktop, если он "
                 "держит файл MCP"),
}

_CYCLE_TITLES = {
    "patterns": "паттерны",
    "releases": "релизы",
    "revocations": "отзыв лицензий",
}


# ======================================================================================
# Печать
# ======================================================================================
def _out(text: str = "") -> None:
    print(text)


def _print_error(exc: CompanionError) -> None:
    """Единый вид отказа: заголовок, причина, что делать.

    В `stderr`, а не в `stdout`: `status --json` обязан оставаться машиночитаемым даже
    когда что-то пошло не так, а перемешанный с JSON текст ошибки ломает разбор.
    """
    lines = [f"ОШИБКА: {exc.title()}"]
    reason = (exc.detail or "").strip() or str(exc).strip()
    if reason and reason != exc.title():
        lines.append(f"Причина: {reason}")
    hint = _HINTS.get(exc.kind)
    if hint:
        lines.append(f"Что делать: {hint}")
    print("\n".join(lines), file=sys.stderr)


def _human_interval(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds == 86400:
        return "раз в сутки"
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"раз в {seconds // 86400} сут"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"каждые {seconds // 3600} ч"
    if seconds >= 60:
        return f"каждые {seconds // 60} мин"
    return f"каждые {seconds} с"


def _print_status(snapshot: dict) -> None:
    """Человеческий статус. Ровно те же данные, что в `--json`, без единого поля больше."""
    settings = snapshot.get("settings") or {}
    context = snapshot.get("context") or {}
    state = snapshot.get("state") or {}
    releases_state = state.get("releases") or {}

    _out("Канал обновлений издателя BPMkit")
    _out(f"  Канал:            {'включён' if settings.get('enabled') else 'выключен'}")
    _out(f"  Версия канала:    {snapshot.get('companion_version') or __version__}")
    _out(f"  Планировщик:      {'работает' if snapshot.get('running') else 'не запущен'}")
    _out(f"  Контекст лицензии: {'получен' if context.get('ok') else 'не получен'}"
         f" — {context.get('detail') or 'нет данных'}")
    cli = context.get("cli") or []
    _out(f"  CLI BPMkit:       {' '.join(str(part) for part in cli) if cli else 'не найден'}")

    _out()
    _out("Циклы:")
    for cycle, info in (snapshot.get("cycles") or {}).items():
        title = _CYCLE_TITLES.get(cycle, cycle)
        flag = "вкл " if info.get("enabled") else "выкл"
        interval = _human_interval(info.get("interval_sec") or 0)
        line = f"  {title:<16} {flag}  {interval:<14} исход: {info.get('last_status')}"
        detail = (info.get("last_detail") or "").strip()
        if detail:
            line += f" — {detail}"
        _out(line)
        if info.get("halted"):
            # Блокировка — не «ошибка была», а «повторов не будет»: без явной строки
            # человек ждал бы следующего тика, которого не случится.
            _out(f"  {'':<16} ВНИМАНИЕ: цикл остановлен до вмешательства "
                 f"({info.get('halt_reason')})")

    _out()
    _out("Обновление MCP:")
    _out(f"  установлено: {releases_state.get('current_version') or 'неизвестно'}; "
         f"доступно: {releases_state.get('known_latest') or '—'}; "
         f"подготовлено: {releases_state.get('staged_version') or '—'}")
    if releases_state.get("restart_required"):
        _out(f"  {RESTART_MESSAGE}")

    actions = [name for name, allowed in (snapshot.get("actions") or {}).items() if allowed]
    _out()
    _out(f"Доступные действия: {', '.join(actions) if actions else 'нет'}")
    last_error = (snapshot.get("last_error") or "").strip()
    if last_error:
        _out(f"Последний сбой планировщика: {last_error}")


# ======================================================================================
# Команды
# ======================================================================================
def _config_path(args):
    """Путь конфига хаба. Импорт хаба отложен: модуль обязан импортироваться сам по себе."""
    if getattr(args, "config", None):
        return args.config
    from standkit_hub.config import HubConfig

    return HubConfig.config_path()


def _cmd_status(args) -> int:
    snapshot = status_snapshot(_config_path(args))
    if args.json:
        # `ensure_ascii=False` — вывод читает человек в консоли Windows; экранированная
        # кириллица делает машинный режим нечитаемым без пользы для разбора.
        _out(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        _print_status(snapshot)
    return 0


def _cmd_sync(args) -> int:
    result = build_runner(_config_path(args)).run_action("sync_patterns")
    _out(f"Паттерны: получено {result.get('fetched', 0)}, применено "
         f"{result.get('applied', 0)}, отозвано {result.get('removed', 0)}, "
         f"пропущено {len(result.get('skipped') or [])}")
    for item in (result.get("skipped") or []):
        _out(f"  пропущен #{item.get('id')}: {item.get('note') or item.get('reason')}")
    return 0


def _cmd_check_update(args) -> int:
    result = build_runner(_config_path(args)).run_action("check_update")
    if result.get("available"):
        _out(f"Доступно обновление: {result.get('latest') or 'latest'} "
             f"(установлена {result.get('current') or 'неизвестно'})")
        if not result.get("signed"):
            _out("Внимание: подпись релиза сервером не подтверждена — "
                 "обновление не будет скачано.")
    else:
        _out(f"Обновления нет. Установлена версия: {result.get('current') or 'неизвестно'}")
    return 0


def _cmd_stage_update(args) -> int:
    result = build_runner(_config_path(args)).run_action("stage_update",
                                                         version=args.version)
    _out(f"Обновление {result.get('version') or 'latest'} подготовлено: "
         f"{result.get('path')}")
    _out("Файл проверен (размер, sha256, подпись). Применение — отдельной командой "
         "apply-update.")
    return 0


def _cmd_apply_update(args) -> int:
    result = build_runner(_config_path(args)).run_action("apply_update")
    _out(f"Установлена версия {result.get('version') or 'latest'} "
         f"(предыдущая сохранена: {result.get('backup') or '—'})")
    _out(RESTART_MESSAGE)
    return 0


def _cmd_rollback(args) -> int:
    result = build_runner(_config_path(args)).run_action("rollback", version=args.version)
    _out(f"Выполнен откат на версию {result.get('version') or 'предыдущую'}")
    # Текст про перезапуск берётся из результата, а не из `RESTART_MESSAGE`: у отката он
    # свой (вернулась прежняя версия, а не установилась новая), и подменять его общим
    # значило бы соврать про то, что именно сейчас на диске.
    _out(result.get("message") or RESTART_MESSAGE)
    return 0


def _cmd_revocations(args) -> int:
    result = build_runner(_config_path(args)).run_action("refresh_revocations")
    _out(f"Список отзыва: отозванных лицензий {result.get('revoked_count', 0)}, "
         f"обновление {'применено' if result.get('changed') else 'не потребовалось'} "
         f"({result.get('reason')})")
    if result.get("path") and not result.get("env_registered"):
        _out("Внимание: клиентский MCP не сообщил, что читает этот файл — "
             "проверьте установку BPMkit.")
    return 0


def _cmd_run(args) -> int:
    """Поднять планировщик. `--once` — один проход циклов, у которых наступил срок."""
    runner = build_runner(_config_path(args))
    if args.once:
        report = runner.run_due()
        for cycle in report.get("order") or []:
            outcome = (report.get("results") or {}).get(cycle) or {}
            detail = (outcome.get("detail") or "").strip()
            _out(f"{_CYCLE_TITLES.get(cycle, cycle)}: {outcome.get('status')}"
                 + (f" — {detail}" if detail else ""))
        # Ненулевой код на отказ цикла: `--once` зовут из планировщика ОС, и «всё хорошо»
        # в ответ на упавший цикл сделало бы такой запуск бессмысленным.
        failed = any((outcome or {}).get("status") == "error"
                     for outcome in (report.get("results") or {}).values())
        return 1 if failed else 0

    runner.start()
    _out("Планировщик канала запущен. Остановка — Ctrl+C.")
    idle = threading.Event()
    try:
        while runner.is_running():
            # Ждём короткими шагами, а не одним бесконечным: Ctrl+C на Windows
            # доставляется главному потоку между шагами ожидания, и длинный шаг
            # превратился бы в «нажал — ничего не происходит».
            idle.wait(1.0)
    except KeyboardInterrupt:
        _out("Останавливаю...")
    finally:
        runner.stop()
    return 0


_COMMANDS = {
    "status": _cmd_status,
    "sync": _cmd_sync,
    "check-update": _cmd_check_update,
    "stage-update": _cmd_stage_update,
    "apply-update": _cmd_apply_update,
    "rollback": _cmd_rollback,
    "revocations": _cmd_revocations,
    "run": _cmd_run,
}


# ======================================================================================
# Разбор аргументов
# ======================================================================================
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m standkit_companion",
        description="Канал доставки обновлений издателя BPMkit: паттерны, релизы, отзыв "
                    "лицензий.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="путь к конфигу хаба (по умолчанию standkit-hub.json в "
                             "каталоге настроек BPMkit)")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", parents=[common],
                            help="состояние канала (без запуска планировщика)")
    status.add_argument("--json", action="store_true",
                        help="машинный вывод вместо человеческого")

    sub.add_parser("sync", parents=[common], help="синхронизировать паттерны")
    sub.add_parser("check-update", parents=[common],
                   help="проверить наличие обновления MCP (файл не качается)")

    stage = sub.add_parser("stage-update", parents=[common],
                           help="скачать и проверить обновление, НЕ применяя его")
    stage.add_argument("--version", default=None,
                       help="конкретная версия (по умолчанию последняя)")

    sub.add_parser("apply-update", parents=[common],
                   help="применить подготовленное обновление (явное действие человека)")

    rollback = sub.add_parser("rollback", parents=[common],
                              help="вернуть предыдущую версию MCP из резервной копии")
    rollback.add_argument("--version", default=None,
                          help="версия, НА которую откатываемся")

    sub.add_parser("revocations", parents=[common],
                   help="обновить локальную копию списка отзыва лицензий")

    run = sub.add_parser("run", parents=[common], help="поднять планировщик канала")
    run.add_argument("--once", action="store_true",
                     help="один проход циклов, у которых наступил срок, и выход")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse «выходит» исключением (неизвестная команда, `--help`). Точка входа
        # обязана ВОЗВРАЩАТЬ код: её зовут и как функцию — из тестов и из хаба.
        return int(exc.code or 0)

    handler = _COMMANDS.get(getattr(args, "command", None) or "")
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler(args)
    except ContextUnavailable as exc:
        # Отдельной веткой: «рядом нет CLI BPMkit» — это окружение (код 2), а не отказ по
        # существу вопроса, который человек задал.
        _print_error(exc)
        return 2
    except CompanionError as exc:
        _print_error(exc)
        return 1
    except KeyboardInterrupt:
        _out("Прервано пользователем.")
        return 0
    except OSError as exc:
        # Файловые отказы вокруг канала (нечитаемый конфиг, недоступный каталог) — тоже
        # окружение. Стек-трейс наружу не отдаём.
        print(f"ОШИБКА: локальная файловая ошибка\nПричина: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
