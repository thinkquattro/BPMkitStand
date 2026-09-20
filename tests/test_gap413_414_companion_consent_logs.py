# -*- coding: utf-8 -*-
"""Серия S5b аудита 19-20.09.2026 — GAP-413 и GAP-414 на стороне BPMkitStand.

Четыре независимых дефекта, каждый из которых наружу выглядит как «ничего не
произошло», и потому не мог быть замечен без регресс-теста:

1. **двойной проход очереди находок за одно пробуждение** (GAP-413,
   `runner.py`). Обратный проход — попутчик, и его повесили СРАЗУ на два
   несущих цикла: `_run_patterns` и `_run_releases`. Когда сроки обоих
   наступают в одну секунду (а после старта диспетчера это именно так —
   первый тик общий), поставку просят разгрузить очередь ДВАЖДЫ: два запуска
   внешнего процесса, две серии сетевых попыток по одним и тем же письмам.
   Требование: один проход за пробуждение, второй вызов возвращает исход
   первого с пометкой `reused`.

2. **инициатор стоп-запроса не назывался** (GAP-413, `standkit_hub/server.py`).
   Писатель кладёт в файл-запрос поле `requester_pid` (`instance.write_stop_request`),
   а читатель искал `by`/`source` — полей, которых в файле НЕТ НИКОГДА. В логе
   оставалось «инициатор=не указан» при любом перехвате: разобрать, кто погасил
   диспетчер (установщик, `hub-stop`, чужой процесс), было нечем.

3. **ротация лога под двумя процессами падала молча** (GAP-414,
   `hub_logging.py`). При перезапуске с повышением прав файл `hub.log` держат
   ДВА процесса; `RotatingFileHandler.doRollover()` не может переименовать
   занятый файл, а `logging` гасит отказ обработчика в `handleError` — под
   `pythonw.exe` (stderr нет) это тишина, и файл растёт без ограничения, то
   есть отказывает ровно та гарантия, ради которой ротацию заводили.

4. **`MAX_COOKBOOK_BYTES` не применялся** (GAP-414, `cookbook.py`/`backend.py`).
   Константа-потолок документа была объявлена и не использована ни разу:
   `client.download` капа не знал, а `cookbook.sync` звал его ДО своего
   `try/except`, поэтому любой отказ скачивания оставлял `.part` на диске.

Сеть, реальный CLI и реальные каталоги профиля здесь не участвуют.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import logging.handlers
import os
import time
from pathlib import Path

import pytest

from standkit_companion import backend, candidates, cookbook, patterns, releases, runner
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from standkit_hub import hub_logging, instance
from standkit_hub import server as hub_server
from tests.test_companion_cookbook import (
    FakeCtx,
    PUBKEY_B64,
    _html,
    _meta,
    _sidecar,
)
from tests.test_companion_runner import (
    Recorder,
    make_runner,
    patch_cycles,
    settings_all_on,
)


# ======================================================================================
# 1. GAP-413 — один проход очереди находок за пробуждение
# ======================================================================================
def test_candidates_flush_runs_once_per_wake(tmp_path, monkeypatch):
    """Оба несущих цикла созрели в одну секунду — разгрузка очереди ОДНА.

    До фикса `run_due` давал два вызова `candidates.flush`: попутчик висел и на
    `_run_patterns`, и на `_run_releases`. Каждый вызов — отдельный запуск CLI
    поставки и отдельная серия сетевых попыток по ТЕМ ЖЕ письмам.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    runner_obj = make_runner(tmp_path, settings_all_on())

    result = runner_obj.run_due()

    assert set(result["order"]) >= {"patterns", "releases"}, (
        "предпосылка теста: оба несущих цикла созрели в этом пробуждении")
    assert stubs["candidates_flush"].calls == 1, (
        "очередь находок разгружается ОДИН раз за пробуждение, а не по разу на цикл")


