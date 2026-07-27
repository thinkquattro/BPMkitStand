"""
Тесты standkit.adopt: поиск процесса-владельца порта стенда и валидация
кандидата на усыновление.

Ни один тест не запускает и не убивает реальные процессы и не открывает
сокеты: разбор вывода проверяется на ЗАФИКСИРОВАННЫХ строках реальных утилит
(``netstat -ano``, ``ss -ltnp``, ``lsof -ti``, ``/proc/net/tcp``, ``tasklist``),
а всё, что дёргает внешние команды, мокается поверх ``standkit.adopt._capture``.
"""

from __future__ import annotations

import os
import sys

import pytest

from standkit import adopt
from standkit.adopt import AdoptCandidate
from standkit.models import Stand


def _make_stand(tmp_path, **overrides) -> Stand:
    stand_dir = tmp_path / "stand"
    stand_dir.mkdir(exist_ok=True)
    kwargs = dict(name="demo", stand_dir=str(stand_dir), stand_host="127.0.0.1", stand_port=5030)
    kwargs.update(overrides)
    return Stand(**kwargs)


# --------------------------------------------------------------------------
# netstat (Windows) — включая локализованный вывод RU-Windows
# --------------------------------------------------------------------------

# Реальный вывод `netstat -ano -p tcp` (EN-локаль).
_NETSTAT_EN = """
Активные подключения

  Имя    Локальный адрес        Внешний адрес          Состояние       PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1084
  TCP    0.0.0.0:5030           0.0.0.0:0              LISTENING       12345
  TCP    127.0.0.1:5030         127.0.0.1:51234        ESTABLISHED     6789
  TCP    [::]:5030              [::]:0                 LISTENING       12345
  TCP    0.0.0.0:5005           0.0.0.0:0              LISTENING       999
"""

# Тот же вывод на русской Windows: слово состояния ЛОКАЛИЗОВАНО. Раньше разбор
# по литералу "LISTENING" здесь молча не находил бы ничего.
_NETSTAT_RU = """
  Имя    Локальный адрес        Внешний адрес          Состояние       PID
  TCP    0.0.0.0:5030           0.0.0.0:0              ПРОСЛУШИВАНИЕ   12345
  TCP    127.0.0.1:5030         127.0.0.1:51234        УСТАНОВЛЕНО     6789
"""


def test_parse_netstat_pids_finds_listener_and_ignores_established():
    assert adopt.parse_netstat_pids(_NETSTAT_EN, 5030) == [12345]


def test_parse_netstat_pids_handles_localized_state_word():
    # Состояние на RU-Windows — «ПРОСЛУШИВАНИЕ»; отличаем слушателя по пустому
    # удалённому адресу (0.0.0.0:0), а не по слову.
    assert adopt.parse_netstat_pids(_NETSTAT_RU, 5030) == [12345]


def test_parse_netstat_pids_ignores_other_ports():
    assert adopt.parse_netstat_pids(_NETSTAT_EN, 5005) == [999]
    assert adopt.parse_netstat_pids(_NETSTAT_EN, 9999) == []


def test_parse_netstat_pids_survives_garbage_lines():
    garbage = "мусор\n\nTCP\nTCP 1 2 3 не-число\n" + _NETSTAT_EN
    assert adopt.parse_netstat_pids(garbage, 5030) == [12345]


# --------------------------------------------------------------------------
# ss / lsof / /proc (Linux)
# --------------------------------------------------------------------------

_SS_OUTPUT = """State      Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process
LISTEN     0      511           0.0.0.0:5030         0.0.0.0:*     users:(("dotnet",pid=4242,fd=200))
LISTEN     0      128           0.0.0.0:22           0.0.0.0:*     users:(("sshd",pid=900,fd=3))
LISTEN     0      511              [::]:5005            [::]:*     users:(("dotnet",pid=777,fd=201))
"""


def test_parse_ss_pids_finds_listener_by_port():
    assert adopt.parse_ss_pids(_SS_OUTPUT, 5030) == [4242]
    assert adopt.parse_ss_pids(_SS_OUTPUT, 5005) == [777]
    assert adopt.parse_ss_pids(_SS_OUTPUT, 1) == []


def test_parse_ss_pids_skips_header_and_lines_without_pid():
    output = "State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 511 0.0.0.0:5030 0.0.0.0:*\n"
    assert adopt.parse_ss_pids(output, 5030) == []


