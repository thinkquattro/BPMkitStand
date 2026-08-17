"""
Hosting backends — как стенд ХОСТИТСЯ на своей машине (kestrel-процесс
standkit / IIS Application Pool / Docker-контейнер), ортогонально
``standkit.models.Transport`` (тот определяет, ГДЕ управлять стендом:
локально или через агента). См. ADR-0001
(docs/adr/0001-hosting-backends.md).

Только стандартная библиотека Python (``subprocess``, ``shutil``,
``pathlib``) — как и весь пакет ``standkit``.

Соглашения об ошибках:
  - ``start``/``stop``/``restart``/``read_logs`` бросают ``HostingError`` с
    понятным текстом (включая stderr внешней команды), если операция не
    удалась — голый ``subprocess.CalledProcessError``/``OSError`` наружу не
    просачивается;
  - ``is_running`` — проба, никогда не бросает: ошибка (appcmd/docker/kubectl
    не найден, команда упала, парсинг не удался) трактуется как "состояние
    выяснить не удалось" и заменяется TCP-фолбэком на порт стенда; если порт
    тоже не открыт — результат ``False``.

ОТВЕТ CLI АВТОРИТЕТНЕЕ ОТКРЫТОГО ПОРТА. Фолбэк применяется ТОЛЬКО там, где
состояние выяснить не удалось. Если appcmd/docker/kubectl успешно ответили
«не запущено», это финальный вердикт: открытый TCP-порт ничего не опровергает —
его может держать http.sys остановленного IIS-сайта, NodePort деплоймента с
нулём реплик или посторонний процесс. Обратное поведение уже давало ложное
«стенд жив» и по IIS, и по k8s (живая приёмка 17.08.2026).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from standkit.models import HostKind, Stand

# Таймаут внешних вызовов (appcmd/docker) по умолчанию — операции над
# App Pool/контейнером обычно быстрые; значение с запасом, чтобы не резать
# легитимно медленный docker compose up на первом старте образа.
_DEFAULT_TIMEOUT = 20.0

# Сколько секунд KubernetesBackend.restart ждёт завершения выката
# (`kubectl rollout status`). 0 — не ждать вовсе, прежнее поведение: kubectl
# только ПРОСИЛИ перезапустить, и сразу после вызова стенд выглядит здоровым,
# потому что старые поды ещё готовы.
K8S_ROLLOUT_WAIT_SEC = 60.0

# Предел параллельных запросов логов при чтении по label-селектору
# (`kubectl logs -l ... --max-log-requests`): защищает от деплоймента с
# десятками подов, но покрывает реальные стенды.
K8S_MAX_LOG_REQUESTS = 10

# Пауза перед единственным повтором ЧИТАЮЩЕЙ команды appcmd, упавшей на
# транзиентном сбое RPC до службы WAS (см. _appcmd_checked).
_TRANSIENT_RPC_RETRY_DELAY = 1.0

# Таймаут ИЗМЕНЯЮЩИХ операций IIS (start/stop/recycle сайта и пула). Общий
# _DEFAULT_TIMEOUT=20с для них мал: `appcmd stop apppool` ждёт завершения
# рабочего процесса, а у самого IIS на это отведён shutdownTimeLimit (по
# умолчанию 90 секунд). Живая приёмка 17.08.2026: остановка пула прогретого
# стенда BPMSoft (.NET Framework) заняла 20.6с — диспетчер получал таймаут и
# рапортовал ошибку, хотя IIS штатно останавливал пул. Берём 120с — с запасом
# над дефолтным лимитом самого IIS.
_IIS_LIFECYCLE_TIMEOUT = 120.0

# Состояния сайта/пула, которые appcmd отдаёт, когда САМ не знает ответа
# (службы IIS остановлены/перезапускаются). Их нельзя приравнивать к
# «остановлен» — иначе сломанный канал управления выглядит как штатно
# погашенный стенд (живая приёмка IIS 17.08.2026).
_INDETERMINATE_IIS_STATES = ("unknown", "")


class HostingError(Exception):
    """Ошибка бэкенда хостинга (внешняя утилита не найдена, команда завершилась с ошибкой и т.п.)."""


class IisServiceUnavailableError(HostingError):
    """
    Службы IIS (WAS/W3SVC) остановлены: ``appcmd`` физически не может узнать
    состояние сайта/пула, а ``list site`` начинает отдавать ``Unknown``.

    Отдельный тип нужен ровно потому, что ДО живой приёмки 17.08.2026 этот
    случай выдавался за нехватку прав администратора (см. ``_ELEVATION_MARKERS``):
    пользователь перезапускал хаб «от имени администратора» вместо того, чтобы
    поднять службу.
    """


class IisElevationError(HostingError):
    """
    Отдельный тип для самой частой причины отказа IIS-операций: диспетчер
    запущен БЕЗ прав администратора, а ``appcmd.exe`` без elevation не может
    даже прочитать ``redirection.config`` в ``%windir%\\system32\\inetsrv``.

    Нужен, чтобы UI показал внятный диагноз «требуются права администратора»
    вместо общего текста ошибки внешней команды.
    """


@runtime_checkable
class HostingBackend(Protocol):
    """Единый протокол бэкенда хостинга — см. ADR-0001."""

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        """Запускает стенд. Возвращает pid (kestrel) либо None (iis/docker — нет понятия pid)."""
        ...

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        """Останавливает стенд. Возвращает True при успешной остановке."""
        ...

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        """Останавливает (если жив) и заново запускает стенд."""
        ...

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        """Проверяет, запущен ли стенд. Никогда не бросает — см. модуль docstring."""
        ...

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        """
        Последние ``n`` строк лога бэкенда, либо ``None``.

        ``None`` означает «этот бэкенд лога не даёт» — CLI не найден, каталог
        лога не задан и т.п.; вызывающий тогда читает файл-лог сам. Ошибка уже
        начатого чтения (утилита есть, но команда упала) — по-прежнему
        ``HostingError``. Контракт единый для всех бэкендов: раньше docker в
        отсутствие CLI отдавал ``None``, а k8s в том же случае бросал
        исключение, и вызывающему приходилось обрабатывать оба варианта.
        """
        ...


def get_backend(stand: Stand) -> HostingBackend:
    """Возвращает экземпляр бэкенда хостинга по ``stand.host_kind``."""
    if stand.host_kind == HostKind.KESTREL:
        return KestrelBackend()
    if stand.host_kind == HostKind.IIS:
        return IisBackend()
    if stand.host_kind == HostKind.DOCKER:
        return DockerBackend()
    if stand.host_kind == HostKind.K8S:
        return KubernetesBackend()
    raise HostingError(f"host_kind={stand.host_kind.value!r} не поддерживается ядром standkit")


def _oem_encoding() -> str:
    """Кодировка вывода консольных утилит Windows (appcmd и т.п. пишут в OEM,
    не в UTF-8/ANSI). Вне Windows — utf-8."""
    if sys.platform == "win32":
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # обычно cp866 (RU)
        except Exception:
            return "cp866"
    return "utf-8"


def _decode_console(data) -> str:
    """Декодирует вывод внешней команды. Принимает bytes (реальный запуск) или
    str (замоканный в тестах — отдаётся как есть). Для bytes перебирает
    utf-8 → OEM (cp866) → cp1251, чтобы не превращать кириллицу appcmd в
    кракозябры (баг «остановка IIS: непонятный текст ошибки»)."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", _oem_encoding(), "cp1251"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _run(cmd: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Выполняет внешнюю команду, оборачивая ошибки спавна/таймаута в ``HostingError``.
    Захватывает вывод БАЙТАМИ и декодирует (см. ``_decode_console``) — надёжнее,
    чем ``text=True, encoding='utf-8'``, для консольных утилит в OEM-кодировке."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostingError(f"Не удалось выполнить команду {cmd!r}: {exc}") from exc
    return subprocess.CompletedProcess(
        cmd, proc.returncode, _decode_console(proc.stdout), _decode_console(proc.stderr)
    )


# Признаки того, что внешняя команда упала из-за НЕХВАТКИ ПРАВ (нужен запуск
# «от имени администратора»). Для appcmd это типично: чтение config/
# redirection.config в %windir%\system32\inetsrv требует elevation.
#
# Список УЗКИЙ намеренно (живая приёмка IIS 17.08.2026). Прежние маркеры
# "error ( message:" и "(код 1168)" ловили ЛЮБУЮ ошибку appcmd, и elevated-
# диспетчер получал диагноз «не хватает прав администратора» там, где реальная
# причина другая: «Не удалось найти объект SITE» (код 1168 — это
# ERROR_NOT_FOUND, а не отказ в доступе) и «Служба WAS недоступна» (код 50).
# Ложный диагноз хуже отсутствия подсказки: он отправляет пользователя
# перезапускать хаб от администратора вместо того, чтобы поднять службу или
# исправить имя сайта.
_ELEVATION_MARKERS = (
    "redirection.config",
    "access is denied",
    "отказано в доступе",
    "необходимых разрешений",
    "0x80070005",
    "requires administrator",
    "elevated",
)

# Служба активации Windows (WAS) / служба веб-публикаций (W3SVC) остановлена:
# appcmd отвечает кодом 50 (ERROR_NOT_SUPPORTED) с явным текстом, а состояние
# сайта в `list site` при этом становится "Unknown". Диагноз — «поднимите
# службу», а НЕ «запустите от имени администратора».
_WAS_DOWN_MARKERS = (
    "служба was недоступна",
    "was service is not available",
    "(код 50)",
)

WAS_DOWN_HINT = (
    "\n\nПохоже, остановлена служба IIS: appcmd не может обратиться к службе "
    "активации Windows (WAS). Запустите службы WAS и W3SVC (`sc start WAS`, "
    "`sc start W3SVC`, либо `iisreset /start`) и повторите операцию."
)

# Транзиентный сбой канала управления IIS: RPC до WAS отвалился (служба
# перезапускается/падала). Живая приёмка 17.08.2026: `appcmd list wp` отдавал
# код 1726 (RPC failed) и 2147549190 (0x80010006, «подключение разорвано»)
# сразу после операций над пулами, а через секунду та же команда работала.
# Для ЧИТАЮЩИХ команд такой ответ ретраится один раз (см. _run), для
# изменяющих — к тексту ошибки добавляется подсказка (повтор небезопасен
# автоматически, решение за пользователем).
_TRANSIENT_RPC_MARKERS = (
    "hresult:800706be",
    "hresult:80010006",
    "(код 1726)",
    "(код 2147549190)",
    "сбой при удаленном вызове процедуры",
    "the remote procedure call failed",
)

TRANSIENT_RPC_HINT = (
    "\n\nПохоже, канал управления IIS отвалился на момент вызова (RPC до службы "
    "WAS): служба перезапускается либо только что падала. Проверьте состояние "
    "служб WAS/W3SVC и повторите операцию."
)

# Подсказка, которая дописывается к любой ошибке appcmd, похожей на нехватку
# прав — одна формулировка на все места (start/stop/restart/list/wp).
ELEVATION_HINT = (
    "\n\nПохоже, не хватает прав администратора: управление IIS через appcmd.exe "
    "требует запуска диспетчера «от имени администратора» (elevated). Запустите "
    "standkit-hub с правами администратора и повторите операцию."
)


def _looks_like_elevation_error(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _ELEVATION_MARKERS)


def _looks_like_was_down(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _WAS_DOWN_MARKERS)


def _looks_like_transient_rpc(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _TRANSIENT_RPC_MARKERS)


def _process_is_elevated() -> Optional[bool]:
    """
    Запущен ли ТЕКУЩИЙ процесс с правами администратора. ``None`` — выяснить не
    удалось (не Windows либо ctypes недоступен).

    Нужен для честной классификации отказа appcmd: без elevation appcmd не
    читает даже свой ``redirection.config``, поэтому ЛЮБАЯ его ошибка в
    неэлевированном процессе — про права, как бы Windows её ни сформулировала
    (живая приёмка 17.08.2026: неэлевированный ``list wp`` отвечает «Служба WAS
    недоступна», хотя служба работает).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _service_state(name: str) -> Optional[str]:
    """
    Состояние Windows-службы по ``sc query`` (``RUNNING``/``STOPPED``/...) либо
    ``None``, если выяснить не удалось. Второй канал для диагноза «служба
    остановлена» — текст ошибки appcmd сам по себе не доказательство.
    """
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(["sc", "query", name], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = _decode_console(proc.stdout).upper()
    for token in ("RUNNING", "STOP_PENDING", "START_PENDING", "STOPPED", "PAUSED"):
        if token in text:
            return token
    return None


def _iis_services_down() -> list[str]:
    """Список остановленных служб IIS из (WAS, W3SVC) — пусто, если обе живы/неизвестны."""
    down = []
    for name in ("WAS", "W3SVC"):
        state = _service_state(name)
        if state in ("STOPPED", "STOP_PENDING"):
            down.append(name)
    return down


def _run_checked(cmd: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Как ``_run``, но дополнительно бросает ``HostingError``, если код возврата не 0."""
    result = _run(cmd, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise HostingError(f"Команда {cmd!r} завершилась с ошибкой (код {result.returncode}): {detail}")
    return result


def _is_read_only_appcmd(cmd: list[str]) -> bool:
    """``appcmd list ...`` — читающая команда (её безопасно повторить)."""
    return len(cmd) > 1 and cmd[1].lower() == "list"


def _appcmd_checked(cmd: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """
    ``_run_checked`` для appcmd с ЧЕСТНОЙ классификацией отказа (живая приёмка
    IIS 17.08.2026 — до неё любая ошибка appcmd объявлялась нехваткой прав):

    1. транзиентный сбой RPC до WAS — читающая команда (``appcmd list ...``)
       повторяется один раз, изменяющая отдаётся с подсказкой про канал
       управления (автоматически повторять изменение нельзя);
    2. явные признаки нехватки прав (``redirection.config``, «отказано в
       доступе», ``0x80070005``) — ``IisElevationError``;
    3. процесс НЕ elevated — ``IisElevationError`` независимо от формулировки
       Windows (без elevation appcmd не работает в принципе);
    4. «Служба WAS недоступна» в elevated-процессе, подтверждённая состоянием
       службы, — ``IisServiceUnavailableError`` с подсказкой «поднимите службу»;
    5. остальное (например «Не удалось найти объект SITE», код 1168) — обычный
       ``HostingError`` без ложных подсказок.
    """
    attempts = 2 if _is_read_only_appcmd(cmd) else 1
    last: Optional[HostingError] = None
    for attempt in range(attempts):
        try:
            return _run_checked(cmd, timeout=timeout)
        except HostingError as exc:
            last = exc
            if attempt + 1 < attempts and _looks_like_transient_rpc(str(exc)):
                time.sleep(_TRANSIENT_RPC_RETRY_DELAY)
                continue
            break

    assert last is not None
    text = str(last)
    if _looks_like_transient_rpc(text):
        raise HostingError(text + TRANSIENT_RPC_HINT) from last
    if _looks_like_elevation_error(text):
        raise IisElevationError(text + ELEVATION_HINT) from last
    if _process_is_elevated() is False:
        raise IisElevationError(text + ELEVATION_HINT) from last
    if _looks_like_was_down(text):
        down = _iis_services_down()
        suffix = WAS_DOWN_HINT
        if down:
            suffix += " Сейчас остановлены: %s." % ", ".join(down)
        raise IisServiceUnavailableError(text + suffix) from last
    raise last


def _tcp_fallback(stand: Stand) -> bool:
    """Фолбэк-проба «жив ли стенд» по открытому TCP-порту (см. ADR-0001)."""
    from standkit import health as _health  # локальный импорт — избегаем цикла

    return _health.tcp_open(stand.stand_host, stand.stand_port)


# --------------------------------------------------------------------------
# KestrelBackend — обёртка над ТЕКУЩЕЙ логикой standkit.lifecycle.
# --------------------------------------------------------------------------


class KestrelBackend:
    """
    Обёртка над существующей логикой ``standkit.lifecycle`` (kestrel-путь).
    Поведение бит-в-бит прежнее; ``read_logs`` → ``None`` (хаб читает
    файл-лог как сейчас, см. ``standkit.logs.tail``).

    Вызывает ПРИВАТНЫЕ функции ``lifecycle._kestrel_*`` напрямую (а не
    публичные ``lifecycle.start``/``stop``/``restart``/``is_running``),
    чтобы избежать рекурсии диспетчер(``lifecycle``) → бэкенд(``hosting``) →
    диспетчер(``lifecycle``) — см. ADR-0001. Импорт ``lifecycle`` — локальный
    (внутри методов), чтобы не создавать цикл модулей при импорте
    ``standkit.hosting``.
    """

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_start(stand, run_dir=run_dir, log_dir=log_dir)

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None, force: bool = False) -> bool:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_stop(stand, run_dir=run_dir, force=force)

    def restart(
        self,
        stand: Stand,
        *,
        run_dir: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        force: bool = False,
    ) -> Optional[int]:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_restart(stand, run_dir=run_dir, log_dir=log_dir, force=force)

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_is_running(stand, run_dir=run_dir)

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None


# --------------------------------------------------------------------------
# IisBackend — через appcmd.exe (Windows-only).
# --------------------------------------------------------------------------


def _resolve_appcmd() -> str:
    """Резолвит путь к ``appcmd.exe``. Бросает ``HostingError`` вне Windows либо если файла нет."""
    if sys.platform != "win32":
        raise HostingError(
            "host_kind=iis поддерживается только на Windows (нужен appcmd.exe) — "
            f"текущая платформа: {sys.platform!r}"
        )
    appcmd = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "system32", "inetsrv", "appcmd.exe")
    if not os.path.isfile(appcmd):
        raise HostingError(
            f"appcmd.exe не найден: {appcmd} — убедитесь, что установлены IIS Management "
            "Tools (Windows Features → Web Management Tools → IIS Management Console/Scripts)"
        )
    return appcmd


@dataclass
class IisSiteMatch:
    """
    Результат автоопределения IIS-сайта стенда (см. ``detect_iis_site``) —
    прямой аналог «поиска pid по порту» для kestrel: у IIS pid'а нет, зато
    есть сайт с physical path и биндингом.
    """

    site: Optional[str] = None
    app_pool: Optional[str] = None
    physical_path: Optional[str] = None
    port: Optional[int] = None
    matched_by: str = ""  # "physical_path" | "binding"

    def to_dict(self) -> dict:
        return {
            "site": self.site,
            "app_pool": self.app_pool,
            "physical_path": self.physical_path,
            "port": self.port,
            "matched_by": self.matched_by,
        }


@dataclass
class IisState:
    """
    Развёрнутое состояние IIS-стенда: наружу (в ``ProbeState``) уходит один
    ``DOWN``, а причин у него три принципиально разных — «сайт остановлен»,
    «пул приложений остановлен» и «порт держит http.sys, стенд отдаёт 503».
    ``reason`` показывается в UI, чтобы не гадать по одному бейджу.
    """

    running: bool = False
    site_state: Optional[str] = None
    pool_state: Optional[str] = None
    port_open: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "site_state": self.site_state,
            "pool_state": self.pool_state,
            "port_open": self.port_open,
            "reason": self.reason,
        }