def test_second_carrier_reports_reused_outcome(tmp_path, monkeypatch):
    """Второй несущий цикл получает ИСХОД ПЕРВОГО прохода с пометкой `reused`.

    Вернуть `None` или пустой словарь было бы хуже отсутствия фикса: в UI и в
    состоянии появился бы «пустой» результат цикла релизов, неотличимый от
    «очередь не разгружалась вовсе».
    """
    log: list = []
    patch_cycles(monkeypatch, log, candidates_stub=Recorder(
        "candidates", [], result={"flushed": True, "reason": "flushed", "sent": 3}))
    runner_obj = make_runner(tmp_path, settings_all_on())

    result = runner_obj.run_due()

    releases_outcome = result["results"]["releases"]["result"]["candidates"]
    patterns_outcome = result["results"]["patterns"]["result"]["candidates"]
    assert patterns_outcome["sent"] == 3
    assert releases_outcome["sent"] == 3, "второй цикл обязан видеть исход первого прохода"
    assert releases_outcome.get("reused") is True, (
        "повторное использование исхода обязано быть НАЗВАНО, а не замаскировано")
    assert patterns_outcome.get("reused") is not True


def test_next_wake_flushes_again(tmp_path, monkeypatch):
    """Ограничение — на пробуждение, а не на жизнь процесса.

    Иначе «один проход за пробуждение» превратилось бы в «один проход за всё
    время работы диспетчера», и очередь перестала бы уезжать вовсе.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    runner_obj = make_runner(tmp_path, settings_all_on())

    runner_obj.run_due()
    runner_obj.run_cycle("patterns", force=True)

    assert stubs["candidates_flush"].calls == 2, (
        "новое пробуждение — новый проход очереди")


# ======================================================================================
# 2. GAP-413 — `held_no_consent` доезжает от поставки до состояния
# ======================================================================================
class _FakeState:
    """Минимальное состояние: `candidates.flush` пишет в него исход прохода."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.marks: list = []
        self.saves = 0

    def mark(self, cycle, status, detail) -> None:
        self.marks.append((cycle, status, detail))

    def save(self) -> None:
        self.saves += 1


class _Settings:
    mcp_cli = "C:/bpmkit/bpmkit.exe"


def _run_returning(payload_json: str):
    def _run(argv):
        return 0, payload_json, ""
    return _run


def test_flush_reports_letters_held_without_consent(monkeypatch):
    """Задержанные без согласия письма НАЗЫВАЮТСЯ в исходе и в тексте для UI.

    Поставка после GAP-413 отказывает по письму со статусом `no_consent` и
    печатает их число в `held_no_consent`. Молчание здесь означало бы, что
    пользователь, снявший галку, видит «очередь пуста» — и не понимает, почему
    находки не уезжают.
    """
    monkeypatch.setattr(candidates, "find_cli", lambda settings: ["bpmkit"])
    state = _FakeState()
    payload = ('{"ok": true, "sent": 0, "failed": 0, "held_no_consent": 2, '
               '"remaining": 2, "stopped_reason": null}')

    result = candidates.flush(state, _Settings(), run=_run_returning(payload))

    assert result["held_no_consent"] == 2
    assert state.marks, "исход обязан оседать в состоянии"
    _cycle, _status, detail = state.marks[-1]
    assert "соглас" in detail.lower(), (
        "текст для пользователя обязан называть причину задержки, а не только число")


def test_flush_without_held_field_is_backward_compatible(monkeypatch):
    """Старая поставка поля не печатает — канал обязан это пережить.

    Companion и MCP обновляются независимо (разные артефакты, разные каналы):
    отсутствие нового поля — норма, а не повод объявить проход неудачным.
    """
    monkeypatch.setattr(candidates, "find_cli", lambda settings: ["bpmkit"])
    state = _FakeState()
    payload = '{"ok": true, "sent": 1, "failed": 0, "remaining": 0}'

    result = candidates.flush(state, _Settings(), run=_run_returning(payload))

    assert result["held_no_consent"] == 0
    assert result["reason"] == "flushed"


