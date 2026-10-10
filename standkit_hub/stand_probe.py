# -*- coding: utf-8 -*-
"""Чтение настроек стенда из его собственных конфигов, проверка соединений и
обзор каталогов для формы «Зарегистрировать стенд» / «Изменить» диспетчера.

Модуль даёт три независимые вещи (HTTP-обёртки — в ``standkit_hub.server``):

* ``probe_folder(path)`` — «Заполнить из конфигов». По папке инстанса читает
  ``ConnectionStrings.config`` (БД и Redis), ``appsettings.json`` (порт сайта) и
  ищет WorkspaceConsole (движок). **Пароли наружу не отдаются никогда**: в ответе
  только отметка ``password_in_config`` (да/нет). Сам пароль читает отдельная
  функция ``read_config_passwords`` — её зовёт только серверный код, которому он
  нужен (проверка входа в БД, перенос в хранилище секретов), и значение не
  попадает ни в ответ, ни в лог.
* ``test_connection(params)`` — кнопка «Тест»: резолв имён, TCP-порты, вход в БД
  (если на машине есть драйвер), ``PING`` Redis по голому RESP-сокету, каталоги.
  Результат — построчный список проверок; ничего не пишет.
* ``browse_dir(path)`` — список подкаталогов для встроенного выбора папки, когда
  нативный диалог ОС недоступен. Только абсолютные локальные пути; корень — диски.

Только stdlib, как и остальной хаб. Все функции не бросают исключений наружу:
сбой чтения конфига — это «не нашли», а не 500.
"""

from __future__ import annotations

import json
import os
import re
import socket
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from standkit.secrets import SecretError, get_secret
from standkit_hub import redis_min

__all__ = [
    "CONNECTION_STRINGS_FILE",
    "DEFAULT_PORTS",
    "BROWSE_MAX_ENTRIES",
    "probe_folder",
    "read_config_passwords",
    "test_connection",
    "browse_dir",
    "list_roots",
    "BrowseError",
]

CONNECTION_STRINGS_FILE = "ConnectionStrings.config"

#: Порты по умолчанию для локальной разработки. У MSSQL порта «по умолчанию» в
#: форме нет намеренно: именованные экземпляры слушают динамический порт.
DEFAULT_PORTS = {"postgres": 5432, "redis": 6379, "mssql": 1433}

#: Подкаталоги инстанса, где может лежать ``ConnectionStrings.config``: корень
#: (NetCore/Kestrel) и ``BPMSoft.WebApp`` (NetFramework под IIS).
_CONFIG_SUBDIRS = ("", "BPMSoft.WebApp")

_WSC_DLL_REL = os.path.join("WorkspaceConsole", "BPMSoft.Tools.WorkspaceConsole.dll")

_MAX_CONFIG_BYTES = 2 * 1024 * 1024

#: Потолок числа подкаталогов в одном ответе обзора — защита от каталога с
#: десятками тысяч папок (ответ и отрисовка иначе зависнут).
BROWSE_MAX_ENTRIES = 1000

#: Таймаут одной сетевой проверки, секунды.
_NET_TIMEOUT_SEC = 4.0
#: Сколько ждём резолв имени: getaddrinfo сам таймаута не имеет.
_RESOLVE_TIMEOUT_SEC = 6.0

_MSSQL_KEYS_RE = re.compile(r"(?i)(?:^|;)\s*(?:data source|initial catalog)\s*=")
_WIN_AUTH_RE = re.compile(
    r"(?i)(?:^|;)\s*(?:trusted_connection|integrated security)\s*=\s*(?:true|yes|sspi)\s*(?:;|$)"
)
_DB_KEYS = {
    "postgres": (
        ("db_name", ("database",)),
        ("db_host", ("host", "server")),
        ("db_port", ("port",)),
        ("db_user", ("username", "user id", "user")),
    ),
    "mssql": (
        ("db_name", ("initial catalog", "database")),
        ("db_host", ("data source", "server")),
        ("db_port", ("port",)),
        ("db_user", ("user id", "uid", "username")),
    ),
}
_PASSWORD_KEYS = ("password", "pwd")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]:\\]{1,255}$")


