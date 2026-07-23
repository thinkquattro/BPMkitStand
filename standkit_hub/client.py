"""
Федеративный клиент хаба: агрегирует стенды из локального ядра (transport=local)
и с удалённых агентов (transport=agent, по HTTP) в единый список для отрисовки.

Намеренно НЕ импортирует веб-слой хаба — это чистый сетевой/доменный слой,
который можно тестировать и переиспользовать (например, из CLI) без
``http.server``. Используется только stdlib (``urllib``) — как и
standkit_agent.server, без сторонних HTTP-клиентов.

TODO(следующая итерация):
  - параллельный (не последовательный) опрос агентов — сейчас FederatedClient
    ходит к агентам по очереди, что при N агентах и таймаутах масштабируется
    плохо; кандидат — concurrent.futures.ThreadPoolExecutor;
  - кэширование/дебаунс частых опросов (polling из хаба по таймеру);
  - TLS/проверка сертификата агента (см. TODO в standkit_agent.server).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
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

    def status_all(self) -> dict[str, StandStatus]:
        """
        Опрашивает все стенды реестра. Ошибки отдельных стендов не прерывают
        общий опрос — недоступный стенд получает StandStatus с UNKNOWN-пробами
        и текстом ошибки в ``details``.

        TODO: см. модульный TODO — сделать параллельным.
        """
        result: dict[str, StandStatus] = {}
        for name in self.registry.names():
            try:
                result[name] = self.status(name)
            except (RemoteCallError, SecretError, NotImplementedError) as exc:
                result[name] = StandStatus(name=name, details={"error": str(exc)})
        return result

    def start(self, name: str) -> None:
        self._dispatch_action(name, "start")

    def stop(self, name: str) -> None:
        self._dispatch_action(name, "stop")

    def restart(self, name: str) -> None:
        self._dispatch_action(name, "restart")

    def _dispatch_action(self, name: str, action: str) -> None:
        stand = self.registry.get(name)

        if stand.transport == Transport.LOCAL:
            fn = getattr(lifecycle, action)
            fn(stand)
            return

        if stand.transport == Transport.AGENT:
            if not stand.agent_url or not stand.agent_secret_ref:
                raise RemoteCallError(stand.agent_url or "?", "не задан agent_url/agent_secret_ref")
            token = get_secret(stand.agent_secret_ref)
            _agent_request(stand.agent_url, f"/stand/{name}/{action}", token, method="POST")
            return

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
