# -*- coding: utf-8 -*-
"""Доверие TLS для канала обновлений компаньона (бэкенд издателя).

Синхронная копия модуля `net_tls` клиентского MCP-сервера BPMkit: пакет `standkit_companion`
stdlib-only и не может его импортировать (отдельный репозиторий и отдельная поставка). Вшитые
корни, отпечатки и API (`tls_context`, `embedded_only_context`, `urlopen`, `probe_tls`,
`is_certificate_verify_failed`) совпадают; менять их надо в обоих местах сразу.

ПРОБЛЕМА. Сертификат сервера издателя выпускает Let's Encrypt (цепочка до корня ISRG Root X1).
На чистой Windows этого корня в хранилище может не быть: система докачивает недостающие корни
по требованию (механизм AuthRoot), но только для СВОИХ TLS-клиентов (браузер, PowerShell,
curl.exe через Schannel). Python ``ssl`` в собранном exe читает хранилище как есть и докачку не
запускает, поэтому онлайн-активация лицензии, проверка версии, паттерны и обновления падали
с ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``.

РЕШЕНИЕ. Доверие = системное хранилище (на Windows ``ssl.create_default_context()`` сам читает
хранилища CA и ROOT) ПЛЮС вшитые корни ISRG Root X1 и ISRG Root X2 (константы ниже) ПЛЮС
необязательный файл корпоративного корня из env ``BPMKIT_TLS_EXTRA_CA`` (прокси с подменой
TLS). Системное хранилище сохраняется, поэтому корпоративные прокси продолжают работать;
вшитые корни закрывают именно чистую машину. Корень X1 действует до 2035 года, X2 -- до 2040,
ротация короткоживущего листового сертификата ничего не меняет. Сторонние пакеты (truststore,
certifi) не нужны: модуль только из стандартной библиотеки.

ОТПЕЧАТКИ (SHA-256 от DER). Сверены с набором Mozilla (certifi) и независимо -- с системным
набором корней Debian (ca-certificates):

    ISRG Root X1  96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6
    ISRG Root X2  69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470

Если издатель сменит центр сертификации, вшитые корни надо обновить в обеих копиях.

ИМПОРТ. Модуль самостоятельный и не импортирует остальной пакет.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
import urllib.request

_LOG = logging.getLogger("standkit_companion.net_tls")

#: Переменная окружения: путь к PEM-файлу с дополнительными корнями (корпоративный прокси).
ENV_EXTRA_CA = "BPMKIT_TLS_EXTRA_CA"

#: Переменная окружения: ``off`` отключает сетевую пробу (тесты, закрытые контуры).
ENV_PROBE = "BPMKIT_TLS_PROBE"

ISRG_ROOT_X1_NAME = "ISRG Root X1"
ISRG_ROOT_X1_SHA256 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"
ISRG_ROOT_X1_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
"""

ISRG_ROOT_X2_NAME = "ISRG Root X2"
ISRG_ROOT_X2_SHA256 = "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470"
ISRG_ROOT_X2_PEM = """\
-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFfLn
-----END CERTIFICATE-----
"""

#: (имя, PEM, SHA-256 от DER) -- вшитые корни в порядке приоритета.
EMBEDDED_ROOTS = (
    (ISRG_ROOT_X1_NAME, ISRG_ROOT_X1_PEM, ISRG_ROOT_X1_SHA256),
    (ISRG_ROOT_X2_NAME, ISRG_ROOT_X2_PEM, ISRG_ROOT_X2_SHA256),
)

#: Человеческая причина отказа проверки сертификата -- ЕДИНЫЙ текст для пользователя.
#: Адрес сервера в нём намеренно не называется; подробности -- в журнале канала.
CERT_VERIFY_USER_TEXT = (
    "не удалось проверить сертификат сервера лицензий -- нет доверенного корня в системе; "
    "подробности: журнал канала обновлений")

_lock = threading.Lock()
_cache = {}
_last_extra_error = [""]


def _all_embedded_pem():
    return "\n".join(pem for _name, pem, _sha in EMBEDDED_ROOTS)


