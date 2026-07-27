"""
Федеративный клиент хаба: агрегирует стенды из локального ядра (transport=local)
и с удалённых агентов (transport=agent, по HTTP) в единый список для отрисовки.

Намеренно НЕ импортирует веб-слой хаба — это чистый сетевой/доменный слой,
который можно тестировать и переиспользовать (например, из CLI) без
``http.server``. Используется только stdlib (``urllib``) — как и
standkit_agent.server, без сторонних HTTP-клиентов.

``status_all`` опрашивает стенды ПАРАЛЛЕЛЬНО (``ThreadPoolExecutor``): раньше
обход был последовательным, и N недоступных стендов складывались в N × таймаут
серого экрана. Порядок результатов при этом остаётся порядком РЕЕСТРА, а не
порядком завершения проб — UI не должен «прыгать» строками между обновлениями.

Остаточные пункты следующих итераций (TLS-проверка сертификата агента) —
см. docs/ARCHITECTURE.md и SECURITY.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from standkit import health, lifecycle
from standkit.models import StandStatus, Transport
from standkit.registry import Registry
from standkit.secrets import SecretError, get_secret


@dataclass
class RemoteCallError(Exception):
    """Ошибка сетевого вызова к агенту (недоступен, таймаут, неверный токен и т.п.)."""

    agent_url: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return f"Ошибка обращения к агенту {self.agent_url}: {self.detail}"


def _agent_request(agent_url: str, path: str, token: str, *, method: str = "GET", timeout: float = 5.0) -> dict:
    url = agent_url.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RemoteCallError(agent_url, f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RemoteCallError(agent_url, str(exc)) from exc


# Сколько стендов опрашивается одновременно в ``FederatedClient.status_all``.
# Каждый воркер — один стенд; внутри стенда его четыре пробы параллелятся
# отдельно (см. standkit.health.PROBE_MAX_WORKERS). Значение подобрано так,
# чтобы типовой реестр (единицы стендов) опрашивался за один «заход», не
# устраивая при этом взрыв потоков на реестре в десятки записей.
STATUS_ALL_MAX_WORKERS = 8


class FederatedClient:
    """
    Единая точка входа для хаба: даёт список стендов реестра со статусами,
    независимо от того, локальный стенд или он живёт за агентом.
    """

    def __init__(self, registry: Registry):
        self.registry = registry

    def list_stand_names(self) -> list[str]:
        return self.registry.names()

    def status(self, name: str) -> StandStatus:
        """
        Возвращает статус одного стенда, маршрутизируя запрос по ``transport``:
        локально через standkit.health, либо по HTTP к соответствующему агенту.
        """
        stand = self.registry.get(name)

        if stand.transport == Transport.LOCAL:
            pf = lifecycle.pidfile_path(stand)
            return health.check_stand(stand, pidfile=pf)

        if stand.transport == Transport.AGENT:
            if not stand.agent_url or not stand.agent_secret_ref:
                raise RemoteCallError(stand.agent_url or "?", "не задан agent_url/agent_secret_ref")
            token = get_secret(stand.agent_secret_ref)
            data = _agent_request(stand.agent_url, f"/stand/{name}/status", token)
            return StandStatus.from_dict(data)

        raise NotImplementedError(
            f"Транспорт {stand.transport.value!r} для стенда '{name}' пока не реализован (TODO)"
        )

    def _status_or_error(self, name: str) -> StandStatus:
        """
        ``status(name)``, но ожидаемые отказы (агент недоступен, секрет не
        задан, транспорт не реализован) превращаются в StandStatus с
        UNKNOWN-пробами и текстом ошибки в ``details`` — чтобы один
        недоступный стенд не ронял опрос всего реестра.
        """
        try:
            return self.status(name)
        except (RemoteCallError, SecretError, NotImplementedError) as exc:
            return StandStatus(name=name, details={"error": str(exc)})

    def status_all(self) -> dict[str, StandStatus]:
        """
        Опрашивает все стенды реестра ПАРАЛЛЕЛЬНО (до
        ``STATUS_ALL_MAX_WORKERS`` одновременно). Ошибки отдельных стендов не
        прерывают общий опрос — см. ``_status_or_error``.

        Порядок ключей результата — порядок РЕЕСТРА (детерминированный), а не
        порядок завершения проб: futures собираются в том же порядке, в каком
        были отправлены, и результат складывается в dict уже по нему.
        """
        names = self.registry.names()
        if not names:
            return {}

        with ThreadPoolExecutor(
            max_workers=min(STATUS_ALL_MAX_WORKERS, len(names)),
            thread_name_prefix="standkit-status",
        ) as pool:
            futures = [(name, pool.submit(self._status_or_error, name)) for name in names]

        return {name: future.result() for name, future in futures}

    def start(self, name: str) -> Optional[int]:
        """Запускает стенд. Возвращает pid, если транспорт его предоставляет (см. ``_dispatch_action``)."""
        return self._dispatch_action(name, "start")

    def stop(self, name: str, *, force: bool = False) -> Optional[bool]:
        """
        Останавливает стенд. ``force=True`` — согласие пользователя на
        усыновление стенда, поднятого вне диспетчера (см.
        ``standkit.lifecycle._kestrel_stop``).
        """
        return self._dispatch_action(name, "stop", force=force)

    def restart(self, name: str, *, force: bool = False) -> Optional[int]:
        """Перезапускает стенд. Возвращает pid, если транспорт его предоставляет."""
        return self._dispatch_action(name, "restart", force=force)

    def adopt(self, name: str) -> Optional[dict]:
        """
        Берёт стенд, поднятый вне диспетчера, под управление (пишет pidfile) и
        возвращает описание усыновлённого процесса — см.
        ``standkit.lifecycle.adopt`` / ``standkit.adopt.AdoptCandidate``.

        Процесс при этом НЕ останавливается: усыновление — отдельный шаг.
        """
        stand = self.registry.get(name)

        if stand.transport == Transport.LOCAL:
            return lifecycle.adopt(stand).to_dict()

        if stand.transport == Transport.AGENT:
            data = self._agent_action(stand, name, "adopt")
            candidate = data.get("candidate") if isinstance(data, dict) else None
            return candidate if isinstance(candidate, dict) else None

        raise NotImplementedError(
            f"Транспорт {stand.transport.value!r} для стенда '{name}' пока не реализован (TODO)"
        )

    def _agent_action(self, stand, name: str, action: str, *, query: str = "") -> dict:
        """POST к агенту стенда с резолвом токена из secretstore (секрет наружу не отдаётся)."""
        if not stand.agent_url or not stand.agent_secret_ref:
            raise RemoteCallError(stand.agent_url or "?", "не задан agent_url/agent_secret_ref")
        token = get_secret(stand.agent_secret_ref)
        return _agent_request(stand.agent_url, f"/stand/{name}/{action}{query}", token, method="POST")

    def _dispatch_action(self, name: str, action: str, *, force: bool = False):
        """
        Диспетчеризует start/stop/restart по транспорту и ПРОКИДЫВАЕТ результат
        наверх (для "local" — то, что вернул ``standkit.lifecycle`` — pid для
        start/restart, bool для stop; для "agent" — ``pid`` из JSON-ответа
        агента, если есть), чтобы UI хаба мог показать pid успешного старта, а
        не только голое "ok".

        ``force`` имеет смысл только для stop/restart (усыновление) и в
        транспорте "agent" уезжает query-параметром ``?force=1``.
        """
        stand = self.registry.get(name)
        supports_force = action in ("stop", "restart")

        if stand.transport == Transport.LOCAL:
            fn = getattr(lifecycle, action)
            if supports_force:
                return fn(stand, force=force)
            return fn(stand)

        if stand.transport == Transport.AGENT:
            query = "?force=1" if (supports_force and force) else ""
            data = self._agent_action(stand, name, action, query=query)
            return data.get("pid") if isinstance(data, dict) else None

        raise NotImplementedError(
            f"Транспорт {stand.transport.value!r} для стенда '{name}' пока не реализован (TODO)"
        )

    def logs(self, name: str, n: int = 100) -> list[str]:
        stand = self.registry.get(name)

        if stand.transport == Transport.LOCAL:
            from standkit import logs as _logs

            return _logs.tail(lifecycle.log_path(stand), n)

        if stand.transport == Transport.AGENT:
            if not stand.agent_url or not stand.agent_secret_ref:
                raise RemoteCallError(stand.agent_url or "?", "не задан agent_url/agent_secret_ref")
            token = get_secret(stand.agent_secret_ref)
            data = _agent_request(stand.agent_url, f"/stand/{name}/logs?n={n}", token)
            return list(data.get("lines", []))

        raise NotImplementedError(
            f"Транспорт {stand.transport.value!r} для стенда '{name}' пока не реализован (TODO)"
        )