# ======================================================================================
# 3. GAP-413 — инициатор стоп-запроса в логе
# ======================================================================================
class _StopSpy:
    """Двойник сервера: наблюдателю нужен только вызов остановки."""

    def __init__(self) -> None:
        self.stopped = 0

    def _schedule_shutdown(self) -> None:
        self.stopped += 1


def _drain_stop_request(tmp_path, request_fields, caplog):
    """Один оборот наблюдателя стоп-запроса над подготовленным файлом."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = instance.stop_request_path(run_dir)
    payload = {"target_pid": os.getpid(), "at": time.time()}
    payload.update(request_fields)
    import json as _json
    path.write_text(_json.dumps(payload), encoding="utf-8")

    watcher = hub_server._StopRequestWatcher.__new__(hub_server._StopRequestWatcher)
    watcher._server = _StopSpy()
    watcher._path = path
    watcher._poll_interval = 0.01
    import threading as _threading
    watcher._stop_event = _threading.Event()
    watcher._trigger_stop = lambda: None

    with caplog.at_level(logging.INFO, logger=hub_logging.LOGGER_NAME):
        watcher._run()
    return caplog.text


def test_stop_request_log_names_requester_pid(tmp_path, caplog):
    """Лог перехвата обязан назвать pid просившего — поле, которое писатель и кладёт.

    Читатель до фикса спрашивал `by`/`source`, которых `write_stop_request` не
    пишет НИКОГДА: строка «инициатор=не указан» печаталась при любом перехвате.
    """
    text = _drain_stop_request(tmp_path, {"requester_pid": 4242}, caplog)

    assert "4242" in text, "pid инициатора обязан быть в строке лога"
    assert "не указан" not in text


def test_stop_request_log_names_requester_process(tmp_path, caplog, monkeypatch):
    """Рядом с pid — имя процесса: голое число на разборе инцидента бесполезно."""
    monkeypatch.setattr(hub_server, "_process_name", lambda pid: "bpmkit-setup.exe")

    text = _drain_stop_request(tmp_path, {"requester_pid": 4242}, caplog)

    assert "bpmkit-setup.exe" in text


def test_stop_request_without_requester_says_so(tmp_path, caplog):
    """Поля нет (файл старого формата) — честное «не указан», а не выдуманный pid."""
    text = _drain_stop_request(tmp_path, {}, caplog)

    assert "не указан" in text


def test_process_name_never_raises():
    """Резолв имени процесса — диагностика: он не имеет права уронить перехват."""
    assert isinstance(hub_server._process_name(-1), str)
    assert isinstance(hub_server._process_name(os.getpid()), str)


# ======================================================================================
# 4. GAP-414 — ротация под двумя процессами не падает молча
# ======================================================================================
@pytest.fixture()
def _fresh_logging():
    hub_logging.reset_logging()
    hub_logging.reset_rotation_state()
    yield
    hub_logging.reset_logging()
    hub_logging.reset_rotation_state()


def test_rotation_failure_is_not_silent(tmp_path, _fresh_logging, monkeypatch):
    """Отказ ротации оставляет след — иначе лог растёт молча и без предела.

    Воспроизводим ровно то, что делает второй (повышенный в правах) процесс:
    переименование занятого файла невозможно.
    """
    monkeypatch.setattr(hub_logging, "MAX_BYTES", 200)
    path = hub_logging.setup_logging(log_dir=tmp_path, level=logging.INFO, force=True)
    assert path is not None

    def _boom(self):
        raise OSError(32, "файл занят другим процессом")

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "doRollover", _boom)

    log = hub_logging.logger()
    for i in range(20):
        log.info("строка %s — заполняем файл до порога ротации", i)

    state = hub_logging.rotation_state()
    assert state["failures"] >= 1, "отказ ротации обязан быть посчитан, а не проглочен"
    assert state["detail"], "причина отказа обязана сохраняться для разбора"


def test_rotation_failure_switches_to_pid_suffixed_file(tmp_path, _fresh_logging,
                                                        monkeypatch):
    """Не смогли повернуть общий файл — пишем в СВОЙ, с pid в имени.

    Это и есть развязка двух процессов: каждый ротирует то, что держит сам.
    Альтернатива («писать в общий и не ротировать») возвращает ровно тот
    безлимитный файл, ради которого ротация и заводилась.
    """
    monkeypatch.setattr(hub_logging, "MAX_BYTES", 200)
    hub_logging.setup_logging(log_dir=tmp_path, level=logging.INFO, force=True)

    def _boom(self):
        raise OSError(32, "файл занят другим процессом")

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "doRollover", _boom)

    log = hub_logging.logger()
    for i in range(20):
        log.info("строка %s — заполняем файл до порога ротации", i)

    fallback = hub_logging.pid_log_path(tmp_path)
    assert fallback.exists(), "после отказа ротации процесс обязан писать в свой файл"
    assert hub_logging.current_log_path() == fallback, (
        "активный путь лога обязан измениться — иначе self_check покажет не тот файл")
    assert "ротац" in fallback.read_text(encoding="utf-8").lower(), (
        "причина переключения обязана быть НАПИСАНА в самом логе")


def test_normal_rotation_still_works(tmp_path, _fresh_logging, monkeypatch):
    """Штатная ротация не сломана страховкой: один процесс — один файл + бэкапы."""
    monkeypatch.setattr(hub_logging, "MAX_BYTES", 200)
    hub_logging.setup_logging(log_dir=tmp_path, level=logging.INFO, force=True)

    log = hub_logging.logger()
    for i in range(50):
        log.info("строка %s — заполняем файл до порога ротации", i)

    assert (tmp_path / (hub_logging.LOG_FILE_NAME + ".1")).exists()
    assert hub_logging.rotation_state()["failures"] == 0
    assert not hub_logging.pid_log_path(tmp_path).exists()


# ======================================================================================
# 5. GAP-414 — кап кукбука применяется, tmp не остаётся
# ======================================================================================
class _CapClient:
    """Транспорт, отдающий документ ЗАВЕДОМО больше капа."""

    def __init__(self, meta: dict, blob: bytes, sidecar: dict | None = None) -> None:
        self.meta = meta
        self.blob = blob
        self.sidecar = sidecar
        self.file_calls: list = []

    def get_json(self, path, *, params=None, authorized=True, etag=None) -> tuple:
        if path.endswith("/meta"):
            return dict(self.meta), {}
        if path.endswith("/signature"):
            if self.sidecar is None:
                raise ChannelError("нет сайдкара", kind="signature_not_available",
                                   http_status=404)
            return dict(self.sidecar), {}
        raise AssertionError(path)

    def download(self, path, dest, *, authorized=True, resume_from=0, etag=None,
                 expected_size=None, chunk_size=1 << 20, max_bytes=None) -> dict:
        self.file_calls.append(path)
        if max_bytes is not None and len(self.blob) > int(max_bytes):
            raise ChannelError("Файл больше разрешённого потолка",
                               kind="too_large", detail=str(len(self.blob)))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blob)
        return {"bytes_written": len(self.blob), "total_bytes": len(self.blob),
                "resumed": False, "status": 200}


def _cap_env(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "app" / "server" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"MZ binar")
    return state, FakeCtx(binary_path=str(binary)), tmp_path / "profile"


def test_sync_refuses_document_larger_than_cap_before_download(tmp_path):
    """Обещанный каналом размер больше капа — СКАЧИВАНИЯ НЕТ ВОВСЕ.

    Fail-closed до сети: тянуть 8 МБ+ на пользовательский канал ради того,
    чтобы потом отказаться, — та же ошибка, что проверять подпись после
    подмены файла.
    """
    state, ctx, config_dir = _cap_env(tmp_path)
    blob = _html("0.420.0-a1b2c3d4")
    meta = _meta("0.420.0-a1b2c3d4", blob)
    meta["size_bytes"] = cookbook.MAX_COOKBOOK_BYTES + 1
    client = _CapClient(meta, blob, sidecar=_sidecar(blob))

    with pytest.raises(ChannelError) as exc:
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert exc.value.kind == "too_large"
    assert client.file_calls == [], "до скачивания дело доходить не должно"
    assert not cookbook.cookbook_path(config_dir).exists()


def test_sync_removes_tmp_when_download_fails(tmp_path):
    """Отказ СКАЧИВАНИЯ не оставляет `.part` рядом с документом.

    До фикса `client.download` стоял ВЫШЕ `try/except`, который убирает tmp, —
    и каждый отказ канала оставлял огрызок в профиле пользователя навсегда.
    """
    state, ctx, config_dir = _cap_env(tmp_path)
    blob = b"x" * (cookbook.MAX_COOKBOOK_BYTES + 10)
    meta = _meta("0.420.0-a1b2c3d4", blob)
    meta["size_bytes"] = 1024  # канал соврал о размере — кап обязан сработать в потоке
    client = _CapClient(meta, blob, sidecar=_sidecar(blob))

    with pytest.raises(ChannelError):
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    target = cookbook.cookbook_path(config_dir)
    tmp_file = target.with_suffix(target.suffix + ".part")
    assert not tmp_file.exists(), "временный файл обязан убираться на ЛЮБОМ отказе"
    assert not target.exists()


def test_sync_passes_cap_to_download(tmp_path):
    """Кап передаётся транспорту, а не остаётся декларацией в константе."""
    state, ctx, config_dir = _cap_env(tmp_path)
    blob = _html("0.420.0-a1b2c3d4")
    seen: dict = {}

    class _Spy(_CapClient):
        def download(self, path, dest, **kwargs):
            seen.update(kwargs)
            return super().download(path, dest, **kwargs)

    client = _Spy(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))
    cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert seen.get("max_bytes") == cookbook.MAX_COOKBOOK_BYTES


# --- сам транспорт --------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, body: bytes, headers: dict, status: int = 200) -> None:
        self._body = body
        self._pos = 0
        self.headers = headers
        self.status = status

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def _client_with_response(monkeypatch, response):
    client = backend.BackendClient.__new__(backend.BackendClient)
    monkeypatch.setattr(client, "_build_request", lambda *a, **k: object(),
                        raising=False)
    monkeypatch.setattr(client, "_open", lambda req, data: response, raising=False)
    return client


def test_download_refuses_declared_length_over_cap(tmp_path, monkeypatch):
    """Объявленная длина больше капа — отказ ДО открытия приёмника."""
    dest = tmp_path / "artifact.bin"
    dest.write_bytes("прежний рабочий файл".encode("utf-8"))
    resp = _FakeResponse(b"x" * 100, {"Content-Length": "100"})
    client = _client_with_response(monkeypatch, resp)

    with pytest.raises(ChannelError) as exc:
        client.download("/p", dest, max_bytes=10)

    assert exc.value.kind == "too_large"
    assert dest.read_bytes() == "прежний рабочий файл".encode("utf-8"), (
        "отказ по капу не имеет права портить уже лежащий файл")


def test_download_aborts_when_body_exceeds_cap(tmp_path, monkeypatch):
    """Сервер соврал о длине — поток режется на превышении, огрызок удаляется."""
    dest = tmp_path / "artifact.bin"
    resp = _FakeResponse(b"x" * 100, {})
    client = _client_with_response(monkeypatch, resp)

    with pytest.raises(ChannelError) as exc:
        client.download("/p", dest, max_bytes=10, chunk_size=4)

    assert exc.value.kind == "too_large"
    assert not dest.exists(), "частично скачанный файл сверх капа не остаётся на диске"


def test_download_without_cap_is_unchanged(tmp_path, monkeypatch):
    """Без `max_bytes` поведение прежнее — кап не становится обязательным для всех."""
    dest = tmp_path / "artifact.bin"
    resp = _FakeResponse(b"x" * 100, {"Content-Length": "100"})
    client = _client_with_response(monkeypatch, resp)

    result = client.download("/p", dest)

    assert result["bytes_written"] == 100
    assert dest.stat().st_size == 100
