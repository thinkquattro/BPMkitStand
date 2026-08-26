# -*- coding: utf-8 -*-
"""HTTP-клиент к бэкенду издателя — единственное место, где канал ходит в сеть.

Зачем отдельный модуль. Три цикла канала (паттерны, релизы, отзыв лицензий) ходят к одному
и тому же сервису, но за разным: JSON, заголовки и многомегабайтный бинарь с докачкой.
Если каждый цикл будет собирать `urllib`-запрос сам, то и заголовок авторизации, и разбор
ошибок, и политика редиректов расползутся в три копии — а разъезжаться они начнут молча.
Здесь транспорт один, а циклы получают уже типизированный исход.

Что тут ЗНАЧИМО и почему сделано именно так:

* **stdlib-only** (`urllib.request`): весь репозиторий без pip-зависимостей, и канал доставки
  обновлений — последнее место, куда стоит тянуть стороннюю библиотеку;
* **304 — это успех, а не ошибка.** `urllib` бросает `HTTPError` на ЛЮБОЙ не-2xx, включая
  `304 Not Modified`. Если не перехватить его здесь, каждый вызывающий будет ловить «ошибку»
  там, где сервер сказал «у тебя уже актуально». Поэтому 304 превращается в `NotModified`
  (см. докстринг исключения в `errors.py`);
* **ETag передаётся ДОСЛОВНО**, вместе с двойными кавычками. Сервер сравнивает
  `If-None-Match` строкой байт-в-байт: снятые кавычки, добавленный `W/` или `*` дают
  промах кэша и полную перекачку на каждом тике вместо дешёвого 304;
* **только `https`, кроме локального бэкенда.** По каналу едет ИСПОЛНЯЕМЫЙ файл MCP,
  поэтому открытый `http://` на внешний хост — отказ ДО сетевого вызова
  (`insecure_transport`), а не «сходим и посмотрим»: разбираться со схемой по факту
  ответа поздно, запрос вместе с заголовком авторизации уже ушёл. Исключение ровно
  одно — loopback: именно так поднимается настоящий бэкенд рядом (интеграционные тесты
  транспорта и живая приёмка канала), и такой трафик машину не покидает. Аварийный
  выход для отладки — именованный `allow_insecure=True` у клиента, и только он:
  переменной окружения, тихо снимающей запрет, здесь нет намеренно — обход обязан быть
  виден в коде вызывающего (тот же приём, что `--insecure` у `standkit_agent`);
* **редиректы за пределы `base_url` запрещены.** Это канал доставки ИСПОЛНЯЕМОГО кода:
  308 на чужой хост — готовая подмена бинаря (и заодно утечка заголовка авторизации
  третьей стороне). Разрешён только редирект внутри собственного префикса, всё
  остальное — `blocked_by_policy`;
* **пустое тело при непустом `size_bytes` — отказ, а не «скачали ноль байт».** Если у
  издателя включена разгрузка отдачи (`X-Accel-Redirect`), а клиент пришёл на uvicorn
  напрямую, ответ приходит `200` с `Content-Length: 0`. Записать это поверх рабочего
  бинаря — худший исход канала, поэтому проверка идёт ДО открытия файла-приёмника;
* **мультидиапазонный `Range` не запрашивается никогда.** Сервер на запятую в `Range` не
  ругается — он молча отдаёт обычный `200` с полным телом; клиент, дописывающий такой ответ
  в хвост частичной закачки, получит мусор. Здесь формируется только `bytes=<offset>-`.

Лицензионный конверт в этом модуле **не логируется и не попадает ни в одно сообщение об
ошибке** — он живёт в приватном поле и уходит ровно в один заголовок.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .errors import ChannelError, NotModified, kind_from_payload

__all__ = [
    "CONTENT_PREFIX",
    "AUTH_SCHEME",
    "BackendResponse",
    "BackendClient",
]

# Общий префикс контентных эндпоинтов бэкенда (`app/routers/content.py`). Держится
# константой, чтобы циклы не собирали путь из строк вразнобой.
CONTENT_PREFIX = "/v1/content"

# Схема авторизации. Сервер сравнивает её ТОЧНО и регистрозависимо, разделитель — ровно
# один пробел: `BPMKIT1 <конверт>`. Ни `Bearer`, ни нижний регистр не пройдут.
AUTH_SCHEME = "BPMKIT1"

# Сколько символов ответа сервера доносим до пользователя. Больше — это уже не сообщение,
# а дамп (бэкенд умеет отдать html-страницу ошибки прокси).
_DETAIL_LIMIT = 300

# Сколько байт тела ошибки читаем, прежде чем разбирать. Ошибки бэкенда — короткий JSON;
# всё, что длиннее, интереса не представляет и читается только чтобы не подвесить сокет.
_ERROR_BODY_LIMIT = 64 * 1024

# Максимум переходов по редиректам. Даже разрешённый (внутри base_url) редирект не должен
# превращаться в бесконечный цикл на тике планировщика.
_MAX_REDIRECTS = 3


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    """Обрезка текста от сервера до вменяемой длины с явным многоточием."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _lower_headers(headers) -> dict:
    """Заголовки ответа с ключами в НИЖНЕМ регистре.

    HTTP-заголовки регистронезависимы, а вызывающие (`releases.py`, `patterns.py`) читают
    их по фиксированному ключу. Нормализация здесь — единственный способ не гадать, пришло
    `ETag`, `Etag` или `etag` от конкретного прокси. Повторяющиеся заголовки схлопываются
    до последнего значения: ни один заголовок нашего контракта не бывает множественным.
    """
    return {str(key).lower(): value for key, value in headers.items()}


