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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from standkit.models import HostKind, Stand

# Таймаут внешних вызовов (appcmd/docker) по умолчанию — операции над
# App Pool/контейнером обычно быстрые; значение с запасом, чтобы не резать
# легитимно медленный docker compose up на первом старте образа.
_DEFAULT_TIMEOUT = 20.0


class HostingError(Exception):
    """Ошибка бэкенда хостинга (внешняя утилита не найдена, команда завершилась с ошибкой и т.п.)."""


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
        """Последние ``n`` строк лога бэкенда, либо None — пусть вызывающий читает файл-лог сам."""
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
_ELEVATION_MARKERS = (
    "redirection.config",
    "access is denied",
    "отказано в доступе",
    "разрешени",       # «…необходимых разрешений»
    "0x80070005",
    "(код 1168)",
    "requires administrator",
    "elevated",
    "error ( message:",  # общий appcmd-ERROR при неудачном открытии config
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


def _run_checked(cmd: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Как ``_run``, но дополнительно бросает ``HostingError``, если код возврата не 0."""
    result = _run(cmd, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise HostingError(f"Команда {cmd!r} завершилась с ошибкой (код {result.returncode}): {detail}")
    return result


def _appcmd_checked(cmd: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """``_run_checked`` для appcmd: при ошибке нехватки прав бросает
    ``IisElevationError`` с понятной подсказкой «запустите от имени
    администратора» (подтип ``HostingError`` — прежние ``except HostingError``
    продолжают работать, но UI может отличить именно этот случай)."""
    try:
        return _run_checked(cmd, timeout=timeout)
    except HostingError as exc:
        if _looks_like_elevation_error(str(exc)):
            raise IisElevationError(str(exc) + ELEVATION_HINT) from exc
        raise


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
            match.app_pool = app.get("applicationPool") or None
            break

    return match


class IisBackend:
    """
    Бэкенд хостинга через IIS (``appcmd.exe``). Windows-only — на других
    платформах любой метод бросает ``HostingError`` с понятным текстом
    (кроме ``is_running``, которая ловит ошибку и уходит в TCP-фолбэк).
    """

    def _query_state(self, appcmd: str, target: str, name: str) -> Optional[str]:
        """``appcmd list <target> <name> /text:state`` → строка состояния либо None при ошибке."""
        result = _run([appcmd, "list", target, name, "/text:state"])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def start(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        appcmd = _resolve_appcmd()
        if not (stand.iis_app_pool or stand.iis_site):
            raise HostingError(
                f"стенд '{stand.name}': host_kind=iis требует iis_site и/или iis_app_pool"
            )
        if stand.iis_app_pool:
            _appcmd_checked([appcmd, "start", "apppool", f"/apppool.name:{stand.iis_app_pool}"])
        if stand.iis_site:
            _appcmd_checked([appcmd, "start", "site", f"/site.name:{stand.iis_site}"])
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
            _appcmd_checked([appcmd, "stop", "site", f"/site.name:{stand.iis_site}"])
            return True
        _appcmd_checked([appcmd, "stop", "apppool", f"/apppool.name:{stand.iis_app_pool}"])
        return True

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        appcmd = _resolve_appcmd()
        # Рестарт стенда = рестарт его SITE (stop+start сайта). App Pool НЕ
        # трогаем/не рециклим — он может быть общим (см. stop). Recycle пула —
        # только когда сайт не задан (пул — единственный хэндл стенда).
        if stand.iis_site:
            _appcmd_checked([appcmd, "stop", "site", f"/site.name:{stand.iis_site}"])
            _appcmd_checked([appcmd, "start", "site", f"/site.name:{stand.iis_site}"])
        elif stand.iis_app_pool:
            _appcmd_checked([appcmd, "recycle", "apppool", f"/apppool.name:{stand.iis_app_pool}"])
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
            state.reason = (
                "appcmd не вернул состояние (сайт/пул не найден либо не хватает прав) — "
                "вердикт по TCP-порту"
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


def _compose_service_up_json(ps_output: str, service: str) -> Optional[bool]:
    """
    Разбирает вывод ``docker compose ps --format json`` и отвечает, запущен ли
    сервис ``service``. Возвращает ``None``, если разобрать не удалось (старая
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
            return True
    return False


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
        try:
            docker = _resolve_docker()
            mode = self._mode(stand)
        except HostingError:
            return _tcp_fallback(stand)

        try:
            if mode == "single":
                # ВАЖНО: спрашиваем .State.Status, а не .State.Running. У
                # приостановленного контейнера (docker pause) Running=true, хотя
                # он не обслуживает ни одного запроса — живая приёмка 17.08.2026
                # показала его как «стенд работает».
                result = _run([docker, "inspect", "-f", "{{.State.Status}}", stand.docker_container])
                if result.returncode == 0:
                    state = result.stdout.strip().lower()
                    if state:
                        # docker ответил ОПРЕДЕЛЁННО — доверяем ему и НЕ маскируем
                        # открытым TCP-портом (порт может держать посторонний
                        # процесс или проброс остановленного соседа). Та же
                        # семантика, что у IIS-бэкенда: фолбэк только там, где
                        # состояние выяснить не удалось.
                        return state == "running"
            else:
                verdict = self._compose_service_state(docker, stand)
                if verdict is not None:
                    return verdict
        except HostingError:
            pass
        return _tcp_fallback(stand)

    def _compose_service_state(self, docker: str, stand: Stand):
        """
        Определённый вердикт «запущен ли compose-сервис» или ``None``, если
        выяснить не удалось (тогда вызывающий уходит в TCP-фолбэк).

        Сначала пробуем машиночитаемый ``docker compose ps --format json``:
        имя сервиса приходит отдельным полем, сравнение точное. Если версия
        docker compose такого формата не понимает — падаем на табличный вывод
        и разбираем его по ТОКЕНАМ (см. ``_compose_service_up``).
        """
        result = _run([docker, "compose", "-f", stand.docker_compose_file, "ps", "--format", "json"])
        if result.returncode == 0:
            verdict = _compose_service_up_json(result.stdout, stand.docker_compose_service)
            if verdict is not None:
                return verdict
        result = _run([docker, "compose", "-f", stand.docker_compose_file, "ps"])
        if result.returncode == 0:
            return self._compose_service_up(result.stdout, stand.docker_compose_service)
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
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        result = _run(
            self._base_args(kubectl, stand)
            + ["scale", f"deployment/{deployment}", "--replicas=0"]
        )
        return result.returncode == 0

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        _run_checked(
            self._base_args(kubectl, stand) + ["rollout", "restart", f"deployment/{deployment}"]
        )
        return None

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        try:
            kubectl = _resolve_kubectl()
            deployment = self._deployment(stand)
            result = _run(
                self._base_args(kubectl, stand)
                + ["get", "deployment", deployment, "-o", "jsonpath={.status.readyReplicas}"]
            )
            if result.returncode == 0:
                ready = (result.stdout or "").strip()
                # ПУСТОЙ вывод при rc=0 — это НЕ неопределённость: поля
                # status.readyReplicas у деплоймента с нулём готовых реплик
                # просто нет. Значит kubectl ОПРЕДЕЛЁННО ответил «готовых
                # реплик нет» — доверяем ему и не маскируем открытым портом.
                # Живая приёмка 17.08.2026: после stop (scale 0) NodePort всё
                # равно слушается, пока существует Service, и TCP-фолбэк
                # показывал остановленный стенд работающим.
                if not ready:
                    return False
                return int(ready) > 0
        except (HostingError, ValueError):
            pass
        # Сюда попадаем, только если состояние выяснить НЕ удалось: kubectl не
        # найден, вызов упал, kubectl вернул ненулевой код (нет деплоймента,
        # нет контекста, нет доступа) или отдал нечисловой ответ.
        return _tcp_fallback(stand)

    def read_logs(
        self, stand: Stand, n: int = 100, *, log_dir: Optional[Path] = None
    ) -> Optional[list[str]]:
        kubectl = _resolve_kubectl()
        deployment = self._deployment(stand)
        cmd = self._base_args(kubectl, stand) + [
            "logs",
            f"deployment/{deployment}",
            "--tail",
            str(n),
        ]
        if stand.k8s_container:
            cmd += ["-c", stand.k8s_container]
        result = _run_checked(cmd)
        combined = result.stdout + result.stderr
        lines = combined.splitlines()
        return lines[-n:] if n > 0 else []