# ---------------------------------------------------------------------------
# Чтение конфигов
# ---------------------------------------------------------------------------


def _parse_kv(conn_str: str) -> dict:
    """'Key=Value; Key=Value' -> {key_lower: value}."""
    out: dict = {}
    for part in (conn_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        if key:
            out[key] = value.strip()
    return out


def _xml_unescape(value: str) -> str:
    """XML-сущности значения атрибута; без модуля ``html`` (его может не быть в сборке)."""

    def _num(m: "re.Match[str]") -> str:
        try:
            code = m.group(1)
            return chr(int(code[1:], 16) if code[:1] in "xX" else int(code))
        except (ValueError, OverflowError):
            return m.group(0)

    out = re.sub(r"&#([xX]?[0-9A-Fa-f]+);", _num, value or "")
    for ent, ch in (("&quot;", '"'), ("&apos;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        out = out.replace(ent, ch)
    return out


def _read_text(path: Path) -> Optional[str]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_CONFIG_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _strip_xml_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.S)


def _find_config_dir(root: Path) -> Optional[Path]:
    """Каталог, где лежит ``ConnectionStrings.config`` (корень инстанса или ``BPMSoft.WebApp``)."""
    for sub in _CONFIG_SUBDIRS:
        candidate = root / sub if sub else root
        try:
            if (candidate / CONNECTION_STRINGS_FILE).is_file():
                return candidate
        except OSError:
            continue
    return None


def _db_entry(config_dir: Path) -> tuple[dict, str]:
    """(info с ключом ``_password``, текст ошибки). Пустой info — запись не найдена."""
    text = _read_text(config_dir / CONNECTION_STRINGS_FILE)
    if text is None:
        return {}, f"{CONNECTION_STRINGS_FILE} не прочитан"
    text = _strip_xml_comments(text)
    for db_type, entry in (("postgres", "db"), ("mssql", "dbMsSQL")):
        m = re.search(r'<add\s+name="%s"\s+connectionString="([^"]*)"' % entry, text, re.IGNORECASE)
        if not m:
            continue
        raw = _xml_unescape(m.group(1))
        if _MSSQL_KEYS_RE.search(raw):
            db_type = "mssql"
        kv = _parse_kv(raw)
        info: dict = {
            "db_type": db_type,
            "windows_auth": bool(_WIN_AUTH_RE.search(raw)),
            "_password": "",
        }
        for field, aliases in _DB_KEYS[db_type]:
            for alias in aliases:
                if kv.get(alias):
                    info[field] = kv[alias]
                    break
        for alias in _PASSWORD_KEYS:
            if kv.get(alias):
                info["_password"] = kv[alias]
                break
        host = str(info.get("db_host", ""))
        if db_type == "mssql" and "," in host:
            # «host,1433» — порт после запятой
            head, tail = host.split(",", 1)
            info["db_host"] = head.strip()
            if tail.strip().isdigit() and not info.get("db_port"):
                info["db_port"] = tail.strip()
        if str(info.get("db_port", "")).strip().isdigit():
            info["db_port"] = int(str(info["db_port"]).strip())
        else:
            info.pop("db_port", None)
        return info, ""
    return {}, f'в {CONNECTION_STRINGS_FILE} нет записи name="db" / name="dbMsSQL"'


def _redis_entry(config_dir: Path) -> dict:
    """Redis из ``ConnectionStrings.config`` (+ запасной поиск ``redis_min``): host/port/db и ``_password``."""
    text = _read_text(config_dir / CONNECTION_STRINGS_FILE)
    if text:
        cs = redis_min._extract_connection_string_from_config_xml(_strip_xml_comments(text))
        if cs:
            kv = _parse_kv(_xml_unescape(cs))
            host = kv.get("host") or kv.get("server") or "127.0.0.1"
            port_raw = kv.get("port", "")
            db_raw = kv.get("db") or kv.get("$db") or kv.get("database") or ""
            found: dict = {
                "host": host,
                "port": int(port_raw) if port_raw.isdigit() else DEFAULT_PORTS["redis"],
                "db": int(db_raw) if db_raw.isdigit() else None,
                "_password": kv.get("password", ""),
            }
            return found
    fallback = redis_min.resolve_redis_from_stand_config(str(config_dir))
    if fallback:
        return {**fallback, "_password": ""}
    return {}


def _site_entry(dirs: list[Path]) -> dict:
    """Схема/порт сайта из ``appsettings.json`` (Kestrel). Пусто — не нашли (IIS: порт в биндингах сайта)."""
    for d in dirs:
        text = _read_text(d / "appsettings.json")
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        urls: list[tuple[str, str]] = []
        endpoints = (data.get("Kestrel") or {}).get("Endpoints") if isinstance(data.get("Kestrel"), dict) else None
        if isinstance(endpoints, dict):
            # Http раньше Https: диспетчер по умолчанию ходит по http, https — только осознанно.
            ordered = sorted(endpoints.items(), key=lambda kv: (str(kv[0]).lower() != "http", str(kv[0])))
            for key, block in ordered:
                if isinstance(block, dict) and isinstance(block.get("Url"), str):
                    urls.append((str(key), block["Url"]))
        for key in ("Urls", "urls"):
            if isinstance(data.get(key), str):
                urls.append((key, data[key]))
        for _key, raw in urls:
            for piece in raw.split(";"):
                m = re.match(r"^\s*(https?)://([^:/\s]+|\[[^\]]+\]):(\d+)", piece, re.IGNORECASE)
                if not m:
                    continue
                site: dict = {"scheme": m.group(1).lower(), "port": int(m.group(3)), "source": "appsettings.json"}
                host = m.group(2)
                if host not in ("*", "+", "0.0.0.0", "[::]", "::"):
                    site["host"] = host
                return site
    return {}


def _engine_entry(root: Path) -> dict:
    """Движок WorkspaceConsole, если он лежит в папке стенда (как в серверной части BPMkit)."""
    try:
        dll = root / _WSC_DLL_REL
        if dll.is_file():
            return {
                "type": "workspaceconsole",
                "wsc_dll": str(dll),
                "workspace_name": "Default",
                "web_app_path": str(root),
            }
    except OSError:
        pass
    return {}


def _is_abs_local(path: str) -> bool:
    return bool(path) and "\x00" not in path and os.path.isabs(path) and not path.startswith(("\\\\", "//"))


def probe_folder(path: Any) -> dict:
    """«Заполнить из конфигов» по папке инстанса. Пароли в ответ не попадают.

    Возвращает ``{"ok": bool, "error"?, "path", "config_dir", "layout", "db", "redis",
    "site", "engine", "notes": [...]}``; ``db``/``redis``/``site``/``engine`` — ``None``,
    если в конфигах не нашлись.
    """
    raw = path.strip() if isinstance(path, str) else ""
    if not raw:
        return {"ok": False, "error": "укажите папку инстанса"}
    if not _is_abs_local(raw):
        return {"ok": False, "error": "нужен абсолютный локальный путь к папке"}
    root = Path(os.path.normpath(raw))
    try:
        if not root.is_dir():
            return {"ok": False, "error": f"папка не найдена: {root}"}
    except OSError as exc:
        return {"ok": False, "error": f"папка недоступна: {exc}"}

    notes: list[str] = []
    config_dir = _find_config_dir(root)
    layout = None
    try:
        if (root / "BPMSoft.WebApp").is_dir():
            layout = "netframework"
        elif (root / "appsettings.json").is_file():
            layout = "netcore"
    except OSError:
        pass

    db = redis = None
    if config_dir is None:
        notes.append(f"{CONNECTION_STRINGS_FILE} не найден ни в папке, ни в BPMSoft.WebApp")
    else:
        info, err = _db_entry(config_dir)
        if info:
            db = {k: v for k, v in info.items() if k != "_password"}
            db["password_in_config"] = bool(info.get("_password"))
            if info.get("windows_auth"):
                notes.append("БД: встроенная аутентификация Windows — пользователь и пароль не нужны")
        elif err:
            notes.append(err)
        rinfo = _redis_entry(config_dir)
        if rinfo:
            redis = {k: v for k, v in rinfo.items() if k != "_password"}
            redis["password_in_config"] = bool(rinfo.get("_password"))
        else:
            notes.append("Redis в конфигах не найден")

    search_dirs = [root] + ([config_dir] if config_dir and config_dir != root else [])
    site = _site_entry(search_dirs) or None
    if site is None:
        if layout == "netframework":
            notes.append("порт сайта под IIS задаётся биндингами сайта — используйте «Определить автоматически»")
        else:
            notes.append("порт сайта в appsettings.json не найден")
    engine = _engine_entry(root) or None

    return {
        "ok": True,
        "path": str(root),
        "config_dir": str(config_dir) if config_dir else None,
        "layout": layout,
        "host_kind_hint": "iis" if layout == "netframework" else ("kestrel" if layout == "netcore" else None),
        "db": db,
        "redis": redis,
        "site": site,
        "engine": engine,
        "notes": notes,
    }


def read_config_passwords(path: Any) -> dict:
    """Пароли БД/Redis из конфигов папки — ТОЛЬКО для серверного кода (вход в БД, перенос в
    хранилище секретов). Значение никогда не возвращается клиенту и не пишется в лог.

    Возвращает ``{"db": str, "redis": str}`` (пустая строка — пароля нет/конфиг не найден).
    """
    out = {"db": "", "redis": ""}
    try:
        raw = path.strip() if isinstance(path, str) else ""
        if not _is_abs_local(raw):
            return out
        root = Path(os.path.normpath(raw))
        config_dir = _find_config_dir(root)
        if config_dir is None:
            return out
        info, _err = _db_entry(config_dir)
        out["db"] = str(info.get("_password") or "")
        out["redis"] = str(_redis_entry(config_dir).get("_password") or "")
    except Exception:  # noqa: BLE001 - чтение конфига не роняет запрос
        pass
    return out


# ---------------------------------------------------------------------------
# Обзор каталогов
# ---------------------------------------------------------------------------


class BrowseError(Exception):
    """Путь не годится для обзора (относительный, сетевой, не каталог, недоступен)."""


def _is_windows() -> bool:
    return os.name == "nt"


def list_roots() -> list[dict]:
    """Корни обзора: диски на Windows, ``/`` в остальных ОС."""
    if _is_windows():
        roots = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            try:
                if os.path.isdir(drive):
                    roots.append({"name": f"{letter}:", "path": drive})
            except OSError:
                continue
        return roots
    return [{"name": "/", "path": "/"}]


def browse_dir(path: Any) -> dict:
    """Подкаталоги ``path`` (пусто — список корней). Файлы не показываются.

    Ограничения: только абсолютный путь, без UNC (сетевые шары могут подвесить
    запрос), без NUL; ``..`` схлопывается ``normpath`` ДО проверки. Выше корня диска
    подняться нельзя: у корня ``parent`` равен ``""`` (экран выбора диска).
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        return {"path": "", "parent": None, "dirs": list_roots(), "roots": True, "truncated": False}
    if not isinstance(path, str):
        raise BrowseError("путь должен быть строкой")
    raw = path.strip()
    if "\x00" in raw:
        raise BrowseError("недопустимый путь")
    if raw.startswith(("\\\\", "//")):
        raise BrowseError("сетевые пути (UNC) не поддерживаются — укажите локальный диск")
    if not os.path.isabs(raw):
        raise BrowseError("нужен абсолютный путь")
    norm = os.path.normpath(raw)
    try:
        if not os.path.isdir(norm):
            raise BrowseError(f"не каталог или не найден: {norm}")
        names = sorted(os.listdir(norm), key=lambda s: s.lower())
    except BrowseError:
        raise
    except OSError as exc:
        raise BrowseError(f"каталог недоступен: {exc}") from exc

    dirs: list[dict] = []
    truncated = False
    for name in names:
        full = os.path.join(norm, name)
        try:
            if not os.path.isdir(full):
                continue
        except OSError:
            continue
        if len(dirs) >= BROWSE_MAX_ENTRIES:
            truncated = True
            break
        dirs.append({"name": name, "path": full})

    # У корня диска (C:\\, /) dirname возвращает тот же путь — выше подняться нельзя,
    # родитель — экран выбора диска (""), а не произвольное место файловой системы.
    parent = os.path.dirname(norm)
    at_root = parent == norm
    try:
        is_instance = os.path.isfile(os.path.join(norm, CONNECTION_STRINGS_FILE))
    except OSError:
        is_instance = False
    return {
        "path": norm,
        "parent": "" if at_root else parent,
        "dirs": dirs,
        "roots": False,
        "truncated": truncated,
        "is_instance": is_instance,
    }


# ---------------------------------------------------------------------------
# Проверка соединений («Тест»)
# ---------------------------------------------------------------------------


def _check(cid: str, label: str, status: str, message: str) -> dict:
    return {"id": cid, "label": label, "status": status, "message": message}


def _mask(text: str, secrets_: list[str]) -> str:
    out = str(text)
    for s in secrets_:
        if s:
            out = out.replace(s, "***")
    return out


def _resolve(host: str, timeout: float = _RESOLVE_TIMEOUT_SEC) -> tuple[Optional[bool], str]:
    """(True|False|None, причина). None — вердикта нет (DNS недоступен/не дождались)."""
    h = (host or "").strip()
    if h in ("", ".", "(local)") or h.startswith("(localdb)"):
        return True, ""
    result: dict = {}

    def _probe() -> None:
        try:
            socket.getaddrinfo(h, None)
            result["ok"] = True
        except socket.gaierror as exc:
            if getattr(exc, "errno", None) in (getattr(socket, "EAI_AGAIN", -3), 11002):
                return
            result["ok"] = False
            result["err"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["err"] = str(exc)

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout)
    if "ok" not in result:
        return None, "ответа DNS нет"
    return (True, "") if result["ok"] else (False, result.get("err", ""))


def _tcp(host: str, port: int, timeout: float = _NET_TIMEOUT_SEC) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def _as_port(value: Any) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 65536 else None


def _mssql_browser_port(host: str, instance: str, timeout: float = _NET_TIMEOUT_SEC) -> Optional[int]:
    """TCP-порт именованного экземпляра MSSQL через SQL Server Browser (UDP 1434)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(b"\x04" + instance.encode("ascii", "ignore") + b"\x00", (host, 1434))
            data, _ = sock.recvfrom(4096)
    except OSError:
        return None
    m = re.search(rb"tcp;(\d{1,5});", data)
    return int(m.group(1)) if m else None


def _site_checks(params: dict) -> list[dict]:
    host = str(params.get("stand_host") or "").strip()
    port = _as_port(params.get("stand_port"))
    if not host or not port:
        return [_check("site", "Сайт стенда", "skip", "адрес и порт сайта не заданы")]
    if not _HOST_RE.match(host):
        return [_check("site", "Сайт стенда", "fail", f"недопустимый адрес: {host!r}")]
    rows = []
    ok, why = _resolve(host)
    if ok is False:
        return [_check("site.resolve", "Имя сайта", "fail", f"'{host}' не резолвится ({why})")]
    rows.append(_check("site.resolve", "Имя сайта", "ok" if ok else "warn", f"'{host}' резолвится" if ok else "резолв не подтверждён"))
    up, err = _tcp(host, port)
    if up:
        rows.append(_check("site.port", "Порт сайта", "ok", f"{host}:{port} принимает соединения"))
    else:
        # Стенд при регистрации часто остановлен — это не ошибка записи.
        rows.append(_check("site.port", "Порт сайта", "warn", f"{host}:{port} не отвечает ({err}); если стенд остановлен — это нормально"))
    return rows


def _pg_login(host: str, port: int, dbname: str, user: str, password: str) -> tuple[str, str]:
    kwargs = dict(host=host, port=port, dbname=dbname or "postgres", user=user, password=password)
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(connect_timeout=int(_NET_TIMEOUT_SEC), **kwargs)
        try:
            cur = conn.cursor()
            cur.execute("select 1")
            cur.fetchone()
        finally:
            conn.close()
        return "ok", f"вход выполнен (драйвер psycopg2), БД {kwargs['dbname']}"
    except ImportError:
        pass
    try:
        import psycopg  # type: ignore
    except ImportError:
        return "skip", "драйвера PostgreSQL (psycopg2/psycopg) нет — проверен только TCP-порт"
    with psycopg.connect(connect_timeout=int(_NET_TIMEOUT_SEC), **kwargs) as conn:
        conn.execute("select 1")
    return "ok", f"вход выполнен (драйвер psycopg), БД {kwargs['dbname']}"


def _pg_login_safe(host: str, port: int, dbname: str, user: str, password: str) -> tuple[str, str]:
    try:
        return _pg_login(host, port, dbname, user, password)
    except Exception as exc:  # noqa: BLE001 - ошибка psycopg2 (отказ пароля, БД нет и т.п.)
        return "fail", _mask(f"{type(exc).__name__}: {exc}".strip(), [password])


def _mssql_login(server: str, port: Optional[int], dbname: str, user: str, password: str) -> tuple[str, str]:
    try:
        import pymssql  # type: ignore

        conn = pymssql.connect(
            server=server, port=str(port or DEFAULT_PORTS["mssql"]), user=user, password=password,
            database=dbname or "master", login_timeout=int(_NET_TIMEOUT_SEC),
        )
        conn.close()
        return "ok", f"вход выполнен (драйвер pymssql), БД {dbname or 'master'}"
    except ImportError:
        pass
    try:
        import pyodbc  # type: ignore
    except ImportError:
        return "skip", "драйвера MSSQL (pymssql/pyodbc) нет — проверен только TCP-порт"
    drivers = [d for d in pyodbc.drivers() if "sql server" in d.lower()]
    if not drivers:
        return "skip", "ODBC-драйвера SQL Server на машине нет — проверен только TCP-порт"
    drivers.sort(reverse=True)  # «ODBC Driver 18…» новее «…17…» и «SQL Server»
    target = f"{server},{port}" if port else server
    conn_str = (
        f"DRIVER={{{drivers[0]}}};SERVER={target};DATABASE={dbname or 'master'};"
        f"UID={user};PWD={password};TrustServerCertificate=yes"
    )
    conn = pyodbc.connect(conn_str, timeout=int(_NET_TIMEOUT_SEC))
    conn.close()
    return "ok", f"вход выполнен (драйвер {drivers[0]}), БД {dbname or 'master'}"


def _mssql_login_safe(server: str, port: Optional[int], dbname: str, user: str, password: str) -> tuple[str, str]:
    try:
        return _mssql_login(server, port, dbname, user, password)
    except Exception as exc:  # noqa: BLE001
        return "fail", _mask(f"{type(exc).__name__}: {exc}".strip(), [password])


def _db_password(params: dict) -> tuple[str, str]:
    """(пароль, откуда). Пароль не возвращается клиенту — только используется для входа."""
    ref = str(params.get("secret_ref_db") or "").strip()
    if ref:
        try:
            return get_secret(ref), "хранилище секретов"
        except SecretError:
            pass
    if params.get("use_config_password") and params.get("stand_dir"):
        pw = read_config_passwords(params.get("stand_dir"))["db"]
        if pw:
            return pw, "конфиг стенда"
    return "", ""


def _db_checks(params: dict) -> list[dict]:
    db_type = str(params.get("db_type") or "postgres").strip().lower()
    raw_host = str(params.get("db_host") or "").strip()
    if not raw_host:
        return [_check("db", "База данных", "skip", "db_host не задан")]
    if not _HOST_RE.match(raw_host):
        return [_check("db", "База данных", "fail", f"недопустимый адрес сервера БД: {raw_host!r}")]
    host, instance = (raw_host.split("\\", 1) + [""])[:2] if "\\" in raw_host else (raw_host, "")
    host = host or "localhost"
    if host in (".", "(local)"):
        host = "localhost"
    rows: list[dict] = []
    ok, why = _resolve(host)
    if ok is False:
        rows.append(_check("db.resolve", "Имя сервера БД", "fail", f"'{host}' не резолвится ({why}) — проверьте опечатку"))
        return rows
    rows.append(_check("db.resolve", "Имя сервера БД", "ok" if ok else "warn", f"'{host}' резолвится" if ok else "резолв не подтверждён"))

    port = _as_port(params.get("db_port"))
    if port is None and db_type == "mssql":
        if instance:
            port = _mssql_browser_port(host, instance)
            if port is None:
                rows.append(_check("db.port", "Порт БД", "warn", f"порт экземпляра '{instance}' не получен от SQL Server Browser (UDP 1434) — укажите порт вручную"))
                return rows
        else:
            port = DEFAULT_PORTS["mssql"]
    elif port is None:
        port = DEFAULT_PORTS.get(db_type, DEFAULT_PORTS["postgres"])
    up, err = _tcp(host, port)
    if not up:
        rows.append(_check("db.port", "Порт БД", "fail", f"{host}:{port} не отвечает ({err})"))
        return rows
    rows.append(_check("db.port", "Порт БД", "ok", f"{host}:{port} принимает соединения ({db_type})"))

    user = str(params.get("db_user") or "").strip()
    password, source = _db_password(params)
    dbname = str(params.get("db_name") or "").strip()
    if not user:
        rows.append(_check("db.login", "Вход в БД", "skip", "db_user не задан — вход не проверялся"))
        return rows
    if not password:
        rows.append(_check("db.login", "Вход в БД", "skip", "пароль не найден (секрет secret_ref_db пуст, из конфига не использован) — вход не проверялся"))
        return rows
    if db_type == "mssql":
        status, msg = _mssql_login_safe(host, port, dbname, user, password)
    else:
        status, msg = _pg_login_safe(host, port, dbname, user, password)
    suffix = f" [пароль: {source}]" if status in ("ok", "fail") else ""
    rows.append(_check("db.login", f"Вход в БД ({user})", "warn" if status == "skip" else status, msg + suffix))
    return rows


def _redis_ping(host: str, port: int, password: str) -> tuple[bool, str]:
    try:
        sock = socket.create_connection((host, port), timeout=_NET_TIMEOUT_SEC)
    except OSError as exc:
        return False, f"не удалось подключиться: {exc}"
    try:
        sock.settimeout(_NET_TIMEOUT_SEC)
        if password:
            sock.sendall(redis_min._encode_command("AUTH", password))
            reply = redis_min._read_line(sock)
            if not reply.startswith(b"+"):
                return False, _mask("AUTH отклонён: " + reply.decode("utf-8", "replace"), [password])
        sock.sendall(redis_min._encode_command("PING"))
        reply = redis_min._read_line(sock).decode("utf-8", "replace")
        if reply.upper().startswith("+PONG"):
            return True, "PONG" + (" (после AUTH)" if password else "")
        if reply.startswith("-NOAUTH") or "NOAUTH" in reply:
            return False, "Redis требует пароль, а пароля в конфиге/форме нет"
        return False, _mask("ответ Redis: " + reply, [password])
    except (OSError, redis_min.RedisClearError) as exc:
        return False, _mask(f"ошибка обмена с Redis: {exc}", [password])
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _redis_checks(params: dict) -> list[dict]:
    host = str(params.get("redis_host") or "").strip()
    port = _as_port(params.get("redis_port"))
    if not host:
        return [_check("redis", "Redis", "skip", "redis_host не задан")]
    if not _HOST_RE.match(host):
        return [_check("redis", "Redis", "fail", f"недопустимый адрес Redis: {host!r}")]
    port = port or DEFAULT_PORTS["redis"]
    rows = []
    ok, why = _resolve(host)
    if ok is False:
        return [_check("redis.resolve", "Имя сервера Redis", "fail", f"'{host}' не резолвится ({why})")]
    rows.append(_check("redis.resolve", "Имя сервера Redis", "ok" if ok else "warn", f"'{host}' резолвится" if ok else "резолв не подтверждён"))
    password = ""
    if params.get("use_config_password") and params.get("stand_dir"):
        password = read_config_passwords(params.get("stand_dir"))["redis"]
    good, msg = _redis_ping(host, port, password)
    rows.append(_check("redis.ping", "Redis PING", "ok" if good else "fail", f"{host}:{port} — {msg}"))
    return rows


def _dir_checks(params: dict) -> list[dict]:
    rows = []
    transport = str(params.get("transport") or "local").strip().lower()
    stand_dir = str(params.get("stand_dir") or "").strip()
    if stand_dir and transport != "http":
        try:
            good = os.path.isdir(stand_dir)
        except OSError:
            good = False
        rows.append(_check("stand_dir", "Каталог стенда", "ok" if good else "fail",
                           stand_dir if good else f"каталог не найден на этой машине: {stand_dir}"))
    logs_dir = str(params.get("logs_dir") or "").strip()
    if logs_dir:
        try:
            good = os.path.isdir(logs_dir)
        except OSError:
            good = False
        rows.append(_check("logs_dir", "Каталог логов", "ok" if good else "warn",
                           logs_dir if good else f"каталог не найден: {logs_dir}"))
    return rows


def test_connection(params: Any) -> dict:
    """Кнопка «Тест»: построчные проверки введённых данных до сохранения.

    ``params`` — поля формы (transport, stand_dir, logs_dir, stand_host, stand_port,
    db_*, secret_ref_db, redis_*, use_config_password). Пароли в ``params`` НЕ
    принимаются: пароль БД берётся из хранилища секретов по ``secret_ref_db`` либо (если
    ``use_config_password``) серверным чтением конфига стенда и наружу не выводится.

    Возвращает ``{"ok": bool, "checks": [{id, label, status, message}]}``; ``ok`` —
    нет ни одной проверки со статусом ``fail`` (``warn``/``skip`` — не ошибка).
    """
    if not isinstance(params, dict):
        return {"ok": False, "checks": [_check("params", "Параметры", "fail", "ожидался объект с полями формы")]}
    groups = [_dir_checks, _site_checks, _db_checks, _redis_checks]
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = [pool.submit(g, params) for g in groups]
        checks: list[dict] = []
        for fut, group in zip(futures, groups):
            try:
                checks.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - одна проверка не роняет остальные
                checks.append(_check(group.__name__.strip("_"), "Проверка", "fail", f"внутренняя ошибка: {type(exc).__name__}: {exc}"))
    return {"ok": not any(c["status"] == "fail" for c in checks), "checks": checks}