def test_parse_lsof_pids():
    assert adopt.parse_lsof_pids("4242\n4243\n\n") == [4242, 4243]
    assert adopt.parse_lsof_pids("") == []


# /proc/net/tcp: порт 5030 = 0x13A6, состояние LISTEN = 0A, inode — 10-я колонка.
_PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:13A6 00000000:0000 0A 00000000:00000000 00:00000000  00000000  1000        0 987654 1 0000 100 0
   1: 00000000:13A6 0100007F:C822 01 00000000:00000000 00:00000000  00000000  1000        0 111111 1 0000 100 0
   2: 00000000:1389 00000000:0000 0A 00000000:00000000 00:00000000  00000000  1000        0 222222 1 0000 100 0
"""


def test_parse_proc_net_tcp_inodes_only_listening_sockets():
    assert adopt.parse_proc_net_tcp_inodes(_PROC_NET_TCP, 5030) == [987654]
    # 0x1389 == 5001 — другой слушающий сокет, не наш.
    assert adopt.parse_proc_net_tcp_inodes(_PROC_NET_TCP, 5001) == [222222]
    assert adopt.parse_proc_net_tcp_inodes(_PROC_NET_TCP, 1234) == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="симлинки /proc/*/fd воспроизводимы только на POSIX"
)
def test_pids_by_socket_inodes_scans_fake_proc(tmp_path):
    # Полностью синтетический /proc: каталог процесса с fd-симлинком на сокет.
    proc = tmp_path / "proc"
    fd_dir = proc / "4242" / "fd"
    fd_dir.mkdir(parents=True)
    target = tmp_path / "socket-target"
    target.write_text("x", encoding="utf-8")
    os.symlink(str(target), str(fd_dir / "3"))
    # Симлинк должен указывать на "socket:[<inode>]" — эмулируем «висячим» линком.
    (fd_dir / "3").unlink()
    os.symlink("socket:[987654]", str(fd_dir / "3"))

    assert adopt._pids_by_socket_inodes([987654], proc_root=proc) == [4242]
    assert adopt._pids_by_socket_inodes([1], proc_root=proc) == []


# --------------------------------------------------------------------------
# tasklist / key=value (Windows)
# --------------------------------------------------------------------------


def test_parse_tasklist_csv_returns_image_name():
    output = '"dotnet.exe","12345","Console","1","1 234 560 КБ"\n'
    assert adopt.parse_tasklist_csv(output, 12345) == "dotnet.exe"
    assert adopt.parse_tasklist_csv(output, 999) == ""


def test_parse_tasklist_csv_skips_header_row():
    output = (
        '"Имя образа","PID","Имя сессии","№ сеанса","Память"\n'
        '"w3wp.exe","6832","Services","0","512 000 КБ"\n'
    )
    assert adopt.parse_tasklist_csv(output, 6832) == "w3wp.exe"


def test_parse_key_value_output_drops_empty_values():
    output = "CommandLine=dotnet BPMSoft.WebHost.dll\nExecutablePath=\n\nмусор\n"
    assert adopt.parse_key_value_output(output) == {"CommandLine": "dotnet BPMSoft.WebHost.dll"}


# --------------------------------------------------------------------------
# find_listener_pids — выбор утилиты по платформе и фолбэки
# --------------------------------------------------------------------------


def test_find_listener_pids_uses_netstat_on_windows(monkeypatch):
    calls = []

    def _fake_capture(cmd, **kw):
        calls.append(cmd)
        return _NETSTAT_EN

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(adopt, "_capture", _fake_capture)
    assert adopt.find_listener_pids(5030) == [12345]
    assert calls == [["netstat", "-ano", "-p", "tcp"]]


def test_find_listener_pids_falls_back_from_ss_to_lsof(monkeypatch):
    calls = []

    def _fake_capture(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "ss":
            return None  # iproute2 нет
        if cmd[0] == "lsof":
            return "4242\n"
        return None

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(adopt, "_capture", _fake_capture)
    assert adopt.find_listener_pids(5030) == [4242]
    assert calls == ["ss", "lsof"]


def test_find_listener_pids_falls_back_to_proc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(adopt, "_capture", lambda cmd, **kw: None)
    monkeypatch.setattr(adopt, "_find_listener_pids_via_proc", lambda port: [31337])
    assert adopt.find_listener_pids(5030) == [31337]


def test_find_listener_pids_returns_empty_for_invalid_port():
    assert adopt.find_listener_pids(0) == []


# --------------------------------------------------------------------------
# Валидация кандидата — сердце безопасности усыновления
# --------------------------------------------------------------------------


def test_validate_candidate_ok_when_port_and_cwd_match(tmp_path):
    stand = _make_stand(tmp_path)
    candidate = AdoptCandidate(pid=12345, port=5030, image="dotnet.exe", cwd=stand.stand_dir)
    ok, reason = adopt.validate_candidate(stand, candidate)
    assert ok is True
    assert reason == ""
    assert candidate.matched_by == stand.stand_dir


def test_validate_candidate_ok_when_cmdline_points_into_stand_dir(tmp_path):
    # Типичный Windows-случай: exe — общесистемный dotnet.exe, а путь к стенду
    # виден только в командной строке.
    stand = _make_stand(tmp_path)
    dll = os.path.join(stand.stand_dir, "BPMSoft.WebHost.dll")
    candidate = AdoptCandidate(
        pid=12345,
        port=5030,
        image="dotnet.exe",
        exe_path=r"C:\Program Files\dotnet\dotnet.exe",
        cmdline=f'"C:\\Program Files\\dotnet\\dotnet.exe" "{dll}"',
    )
    ok, _ = adopt.validate_candidate(stand, candidate)
    assert ok is True


def test_validate_candidate_rejects_when_only_port_matches(tmp_path):
    # Совпал ТОЛЬКО порт: процесс живёт в чужом каталоге → усыновления нет.
    stand = _make_stand(tmp_path)
    other = tmp_path / "other-stand"
    other.mkdir()
    candidate = AdoptCandidate(pid=12345, port=5030, image="dotnet.exe", cwd=str(other))
    ok, reason = adopt.validate_candidate(stand, candidate)
    assert ok is False
    assert "не похож на этот стенд" in reason
    assert "12345" in reason


def test_validate_candidate_rejects_image_outside_allowlist(tmp_path):
    # Каталог совпал, но образ не из allowlist — всё равно отказ.
    stand = _make_stand(tmp_path)
    candidate = AdoptCandidate(pid=12345, port=5030, image="nginx.exe", cwd=stand.stand_dir)
    ok, reason = adopt.validate_candidate(stand, candidate)
    assert ok is False
    assert "не похож на процесс стенда" in reason


def test_validate_candidate_rejects_unknown_image(tmp_path):
    stand = _make_stand(tmp_path)
    candidate = AdoptCandidate(pid=12345, port=5030, image="", cwd=stand.stand_dir)
    ok, reason = adopt.validate_candidate(stand, candidate)
    assert ok is False
    assert "имя образа не определено" in reason


def test_validate_candidate_rejects_port_mismatch(tmp_path):
    stand = _make_stand(tmp_path, stand_port=5030)
    candidate = AdoptCandidate(pid=12345, port=5005, image="dotnet.exe", cwd=stand.stand_dir)
    ok, reason = adopt.validate_candidate(stand, candidate)
    assert ok is False
    assert "5005" in reason


def test_validate_candidate_accepts_bpmsoft_webhost_and_w3wp(tmp_path):
    stand = _make_stand(tmp_path)
    for image in ("BPMSoft.WebHost.exe", "w3wp.exe", "dotnet"):
        candidate = AdoptCandidate(pid=1, port=5030, image=image, cwd=stand.stand_dir)
        ok, reason = adopt.validate_candidate(stand, candidate)
        assert ok is True, f"{image}: {reason}"


def test_path_evidence_matches_subdirectory(tmp_path):
    stand = _make_stand(tmp_path)
    nested = os.path.join(stand.stand_dir, "bin", "BPMSoft.WebHost.exe")
    candidate = AdoptCandidate(pid=1, port=5030, image="BPMSoft.WebHost.exe", exe_path=nested)
    assert adopt.path_evidence(stand, candidate) == nested


def test_path_evidence_empty_when_stand_dir_not_set(tmp_path):
    stand = _make_stand(tmp_path, stand_dir="")
    candidate = AdoptCandidate(pid=1, port=5030, image="dotnet", cwd=str(tmp_path))
    assert adopt.path_evidence(stand, candidate) == ""


# --------------------------------------------------------------------------
# find_candidate / describe_process
# --------------------------------------------------------------------------


def test_find_candidate_returns_none_when_nobody_listens(tmp_path, monkeypatch):
    stand = _make_stand(tmp_path)
    monkeypatch.setattr(adopt, "find_listener_pids", lambda port: [])
    assert adopt.find_candidate(stand) is None


def test_find_candidate_describes_first_listener(tmp_path, monkeypatch):
    stand = _make_stand(tmp_path)
    monkeypatch.setattr(adopt, "find_listener_pids", lambda port: [12345, 999])
    monkeypatch.setattr(
        adopt,
        "describe_process",
        lambda pid, port: AdoptCandidate(pid=pid, port=port, image="dotnet"),
    )
    candidate = adopt.find_candidate(stand)
    assert candidate.pid == 12345
    assert candidate.port == 5030


def test_describe_process_windows_collects_image_path_and_cmdline(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def _fake_capture(cmd, **kw):
        if cmd[0] == "powershell":
            return (
                "ExecutablePath=C:\\Program Files\\dotnet\\dotnet.exe\n"
                "CommandLine=dotnet D:\\stands\\demo9\\BPMSoft.WebHost.dll\n"
            )
        if cmd[0] == "tasklist":
            return '"dotnet.exe","12345","Console","1","1 000 КБ"\n'
        raise AssertionError(f"неожиданная команда: {cmd}")

    monkeypatch.setattr(adopt, "_capture", _fake_capture)
    candidate = adopt.describe_process(12345, 5030)
    assert candidate.image == "dotnet.exe"
    assert candidate.exe_path.endswith("dotnet.exe")
    assert "BPMSoft.WebHost.dll" in candidate.cmdline
    assert candidate.cwd == ""  # рабочий каталог на Windows штатно не читается


def test_describe_process_windows_falls_back_to_wmic(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []

    def _fake_capture(cmd, **kw):
        seen.append(cmd[0])
        if cmd[0] == "powershell":
            return None  # PowerShell недоступен/упал
        if cmd[0] == "wmic":
            return "CommandLine=dotnet app.dll\nExecutablePath=C:\\dotnet\\dotnet.exe\n"
        return None

    monkeypatch.setattr(adopt, "_capture", _fake_capture)
    candidate = adopt.describe_process(12345, 5030)
    assert "wmic" in seen
    # Имя образа выведено из ExecutablePath, раз tasklist ничего не дал.
    assert candidate.image == "dotnet.exe"


# --- рабочий каталог процесса на Windows (чтение PEB) ---------------------
#
# Это единственная надёжная улика для стенда, поднятого руками: живая проверка
# на demo9 показала, что exe_path указывает на общесистемный
# C:\Program Files\dotnet\dotnet.exe, а командная строка выглядит как
# "dotnet BPMSoft.WebHost.dll" — каталога стенда нет ни там, ни там.


@pytest.mark.skipif(sys.platform != "win32", reason="чтение PEB — только Windows")
def test_windows_process_cwd_reads_own_working_directory():
    """
    Самопроверка на собственном процессе: функция обязана вернуть тот же
    каталог, который отдаёт os.getcwd(). Внешних процессов не запускаем.
    """
    got = adopt._windows_process_cwd(os.getpid())
    assert got, "рабочий каталог собственного процесса должен читаться"
    assert os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.getcwd())


@pytest.mark.skipif(sys.platform != "win32", reason="чтение PEB — только Windows")
def test_windows_process_cwd_returns_empty_for_missing_process():
    """
    Несуществующий pid — пустая строка, а не исключение: это проба,
    вызывающий трактует пустоту как «улики нет» и откажет в усыновлении.
    """
    assert adopt._windows_process_cwd(0x7FFFFFFE) == ""


def test_windows_process_cwd_is_noop_on_posix(monkeypatch):
    monkeypatch.setattr(adopt.sys, "platform", "linux")
    assert adopt._windows_process_cwd(1) == ""


def test_capture_returns_none_on_failed_command(monkeypatch):
    from standkit import hosting

    monkeypatch.setattr(
        hosting, "_run", lambda cmd, timeout=None: _fake_completed(cmd, returncode=1)
    )
    assert adopt._capture(["netstat", "-ano"]) is None


def _fake_completed(cmd, returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)