def _fallback_kind(status: int) -> str:
    """Запасной `kind`, когда тело ответа не назвало `error_code` явно.

    Единственная тонкость — `403`. Бэкенд отдаёт им ровно одну вещь: выключенный у издателя
    ПРИЁМ (`feature_disabled`), и это НЕ проблема лицензии — путать их уже приходилось
    (см. докстринг `errors.py`). При этом `feature_disabled` отсутствует в списке
    `error_code`, который разбирает `kind_from_payload`, поэтому различение делается там,
    где оно однозначно: по коду ответа, через штатный параметр `fallback`. Собственной
    классификации тел ответа здесь нет — разбор целиком на `errors.kind_from_payload`.
    """
    if status == 403:
        return "feature_disabled"
    return "http_error"


def _detail_from_body(body: bytes) -> str:
    """Человеческая часть тела ошибки: вложенный `detail.detail`, плоский `detail` или
    сырой текст. Нужна ровно для того, чтобы РАЗВЕСТИ два внешне одинаковых `404`
    («релиз не выложен» vs «подписи нет») в глазах пользователя и в логе."""
    text = body.decode("utf-8", "replace") if body else ""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return _clip(text)
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return _clip(str(detail.get("detail") or detail.get("error_code") or text))
        if isinstance(detail, str):
            return _clip(detail)
    return _clip(text)