def _extra_ca_path(extra_ca_file=None):
    return (extra_ca_file or os.environ.get(ENV_EXTRA_CA) or "").strip()


def _load_extra_ca(context, path):
    """Подмешать файл корпоративных корней. Ошибка НЕ фатальна: WARN в лог, контекст остаётся
    рабочим (системные + вшитые корни). Возвращает True, если файл загружен."""
    if not path:
        return False
    try:
        context.load_verify_locations(cafile=path)
        _last_extra_error[0] = ""
        return True
    except (OSError, ssl.SSLError, ValueError) as exc:
        _last_extra_error[0] = "{}: {}".format(type(exc).__name__, exc)
        _LOG.warning("%s: файл дополнительных корней не загружен (%s)", ENV_EXTRA_CA,
                     _last_extra_error[0])
        return False


def last_extra_ca_error():
    """Текст последней ошибки загрузки файла из env BPMKIT_TLS_EXTRA_CA ('' -- ошибок нет)."""
    return _last_extra_error[0]


def extra_ca_configured():
    """Путь из env BPMKIT_TLS_EXTRA_CA ('' -- не задан)."""
    return _extra_ca_path()


def system_context():
    """Контекст ТОЛЬКО с системным доверием (без вшитых корней) -- для диагностики источника."""
    return ssl.create_default_context()


def tls_context(extra_ca_file=None):
    """Кэшированный ``ssl.SSLContext`` для клиентских каналов: системное хранилище + вшитые
    корни ISRG + (необязательно) файл корпоративных корней из env BPMKIT_TLS_EXTRA_CA либо
    параметра. Проверка сертификата и имени хоста включены (``create_default_context``)."""
    path = _extra_ca_path(extra_ca_file)
    with _lock:
        ctx = _cache.get(path)
        if ctx is not None:
            return ctx
        ctx = ssl.create_default_context()
        try:
            ctx.load_verify_locations(cadata=_all_embedded_pem())
        except ssl.SSLError as exc:  # не должно случаться: PEM проверяются тестами
            _LOG.warning("вшитые корни не загружены (%s)", exc)
        _load_extra_ca(ctx, path)
        _cache[path] = ctx
        return ctx


def embedded_only_context():
    """Контекст ТОЛЬКО со вшитыми корнями (без системного хранилища и без extra) -- для диагностики
    и тестов: показывает, что цепочка бэкенда проверяется без помощи системы."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    ctx.load_verify_locations(cadata=_all_embedded_pem())
    return ctx


def _single_root_context(pem):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.load_verify_locations(cadata=pem)
    return ctx


def reset_cache():
    """Сбросить кэш контекстов (тесты, смена env в процессе)."""
    with _lock:
        _cache.clear()
    _last_extra_error[0] = ""


def urlopen(req, timeout=None, **kw):
    """Тонкая обёртка над ``urllib.request.urlopen`` с контекстом ``tls_context()``.

    ``context`` из kwargs имеет приоритет (явное решение вызывающего). Для http-адресов
    (loopback-бэкенд) контекст urllib игнорирует."""
    if "context" not in kw:
        kw["context"] = tls_context()
    if timeout is not None:
        kw["timeout"] = timeout
    return urllib.request.urlopen(req, **kw)


def is_certificate_verify_failed(exc):
    """True, если исключение (или его причина/цепочка) -- отказ проверки сертификата."""
    seen = set()
    stack = [exc]
    while stack:
        cand = stack.pop()
        if cand is None or id(cand) in seen:
            continue
        seen.add(id(cand))
        if isinstance(cand, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cand).upper():
            return True
        if not isinstance(cand, BaseException):
            continue  # URLError.reason бывает строкой -- её текст проверен выше
        stack.extend([getattr(cand, "reason", None), cand.__cause__, cand.__context__])
    return False


# ---------------------------------------------------------------------------
# Проба соединения (диагностика)
# ---------------------------------------------------------------------------
def _cn(rdns):
    for rdn in rdns or ():
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


def _handshake(host, port, timeout, context):
    """Установить TLS-соединение, вернуть ``getpeercert()`` листа. Исключения наружу."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        with context.wrap_socket(sock, server_hostname=host) as ssock:
            return ssock.getpeercert() or {}
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _classify(exc):
    """(код, текст для пользователя без адресов)."""
    if is_certificate_verify_failed(exc):
        text = CERT_VERIFY_USER_TEXT
        if "self-signed certificate in certificate chain" in str(exc) or \
                "self signed certificate in certificate chain" in str(exc):
            text += ("; похоже, трафик перехватывает прокси с подменой сертификата -- "
                     "корпоративный корень можно указать в env {}".format(ENV_EXTRA_CA))
        return "verify_failed", text
    reason = getattr(exc, "reason", None)
    for cand in (exc, reason):
        if isinstance(cand, socket.gaierror):
            return "dns", "имя сервера не разрешается -- нет связи с интернетом или DNS недоступен"
        if isinstance(cand, (socket.timeout, TimeoutError)):
            return "timeout", "сервер не ответил за отведённое время"
        if isinstance(cand, ConnectionRefusedError):
            return "unreachable", "соединение отклонено"
        if isinstance(cand, ssl.SSLError):
            return "handshake_failed", "TLS-рукопожатие не состоялось"
        if isinstance(cand, OSError):
            return "unreachable", "сервер недоступен"
    return "error", "сбой соединения"


