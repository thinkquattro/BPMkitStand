"""
Поиск и валидация «чужого» процесса стенда — усыновление (adoption) стендов,
поднятых МИМО диспетчера.

Зачем. Управление kestrel-стендом завязано на pidfile (см.
``standkit.lifecycle``): диспетчер знает pid только тех стендов, которые сам и
запустил. Стенд, поднятый руками (``dotnet BPMSoft.WebHost.dll`` из консоли,
скриптом, чужой сессией), виден живым по TCP-порту, но остановить/перезапустить
его нечем — pid неизвестен. Этот модуль закрывает разрыв: находит владельца
порта стенда, собирает по нему улики и решает, ЭТОТ ли это стенд.

Как. Только stdlib + системные утилиты (тот же приём, что уже используется для
``tasklist``/``taskkill``/``appcmd`` в ``standkit.platform``/``standkit.hosting``):

  - Windows: ``netstat -ano -p tcp`` → pid слушателя; имя образа —
    ``tasklist /FI "PID eq X" /FO CSV``; путь и командная строка —
    ``Get-CimInstance Win32_Process`` (PowerShell), фолбэк на ``wmic``.
  - Linux: ``ss -ltnp`` → фолбэк ``lsof -ti :PORT`` → фолбэк на разбор
    ``/proc/net/tcp`` + поиск inode сокета в ``/proc/*/fd`` (чистый stdlib,
    но требует того же пользователя или root). Путь/рабочий каталог/командная
    строка — ``/proc/PID/exe``, ``/proc/PID/cwd``, ``/proc/PID/cmdline``.

Вывод внешних команд декодируется НЕ здесь: используется общая цепочка
utf-8 → OEM(cp866) → cp1251 из ``standkit.hosting`` (``_run``/``_decode_console``),
чтобы не заводить второй экземпляр той же логики.

Безопасность — главное правило модуля. Найденный по порту процесс НИКОГДА не
убивается «по факту находки»: сначала обязательная валидация
(``validate_candidate``), затем явное согласие пользователя на стороне
вызывающего (см. ``standkit.lifecycle.AdoptionRequired`` и
``POST /api/stand/<name>/adopt``). Без совпадения пути к стенду усыновления не
происходит — иначе диспетчер прибил бы чужой процесс, случайно севший на тот же
порт.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from standkit.models import Stand

# Имена образов, которые вообще могут быть процессом стенда BPMSoft. Всё, чего
# нет в этом списке, усыновлению не подлежит, даже если сидит на нужном порту
# (например, случайно запущенный nginx/node в том же каталоге).
ALLOWED_IMAGE_NAMES = ("dotnet", "bpmsoft.webhost", "w3wp")

# Таймаут вспомогательных внешних вызовов (netstat/ss/tasklist/powershell) —
# это диагностика в интерактивном сценарии «нажали Стоп», ждать долго нельзя.
_PROBE_TIMEOUT = 8.0


@dataclass
class AdoptCandidate:
    """
    Кандидат на усыновление: процесс, который слушает порт стенда, вместе со
    всеми уликами, по которым принимается решение «это тот самый стенд».

    Пустая строка в поле улики означает «определить не удалось» (нет прав на
    чтение чужого процесса, другая разрядность, процесс уже завершился) — это
    нормально и трактуется валидацией как «улики нет», а не как совпадение.
    """

    pid: int
    port: int
    image: str = ""       # имя образа (``dotnet.exe`` / ``w3wp.exe`` / ``dotnet``)
    exe_path: str = ""    # полный путь к исполняемому файлу
    cwd: str = ""         # рабочий каталог: /proc/PID/cwd на POSIX, PEB на Windows
    cmdline: str = ""     # командная строка целиком
    matched_by: str = ""  # какая улика дала совпадение с каталогом стенда

    def to_dict(self) -> dict:
        """Сериализация для JSON-ответа хаба/агента (секретов здесь нет по построению)."""
        return {
            "pid": self.pid,
            "port": self.port,
            "image": self.image,
            "exe_path": self.exe_path,
            "cwd": self.cwd,
            "cmdline": self.cmdline,
            "matched_by": self.matched_by,
        }

    def describe(self) -> str:
        """Короткое человекочитаемое описание для текста подтверждения/ошибки."""
        where = self.cwd or self.exe_path or self.cmdline or "путь неизвестен"
        return f"PID {self.pid}, {self.image or 'образ неизвестен'}, {where}"


# --------------------------------------------------------------------------
# Запуск внешних утилит (переиспользуем декодирование из standkit.hosting)
# --------------------------------------------------------------------------


def _capture(cmd: list[str], *, timeout: float = _PROBE_TIMEOUT) -> Optional[str]:
    """
    Выполняет внешнюю команду и возвращает её stdout (уже декодированный
    цепочкой utf-8 → OEM → cp1251 в ``standkit.hosting._run``).

    ``None`` — команда не найдена/упала/вернула ненулевой код. Это ПРОБА:
    наружу ничего не бросается, отсутствие утилиты — штатная ситуация
    (фолбэк на следующий способ поиска).
    """
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    try:
        result = _hosting._run(cmd, timeout=timeout)
    except _hosting.HostingError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


# --------------------------------------------------------------------------
# Разбор вывода сетевых утилит — чистые функции, тестируются на зафиксированных
# строках вывода (в т.ч. локализованного netstat на RU-Windows).
# --------------------------------------------------------------------------

_ADDR_PORT_RE = re.compile(r":(?P<port>\d+)$")
_SS_PID_RE = re.compile(r"pid=(\d+)")


def _addr_port(address: str) -> Optional[int]:
    """Порт из ``127.0.0.1:5030`` / ``[::]:5030`` / ``*:5030``. ``None``, если порта нет."""
    m = _ADDR_PORT_RE.search(address.strip())
    if not m:
        return None
    try:
        return int(m.group("port"))
    except ValueError:
        return None


def _is_wildcard_remote(address: str) -> bool:
    """
    True для «пустого» удалённого адреса слушающего сокета (``0.0.0.0:0``,
    ``[::]:0``, ``*:*``).

    Именно по нему отличаем строку LISTENING от ESTABLISHED, а НЕ по слову
    состояния: на русской Windows ``netstat`` печатает «ПРОСЛУШИВАНИЕ», и
    сравнение с ``"LISTENING"`` там молча не сработало бы.
    """
    value = address.strip()
    return value in ("*:*", "*") or value.endswith(":0")


def parse_netstat_pids(output: str, port: int) -> list[int]:
    """
    Разбирает вывод ``netstat -ano -p tcp`` и возвращает pid'ы процессов,
    слушающих ``port`` (в порядке появления, без повторов).

    Формат строки: ``TCP  0.0.0.0:5030  0.0.0.0:0  LISTENING  12345``.
    Слово состояния локализуется — см. ``_is_wildcard_remote``.
    """
    pids: list[int] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        if _addr_port(parts[1]) != port:
            continue
        if not _is_wildcard_remote(parts[2]):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def parse_ss_pids(output: str, port: int) -> list[int]:
    """
    Разбирает вывод ``ss -ltnp`` и возвращает pid'ы слушателей ``port``.

    Формат строки:
    ``LISTEN 0 511 0.0.0.0:5030 0.0.0.0:* users:(("dotnet",pid=1234,fd=200))``.
    Заголовок (``State  Recv-Q ...``) и строки без ``pid=`` пропускаются.
    """
    pids: list[int] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("State"):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            continue
        # ss -ltn печатает только LISTEN, но подстраховываемся от чужих режимов.
        if parts[0].upper() not in ("LISTEN", "LISTENING", "UNCONN"):
            continue
        if _addr_port(parts[3]) != port:
            continue
        for raw in _SS_PID_RE.findall(stripped):
            try:
                pid = int(raw)
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
    return pids


def parse_lsof_pids(output: str) -> list[int]:
    """Разбирает вывод ``lsof -ti :PORT`` — по одному pid в строке."""
    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


# Состояние TCP_LISTEN в /proc/net/tcp — шестнадцатеричное "0A".
_PROC_TCP_LISTEN = "0A"


def parse_proc_net_tcp_inodes(text: str, port: int) -> list[int]:
    """
    Разбирает ``/proc/net/tcp`` (или ``tcp6``) и возвращает inode'ы слушающих
    сокетов на ``port``.

    Колонки: ``sl local_address rem_address st tx_queue:rx_queue tr:when
    retrnsmt uid timeout inode``. Локальный порт — шестнадцатеричный, после
    двоеточия в ``local_address``.
    """
    inodes: list[int] = []
    for line in text.splitlines()[1:]:  # первая строка — заголовок
        parts = line.split()
        if len(parts) < 10:
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            local_port = int(local.rsplit(":", 1)[1], 16)
        except ValueError:
            continue
        if local_port != port:
            continue
        if parts[3].upper() != _PROC_TCP_LISTEN:
            continue
        try:
            inode = int(parts[9])
        except ValueError:
            continue
        if inode > 0 and inode not in inodes:
            inodes.append(inode)
    return inodes


def _pids_by_socket_inodes(inodes: list[int], *, proc_root: Optional[Path] = None) -> list[int]:
    """
    Находит pid'ы процессов, у которых открыт сокет с одним из ``inodes``
    (скан ``/proc/*/fd``). Работает только для процессов того же пользователя
    (или из-под root) — чужие ``/proc/PID/fd`` не читаются, это ожидаемо и не
    ошибка.
    """
    root = Path(proc_root) if proc_root else Path("/proc")
    wanted = {f"socket:[{inode}]" for inode in inodes}
    pids: list[int] = []
    if not wanted or not root.is_dir():
        return pids
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue  # чужой процесс/нет прав — штатно пропускаем
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in wanted:
                pid = int(entry.name)
                if pid not in pids:
                    pids.append(pid)
                break
    return pids


# --------------------------------------------------------------------------
# Поиск pid по порту
# --------------------------------------------------------------------------


def find_listener_pids(port: int) -> list[int]:
    """
    Возвращает pid'ы процессов, слушающих TCP-порт ``port``.

    Windows — ``netstat -ano -p tcp``; Linux — ``ss -ltnp`` → ``lsof -ti`` →
    разбор ``/proc``. Пустой список означает «определить не удалось» (утилит
    нет, прав нет, никто не слушает) — вызывающая сторона обязана трактовать
    это как честный отказ, а не как «свободно».
    """
    if port <= 0:
        return []
    if sys.platform == "win32":
        output = _capture(["netstat", "-ano", "-p", "tcp"])
        return parse_netstat_pids(output, port) if output else []

    output = _capture(["ss", "-ltnp"])
    if output:
        pids = parse_ss_pids(output, port)
        if pids:
            return pids

    output = _capture(["lsof", "-ti", f":{port}"])
    if output:
        pids = parse_lsof_pids(output)
        if pids:
            return pids

    return _find_listener_pids_via_proc(port)


def _find_listener_pids_via_proc(port: int, *, proc_root: Optional[Path] = None) -> list[int]:
    """Последний фолбэк Linux: ``/proc/net/tcp[6]`` + скан ``/proc/*/fd``. Чистый stdlib."""
    root = Path(proc_root) if proc_root else Path("/proc")
    inodes: list[int] = []
    for name in ("net/tcp", "net/tcp6"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for inode in parse_proc_net_tcp_inodes(text, port):
            if inode not in inodes:
                inodes.append(inode)
    if not inodes:
        return []
    return _pids_by_socket_inodes(inodes, proc_root=root)


# --------------------------------------------------------------------------
# Сведения о процессе
# --------------------------------------------------------------------------


def parse_tasklist_csv(output: str, pid: int) -> str:
    """
    Имя образа из ``tasklist /FI "PID eq X" /FO CSV`` (с заголовком или без).

    Пустая строка — процесс не найден/вывод не разобран (tasklist при
    отсутствии процесса печатает информационное сообщение, а не CSV).
    """
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2:
            continue
        image, raw_pid = row[0].strip(), row[1].strip()
        if not raw_pid.isdigit():
            continue  # строка заголовка
        if int(raw_pid) == pid:
            return image
    return ""


def parse_key_value_output(output: str) -> dict:
    """
    Разбирает вывод вида ``Ключ=значение`` построчно (формат
    ``wmic ... /format:list`` и наш PowerShell-фолбэк). Пустые значения
    отбрасываются — «не определили» и «пусто» для нас одно и то же.
    """
    result: dict = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


_PS_PROCESS_INFO_TEMPLATE = (
    "$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
    "if ($p) {{ 'ExecutablePath=' + $p.ExecutablePath; 'CommandLine=' + $p.CommandLine }}"
)


def _windows_process_cwd(pid: int) -> str:
    """
    Рабочий каталог процесса на Windows — читается из его PEB через ctypes.

    ЗАЧЕМ ТАК. Для стенда, поднятого руками, это ЕДИНСТВЕННАЯ надёжная улика.
    Живая проверка на demo9 показала, почему остальных не хватает: исполняемый
    файл — общесистемный ``C:\\Program Files\\dotnet\\dotnet.exe`` (он одинаков
    у всех стендов), а командная строка при типичном запуске выглядит как
    ``dotnet BPMSoft.WebHost.dll`` — путь относительный, каталога стенда в ней
    нет вовсе. Ни WMI, ни ``Get-CimInstance``, ни ``tasklist`` рабочий каталог
    не отдают: в Win32 API его попросту нет — он лежит в
    ``PEB → RTL_USER_PROCESS_PARAMETERS → CurrentDirectory``.

    Реализация — только stdlib (``ctypes``, как уже сделано для ``is_alive`` в
    ``standkit.platform``): ``NtQueryInformationProcess`` даёт адрес PEB,
    дальше два чтения ``ReadProcessMemory`` по фиксированным смещениям.

    Ограничения, при которых честно возвращается пустая строка:
      - процесс другой разрядности (32-битный под 64-битным Python) — смещения
        в его PEB другие, гадать не будем;
      - нет прав ``PROCESS_VM_READ`` (чужой пользователь, сервис, elevated
        процесс). Убить такой процесс мы всё равно не смогли бы;
      - процесс успел завершиться между поиском и чтением.

    Пустая строка означает «улику получить не удалось», а не «не совпало» —
    вызывающий (``validate_candidate``) в этом случае откажет в усыновлении.
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - ctypes есть в любой CPython на Windows
        return ""

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    # Смещения для x64. Для x86-процесса под x86-Python они другие; мешать
    # разрядности нельзя, поэтому ниже стоит явная проверка.
    PEB_OFFSET_PROCESS_PARAMETERS = 0x20
    RTL_OFFSET_CURRENT_DIRECTORY = 0x38

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return ""

    try:
        # Смешивать разрядности нельзя: у WOW64-процесса свой PEB с другой
        # раскладкой. 64-битный Python + 32-битный процесс — отказ.
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            is_wow64 = wintypes.BOOL()
            if kernel32.IsWow64Process(handle, ctypes.byref(is_wow64)) and is_wow64.value:
                return ""

        class _UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p),
            ]

        class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p),
            ]

        pbi = _PROCESS_BASIC_INFORMATION()
        returned = ctypes.c_ulong()
        # ProcessBasicInformation == 0
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(returned)
        )
        if status != 0 or not pbi.PebBaseAddress:
            return ""

        def _read(address: int, size: int) -> bytes:
            buf = (ctypes.c_char * size)()
            read = ctypes.c_size_t()
            ok = kernel32.ReadProcessMemory(
                handle,
                ctypes.c_void_p(address),
                buf,
                ctypes.c_size_t(size),
                ctypes.byref(read),
            )
            if not ok or read.value != size:
                raise OSError("ReadProcessMemory")
            return bytes(buf)

        params_ptr = int.from_bytes(
            _read(int(pbi.PebBaseAddress) + PEB_OFFSET_PROCESS_PARAMETERS, 8), "little"
        )
        if not params_ptr:
            return ""

        raw = _read(params_ptr + RTL_OFFSET_CURRENT_DIRECTORY, ctypes.sizeof(_UNICODE_STRING))
        us = _UNICODE_STRING.from_buffer_copy(raw)
        if not us.Length or not us.Buffer:
            return ""
        # Length — в БАЙТАХ, строка в UTF-16LE.
        data = _read(int(us.Buffer), us.Length)
        return data.decode("utf-16-le", errors="replace").rstrip("\\").strip()
    except (OSError, ValueError, AttributeError):
        # Любая неудача — «улику получить не удалось». Падать нельзя: это
        # диагностика в интерактивном сценарии, а не операция.
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_info(pid: int) -> dict:
    """
    Путь к исполняемому файлу и командная строка процесса на Windows.

    Сначала PowerShell + CIM (``wmic`` объявлен устаревшим и на свежих сборках
    Windows уже отсутствует), затем ``wmic`` как фолбэк для старых систем.
    Рабочий каталог процесса штатными средствами Windows не читается — поле
    ``cwd`` остаётся пустым, и совпадение ищется по пути/командной строке.
    """
    info: dict = {}
    output = _capture(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _PS_PROCESS_INFO_TEMPLATE.format(pid=pid),
        ]
    )
    if output:
        info = parse_key_value_output(output)
    if not info:
        output = _capture(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "ExecutablePath,CommandLine",
                "/format:list",
            ]
        )
        if output:
            info = parse_key_value_output(output)

    image = ""
    tasklist_out = _capture(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if tasklist_out:
        image = parse_tasklist_csv(tasklist_out, pid)

    exe_path = info.get("ExecutablePath", "")
    if not image and exe_path:
        image = Path(exe_path).name
    return {
        "image": image,
        "exe_path": exe_path,
        "cmdline": info.get("CommandLine", ""),
        # Рабочий каталог Win32 API не отдаёт — читаем из PEB, см.
        # _windows_process_cwd. Для стенда, запущенного через `cd` в его
        # каталог, это единственная улика, связывающая процесс со стендом.
        "cwd": _windows_process_cwd(pid),
    }


def _posix_process_info(pid: int, *, proc_root: Optional[Path] = None) -> dict:
    """Путь/рабочий каталог/командная строка процесса из ``/proc/PID`` (Linux)."""
    root = (Path(proc_root) if proc_root else Path("/proc")) / str(pid)

    def _readlink(name: str) -> str:
        try:
            return os.readlink(root / name)
        except OSError:
            return ""

    exe_path = _readlink("exe")
    cwd = _readlink("cwd")
    try:
        raw = (root / "cmdline").read_bytes()
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        cmdline = ""
    image = Path(exe_path).name if exe_path else ""
    return {"image": image, "exe_path": exe_path, "cmdline": cmdline, "cwd": cwd}


def describe_process(pid: int, port: int) -> AdoptCandidate:
    """Собирает ``AdoptCandidate`` по pid: имя образа, путь, рабочий каталог, командную строку."""
    info = _windows_process_info(pid) if sys.platform == "win32" else _posix_process_info(pid)
    return AdoptCandidate(
        pid=pid,
        port=port,
        image=info.get("image", ""),
        exe_path=info.get("exe_path", ""),
        cwd=info.get("cwd", ""),
        cmdline=info.get("cmdline", ""),
    )


# --------------------------------------------------------------------------
# Поиск и валидация кандидата
# --------------------------------------------------------------------------


def find_candidate(stand: Stand) -> Optional[AdoptCandidate]:
    """
    Ищет процесс-владельца порта ``stand.stand_port`` и возвращает его как
    кандидата на усыновление (БЕЗ валидации — см. ``validate_candidate``).

    ``None`` — владельца определить не удалось.
    """
    if not stand.stand_port:
        return None
    for pid in find_listener_pids(int(stand.stand_port)):
        return describe_process(pid, int(stand.stand_port))
    return None


def _normalize(value: str) -> str:
    """Нормализует путь для сравнения (регистр/разделители — по правилам текущей ОС)."""
    return os.path.normcase(os.path.normpath(str(value)))


def _is_within(base: str, path: str) -> bool:
    """True, если ``path`` — это ``base`` либо лежит внутри него."""
    if not base or not path:
        return False
    base_n = _normalize(base)
    path_n = _normalize(path)
    return path_n == base_n or path_n.startswith(base_n + os.sep)


def _image_allowed(image: str) -> bool:
    """Проверка имени образа по allowlist (регистр и расширение не важны)."""
    if not image:
        return False
    stem = Path(image).stem.lower()
    return stem in ALLOWED_IMAGE_NAMES


def path_evidence(stand: Stand, candidate: AdoptCandidate) -> str:
    """
    Возвращает улику, связывающую процесс с каталогом стенда, либо пустую
    строку, если такой улики нет.

    Проверяются, в порядке убывания надёжности: рабочий каталог процесса
    (POSIX), путь исполняемого файла, командная строка (на Windows именно она
    обычно и содержит путь к ``BPMSoft.WebHost.dll``, потому что сам
    исполняемый файл — общесистемный ``dotnet.exe``).
    """
    stand_dir = stand.stand_dir or ""
    if not stand_dir:
        return ""
    for value in (candidate.cwd, candidate.exe_path):
        if _is_within(stand_dir, value):
            return value
    if candidate.cmdline and _normalize(stand_dir) in _normalize(candidate.cmdline):
        return candidate.cmdline
    return ""


def validate_candidate(stand: Stand, candidate: AdoptCandidate) -> tuple[bool, str]:
    """
    Решает, можно ли считать ``candidate`` процессом ЭТОГО стенда.

    Три обязательных условия (все, а не любое):
      1. порт процесса совпадает со ``stand.stand_port``;
      2. имя образа входит в ``ALLOWED_IMAGE_NAMES``;
      3. рабочий каталог/исполняемый файл/командная строка указывают внутрь
         ``stand.stand_dir``.

    Возвращает ``(True, "")`` либо ``(False, <текст отказа для пользователя>)``.
    Валидация не опциональна: без неё диспетчер убил бы чужой процесс,
    случайно занявший тот же порт.
    """
    if not stand.stand_port or candidate.port != int(stand.stand_port):
        return False, (
            f"процесс PID {candidate.pid} слушает порт {candidate.port}, "
            f"а у стенда '{stand.name}' порт {stand.stand_port} — это не он"
        )
    if not _image_allowed(candidate.image):
        image = candidate.image or "имя образа не определено"
        return False, (
            f"порт {candidate.port} занят процессом PID {candidate.pid} ({image}) — "
            "он не похож на процесс стенда BPMSoft (ожидались "
            f"{', '.join(ALLOWED_IMAGE_NAMES)}), брать его под управление я не буду"
        )
    evidence = path_evidence(stand, candidate)
    if not evidence:
        where = candidate.exe_path or candidate.cmdline or "путь определить не удалось"
        return False, (
            f"порт {candidate.port} занят процессом PID {candidate.pid} ({where}), "
            f"он не похож на этот стенд — ожидался процесс из каталога {stand.stand_dir}"
        )
    candidate.matched_by = evidence
    return True, ""