def _is_loopback_host(hostname: Optional[str]) -> bool:
    """Loopback ли хост: `localhost`, `::1` или ЛЮБОЙ адрес из 127.0.0.0/8.

    Вся сеть 127.0.0.0/8, а не один `127.0.0.1`: тесты транспорта берут свободный порт,
    а локальные сборки бэкенда спокойно живут и на 127.0.0.2. Разбор через `ipaddress`,
    а не сравнение строк — иначе `127.0.0.1.evil.example` прошёл бы как «начинается
    на 127.».

    Двойник этой функции есть в `standkit_agent.security.is_loopback_host` (там она
    решает, можно ли слушать открытый HTTP). Общего модуля намеренно нет: пакеты
    поставляются раздельно, и сцеплять канал обновлений с агентом ради шести строк —
    размен хуже, чем повтор с этой ссылкой.
    """
    host = (hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _ensure_secure_transport(base_url: str, *, allow_insecure: bool = False) -> None:
    """Отказ ДО сетевого вызова, если адрес издателя не защищён TLS.

    Fail-closed, и по той же причине, по которой fail-closed вся политика подписи: по
    этому каналу приезжает исполняемый файл MCP. Открытый `http://` на внешний хост
    означает, что содержимое ответа может подменить любой, кто стоит на пути, а
    заголовок авторизации (лицензионный конверт) уезжает открытым текстом. Проверять
    это в момент ответа поздно — запрос уже ушёл, поэтому проверка стоит в конструкторе
    клиента: собрать транспорт к незащищённому адресу нельзя вовсе.

    Разрешено ровно два случая:

    * `https://` — штатный адрес издателя;
    * `http://` на loopback — так поднимается настоящий бэкенд рядом: интеграционные
      тесты транспорта (`127.0.0.1:<свободный порт>`) и живая приёмка канала
      (`localhost:8000`). Этот трафик не покидает машину.

    Всё остальное — `insecure_transport`: и `http://` на внешний хост, и адрес без
    схемы (`api.example`, который `urllib` всё равно не откроет), и экзотика вроде
    `ftp://`. `allow_insecure=True` снимает запрет ТОЛЬКО с `http://`: это отладочный
    обход TLS, а не разрешение открыть неоткрываемое.
    """
    try:
        parsed = urllib.parse.urlsplit(base_url or "")
        scheme = (parsed.scheme or "").lower()
        hostname = parsed.hostname
    except ValueError:
        # Неразбираемый адрес (битые скобки IPv6 и т.п.) — тот же отказ, что и у
        # незащищённой схемы: доверять такому адресу не на чем.
        scheme, hostname = "", None
    if scheme == "https":
        return
    if scheme == "http" and (_is_loopback_host(hostname) or allow_insecure):
        return
    raise ChannelError(
        "Адрес бэкенда издателя не защищён TLS — канал доставки обновлений BPMkit по "
        "нему не работает: по каналу едет исполняемый файл MCP, и по открытому "
        "соединению его можно подменить по дороге. Укажите адрес издателя как "
        "https://… (в хабе: «Канал обновлений» → адрес бэкенда, поле "
        "companion.backend_url); http:// допустим только для локального бэкенда "
        "(localhost, 127.0.0.1, ::1)",
        kind="insecure_transport",
        detail=_clip(f"схема: {scheme or '(не указана)'}; адрес: {base_url}"),
    )


def _inside_base_url(newurl: str, base_url: str) -> bool:
    """Ведёт ли `Location` внутрь собственного адреса издателя.

    Сравнение префиксом, и его ДОСТАТОЧНО, чтобы запретить downgrade `https→http`:
    схема — часть префикса, поэтому `http://api.example/...` не начинается с
    `https://api.example` и отсекается тем же условием, что и чужой хост. Отдельной
    проверки схемы на редиректе нет намеренно: две политики про одно и то же однажды
    разъедутся, а разъедутся они молча.

    Чего голому `startswith` не хватает — ГРАНИЦЫ: `https://api.example` формально
    является префиксом `https://api.example.evil.test/`, то есть чужой хост проходил бы
    как свой. Поэтому за префиксом обязан идти разделитель (`/`, `?`, `#`) или конец
    строки.
    """
    if not base_url or not newurl.startswith(base_url):
        return False
    rest = newurl[len(base_url):]
    return rest == "" or rest[0] in "/?#"


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Редиректы только внутрь `base_url`.

    Штатный `HTTPRedirectHandler` пойдёт куда угодно, включая другой хост, и перенесёт туда
    заголовки запроса. Для канала, по которому едет исполняемый бинарь, это дыра: тот, кто
    подменил ответ сервера (или сам сервер после компрометации), уводит клиента на свой
    хост и отдаёт свой `.exe`. Поэтому чужой `Location` — не «следуем и проверим потом»,
    а немедленный отказ `blocked_by_policy`, до какого-либо чтения тела.

    Этой же проверкой закрыт downgrade `https→http`: схема входит в префикс `base_url`,
    поэтому отдельной политики схемы на редиректе нет (см. `_inside_base_url`).
    """

    max_redirections = _MAX_REDIRECTS

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not _inside_base_url(newurl, self._base_url):
            raise ChannelError(
                "Бэкенд перенаправил запрос за пределы адреса издателя — "
                "переход заблокирован политикой безопасности",
                kind="blocked_by_policy",
                http_status=code,
                detail=_clip(f"Location: {newurl}"),
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class BackendResponse:
    """Ответ бэкенда, отвязанный от `urllib`.

    Тело уже вычитано целиком: объект ответа `urllib` держит открытый сокет, а вызывающие
    циклы — не менеджеры контекста. Для больших файлов есть отдельный `download`, который
    ответ не материализует.
    """

    status: int
    headers: dict
    body: bytes


class BackendClient:
    """Транспорт к бэкенду издателя. Ничего не знает о смысле эндпоинтов."""

    def __init__(self, base_url: str, envelope: Optional[str] = None, *,
                 timeout: float = 15.0, opener=None,
                 allow_insecure: bool = False) -> None:
        self.base_url = (base_url or "").rstrip("/")
        # Схема проверяется ЗДЕСЬ, раньше всего остального: клиент, который нельзя
        # собрать, не сходит в сеть ни одним из трёх циклов — включая подменённый
        # `opener`. `allow_insecure` именованный и без парного env-переключателя:
        # отладочный обход обязан быть виден в коде вызывающего.
        _ensure_secure_transport(self.base_url, allow_insecure=allow_insecure)
        self.allow_insecure = bool(allow_insecure)
        # Конверт — приватным полем: он не должен попасть ни в `repr`, ни в текст ошибки.
        self._envelope = (envelope or "").strip()
        self.timeout = float(timeout)
        # `opener` подставляется тестами. Свой строится ОДИН раз: он держит политику
        # редиректов, и создавать его на каждый запрос — терять эту привязку из виду.
        self._opener = opener if opener is not None else self._build_opener()

    def __repr__(self) -> str:
        """Без конверта. Отдельный `repr` нужен именно затем, чтобы клиент можно было
        безопасно печатать в отладочный лог."""
        return (f"BackendClient(base_url={self.base_url!r}, "
                f"authorized={bool(self._envelope)})")

    # -- инфраструктура -----------------------------------------------------------------
    def _build_opener(self) -> urllib.request.OpenerDirector:
        """Свой `OpenerDirector` с политикой редиректов вместо штатной.

        `build_opener` пропускает дефолтный `HTTPRedirectHandler`, если ему передан
        экземпляр подкласса, — поэтому подмена именно такая, а не «удалить обработчик».
        Полностью выключать редиректы нельзя: `latest` у издателя вполне может однажды
        стать 302 на конкретную версию в пределах того же адреса.
        """
        return urllib.request.build_opener(_SameOriginRedirectHandler(self.base_url))

    @property
    def has_envelope(self) -> bool:
        """Есть ли чем авторизоваться. Значение конверта наружу не отдаётся никогда."""
        return bool(self._envelope)

    def _url(self, path: str, params: Optional[dict] = None) -> str:
        """Полный URL. Хвостовой слэш срезается всегда: FastAPI на `/path/` отвечает
        `307` на `/path`, и лишний переход тут никому не нужен."""
        path = path if path.startswith("/") else "/" + path
        path = path.rstrip("/") or "/"
        url = self.base_url + path
        query = _encode_params(params)
        return f"{url}?{query}" if query else url

    def _build_request(self, path: str, *, method: str, params: Optional[dict],
                       authorized: bool, etag: Optional[str],
                       range_start: Optional[int],
                       extra_headers: Optional[dict]) -> urllib.request.Request:
        if authorized and not self._envelope:
            # ДО сетевого вызова: без конверта авторизованный запрос гарантированно
            # вернёт 401, а тик заплатит за это круговой задержкой и записью в лог
            # бэкенда. Отказываем сразу и называем причину так, как её чинит человек.
            raise ChannelError(
                "Лицензионный ключ BPMkit не найден — канал обновлений не запрашивает "
                "данные у издателя",
                kind="no_license",
            )
        headers: dict = {"User-Agent": f"BPMkitStand-Companion/{__version__}",
                         "Accept-Encoding": "identity"}
        if authorized:
            headers["Authorization"] = f"{AUTH_SCHEME} {self._envelope}"
        if etag:
            # Дословно, как прислал сервер: с кавычками и без нормализации.
            headers["If-None-Match"] = etag
        if range_start:
            # Только один диапазон и только открытый справа: запятая в `Range` уводит
            # сервер в обычный 200 с полным телом, и докачка молча портит файл.
            headers["Range"] = f"bytes={int(range_start)}-"
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        return urllib.request.Request(self._url(path, params), method=method,
                                      headers=headers)

    def _open(self, req: urllib.request.Request, timeout: Optional[float]):
        """Выполнить запрос, превратив исходы `urllib` в исходы канала.

        Возвращает открытый ответ — закрыть обязан вызывающий (`download` стримит его,
        `request` вычитывает целиком).
        """
        try:
            return self._opener.open(req, timeout=self.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                # Успех, а не ошибка: у нас уже актуальная версия.
                try:
                    exc.close()
                except Exception:  # noqa: BLE001 - закрытие не должно подменять исход
                    pass
                raise NotModified("Данные не изменились с прошлой проверки") from None
            body = b""
            try:
                body = exc.read(_ERROR_BODY_LIMIT) or b""
            except Exception:  # noqa: BLE001 - тело ошибки не обязано читаться
                body = b""
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            payload: Any = None
            try:
                payload = json.loads(body.decode("utf-8", "replace")) if body else None
            except ValueError:
                payload = None
            kind = kind_from_payload(payload, _fallback_kind(exc.code))
            raise ChannelError(
                f"Бэкенд издателя ответил {exc.code}",
                kind=kind,
                http_status=exc.code,
                detail=_detail_from_body(body),
            ) from None
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            # Сеть недоступна/таймаут/DNS — штатное состояние ноутбука, а не авария.
            # Стек-трейс наружу не отдаём: в состоянии канала он бесполезен, а в UI
            # выглядит как падение программы.
            raise ChannelError(
                "Бэкенд издателя недоступен — попробуем на следующей проверке",
                kind="offline",
                detail=_clip(_reason_text(exc)),
            ) from None

    # -- публичные операции --------------------------------------------------------------
    def request(self, path: str, *, method: str = "GET", params: Optional[dict] = None,
                authorized: bool = True, etag: Optional[str] = None,
                range_start: Optional[int] = None,
                extra_headers: Optional[dict] = None,
                timeout: Optional[float] = None) -> BackendResponse:
        """Один запрос с полностью вычитанным телом.

        `authorized=False` — для ПУБЛИЧНОГО `revocations.json`: он читается и тогда, когда
        лицензия уже отозвана и авторизация заведомо не пройдёт, иначе отозванный клиент
        никогда бы об отзыве не узнал.
        """
        req = self._build_request(path, method=method, params=params, authorized=authorized,
                                  etag=etag, range_start=range_start,
                                  extra_headers=extra_headers)
        resp = self._open(req, timeout)
        try:
            body = b"" if method.upper() == "HEAD" else (resp.read() or b"")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ChannelError(
                "Обрыв соединения при чтении ответа бэкенда",
                kind="offline",
                detail=_clip(_reason_text(exc)),
            ) from None
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return BackendResponse(status=int(getattr(resp, "status", 0) or 200),
                               headers=_lower_headers(resp.headers), body=body)

    def get_json(self, path: str, *, params: Optional[dict] = None,
                 authorized: bool = True, etag: Optional[str] = None) -> tuple:
        """`(payload, headers)` для JSON-эндпоинтов.

        Тело, которое не разобралось в JSON (или разобралось в скаляр), — это `bad_response`,
        а не «пустой ответ»: молча принять такое за «нет данных» значит спрятать поломку
        контракта или страницу-заглушку прокси.
        """
        resp = self.request(path, params=params, authorized=authorized, etag=etag)
        try:
            payload = json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelError(
                "Ответ бэкенда не разобран как JSON",
                kind="bad_response",
                http_status=resp.status,
                detail=_clip(f"{exc}"),
            ) from None
        if not isinstance(payload, (dict, list)):
            raise ChannelError(
                "Ответ бэкенда не похож на ожидаемый JSON-объект",
                kind="bad_response",
                http_status=resp.status,
                detail=_clip(f"тип тела: {type(payload).__name__}"),
            )
        return payload, resp.headers

    def head(self, path: str, *, authorized: bool = True,
             etag: Optional[str] = None) -> dict:
        """Только заголовки (`X-BPMkit-Version`, `X-BPMkit-SHA256`, `ETag`,
        `Content-Length`) — дешёвая проверка «что там сейчас лежит» без скачивания."""
        return self.request(path, method="HEAD", authorized=authorized, etag=etag).headers

    def download(self, path: str, dest, *, authorized: bool = True,
                 resume_from: int = 0, etag: Optional[str] = None,
                 expected_size: Optional[int] = None,
                 chunk_size: int = 1024 * 1024) -> dict:
        """Скачать файл в `dest` потоком, при необходимости — с докачкой.

        Три решения, каждое из-за конкретного способа испортить файл:

        1. **дописываем только на честный `206`.** Сервер, ответивший `200` на запрос с
           `Range`, диапазон проигнорировал и прислал файл ЦЕЛИКОМ; дописать такое в хвост
           частичной закачки — гарантированный мусор, поэтому файл в этом случае
           переписывается с нуля;
        2. **приёмник открывается только при первом байте.** Пустой ответ (разгрузка
           отдачи `X-Accel` мимо uvicorn) не должен обнулять уже лежащий рядом рабочий
           файл — до появления данных мы его не трогаем вовсе;
        3. **`416` сбрасывает состояние докачки.** Сервер отверг наш диапазон — значит
           частичный файл ему не соответствует (релиз перевыложен). Он удаляется, иначе
           клиент будет вечно повторять один и тот же битый `Range`.
        """
        dest = Path(dest)
        resume_from = max(0, int(resume_from or 0))
        req = self._build_request(path, method="GET", params=None, authorized=authorized,
                                  etag=etag, range_start=resume_from or None,
                                  extra_headers=None)
        try:
            resp = self._open(req, None)
        except ChannelError as exc:
            if exc.http_status == 416:
                _unlink_quietly(dest)
                raise ChannelError(
                    "Сервер отверг докачку — частично скачанный файл удалён, "
                    "следующая попытка начнётся с нуля",
                    kind="range_invalid",
                    http_status=416,
                    detail=exc.detail,
                ) from None
            raise

        try:
            status = int(getattr(resp, "status", 0) or 200)
            headers = _lower_headers(resp.headers)
            resumed = bool(resume_from) and status == 206
            declared = _int_or_none(headers.get("content-length"))
            expected = _int_or_none(expected_size)

            if declared == 0 and (expected or 0) > 0:
                # Разгрузка отдачи включена, а отдавать некому: uvicorn отдал заголовки
                # без тела. Явный отказ ДО открытия приёмника.
                raise ChannelError(
                    "Бэкенд отдал пустое тело вместо файла релиза — "
                    "обновление не применяется",
                    kind="bad_response",
                    http_status=status,
                    detail=f"Content-Length: 0, ожидалось {expected} байт",
                )

            mode = "ab" if resumed else "wb"
            written = _stream_to_file(resp, dest, mode=mode, chunk_size=chunk_size)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        if written == 0 and (expected or 0) > 0:
            # Тот же дефект, но без объявленной длины: тело кончилось, не начавшись.
            # Приёмник к этому моменту НЕ тронут (см. `_stream_to_file`).
            raise ChannelError(
                "Бэкенд отдал пустое тело вместо файла релиза — обновление не применяется",
                kind="bad_response",
                http_status=status,
                detail=f"получено 0 байт, ожидалось {expected}",
            )
        if written == 0 and not resumed:
            # Данных не было, и вызывающий этого не запрещал: «скачали пустой файл» —
            # валидный исход, наблюдаемо он обязан совпадать с записью пустого тела.
            _open_dest(dest, mode).close()

        total = _total_from_range(headers.get("content-range"))
        if total is None:
            if declared is not None:
                total = (resume_from if resumed else 0) + declared
            else:
                total = expected if expected is not None else written
        return {
            "bytes_written": written,
            "total_bytes": int(total),
            "resumed": resumed,
            "status": status,
            "etag": headers.get("etag"),
            "sha256_header": headers.get("x-bpmkit-sha256"),
            "version_header": headers.get("x-bpmkit-version"),
        }


# ------------------------------------------------------------------------------------
# Вспомогательное
# ------------------------------------------------------------------------------------
def _encode_params(params: Optional[dict]) -> str:
    """Query-строка без `None`-значений.

    `None` в параметре означает «не задан» (например, курсора синхронизации ещё нет).
    Пропустить его через `urlencode` как есть — прислать серверу литерал `since=None`,
    который тот честно попытается разобрать как дату и ответит `422`.
    """
    if not params:
        return ""
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            # FastAPI ждёт `true`/`false`, а `str(True)` дал бы `True`.
            value = "true" if value else "false"
        pairs.append((str(key), str(value)))
    return urllib.parse.urlencode(pairs)


def _reason_text(exc: BaseException) -> str:
    """Короткая причина сетевого отказа без стек-трейса."""
    reason = getattr(exc, "reason", None)
    return str(reason) if reason is not None else str(exc)


def _int_or_none(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _total_from_range(content_range: Optional[str]) -> Optional[int]:
    """Полный размер файла из `Content-Range: bytes a-b/size`. `*` вместо размера
    (его сервер шлёт в `416`) — не размер, а «неизвестно»."""
    if not content_range or "/" not in content_range:
        return None
    return _int_or_none(content_range.rsplit("/", 1)[1])


def _unlink_quietly(path: Path) -> None:
    """Удаление, которое не может стать второй ошибкой поверх первой."""
    try:
        path.unlink()
    except OSError:
        pass


def _stream_to_file(resp, dest: Path, *, mode: str, chunk_size: int) -> int:
    """Переливание тела ответа в файл кусками. Возвращает число ЗАПИСАННЫХ байт.

    Файл открывается лениво — на первом непустом куске (см. п.2 докстринга `download`).
    Если данных не пришло вовсе, приёмник НЕ трогается здесь вообще: решение «это пустой
    файл» или «это отказ отдачи» принимает `download`, у которого есть `expected_size`.
    Создать пустышку тут же значило бы обнулить рабочий бинарь ещё до проверки.
    """
    handle = None
    written = 0
    try:
        while True:
            try:
                chunk = resp.read(max(1, int(chunk_size)))
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                raise ChannelError(
                    "Обрыв соединения при скачивании файла",
                    kind="offline",
                    detail=_clip(_reason_text(exc)),
                ) from None
            if not chunk:
                break
            if handle is None:
                handle = _open_dest(dest, mode)
            try:
                handle.write(chunk)
            except OSError as exc:
                raise ChannelError(
                    "Не удалось записать скачанные данные на диск",
                    kind="local_io",
                    detail=_clip(str(exc)),
                ) from None
            written += len(chunk)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    return written


def _open_dest(dest: Path, mode: str):
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return open(dest, mode)
    except OSError as exc:
        raise ChannelError(
            "Не удалось открыть файл для записи скачанного",
            kind="local_io",
            detail=_clip(str(exc)),
        ) from None