def probe_tls(host, port=443, timeout=10, embedded_only=False):
    """Проба TLS-цепочки сервера. Никогда не бросает исключение.

    Возвращает dict: ``ok``; ``reason`` (ok/verify_failed/handshake_failed/dns/timeout/
    unreachable/error); ``user_text`` (человекочитаемо, БЕЗ адресов); ``detail`` (текст
    исключения -- для диагностики поддержки, может содержать адрес); ``host``/``port``;
    ``leaf_subject``/``leaf_issuer``/``leaf_not_after``; ``root`` (имя вшитого корня, которым
    цепочка проверяется, либо ''); ``root_source`` ('embedded' -- системное хранилище цепочку не
    проверяет, помогли вшитые корни; 'system' -- хватает системного; 'extra' -- помог файл из
    env BPMKIT_TLS_EXTRA_CA; '' -- не определён); ``embedded_only``."""
    res = {"ok": False, "reason": "", "user_text": "", "detail": "", "host": host,
           "port": port, "embedded_only": bool(embedded_only), "leaf_subject": "",
           "leaf_issuer": "", "leaf_not_after": "", "root": "", "root_source": ""}
    ctx = embedded_only_context() if embedded_only else tls_context()
    try:
        cert = _handshake(host, port, timeout, ctx)
    except Exception as exc:  # noqa: BLE001 -- проба обязана вернуть dict при любом сбое
        code, text = _classify(exc)
        res.update(reason=code, user_text=text, detail="{}: {}".format(type(exc).__name__, exc))
        return res
    res.update(ok=True, reason="ok", leaf_subject=_cn(cert.get("subject")),
               leaf_issuer=_cn(cert.get("issuer")), leaf_not_after=cert.get("notAfter", ""))
    # Какой вшитый корень закрывает цепочку (по одному корню за раз).
    for name, pem, _sha in EMBEDDED_ROOTS:
        try:
            _handshake(host, port, timeout, _single_root_context(pem))
            res["root"] = name
            break
        except Exception:  # noqa: BLE001
            continue
    if embedded_only:
        res["root_source"] = "embedded"
        return res
    try:
        _handshake(host, port, timeout, system_context())
        res["root_source"] = "system"
    except Exception:  # noqa: BLE001
        res["root_source"] = "embedded" if res["root"] else (
            "extra" if _extra_ca_path() else "")
    return res


#: Короткое имя для внешних потребителей. Алиас присваиванием, а не вторым ``def``: в пакете уже
#: есть функция ``probe`` (fsprobe), и второе определение с тем же именем делает вызовы
#: ``probe(...)`` неоднозначными для генератора каналов инструментов.
probe = probe_tls