# `appcmd list wp` печатает строки вида: WP "6832" (applicationPool:DefaultAppPool)
_WP_RE = re.compile(r'^\s*WP\s+"?(?P<pid>\d+)"?\s*\(applicationPool:(?P<pool>[^)]*)\)', re.IGNORECASE)


def parse_worker_processes(output: str) -> list[dict]:
    """Разбирает вывод ``appcmd list wp`` → ``[{"pid": int, "app_pool": str}, ...]``."""
    result: list[dict] = []
    for line in output.splitlines():
        m = _WP_RE.match(line)
        if not m:
            continue
        try:
            pid = int(m.group("pid"))
        except ValueError:
            continue
        result.append({"pid": pid, "app_pool": m.group("pool").strip()})
    return result


def _parse_appcmd_xml(xml_text: str, tag: str) -> list[dict]:
    """
    Разбирает XML-вывод ``appcmd list <объекты> /xml`` и возвращает атрибуты
    элементов ``tag`` (``SITE``/``VDIR``/``APP``) как список словарей.

    Пустой список — вывод пуст или это не XML (например, appcmd напечатал
    ошибку). Разбор — ``xml.etree.ElementTree`` из stdlib.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    return [dict(el.attrib) for el in root.iter(tag)]


def parse_binding_ports(bindings: str) -> list[int]:
    """
    Порты HTTP(S)-биндингов из атрибута ``bindings`` элемента ``SITE``
    (``"http/*:5000:,net.tcp/808:*"``). Не-HTTP протоколы игнорируются — иначе
    ``net.tcp/808:*`` дал бы ложный «порт 808».
    """
    ports: list[int] = []
    for binding in (bindings or "").split(","):
        binding = binding.strip()
        if not binding:
            continue
        protocol, _, rest = binding.partition("/")
        if protocol.lower() not in ("http", "https"):
            continue
        parts = rest.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        port = int(parts[1])
        if port not in ports:
            ports.append(port)
    return ports


def _same_path(left: Optional[str], right: Optional[str]) -> bool:
    """Сравнение путей IIS и реестра: раскрываем ``%SystemDrive%`` и нормализуем регистр/слэши."""
    if not left or not right:
        return False
    a = os.path.normcase(os.path.normpath(os.path.expandvars(str(left))))
    b = os.path.normcase(os.path.normpath(os.path.expandvars(str(right))))
    return a == b


def _site_of_vdir(vdir_name: str) -> str:
    """Имя сайта из ``VDIR.NAME`` вида ``"Default Web Site/"`` → ``"Default Web Site"``."""
    return (vdir_name or "").split("/", 1)[0]


def detect_iis_site(stand: Stand) -> Optional[IisSiteMatch]:
    """
    Автоопределение IIS-сайта стенда, развёрнутого мимо диспетчера (в реестре
    нет ``iis_site``/``iis_app_pool``).

    Сопоставление — по двум признакам, в порядке надёжности:
      1. physical path виртуального каталога == ``stand.stand_dir``
         (``appcmd list vdirs /xml``);
      2. HTTP-биндинг сайта на ``stand.stand_port`` (``appcmd list sites /xml``).

    Пул приложений подтягивается из ``appcmd list apps /xml`` — чтобы кнопка
    «Определить автоматически» в форме регистрации заполнила оба поля разом.

    ``None`` — сопоставить не удалось. Ошибка прав администратора НЕ глотается
    (пробрасывается ``IisElevationError``): иначе пользователь видел бы «не
    нашли сайт» вместо реальной причины.
    """
    appcmd = _resolve_appcmd()
    sites = _parse_appcmd_xml(_appcmd_checked([appcmd, "list", "sites", "/xml"]).stdout, "SITE")
    vdirs = _parse_appcmd_xml(_appcmd_checked([appcmd, "list", "vdirs", "/xml"]).stdout, "VDIR")

    match: Optional[IisSiteMatch] = None

    for vdir in vdirs:
        if _same_path(vdir.get("physicalPath"), stand.stand_dir):
            match = IisSiteMatch(
                site=_site_of_vdir(vdir.get("VDIR.NAME", "")) or None,
                physical_path=os.path.expandvars(vdir.get("physicalPath", "")) or None,
                matched_by="physical_path",
            )
            break

    if match is None and stand.stand_port:
        for site in sites:
            if int(stand.stand_port) in parse_binding_ports(site.get("bindings", "")):
                match = IisSiteMatch(
                    site=site.get("SITE.NAME") or None,
                    port=int(stand.stand_port),
                    matched_by="binding",
                )
                break

    if match is None:
        return None

    # Порт из биндинга сайта — даже когда совпали по physical path (полезно
    # показать пользователю, что найденный сайт действительно на его порту).
    if match.port is None:
        for site in sites:
            if site.get("SITE.NAME") == match.site:
                ports = parse_binding_ports(site.get("bindings", ""))
                match.port = ports[0] if ports else None
                break

    # Physical path — когда совпали по биндингу.
    if match.physical_path is None:
        for vdir in vdirs:
            if _site_of_vdir(vdir.get("VDIR.NAME", "")) == match.site:
                match.physical_path = os.path.expandvars(vdir.get("physicalPath", "")) or None
                break

    apps = _parse_appcmd_xml(_appcmd_checked([appcmd, "list", "apps", "/xml"]).stdout, "APP")
    for app in apps:
        if _site_of_vdir(app.get("APP.NAME", "")) == match.site:
            # ВНИМАНИЕ: `appcmd list apps /xml` называет атрибут пула
            # APPPOOL.NAME (а НЕ applicationPool, как в applicationHost.config
            # и как читал прежний код) — из-за этого автоопределение всегда
            # возвращало пустой пул, кнопка «Определить автоматически»
            # заполняла только сайт, а kill_worker_processes (требует явного
            # iis_app_pool) оставался недоступен. Живая приёмка 17.08.2026;
            # юнит-тесты этот атрибут не мокали вовсе. Второе имя оставлено
            # фолбэком — на случай иной версии appcmd.
            match.app_pool = app.get("APPPOOL.NAME") or app.get("applicationPool") or None
            break

    return match


class IisBackend:
    """
    Бэкенд хостинга через IIS (``appcmd.exe``). Windows-only — на других
    платформах любой метод бросает ``HostingError`` с понятным текстом
    (кроме ``is_running``, которая ловит ошибку и уходит в TCP-фолбэк).
    """

    def _query_state(self, appcmd: str, target: str, name: str) -> Optional[str]:
        """
        ``appcmd list <target> <name> /text:state`` → строка состояния либо
        ``None``, если состояние выяснить НЕ удалось.

        ``None`` возвращается не только при ошибке команды, но и когда appcmd
        ответил ``Unknown`` (или пустотой): так он говорит «я сам не знаю» —
        типично при остановленных службах WAS/W3SVC. Приравнивать это к
        «остановлен» нельзя (живая приёмка 17.08.2026: остановка W3SVC давала
        вердикт «сайт остановлен (state=Unknown)» — сломанный канал управления
        выглядел как погашенный стенд).
        """
        result = _run([appcmd, "list", target, name, "/text:state"])
        if result.returncode != 0:
            return None
        state = result.stdout.strip()
        if state.lower() in _INDETERMINATE_IIS_STATES:
            return None
        return state

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        appcmd = _resolve_appcmd()
        if not (stand.iis_app_pool or stand.iis_site):
            raise HostingError(
                f"стенд '{stand.name}': host_kind=iis требует iis_site и/или iis_app_pool"
            )
        if stand.iis_app_pool:
            _appcmd_checked([appcmd, "start", "apppool", f"/apppool.name:{stand.iis_app_pool}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
        if stand.iis_site:
            _appcmd_checked([appcmd, "start", "site", f"/site.name:{stand.iis_site}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
        return None

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        appcmd = _resolve_appcmd()
        if not (stand.iis_app_pool or stand.iis_site):
            raise HostingError(
                f"стенд '{stand.name}': host_kind=iis требует iis_site и/или iis_app_pool"
            )
        # «Стенд в IIS» = его SITE. Останавливаем ТОЛЬКО сайт и НЕ трогаем
        # App Pool: пул может быть общим с другими приложениями, а его остановка
        # положила бы и их (решение Владимира — гасить только стенд). App Pool
        # гасим лишь как единственный хэндл, когда сайт вообще не задан.
        if stand.iis_site:
            _appcmd_checked([appcmd, "stop", "site", f"/site.name:{stand.iis_site}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
            return True
        _appcmd_checked([appcmd, "stop", "apppool", f"/apppool.name:{stand.iis_app_pool}"],
                        timeout=_IIS_LIFECYCLE_TIMEOUT)
        return True

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        appcmd = _resolve_appcmd()
        # Рестарт стенда = рестарт его SITE (stop+start сайта). App Pool НЕ
        # трогаем/не рециклим — он может быть общим (см. stop). Recycle пула —
        # только когда сайт не задан (пул — единственный хэндл стенда).
        if stand.iis_site:
            _appcmd_checked([appcmd, "stop", "site", f"/site.name:{stand.iis_site}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
            _appcmd_checked([appcmd, "start", "site", f"/site.name:{stand.iis_site}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
        elif stand.iis_app_pool:
            _appcmd_checked([appcmd, "recycle", "apppool", f"/apppool.name:{stand.iis_app_pool}"],
                            timeout=_IIS_LIFECYCLE_TIMEOUT)
        else:
            raise HostingError(
                f"стенд '{stand.name}': host_kind=iis требует iis_site и/или iis_app_pool"
            )
        return None

    def describe_state(self, stand: Stand, *, run_dir: Optional[Path] = None) -> IisState:
        """
        Развёрнутое состояние стенда в IIS — тот же вердикт, что у
        ``is_running``, плюс ПРИЧИНА (см. ``IisState``). Проба: наружу ничего
        не бросает.

        Идентичность стенда в IIS — его SITE (см. stop/restart: управляем
        только сайтом, App Pool не трогаем). Поэтому «работает ли стенд» =
        состояние САЙТА; пул смотрим дополнительно — остановленный пул
        означает, что сайт хоть и Started, но запросы не обслуживаются.
        Когда сайт не задан, пул остаётся единственным хэндлом.

        ВАЖНО: НЕ падать на TCP-фолбэк при определённом ответе appcmd —
        IIS/http.sys держит порт 80/443 даже у остановленного сайта (503),
        открытый порт НЕ означает «стенд работает». TCP-фолбэк — только когда
        appcmd состояния не дал (сайт/пул не найден, ошибка команды, нет прав).
        """
        state = IisState()
        try:
            appcmd = _resolve_appcmd()
        except HostingError as exc:
            state.port_open = _tcp_fallback(stand)
            state.running = state.port_open
            state.reason = f"состояние IIS не опрошено ({exc}) — вердикт по TCP-порту"
            return state

        if stand.iis_site:
            state.site_state = self._query_state(appcmd, "site", stand.iis_site)
        if stand.iis_app_pool:
            state.pool_state = self._query_state(appcmd, "apppool", stand.iis_app_pool)

        primary = state.site_state if stand.iis_site else state.pool_state
        if primary is None:
            state.port_open = _tcp_fallback(stand)
            state.running = state.port_open
            down = _iis_services_down()
            if down:
                # Самая частая причина «состояние Unknown» — остановленные службы
                # IIS. Говорим это прямо, чтобы пользователь поднимал службу, а не
                # искал стенд (живая приёмка 17.08.2026).
                state.reason = (
                    "состояние не выяснено: остановлены службы IIS (%s) — appcmd отдаёт "
                    "Unknown; вердикт по TCP-порту" % ", ".join(down)
                )
            else:
                state.reason = (
                    "appcmd не вернул определённого состояния (сайт/пул не найден, "
                    "состояние Unknown либо не хватает прав) — вердикт по TCP-порту"
                )
            return state

        site_ok = (state.site_state == "Started") if stand.iis_site else True
        pool_ok = (state.pool_state != "Stopped") if stand.iis_app_pool else True
        state.running = site_ok and pool_ok and primary == "Started"

        if state.running:
            state.reason = "сайт запущен" if stand.iis_site else "пул приложений запущен"
            return state

        # Стенд не работает — говорим, ПОЧЕМУ именно.
        state.port_open = _tcp_fallback(stand)
        if stand.iis_site and state.site_state != "Started":
            state.reason = f"сайт остановлен (state={state.site_state})"
        elif state.pool_state == "Stopped":
            state.reason = "пул приложений остановлен"
        else:
            state.reason = f"состояние: {primary}"
        if state.port_open:
            state.reason += " — порт при этом занят http.sys, стенд отдаёт 503"
        return state

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        return self.describe_state(stand, run_dir=run_dir).running

    def worker_processes(self, stand: Stand) -> list[dict]:
        """
        Worker-процессы (``w3wp.exe``) пула этого стенда — ``appcmd list wp``.

        Если пул стенда известен (``iis_app_pool``), список фильтруется по нему;
        иначе возвращаются все worker-процессы (пользователь сам решит, что с
        ними делать — гадать за него нельзя).
        """
        appcmd = _resolve_appcmd()
        result = _appcmd_checked([appcmd, "list", "wp"])
        wps = parse_worker_processes(result.stdout)
        if stand.iis_app_pool:
            return [wp for wp in wps if wp["app_pool"] == stand.iis_app_pool]
        return wps

    def kill_worker_processes(self, stand: Stand) -> list[int]:
        """
        Снимает зависшие worker-процессы стенда по pid (``standkit.platform.stop``
        с эскалацией мягко→жёстко).

        Закрывает случай «``appcmd stop site`` отработал, а ``w3wp.exe`` висит
        намертво, и убить его нечем» — той же машинерией, что и усыновление
        kestrel-стенда. Требует ``iis_app_pool``: снимать ВСЕ worker-процессы
        машины (в т.ч. чужих приложений) диспетчер не станет.
        """
        if not stand.iis_app_pool:
            raise HostingError(
                f"стенд '{stand.name}': снятие worker-процессов требует явного iis_app_pool — "
                "иначе непонятно, какие именно w3wp.exe принадлежат этому стенду"
            )
        from standkit import platform as _platform  # локальный импорт — избегаем цикла

        killed: list[int] = []
        for wp in self.worker_processes(stand):
            if _platform.stop(wp["pid"]):
                killed.append(wp["pid"])
        return killed

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        directory = (
            Path(stand.iis_stdout_log_dir)
            if stand.iis_stdout_log_dir
            else Path(stand.stand_dir) / "logs"
        )
        if not directory.exists() or not directory.is_dir():
            return None
        log_files = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            # Стенд BPMSoft под .NET Framework раскладывает логи по ПОДПАПКАМ-датам
            # (Logs\2026_08_17\Application.log), в корне каталога *.log нет вовсе —
            # плоский glob возвращал None, и консоль стенда в диспетчере оставалась
            # пустой при 11 живых файлах лога (живая приёмка IIS 17.08.2026).
            # Обход ограничен ближайшими подпапками: рекурсия на весь каталог
            # стенда дорога, а логи лежат ровно на этом уровне.
            log_files = sorted(
                (p for p in directory.glob("*/*.log") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        if not log_files:
            return None
        from standkit import logs as _logs  # локальный импорт — избегаем цикла

        return _logs.tail(log_files[0], n)


# --------------------------------------------------------------------------
# DockerBackend — через CLI docker / docker compose (кроссплатформенно).
# --------------------------------------------------------------------------


def _resolve_docker() -> str:
    """Резолвит путь к ``docker``. Бросает ``HostingError``, если утилита не найдена в PATH."""
    docker = shutil.which("docker")
    if docker is None:
        raise HostingError(
            "docker не найден в PATH — установите Docker Engine/Docker Desktop, "
            "либо укажите его в PATH процесса standkit"
        )
    return docker


@dataclass
class ContainerState:
    """
    Развёрнутое состояние контейнерной нагрузки (docker-контейнер, compose-сервис,
    k8s-деплоймент) — аналог ``IisState`` для IIS.

    ``running`` — вердикт «обслуживает ли стенд» (его отдаёт ``is_running``),
    ``status`` — сырое состояние от CLI (``running``/``paused``/``exited`` и т.п.),
    ``restart_count`` — счётчик перезапусков (docker), ``reason`` — человеческое
    объяснение для витрины: почему стенд не работает или чем примечательно его
    состояние. ``reason`` попадает в ``StandStatus.details['process_reason']``
    (см. ``standkit.health.check_stand``), поэтому пишется по-русски и коротко.
    """

    running: bool
    status: str = ""
    restart_count: int = 0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "status": self.status,
            "restart_count": self.restart_count,
            "reason": self.reason,
        }


def _compose_service_state_json(ps_output: str, service: str) -> Optional[ContainerState]:
    """
    Разбирает вывод ``docker compose ps --format json`` и отдаёт состояние
    сервиса ``service``. Возвращает ``None``, если разобрать не удалось (старая
    версия compose, неожиданный формат) — вызывающий тогда пробует табличный
    вывод.

    Формат отличается между версиями: одни печатают JSON-массив, другие —
    NDJSON (по объекту на строку). Поддерживаем оба. Имя сервиса берём из поля
    ``Service`` и сравниваем НА РАВЕНСТВО — именно ради этого json и нужен
    (табличный разбор по подстроке путал ``web`` с ``webhook``).
    """
    text = (ps_output or "").strip()
    if not text:
        return None

    records: list[dict] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            records = [parsed]
        elif isinstance(parsed, list):
            records = [item for item in parsed if isinstance(item, dict)]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                return None
            if isinstance(item, dict):
                records.append(item)

    if not records:
        return None
    if not any("Service" in record for record in records):
        return None

    for record in records:
        if record.get("Service") != service:
            continue
        state = str(record.get("State") or record.get("Status") or "").lower()
        if state.startswith("running") or state.startswith("up"):
            return ContainerState(running=True, status=state)
        return ContainerState(
            running=False,
            status=state,
            reason="compose-сервис '%s' в состоянии '%s'" % (service, state or "неизвестно"),
        )
    return ContainerState(
        running=False,
        status="absent",
        reason="compose-сервис '%s' не поднят (нет в выводе docker compose ps)" % service,
    )


def _container_state_from_inspect(raw: str) -> ContainerState:
    """
    Строит ``ContainerState`` из вывода
    ``docker inspect -f {{.State.Status}}|{{.RestartCount}}|{{.State.ExitCode}}``.

    Ключевое: «жив» — это ``status == running``, а не ``State.Running``.
    У приостановленного контейнера ``Running`` остаётся ``true``, хотя запросов
    он не обслуживает; у контейнера в цикле перезапуска состояние прыгает между
    ``running`` и ``restarting``, и вердикт без пояснения выглядит как мигание.
    """
    parts = (raw or "").strip().split("|")
    status = (parts[0] if parts else "").strip().lower()
    restarts = 0
    exit_code = ""
    if len(parts) > 1:
        try:
            restarts = int(parts[1].strip() or 0)
        except ValueError:
            restarts = 0
    if len(parts) > 2:
        exit_code = parts[2].strip()

    if status == "running":
        reason = ""
        if restarts > 0:
            reason = (
                "контейнер уже перезапускался (перезапусков: %d) — возможен цикл падений, "
                "проверьте логи" % restarts
            )
        return ContainerState(running=True, status=status, restart_count=restarts, reason=reason)

    reasons = {
        "paused": "контейнер приостановлен (docker pause) — запросы не обслуживаются",
        "restarting": "контейнер в цикле перезапуска (перезапусков: %d)" % restarts,
        "exited": "контейнер остановлен (код выхода %s)" % (exit_code or "?"),
        "created": "контейнер создан, но ни разу не запускался",
        "dead": "контейнер в состоянии dead — требуется пересоздание",
        "removing": "контейнер удаляется",
    }
    return ContainerState(
        running=False,
        status=status,
        restart_count=restarts,
        reason=reasons.get(status, "состояние контейнера: '%s'" % (status or "неизвестно")),
    )


def _undetermined_state(stand: Stand, detail: str) -> ContainerState:
    """
    Состояние «выяснить не удалось»: вердикт выносится TCP-фолбэком, а причина
    честно сообщается витрине — чтобы «зелёный» по порту не выглядел как
    подтверждённый ответ CLI.
    """
    alive = _tcp_fallback(stand)
    return ContainerState(
        running=alive,
        status="unknown",
        reason="состояние выяснить не удалось (%s); вердикт по TCP-порту %s:%s"
        % (detail.strip() or "нет деталей", stand.stand_host, stand.stand_port),
    )


class DockerBackend:
    """
    Бэкенд хостинга через Docker CLI. Два режима — определяются полями
    записи стенда (см. ``Stand.validate``):
      - одиночный контейнер (``docker_container``) — ``docker start/stop/restart``;
      - compose-сервис (``docker_compose_file`` + ``docker_compose_service``) —
        ``docker compose -f <file> up -d|stop|restart <service>``.
    """

    def _mode(self, stand: Stand) -> str:
        if stand.docker_container:
            return "single"
        if stand.docker_compose_file and stand.docker_compose_service:
            return "compose"
        raise HostingError(
            f"стенд '{stand.name}': host_kind=docker требует docker_container либо "
            "(docker_compose_file И docker_compose_service)"
        )

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        docker = _resolve_docker()
        mode = self._mode(stand)
        if mode == "single":
            _run_checked([docker, "start", stand.docker_container])
        else:
            _run_checked(
                [docker, "compose", "-f", stand.docker_compose_file, "up", "-d", stand.docker_compose_service]
            )
        return None

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        docker = _resolve_docker()
        mode = self._mode(stand)
        if mode == "single":
            _run_checked([docker, "stop", stand.docker_container])
        else:
            _run_checked([docker, "compose", "-f", stand.docker_compose_file, "stop", stand.docker_compose_service])
        return True

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        docker = _resolve_docker()
        mode = self._mode(stand)
        if mode == "single":
            _run_checked([docker, "restart", stand.docker_container])
        else:
            _run_checked(
                [docker, "compose", "-f", stand.docker_compose_file, "restart", stand.docker_compose_service]
            )
        return None

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        return self.describe_state(stand, run_dir=run_dir).running

    def describe_state(self, stand: Stand, *, run_dir: Optional[Path] = None) -> ContainerState:
        """
        Разбирает состояние контейнера/compose-сервиса и объясняет его словами
        (см. ``ContainerState``). ``is_running`` — тонкая обёртка над этим методом,
        а ``standkit.health.check_stand`` забирает ``reason`` в детали статуса,
        как уже делает для IIS.

        Спрашивается ``.State.Status``, а не ``.State.Running``: у
        приостановленного контейнера (``docker pause``) ``Running=true``, хотя он
        не обслуживает ни одного запроса — живая приёмка 17.08.2026 показала его
        как «стенд работает».

        Ответ docker при rc=0 — ОКОНЧАТЕЛЬНЫЙ: открытым TCP-портом он не
        переопределяется (порт может держать посторонний процесс). Фолбэк — только
        там, где состояние выяснить не удалось. Та же семантика, что у IIS.
        """
        try:
            docker = _resolve_docker()
            mode = self._mode(stand)
        except HostingError as exc:
            return _undetermined_state(stand, str(exc))

        try:
            if mode == "single":
                result = _run(
                    [docker, "inspect", "-f", "{{.State.Status}}|{{.RestartCount}}|{{.State.ExitCode}}",
                     stand.docker_container]
                )
                if result.returncode == 0 and result.stdout.strip():
                    return _container_state_from_inspect(result.stdout)
                return _undetermined_state(stand, (result.stderr or result.stdout or "").strip())
            state = self._compose_service_state(docker, stand)
            if state is not None:
                return state
            return _undetermined_state(stand, "docker compose ps не дал разбираемого ответа")
        except HostingError as exc:
            return _undetermined_state(stand, str(exc))

    def _compose_service_state(self, docker: str, stand: Stand) -> Optional[ContainerState]:
        """
        Состояние compose-сервиса или ``None``, если выяснить не удалось (тогда
        вызывающий уходит в TCP-фолбэк).

        Сначала пробуем машиночитаемый ``docker compose ps --format json``:
        имя сервиса приходит отдельным полем, сравнение точное. Если версия
        docker compose такого формата не понимает — падаем на табличный вывод
        и разбираем его по ТОКЕНАМ (см. ``_compose_service_up``).
        """
        result = _run([docker, "compose", "-f", stand.docker_compose_file, "ps", "--format", "json"])
        if result.returncode == 0:
            state = _compose_service_state_json(result.stdout, stand.docker_compose_service)
            if state is not None:
                return state
        result = _run([docker, "compose", "-f", stand.docker_compose_file, "ps"])
        if result.returncode == 0:
            up = self._compose_service_up(result.stdout, stand.docker_compose_service)
            return ContainerState(
                running=up,
                status="up" if up else "not up",
                reason="" if up else "compose-сервис '%s' не значится запущенным в docker compose ps"
                % stand.docker_compose_service,
            )
        return None

    @staticmethod
    def _compose_service_up(ps_output: str, service: str) -> bool:
        """
        Ищет строку сервиса ``service`` в табличном выводе ``docker compose ps``
        и проверяет, что состояние похоже на "запущено" (``Up``/``running``,
        без учёта регистра) — формат вывода отличается между версиями
        docker compose (v1 таблица "Up 2 hours", v2 "running"/"Up").

        Имя сервиса сопоставляется как ОТДЕЛЬНЫЙ ТОКЕН строки, а не как
        подстрока: живая приёмка 17.08.2026 поймала ложный вердикт «запущен»
        для неподнятого сервиса ``web``, потому что в выводе была строка
        соседнего сервиса ``webhook`` со статусом ``Up 2 seconds`` — подстрока
        ``web`` в ней есть. Имена-подстроки в реальных compose-файлах обычны
        (``api``/``api-gateway``, ``app``/``app-worker``).
        """
        for line in ps_output.splitlines():
            tokens = line.split()
            if service not in tokens:
                continue
            lowered = [token.lower().strip(",") for token in tokens]
            if "up" in lowered or "running" in lowered:
                return True
        return False

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        try:
            docker = _resolve_docker()
            mode = self._mode(stand)
        except HostingError:
            return None

        if mode == "single":
            result = _run_checked([docker, "logs", "--tail", str(n), stand.docker_container])
        else:
            result = _run_checked(
                [
                    docker,
                    "compose",
                    "-f",
                    stand.docker_compose_file,
                    "logs",
                    "--tail",
                    str(n),
                    stand.docker_compose_service,
                ]
            )
        combined = result.stdout + result.stderr
        lines = combined.splitlines()
        return lines[-n:] if n > 0 else []


# --------------------------------------------------------------------------
# KubernetesBackend — через CLI kubectl (кроссплатформенно).
# --------------------------------------------------------------------------


def _resolve_kubectl() -> str:
    """Резолвит путь к ``kubectl``. Бросает ``HostingError``, если утилита не найдена в PATH."""
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise HostingError(
            "kubectl не найден в PATH — установите kubectl и настройте доступ к кластеру "
            "(kubeconfig/context), либо укажите kubectl в PATH процесса standkit"
        )
    return kubectl


class KubernetesBackend:
    """
    Бэкенд хостинга через Kubernetes CLI (``kubectl``). Требует
    ``k8s_deployment`` в записи стенда (см. ``Stand.validate``); ``k8s_namespace``
    пустой трактуется как ``default``, ``k8s_context`` — опционален (текущий
    контекст kubeconfig, если не задан).

    В Kubernetes нет понятия pid — ``start``/``restart`` возвращают ``None``
    (как iis/docker). "Стоп" реализован через масштабирование деплоймента до
    0 реплик (``scale --replicas=0``) — в Kubernetes нет отдельной команды
    "остановить", это общепринятый эквивалент.
    """

    def _namespace(self, stand: Stand) -> str:
        return stand.k8s_namespace or "default"

    def _deployment(self, stand: Stand) -> str:
        if not stand.k8s_deployment:
            raise HostingError(f"стенд '{stand.name}': host_kind=k8s требует k8s_deployment")
        return stand.k8s_deployment

    def _base_args(self, kubectl: str, stand: Stand) -> list[str]:
        args = [kubectl]
        if stand.k8s_context:
            args += ["--context", stand.k8s_context]
        args += ["-n", self._namespace(stand)]
        return args

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        replicas = stand.k8s_replicas or 1
        _run_checked(
            self._base_args(kubectl, stand)
            + ["scale", f"deployment/{deployment}", f"--replicas={replicas}"]
        )
        return None

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        """
        Масштабирует деплоймент в ноль реплик.

        При неудаче бросает ``HostingError`` с текстом kubectl — как это делает
        ``DockerBackend.stop``. Прежняя версия возвращала молчаливый ``False`` по
        ненулевому коду возврата, и вызывающий не мог отличить «остановлено» от
        «остановить не удалось, причина потеряна».
        """
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        _run_checked(
            self._base_args(kubectl, stand)
            + ["scale", f"deployment/{deployment}", "--replicas=0"]
        )
        return True

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        """
        Перезапускает деплоймент (``rollout restart``) и, если
        ``K8S_ROLLOUT_WAIT_SEC`` больше нуля, ДОЖИДАЕТСЯ завершения выката.

        Без ожидания ``rollout restart`` возвращается за доли секунды, старые
        поды ещё готовы, и сразу после вызова стенд выглядит здоровым, хотя
        новые поды могут не подняться вовсе. Живая приёмка 17.08.2026: вызов
        отработал за 0.19 с, ``is_running`` немедленно после него — ``True``.
        Теперь «перезапустили» означает перезапустили, а не «попросили».
        """
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        _run_checked(
            self._base_args(kubectl, stand) + ["rollout", "restart", f"deployment/{deployment}"]
        )
        if K8S_ROLLOUT_WAIT_SEC > 0:
            _run_checked(
                self._base_args(kubectl, stand)
                + [
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "--timeout=%ds" % int(K8S_ROLLOUT_WAIT_SEC),
                ],
                timeout=K8S_ROLLOUT_WAIT_SEC + 15,
            )
        return None

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        return self.describe_state(stand, run_dir=run_dir).running

    def describe_state(self, stand: Stand, *, run_dir: Optional[Path] = None) -> ContainerState:
        """
        Состояние деплоймента словами: сколько реплик готово из запрошенных.

        ПУСТОЙ ответ ``jsonpath={.status.readyReplicas}`` при rc=0 — это НЕ
        неопределённость: поля ``status.readyReplicas`` у деплоймента с нулём
        готовых реплик просто нет. Значит kubectl ОПРЕДЕЛЁННО ответил «готовых
        реплик нет» — доверяем ему и не маскируем открытым портом. Живая приёмка
        17.08.2026: после ``stop`` (scale 0) NodePort всё равно слушается, пока
        существует Service, и TCP-фолбэк показывал остановленный стенд
        работающим (``process=ok`` при ``http=down``).
        """
        try:
            kubectl = _resolve_kubectl()
            deployment = self._deployment(stand)
        except HostingError as exc:
            return _undetermined_state(stand, str(exc))

        try:
            result = _run(
                self._base_args(kubectl, stand)
                + [
                    "get",
                    "deployment",
                    deployment,
                    "-o",
                    "jsonpath={.status.readyReplicas}|{.spec.replicas}|{.status.unavailableReplicas}",
                ]
            )
        except HostingError as exc:
            return _undetermined_state(stand, str(exc))

        if result.returncode != 0:
            return _undetermined_state(stand, (result.stderr or result.stdout or "").strip())

        parts = (result.stdout or "").strip().split("|")

        def _num(index: int) -> Optional[int]:
            if len(parts) <= index:
                return 0
            raw = parts[index].strip()
            if not raw:
                return 0
            try:
                return int(raw)
            except ValueError:
                return None

        ready, desired, unavailable = _num(0), _num(1), _num(2)
        if ready is None or desired is None:
            # Нечисловой ответ — формат не тот, что ожидался: вердикт выносить
            # не на чем, это настоящая неопределённость.
            return _undetermined_state(stand, "неожиданный ответ kubectl: %r" % result.stdout.strip())

        if ready > 0:
            reason = ""
            if desired and ready < desired:
                reason = "готово %d из %d реплик (недоступно: %d)" % (ready, desired, unavailable or 0)
            return ContainerState(running=True, status="ready=%d/%d" % (ready, desired or ready), reason=reason)

        if not desired:
            reason = "деплоймент масштабирован в 0 реплик — стенд остановлен"
        else:
            reason = "нет готовых реплик (запрошено %d, недоступно %d)" % (desired, unavailable or desired)
        return ContainerState(running=False, status="ready=0/%d" % (desired or 0), reason=reason)

    def _selector(self, kubectl: str, stand: Stand, deployment: str) -> str:
        """
        Label-селектор деплоймента в форме ``k=v,k2=v2`` — нужен, чтобы читать
        логи ВСЕХ подов, а не одного. Пустая строка, если получить не удалось.
        """
        result = _run(
            self._base_args(kubectl, stand)
            + ["get", "deployment", deployment, "-o", "jsonpath={.spec.selector.matchLabels}"]
        )
        if result.returncode != 0:
            return ""
        raw = (result.stdout or "").strip()
        if not raw:
            return ""
        try:
            labels = json.loads(raw)
        except ValueError:
            return ""
        if not isinstance(labels, dict) or not labels:
            return ""
        return ",".join("%s=%s" % (key, value) for key, value in labels.items())

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        """
        Логи стенда. Читаются со ВСЕХ подов деплоймента по label-селектору с
        префиксом имени пода: ``kubectl logs deployment/X`` показывает логи
        ОДНОГО пода, выбранного самим kubectl, и при нескольких репликах часть
        событий стенда молча не видна (живая приёмка 17.08.2026 на двух
        репликах). Если селектор получить не удалось — старый путь по
        ``deployment/X``.

        ``None`` (а не исключение) при отсутствии kubectl — общий контракт
        ``read_logs`` для всех бэкендов, см. протокол ``HostingBackend``.
        """
        try:
            kubectl = _resolve_kubectl()
            deployment = self._deployment(stand)
        except HostingError:
            return None

        selector = self._selector(kubectl, stand, deployment)
        if selector:
            cmd = self._base_args(kubectl, stand) + [
                "logs",
                "-l",
                selector,
                "--prefix",
                "--max-log-requests",
                str(K8S_MAX_LOG_REQUESTS),
                "--tail",
                str(n),
            ]
        else:
            cmd = self._base_args(kubectl, stand) + ["logs", f"deployment/{deployment}", "--tail", str(n)]
        if stand.k8s_container:
            cmd += ["-c", stand.k8s_container]
        result = _run_checked(cmd)
        combined = result.stdout + result.stderr
        lines = combined.splitlines()
        if n <= 0:
            return []
        if selector:
            # ВАЖНО: при чтении по селектору `--tail n` уже применён kubectl'ом к
            # КАЖДОМУ поду. Дополнительно обрезать общий вывод хвостом нельзя —
            # хвост целиком принадлежит поду, который писал последним, и логи
            # остальных реплик снова пропадают (ровно это показал перепрогон
            # живой приёмки на двух репликах: строки только одного пода).
            return lines
        return lines[-n:]
