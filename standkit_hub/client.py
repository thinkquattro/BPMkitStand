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

Доверие к TLS-сертификату агента задаётся ЗАПИСЬЮ СТЕНДА (``agent_ca`` /
``agent_verify_tls``), а не окружением процесса: см. блок «доверие к
TLS-сертификату агента» ниже и docs/ARCHITECTURE.md / SECURITY.md.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from standkit import health, lifecycle
from standkit.models import Stand, StandStatus, Transport
from standkit.registry import Registry
from standkit.secrets import SecretError, get_secret


@dataclass
class RemoteCallError(Exception):
    """Ошибка сетевого вызова к агенту (недоступен, таймаут, неверный токен и т.п.)."""

    agent_url: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return f"Ошибка обращения к агенту {self.agent_url}: {self.detail}"


# --- доверие к TLS-сертификату агента (GAP-008) ------------------------------
#
# Канал «хаб → агент» — УПРАВЛЯЮЩИЙ: по нему уезжает bearer-токен и приезжают
# команды start/stop. Поэтому по умолчанию цепочка проверяется штатно, по
# системному хранилищу, а послабления делаются только явно и только полями
# записи стенда (``Stand.agent_ca`` / ``Stand.agent_verify_tls``).
#
# До этого управляемого исключения не было вовсе: единственным способом
# доверять самоподписанному сертификату агента (а кукбук предлагает такой
# сертификат как штатный вариант первого развёртывания) была переменная
# окружения SSL_CERT_FILE, выставленная ДО запуска процесса хаба.
#
# Тексты диагнозов — С РЕЦЕПТОМ: «сертификат не доверенный» без «что с этим
# делать» для оператора ровно так же бесполезен, как прежнее голое «агент
# недоступен» со скобкой из _ssl.c (GAP-008 п.2).
_TRUST_HINT_CA = "укажите agent_ca (путь к сертификату агента) в записи стенда"
_TRUST_HINT_HOSTNAME = "имя в agent_url должно совпадать с CN/SAN сертификата агента"

# Текст ошибки уезжает в UI одной строкой (details.error карточки стенда), а
# сообщения OpenSSL бывают длинными и многострочными — режем.
_DETAIL_MAX_LEN = 200


def _short(value: object) -> str:
    """
    Однострочный и обрезанный текст ошибки. Локальный близнец
    ``standkit.health._short``: дублируется сознательно — health.py это ядро,
    а его приватные хелперы не публичный API, на который хабу стоит опираться.
    """
    text = " ".join(str(value).split())
    if len(text) > _DETAIL_MAX_LEN:
        text = text[: _DETAIL_MAX_LEN - 1].rstrip() + "…"
    return text


def _unwrap_url_error(exc: BaseException) -> BaseException:
    """
    Достаёт исходную ошибку из ``URLError``: urllib оборачивает в него всё, что
    случилось на этапе соединения, — настоящая ``SSLCertVerificationError``
    приезжает как ``URLError(SSLCertVerificationError(...))``, и без
    разворачивания её не распознать.
    """
    cause = exc
    while isinstance(cause, urllib.error.URLError) and isinstance(cause.reason, BaseException):
        cause = cause.reason
    return cause


def _cert_reason(exc: BaseException) -> str:
    """
    Краткая причина отказа сертификата: у настоящей ``SSLCertVerificationError``
    из OpenSSL есть ``verify_message`` («self-signed certificate») — он читается
    лучше полного текста с координатами в ``_ssl.c``.
    """
    for attr in ("verify_message", "reason"):
        value = getattr(exc, attr, None)
        if value:
            return _short(value)
    return _short(exc)


def _is_hostname_mismatch(exc: BaseException) -> bool:
    """
    Отличает «сертификат не тому имени выписан» от «цепочка не доверенная».

    Случаи разные по рецепту: во втором надо ДОБАВИТЬ доверие (agent_ca), в
    первом — поправить сам ``agent_url`` (типовая история: сертификат выписан
    на имя хоста, а в реестре записан IP, или наоборот). OpenSSL отдаёт это
    той же ``SSLCertVerificationError``, поэтому различаем по тексту —
    современный вариант «Hostname mismatch, certificate is not valid for …» и
    исторический из ``ssl.match_hostname`` («… doesn't match …»).
    """
    text = " ".join((str(getattr(exc, "verify_message", "") or ""), str(exc))).lower()
    return "hostname mismatch" in text or "doesn't match" in text


