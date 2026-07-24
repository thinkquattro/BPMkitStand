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
  - ``is_running`` — проба, никогда не бросает: любая ошибка (appcmd/docker
    не найден, команда упала, парсинг не удался) трактуется как "не
    подтверждено запущенным" и заменяется TCP-фолбэком на порт стенда, если
    он тоже не открыт — результат ``False``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from standkit.models import HostKind, Stand

# Таймаут внешних вызовов (appcmd/docker) по умолчанию — операции над
# App Pool/контейнером обычно быстрые; значение с запасом, чтобы не резать
# легитимно медленный docker compose up на первом старте образа.
_DEFAULT_TIMEOUT = 20.0


class HostingError(Exception):
    """Ошибка бэкенда хостинга (внешняя утилита не найдена, команда завершилась с ошибкой и т.п.)."""


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
    "error ( message:",  # общий appcmd-ERROR при неудачном открытии config
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
    """``_run_checked`` для appcmd: при ошибке нехватки прав добавляет к тексту
    понятную IIS-подсказку «запустите от имени администратора»."""
    try:
        return _run_checked(cmd, timeout=timeout)
    except HostingError as exc:
        if _looks_like_elevation_error(str(exc)):
            raise HostingError(
                str(exc)
                + "\n\nПохоже, не хватает прав администратора: управление IIS через appcmd.exe "
                "требует запуска диспетчера «от имени администратора» (elevated). Запустите "
                "standkit-hub с правами администратора и повторите операцию."
            ) from exc
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

    def stop(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_stop(stand, run_dir=run_dir)

    def restart(
        self, stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
    ) -> Optional[int]:
        from standkit import lifecycle as _lifecycle

        return _lifecycle._kestrel_restart(stand, run_dir=run_dir, log_dir=log_dir)

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
    import os

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

    def is_running(self, stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
        try:
            appcmd = _resolve_appcmd()
        except HostingError:
            return _tcp_fallback(stand)

        # Идентичность стенда в IIS — его SITE (см. stop/restart: управляем
        # только сайтом, App Pool не трогаем). Поэтому «работает ли стенд» =
        # состояние САЙТА; пул смотрим лишь когда сайт не задан (единственный
        # хэндл). ВАЖНО: НЕ падать на TCP-фолбэк при определённом ответе appcmd —
        # IIS/http.sys держит порт 80/443 даже у остановленного сайта (503),
        # открытый порт НЕ означает «стенд работает». TCP-фолбэк — только когда
        # appcmd состояния не дал (сайт/пул не найден, ошибка команды).
        target, name = ("site", stand.iis_site) if stand.iis_site else ("apppool", stand.iis_app_pool)
        if name:
            state = self._query_state(appcmd, target, name)
            if state is not None:
                return state == "Started"
        return _tcp_fallback(stand)

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
                result = _run([docker, "inspect", "-f", "{{.State.Running}}", stand.docker_container])
                if result.returncode == 0 and result.stdout.strip().lower() == "true":
                    return True
            else:
                result = _run([docker, "compose", "-f", stand.docker_compose_file, "ps"])
                if result.returncode == 0 and self._compose_service_up(
                    result.stdout, stand.docker_compose_service
                ):
                    return True
        except HostingError:
            pass
        return _tcp_fallback(stand)

    @staticmethod
    def _compose_service_up(ps_output: str, service: str) -> bool:
        """
        Ищет строку сервиса ``service`` в выводе ``docker compose ps`` и
        проверяет, что состояние похоже на "запущено" (``Up``/``running``,
        без учёта регистра) — формат вывода отличается между версиями
        docker compose (v1 таблица "Up 2 hours", v2 "running"/"Up").
        """
        for line in ps_output.splitlines():
            if service not in line:
                continue
            lowered = line.lower()
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
                if ready and int(ready) > 0:
                    return True
        except (HostingError, ValueError):
            pass
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