def _build_agent_ssl_context(
    url: str,
    *,
    ca_file: Optional[str] = None,
    verify: bool = True,
) -> Optional[ssl.SSLContext]:
    """
    Строит SSL-контекст для запроса к агенту — по образцу
    ``standkit.health.http_probe``.

    Контекст строится ТОЛЬКО для ``https://``-адресов: для http он не нужен и
    ничего не значит, а молча «настроенный TLS» на голом HTTP — обман
    оператора. ``None`` означает «поведение urllib по умолчанию», то есть
    штатную проверку по системному хранилищу.

    Приоритет отключения проверки над ``ca_file`` намеренный: если оператор
    явно снял ``agent_verify_tls``, соединение должно состояться, даже если в
    ``agent_ca`` осталась старая или неподходящая ссылка.

    ``ssl.create_default_context()`` с явным снятием проверок — публичный API;
    приватный ``ssl._create_unverified_context()`` здесь не используется (тот
    же выбор, что в health.py).

    Бросает ``OSError`` (в том числе ``FileNotFoundError``) или ``ssl.SSLError``,
    если ``ca_file`` не читается/не разбирается — вызывающий превращает это в
    ``RemoteCallError`` с УКАЗАНИЕМ ПУТИ.
    """
    if not url.lower().startswith("https://"):
        return None
    if not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return None


def _ca_load_detail(ca_file: Optional[str], exc: BaseException) -> str:
    """Текст ошибки загрузки ``agent_ca`` — обязан называть путь, иначе искать нечего."""
    if isinstance(exc, FileNotFoundError):
        return f"файл сертификата агента не найден: {ca_file} (поле agent_ca записи стенда)"
    return (
        f"не удалось прочитать сертификат агента {ca_file} "
        f"(поле agent_ca записи стенда): {_short(exc)}"
    )


def _agent_failure_detail(exc: BaseException) -> str:
    """
    Превращает сетевое исключение в текст для ``RemoteCallError``.

    Узнаваемые случаи получают диагноз с рецептом, ВСЁ ОСТАЛЬНОЕ уходит как и
    раньше — голым ``str(exc)``: глотать неизвестную ошибку ради красивого
    текста нельзя, оператор должен видеть настоящую причину.
    """
    cause = _unwrap_url_error(exc)
    if isinstance(cause, ssl.SSLCertVerificationError):
        reason = _cert_reason(cause)
        if _is_hostname_mismatch(cause):
            return f"имя в сертификате агента не совпадает с адресом ({reason}) — {_TRUST_HINT_HOSTNAME}"
        return f"сертификат агента не доверенный ({reason}) — {_TRUST_HINT_CA}"
    return str(exc)


def _agent_trust(stand: Stand) -> dict:
    """
    Параметры доверия к сертификату агента, взятые из ЗАПИСИ СТЕНДА, — одним
    словарём, чтобы ни один вызов ``_agent_request`` не забыл их прокинуть.
    """
    return {"ca_file": stand.agent_ca or None, "verify": bool(stand.agent_verify_tls)}


def _agent_request(
    agent_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    timeout: float = 5.0,
    ca_file: Optional[str] = None,
    verify: bool = True,
) -> dict:
    """
    Один HTTP(S)-вызов к агенту с bearer-токеном.

    ``ca_file``/``verify`` — доверие к сертификату агента из записи стенда
    (``Stand.agent_ca`` / ``Stand.agent_verify_tls``). Оба параметра именованные
    и их значения по умолчанию ТОЧНО повторяют прежнее поведение (штатная
    проверка цепочки по системному хранилищу), поэтому старые вызовы функции
    остаются валидными и ведут себя как раньше.
    """
    url = agent_url.rstrip("/") + path
    try:
        context = _build_agent_ssl_context(url, ca_file=ca_file, verify=verify)
    except OSError as exc:
        # ssl.SSLError — наследник OSError, так что сюда попадает и «файла нет»,
        # и «файл есть, но это не PEM».
        raise RemoteCallError(agent_url, _ca_load_detail(ca_file, exc)) from exc
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RemoteCallError(agent_url, f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RemoteCallError(agent_url, _agent_failure_detail(exc)) from exc


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
            data = _agent_request(
                stand.agent_url, f"/stand/{name}/status", token, **_agent_trust(stand)
            )
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

    def _agent_action(self, stand: Stand, name: str, action: str, *, query: str = "") -> dict:
        """POST к агенту стенда с резолвом токена из secretstore (секрет наружу не отдаётся)."""
        if not stand.agent_url or not stand.agent_secret_ref:
            raise RemoteCallError(stand.agent_url or "?", "не задан agent_url/agent_secret_ref")
        token = get_secret(stand.agent_secret_ref)
        return _agent_request(
            stand.agent_url,
            f"/stand/{name}/{action}{query}",
            token,
            method="POST",
            **_agent_trust(stand),
        )

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
            data = _agent_request(
                stand.agent_url, f"/stand/{name}/logs?n={n}", token, **_agent_trust(stand)
            )
            return list(data.get("lines", []))

        raise NotImplementedError(
            f"Транспорт {stand.transport.value!r} для стенда '{name}' пока не реализован (TODO)"
        )
