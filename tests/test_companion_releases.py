"""
Тесты канала релизов MCP: `standkit_companion.releases`.

Зачем этот файл существует. Модуль качает и ставит ИСПОЛНЯЕМЫЙ файл, и почти каждый его
отказ внешне выглядит одинаково — «обновление не поставилось». Поэтому тесты проверяют не
«функция вернула что-то», а ровно те четыре свойства, ради которых модуль написан именно
так, а не проще:

1. **проверка обновления не качает файл** — иначе каждый тик планировщика стоил бы
   пользователю десятки мегабайт (счётчик запросов подставного клиента);
2. **fail-closed без лазеек** — `signed: false`, отсутствующий/чужой сайдкар, несошедшийся
   sha256, плейсхолдер вместо публичного ключа: каждый из этих случаев обязан
   ЗАБЛОКИРОВАТЬ установку, а два из них — ещё и не потратить трафик;
3. **докачка действительно докачивает** — обрыв на половине не должен начинать всё заново,
   а `416` обязан сбрасывать состояние, иначе клиент вечно повторяет битый `Range`;
4. **никакого «тихо обновлено»** — после подмены обязан быть `restart_required` и текст про
   перезапуск Claude Desktop; занятый файл обязан оставить старую версию целой ПОБАЙТНО.

Сеть в тестах не используется: вместо `BackendClient` подставлен `FakeClient` с теми же
сигнатурами (`head`/`get_json`/`download`), который считает вызовы. Подписи настоящие —
тестовый подписыватель по RFC 8032 переиспользуется из `tests/test_companion_signature.py`
(дублировать там реализацию Ed25519 значило бы завести вторую, которая разойдётся с первой).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from standkit_companion import fsutil, releases
from standkit_companion import signature as sigmod
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from tests.test_companion_signature import FAKE_SEED, OTHER_SEED, _public_key, _sign

# --------------------------------------------------------------------------------------
# Ключи и вспомогательные конструкторы данных
# --------------------------------------------------------------------------------------
PUBKEY_RAW = _public_key(FAKE_SEED)
PUBKEY_B64 = base64.b64encode(PUBKEY_RAW).decode("ascii")
KEY_ID = hashlib.sha256(PUBKEY_RAW).hexdigest()[:16]

RELEASES = releases.RELEASES_PREFIX


def _blob(version: str, size: int = 8192) -> bytes:
    """Детерминированное «тело релиза» нужного размера.

    Не `b"x" * n`: побайтное сравнение после докачки на однородном буфере прошло бы даже
    при склейке кусков в неправильном порядке.

    Начинается с `MZ` (GAP-212): тело релиза -- исполняемый файл Windows, и `apply_staged`
    теперь это проверяет. Фикстура без PE-заголовка описывала бы артефакт, которого в канале
    не бывает, и прятала бы сам гард от тестов подмены.
    """
    out = bytearray(b"MZ")
    chunk = hashlib.sha256(f"BPMkit {version}".encode("utf-8")).digest()
    while len(out) < size:
        out += chunk
        chunk = hashlib.sha256(chunk).digest()
    return bytes(out[:size])


def _meta(version: str, blob: bytes, *, filename: str | None = None,
          signed: bool = True) -> dict:
    """Ответ `GET .../meta` — ровно те поля, что отдаёт бэкенд издателя."""
    return {
        "version": version,
        "filename": filename or f"bpmkit-{version}.exe",
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published_at": "2026-08-20T10:00:00Z",
        "signed": signed,
        "sig_key_id": KEY_ID,
        "is_latest": True,
    }


def _sidecar(filename: str, blob: bytes, *, seed: bytes = FAKE_SEED,
             artifact: str | None = None) -> dict:
    """Настоящий сайдкар: подписан 32-байтовый сырой sha256 файла, как у издателя."""
    digest = hashlib.sha256(blob).hexdigest()
    raw = _sign(seed, bytes.fromhex(digest))
    return {
        "format": sigmod.SIG_FORMAT,
        "artifact": artifact or filename,
        "size": len(blob),
        "sha256": digest,
        "signed_at": "2026-08-20T10:00:00Z",
        "key_id": hashlib.sha256(_public_key(seed)).hexdigest()[:16],
        "signature": base64.b64encode(raw).decode("ascii"),
    }


# --------------------------------------------------------------------------------------
# Подставные клиент и контекст
# --------------------------------------------------------------------------------------
class FakeClient:
    """Заглушка `BackendClient` с теми же сигнатурами и счётчиком вызовов.

    Именно счётчик — предмет половины тестов: «не скачал» доказывается отсутствием вызова
    `download`, а не тем, что файла нет на диске (его могло не быть и по другой причине).
    """

    def __init__(self, meta: dict, blob: bytes, *, sidecar: dict | None = None,
                 etag: str = '"rel-1"') -> None:
        self.meta = meta
        self.blob = blob
        self.sidecar = sidecar
        self.etag = etag
        self.calls: list = []
        self.downloads: list = []
        self.download_hook = None
        self.head_error: BaseException | None = None
        self.meta_error: BaseException | None = None
        self.sidecar_error: BaseException | None = None

    # -- совместимые с BackendClient операции ------------------------------------------
    def head(self, path, *, authorized=True, etag=None) -> dict:
        self.calls.append(("HEAD", path))
        if self.head_error is not None:
            raise self.head_error
        return {
            "x-bpmkit-version": str(self.meta.get("version") or ""),
            "x-bpmkit-sha256": str(self.meta.get("sha256") or ""),
            "etag": self.etag,
            "accept-ranges": "bytes",
            "content-length": str(self.meta.get("size_bytes") or 0),
        }

    def get_json(self, path, *, params=None, authorized=True, etag=None) -> tuple:
        self.calls.append(("GET", path))
        if path.endswith("/meta"):
            if self.meta_error is not None:
                raise self.meta_error
            return dict(self.meta), {"etag": self.etag}
        if path.endswith("/signature"):
            if self.sidecar_error is not None:
                raise self.sidecar_error
            if self.sidecar is None:
                raise ChannelError("Бэкенд издателя ответил 404",
                                   kind="signature_not_available", http_status=404,
                                   detail="signature not available")
            return dict(self.sidecar), {}
        raise AssertionError(f"неожиданный JSON-запрос: {path}")

    def download(self, path, dest, *, authorized=True, resume_from=0, etag=None,
                 expected_size=None, chunk_size=1 << 20) -> dict:
        self.calls.append(("GET-FILE", path))
        self.downloads.append(int(resume_from))
        dest = Path(dest)
        if self.download_hook is not None:
            return self.download_hook(self, path, dest, int(resume_from))
        data = self.blob[int(resume_from):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "ab" if resume_from else "wb") as handle:
            handle.write(data)
        return {
            "bytes_written": len(data),
            "total_bytes": len(self.blob),
            "resumed": bool(resume_from),
            "status": 206 if resume_from else 200,
            "etag": self.etag,
            "sha256_header": self.meta.get("sha256"),
            "version_header": self.meta.get("version"),
        }

    # -- удобства тестов -----------------------------------------------------------------
    @property
    def file_calls(self) -> list:
        return [call for call in self.calls if call[0] == "GET-FILE"]


@dataclass
class FakeCtx:
    """Лицензионный контекст в объёме, который читает `releases` (duck-typing)."""

    workdir: str
    binary_path: str = ""
    artifact_pubkey: str = PUBKEY_B64
    mcp_version: str = "0.300.0"
    package_root: str = ""
    revocations_target: str = ""
    revocations_env_registered: bool = True
    backend_url: str = "https://updates.example.invalid"
    envelope: str = "конверт-заглушка"


@pytest.fixture()
def env(tmp_path):
    """Состояние + контекст с рабочим каталогом внутри `tmp_path`.

    Бинарь заранее лежит на диске с узнаваемым содержимым: без «прежней версии» нельзя
    доказать, что неудачная подмена её не испортила.
    """
    binary = tmp_path / "mcp" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stary binar 0.300.0")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    return state, ctx


@pytest.fixture(autouse=True)
def _no_real_mutex_probe(monkeypatch):
    """GAP-161: `apply_staged` теперь спрашивает `mcp_mutex.server_mutex_exists()` ДО
    бэкапа. Реальный WinAPI здесь недопустим: машина, на которой гоняются эти тесты,
    вполне может держать живой `bpmkit.exe` под тем же именем мьютекса (обычный дневной
    случай для разработчика самого MCP) — тогда КАЖДЫЙ тест `apply_staged` ловил бы
    `mcp_running` независимо от того, что он на самом деле проверяет. Автоприменяемый
    фикстур глушит реальный детект (`False` по умолчанию, «мьютекса не видно»); тесты
    самого детекта — `tests/test_companion_mcp_mutex.py`, тесты пути «мьютекс найден» —
    ниже, они переопределяют этот мок явно.
    """
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: False)


def _staging(ctx) -> Path:
    return releases.companion_workdir(ctx) / releases.STAGING_DIRNAME


def _backups(ctx) -> Path:
    return releases.companion_workdir(ctx) / releases.BACKUP_DIRNAME


def _client(version="0.307.0", *, signed=True, sidecar_seed=FAKE_SEED,
            sidecar_artifact=None, with_sidecar=True):
    blob = _blob(version)
    meta = _meta(version, blob, signed=signed)
    sidecar = None
    if with_sidecar:
        sidecar = _sidecar(meta["filename"], blob, seed=sidecar_seed,
                           artifact=sidecar_artifact)
    return FakeClient(meta, blob, sidecar=sidecar), blob, meta


# ======================================================================================
# 0. Сравнение версий (ловушка «0.10.0 старше 0.9.0»)
# ======================================================================================
@pytest.mark.parametrize("newer,older", [
    ("0.10.0", "0.9.0"),
    ("1.0.0", "0.999.999"),
    ("0.307.1", "0.307"),
    ("2", "1.99.99"),
])
def test_versions_compare_numerically_not_lexicographically(newer, older):
    assert releases.compare_versions(newer, older) == 1, (
        f"{newer} обязана считаться новее {older}: строковое сравнение здесь даёт обратный "
        f"результат и молча отключает обновления после десятого минорного релиза")
    assert releases.compare_versions(older, newer) == -1
    assert releases.compare_versions(newer, newer) == 0


def test_missing_trailing_zero_is_the_same_version():
    """`0.307` и `0.307.0` — одна версия, а не разные: иначе канал предлагал бы
    «обновление» на тот же самый файл при смене формы записи у издателя."""
    assert releases.compare_versions("0.307", "0.307.0") == 0
    assert releases.parse_version("v0.307.0") == (0, 307, 0), "ведущее v срезается"
    assert releases.parse_version("1.2.0-rc1") == (1, 2, 0), "нечисловой сегмент = 0"


def test_version_regexp_matches_server_contract():
    """Сервер принимает в пути только `^\\d+(\\.\\d+)*$` — проверка обязана совпадать с ним."""
    assert releases.is_numeric_version("0.307.0") is True
    assert releases.is_numeric_version("1") is True
    for bad in ("unknown", "", "v1.2.3", "1.2.3-rc1", "latest", "1.2.х"):
        assert releases.is_numeric_version(bad) is False, (
            f"{bad!r} сервер отвергнет 404 — подставлять такое в URL нельзя")


def test_workdir_is_user_writable_and_not_package_root(env):
    """Рабочий каталог — пользовательский, не `Program Files` (критерий приёмки №10)."""
    _state, ctx = env
    workdir = releases.companion_workdir(ctx)
    assert workdir == Path(ctx.workdir)
    assert not str(workdir).lower().startswith("c:\\program files"), (
        "запись в Program Files требует прав администратора, которых у Companion нет")


# ======================================================================================
# 1. Проверка обновления не качает файл
# ======================================================================================
def test_check_uses_head_and_meta_but_never_downloads(env):
    state, ctx = env
    client, _blob_bytes, meta = _client("0.307.0")

    result = releases.check(client, state, ctx)

    assert client.file_calls == [], (
        "проверка обновления обязана обходиться HEAD и /meta: GET файла — это десятки "
        "мегабайт на каждом тике планировщика")
    assert ("HEAD", f"{RELEASES}/latest") in client.calls
    assert ("GET", f"{RELEASES}/latest/meta") in client.calls
    assert result["available"] is True
    assert result["latest"] == "0.307.0"
    assert result["current"] == "0.300.0"
    assert result["signed"] is True
    assert result["size_bytes"] == meta["size_bytes"]
    assert result["etag"] == '"rel-1"'
    assert state.releases["known_latest"] == "0.307.0"


def test_check_reports_up_to_date_without_download(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.300.0")

    result = releases.check(client, state, ctx)

    assert result["available"] is False
    assert result["reason"] == "up_to_date"
    assert client.file_calls == []


# ======================================================================================
# 2. 404 «release not configured» — штатное состояние
# ======================================================================================
def test_check_release_not_configured_is_skipped_not_error(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client()
    client.head_error = ChannelError("Бэкенд издателя ответил 404",
                                     kind="release_not_configured", http_status=404,
                                     detail="release not configured")

    result = releases.check(client, state, ctx)

    assert result["available"] is False
    assert result["reason"] == "release_not_configured"
    assert state.releases["last_status"] == "skipped", (
        "владелец просто ещё не выложил релиз — это пропуск тика, а не ошибка канала")
    assert client.file_calls == []


# ======================================================================================
# 3. Версия «unknown» — работаем только через latest
# ======================================================================================
def test_unknown_version_falls_back_to_latest_path(env):
    state, ctx = env
    blob = _blob("unknown")
    meta = _meta("unknown", blob, filename="bpmkit.exe")
    client = FakeClient(meta, blob, sidecar=_sidecar("bpmkit.exe", blob))

    checked = releases.check(client, state, ctx)

    assert checked["latest"] == "unknown"
    assert checked["target"] == "latest", "по 'unknown' сервер отдаст 404 — только latest"
    assert checked["reason"] == "version_unknown_use_latest"
    assert "номер версии" in state.releases["last_detail"].lower(), (
        f"состояние обязано честно объяснять, почему версия не сравнивается: "
        f"{state.releases['last_detail']!r}")

    # И сама подготовка обязана уйти на `latest`, а не на `/releases/unknown`.
    staged = releases.stage(client, state, ctx, version=checked["latest"])

    assert client.file_calls == [("GET-FILE", f"{RELEASES}/latest")]
    assert "unknown" not in [call[1].rsplit("/", 1)[-1] for call in client.calls], (
        "нечисловая версия не должна попадать в URL ни одним запросом")
    assert staged["note"], "причина отказа от адресации по номеру обязана быть в результате"
    assert Path(staged["path"]).read_bytes() == blob


# ======================================================================================
# 4. signed: false — отказ ДО скачивания
# ======================================================================================
def test_unsigned_release_is_refused_without_spending_traffic(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0", signed=False)

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "signature_not_available", (
        "signed:false — это «подпись ЭТОГО файла не подтверждена», а не «издатель забыл»")
    assert client.file_calls == [], "неподписанный релиз не должен скачиваться вовсе"
    assert releases.staged_info(state) is None
    assert not _staging(ctx).exists() or list(_staging(ctx).iterdir()) == []


# ======================================================================================
# 5. sha256 не сошёлся
# ======================================================================================
def test_sha256_mismatch_gives_integrity_mismatch_and_empty_staging(env):
    state, ctx = env
    blob = _blob("0.307.0")
    meta = _meta("0.307.0", blob)
    # Сервер отдаёт ДРУГОЕ тело, чем обещал в метаданных (порча в пути, подмена CDN).
    client = FakeClient(meta, _blob("подменённое тело"), sidecar=_sidecar(meta["filename"], blob))

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "integrity_mismatch"
    assert releases.staged_info(state) is None
    assert list(_staging(ctx).iterdir()) == [], (
        "содержимое доказано неверное: докачивать нечего, и сохранённый кусок обрекал бы "
        "клиента на вечный повтор одного и того же отказа")
    assert state.releases["partial"] is None


# ======================================================================================
# 6. Сайдкар от другого файла
# ======================================================================================
def test_sidecar_for_another_artifact_is_refused(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0", sidecar_artifact="bpmkit-9.9.9.exe")

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "signature_not_available", (
        "сайдкар от другого файла — путь к откату на старую уязвимую версию с её же "
        "валидной подписью")
    assert releases.staged_info(state) is None
    assert state.releases["staged"] is None


def test_sidecar_signed_by_foreign_key_is_refused(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0", sidecar_seed=OTHER_SEED)

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "artifact_signature_invalid"
    assert releases.staged_info(state) is None


def test_missing_sidecar_is_fail_closed(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0", with_sidecar=False)

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "signature_not_available"
    assert releases.staged_info(state) is None


# ======================================================================================
# 7. Публичный ключ отсутствует или плейсхолдер
# ======================================================================================
@pytest.mark.parametrize("pubkey,why", [
    ("", "ключ в поставке ещё не выпущен"),
    ("# ключ артефактов ещё не сгенерирован", "в файле лежит комментарий-плейсхолдер"),
    ("bm90LWEta2V5", "строка декодируется, но это не 32 байта"),
])
def test_placeholder_pubkey_blocks_download(env, pubkey, why):
    state, ctx = env
    ctx.artifact_pubkey = pubkey
    client, _blob_bytes, _meta_dict = _client("0.307.0")

    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "pubkey_missing", (
        f"{why}: пользователю нужно сказать «канал не настроен», а не «подпись неверна»")
    assert client.file_calls == [], (
        "проверять подпись всё равно нечем — качать десятки мегабайт незачем")


# ======================================================================================
# 8. Докачка продолжается с места обрыва
# ======================================================================================
def test_interrupted_download_resumes_from_offset(env):
    state, ctx = env
    client, blob, meta = _client("0.307.0")
    half = len(blob) // 2

    def half_then_break(cl, path, dest, resume_from):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "ab" if resume_from else "wb") as handle:
            handle.write(cl.blob[resume_from:half])
        raise ChannelError("Обрыв соединения при скачивании файла", kind="offline")

    client.download_hook = half_then_break
    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)
    assert excinfo.value.kind == "offline"

    part = _staging(ctx) / (meta["filename"] + releases.PART_SUFFIX)
    assert part.stat().st_size == half, "половина обязана остаться на диске"
    assert state.releases["partial"]["bytes"] == half

    client.download_hook = None
    result = releases.stage(client, state, ctx)

    assert client.downloads == [0, half], (
        f"вторая попытка обязана прийти с resume_from={half}, а не качать с нуля: "
        f"фактически {client.downloads}")
    assert Path(result["path"]).read_bytes() == blob, (
        "склеенный из двух кусков файл обязан побайтно совпадать с эталоном")
    assert result["resumed"] is True
    assert state.releases["partial"] is None


# ======================================================================================
# 9. 416 сбрасывает состояние докачки
# ======================================================================================
def test_range_invalid_drops_part_and_restarts_from_scratch(env):
    state, ctx = env
    client, blob, meta = _client("0.307.0")
    half = len(blob) // 2

    def half_then_break(cl, path, dest, resume_from):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "ab" if resume_from else "wb") as handle:
            handle.write(cl.blob[resume_from:half])
        raise ChannelError("Обрыв", kind="offline")

    client.download_hook = half_then_break
    with pytest.raises(ChannelError):
        releases.stage(client, state, ctx)

    part = _staging(ctx) / (meta["filename"] + releases.PART_SUFFIX)
    assert part.is_file()

    def reject_range(cl, path, dest, resume_from):
        # Реальный `BackendClient.download` на 416 сам удаляет недокачанный файл.
        try:
            dest.unlink()
        except OSError:
            pass
        raise ChannelError("Сервер отверг докачку", kind="range_invalid", http_status=416)

    client.download_hook = reject_range
    with pytest.raises(ChannelError) as excinfo:
        releases.stage(client, state, ctx)

    assert excinfo.value.kind == "range_invalid"
    assert not part.exists(), "частичный файл обязан исчезнуть вместе с отвергнутым Range"
    assert state.releases["partial"] is None, (
        "иначе следующий заход снова пошлёт тот же битый Range — и так до бесконечности")

    client.download_hook = None
    result = releases.stage(client, state, ctx)

    assert client.downloads[-1] == 0, "после сброса качаем с нуля"
    assert Path(result["path"]).read_bytes() == blob


# ======================================================================================
# 10. Применение: бэкап, подмена, требование перезапуска
# ======================================================================================
def test_apply_staged_backs_up_replaces_and_demands_restart(env):
    state, ctx = env
    client, blob, meta = _client("0.307.0")
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    result = releases.apply_staged(state, ctx)

    assert Path(ctx.binary_path).read_bytes() == blob, "бинарь обязан быть подменён"
    backups = sorted(_backups(ctx).iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes, (
        "бэкап прежней версии — единственный путь отката, он обязан быть сделан ДО подмены")
    assert state.releases["restart_required"] is True, (
        "подмена файла НЕ перезапускает работающий MCP-сервер — без этого флага "
        "пользователь считал бы обновление уже действующим")
    assert "перезапустите claude desktop" in result["message"].lower()
    assert result["version"] == "0.307.0"
    assert result["previous_version"] == "0.300.0"
    assert releases.staged_info(state) is None
    assert state.releases["current"]["version"] == "0.307.0"
    assert state.releases["current"]["key_id"] == KEY_ID


def test_check_after_apply_does_not_offer_the_same_update_again(env):
    """После применения MCP до перезапуска докладывает СТАРУЮ версию — канал обязан
    ориентироваться на состояние, иначе он качал бы одно и то же обновление вечно."""
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    releases.stage(client, state, ctx)
    releases.apply_staged(state, ctx)

    result = releases.check(client, state, ctx)

    assert ctx.mcp_version == "0.300.0", "работающий MCP всё ещё старый — это норма"
    assert result["available"] is False
    assert result["current"] == "0.307.0"


# ======================================================================================
# 11. Занятый файл: старая версия цела
# ======================================================================================
def test_busy_binary_keeps_old_version_intact(env, monkeypatch):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    def busy(src, dst, *args, **kwargs):
        raise PermissionError(32, "The process cannot access the file")

    # Подменяем ПОСЛЕ подготовки: стейджинг пользуется той же функцией.
    monkeypatch.setattr(fsutil, "replace_with_retry", busy)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "local_io"
    text = str(excinfo.value).lower()
    assert "claude desktop" in text and "запущен" in text, (
        f"текст обязан прямо называть причину и действие, а не «ошибка ввода-вывода»: {text!r}")
    assert Path(ctx.binary_path).read_bytes() == old_bytes, (
        "неудачная подмена не имеет права испортить установленную версию")
    assert state.releases["restart_required"] is False
    assert releases.staged_info(state) is not None, (
        "подготовленное обновление обязано пережить неудачу — иначе повтор потребует "
        "заново качать десятки мегабайт")


# ======================================================================================
# 12. Откат
# ======================================================================================
def _stage_and_apply(state, ctx, version: str):
    client, blob, _meta_dict = _client(version)
    releases.stage(client, state, ctx)
    releases.apply_staged(state, ctx)
    return blob


def test_rollback_restores_previous_binary_and_shifts_history(env):
    state, ctx = env
    blob_306 = _stage_and_apply(state, ctx, "0.306.0")
    blob_307 = _stage_and_apply(state, ctx, "0.307.0")
    assert Path(ctx.binary_path).read_bytes() == blob_307
    assert len(state.releases["history"]) == 2

    result = releases.rollback(state, ctx)

    assert Path(ctx.binary_path).read_bytes() == blob_306, (
        "откат обязан вернуть ровно тот файл, что лежал до последнего обновления")
    assert result["version"] == "0.306.0"
    assert result["from_version"] == "0.307.0"
    assert state.releases["current"]["version"] == "0.306.0"
    assert len(state.releases["history"]) == 1, (
        "запись об откаченном обновлении больше не описывает реальность")
    assert state.releases["restart_required"] is True


def test_rollback_without_history_is_honest_refusal(env):
    state, ctx = env

    with pytest.raises(ChannelError) as excinfo:
        releases.rollback(state, ctx)

    assert excinfo.value.kind == "nothing_to_rollback", (
        "отказ «откатываться не на что» обязан отличаться от локальной ошибки ФС — "
        "пользователь чинит в этих случаях совершенно разное")
    assert "откат" in str(excinfo.value).lower()


def test_rollback_to_named_version(env):
    state, ctx = env
    original = Path(ctx.binary_path).read_bytes()
    _stage_and_apply(state, ctx, "0.306.0")
    _stage_and_apply(state, ctx, "0.307.0")

    result = releases.rollback(state, ctx, version="0.300.0")

    assert Path(ctx.binary_path).read_bytes() == original
    assert result["version"] == "0.300.0"
    assert state.releases["history"] == [], (
        "откат через две ступени делает обе записи истории недействительными")


# ======================================================================================
# 13. Уборка бэкапов
# ======================================================================================
def test_prune_backups_keeps_current_and_previous(env):
    state, ctx = env
    _stage_and_apply(state, ctx, "0.306.0")
    _stage_and_apply(state, ctx, "0.307.0")
    referenced = {Path(entry["backup"]).name for entry in state.releases["history"]}
    assert len(referenced) == 2
    stale = _backups(ctx) / "bpmkit-0.100.0-20200101T000000Z.exe"
    stale.write_bytes("забытый бэкап давно откаченной версии".encode("utf-8"))

    removed = releases.prune_backups(state, ctx)

    assert removed == 1
    survivors = {path.name for path in _backups(ctx).iterdir()}
    assert survivors == referenced, (
        "бэкапы текущей и предыдущей версий — единственный путь отката, трогать их нельзя")


# ======================================================================================
# 14. Регресс-гард: неподписанное применить нечем
# ======================================================================================
def test_public_api_has_no_switch_to_apply_unsigned_binary():
    """Статический гард на будущее.

    Проверяется не поведение, а СИГНАТУРА: любой добровольно добавленный `force=True`
    выглядит в коде безобидно («иногда же надо»), а на деле снимает единственную защиту от
    установки чужого исполняемого файла. Пусть такая правка ломает тест сразу.
    """
    params = inspect.signature(releases.apply_staged).parameters
    assert list(params) == ["state", "ctx", "target"], (
        f"у apply_staged появились новые параметры: {list(params)}")
    for name in params:
        low = name.lower()
        assert not low.startswith(("skip", "force", "allow")), (
            f"параметр {name!r} — это выключатель fail-closed политики")
        assert "unsigned" not in low and "signature" not in low

    # `allow_unsigned` есть ровно в одном месте — в `stage`, и только для тестов механизма.
    for name in releases.__all__:
        member = getattr(releases, name)
        if not inspect.isfunction(member) or member is releases.stage:
            continue
        assert "allow_unsigned" not in inspect.signature(member).parameters, (
            f"{name} не должен уметь обходить проверку подписи")


def test_stage_allow_unsigned_cannot_be_laundered_into_apply(env):
    """`allow_unsigned` в `stage` — не чёрный ход: применить такой стейдж нельзя."""
    state, ctx = env
    client, blob, _meta_dict = _client("0.307.0", signed=False, with_sidecar=False)
    old_bytes = Path(ctx.binary_path).read_bytes()

    staged = releases.stage(client, state, ctx, allow_unsigned=True)
    assert staged["signed"] is False
    assert Path(staged["path"]).read_bytes() == blob

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "signature_not_available"
    assert Path(ctx.binary_path).read_bytes() == old_bytes


def test_apply_reverifies_signature_against_tampered_staging(env):
    """Между подготовкой и применением файл в стейджинге доступен на запись — подпись
    обязана проверяться ЗАНОВО, непосредственно перед подменой."""
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    old_bytes = Path(ctx.binary_path).read_bytes()
    staged = releases.stage(client, state, ctx)
    Path(staged["path"]).write_bytes(b"chuzhoj ispolnyaemyj fajl")

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "integrity_mismatch"
    assert Path(ctx.binary_path).read_bytes() == old_bytes


def test_staged_info_reports_nothing_when_file_disappeared(env):
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    staged = releases.stage(client, state, ctx)
    assert releases.staged_info(state) is not None

    Path(staged["path"]).unlink()

    assert releases.staged_info(state) is None, (
        "иначе UI предложит кнопку «применить», которая гарантированно упадёт")


def test_head_404_without_body_is_release_not_configured(env, monkeypatch):
    """404 на `HEAD` классифицируется как «релиз не выложен», а не как ошибка канала.

    Находка ЖИВОГО прогона 20.08.2026 против бэкенда издателя. `HEAD` по определению
    отдаёт ответ без тела, поэтому разбор `detail` из JSON на нём слеп и падал в общий
    `http_error` — пользователь видел «Бэкенд ответил ошибкой» вместо штатного «владелец
    ещё не выложил релиз». Под `/v1/content/releases/*` других 404 не бывает: второй
    возможный (`signature not available`) живёт только на `.../signature`, куда `check`
    не ходит.
    """
    state, ctx = env

    class _HeadNotFound:
        def head(self, path, **kwargs):
            raise ChannelError("Бэкенд ответил ошибкой", kind="http_error",
                               http_status=404, detail="")

        def get_json(self, path, **kwargs):  # pragma: no cover - не должен вызываться
            raise AssertionError("после отказа HEAD за /meta ходить незачем")

    report = releases.check(_HeadNotFound(), state, ctx)

    assert report["available"] is False
    assert report["reason"] == "release_not_configured", (
        "пустое тело 404 обязано доклассифицироваться по коду и пути, иначе штатное "
        "состояние показывается пользователю как поломка")
    assert state.releases["last_status"] == "skipped", (
        "«релиз не выложен» — пропуск тика, а не ошибка: чинить пользователю нечего")


# ======================================================================================
# 15. GAP-161: детект мьютекса и файловая проба — отказ ДО бэкапа
# ======================================================================================
def test_apply_staged_refuses_before_backup_when_mutex_detected(env, monkeypatch):
    """Мьютекс сервера виден — канал отказывает СРАЗУ, не трогая бэкап/подпись/пробу."""
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "мьютекс обнаружен — до файловой пробы/бэкапа дело дойти не должно")

    monkeypatch.setattr(fsutil, "probe_writable", must_not_be_called)
    monkeypatch.setattr(fsutil, "backup_copy", must_not_be_called)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "mcp_running", (
        "детект мьютекса обязан давать СВОЙ, отличимый от local_io код отказа — иначе UI "
        "не сможет сказать «сервер точно занят» вместо обычной файловой ошибки")
    text = str(excinfo.value).lower()
    assert "закройте claude desktop" in text or "claude desktop" in text
    assert Path(ctx.binary_path).read_bytes() == old_bytes, (
        "установленная версия не тронута")
    assert not _backups(ctx).exists() or list(_backups(ctx).iterdir()) == [], (
        "бэкап не должен быть создан вовсе — отказ произошёл раньше")
    assert state.releases["restart_required"] is False
    assert releases.staged_info(state) is not None, (
        "подготовленное обновление обязано пережить отказ — повторная попытка не должна "
        "заново качать файл")


def test_apply_staged_refuses_before_backup_when_target_file_is_probed_busy(env, monkeypatch):
    """Мьютекса не видно (старый сервер без него/чужой процесс), но файл всё равно занят —
    файловая проба ловит это ДО бэкапа, тем же текстом, каким закончилась бы настоящая
    подмена."""
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    def probe_says_busy(path):
        raise PermissionError(32, "The process cannot access the file")

    backup_calls: list = []
    original_backup_copy = fsutil.backup_copy

    def spy_backup_copy(*args, **kwargs):
        backup_calls.append(args)
        return original_backup_copy(*args, **kwargs)

    monkeypatch.setattr(fsutil, "probe_writable", probe_says_busy)
    monkeypatch.setattr(fsutil, "backup_copy", spy_backup_copy)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "local_io"
    text = str(excinfo.value).lower()
    assert "claude desktop" in text and "запущен" in text, (
        f"тот же внятный текст, каким закончилась бы настоящая подмена: {text!r}")
    assert backup_calls == [], "проба обязана отказать РАНЬШЕ, чем начат бэкап"
    assert Path(ctx.binary_path).read_bytes() == old_bytes
    assert not _backups(ctx).exists() or list(_backups(ctx).iterdir()) == []
    assert state.releases["restart_required"] is False
    assert releases.staged_info(state) is not None


def test_mutex_check_runs_before_signature_and_probe_when_server_is_running(env, monkeypatch):
    """Порядок: мьютекс — самая дешёвая проверка, идёт первой. Если он говорит «сервер
    работает», ни проверка подписи, ни файловая проба не должны выполняться."""
    state, ctx = env
    client, _blob_bytes, _meta_dict = _client("0.307.0")
    releases.stage(client, state, ctx)

    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("мьютекс уже сказал 'сервер работает' — идти дальше незачем")

    monkeypatch.setattr(sigmod, "verify_artifact", must_not_be_called)
    monkeypatch.setattr(fsutil, "probe_writable", must_not_be_called)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "mcp_running"


def test_mutex_not_detected_does_not_block_normal_apply(env, monkeypatch):
    """Явная проверка обратного пути: мьютекс явно не найден (детект отработал и вернул
    False, а не просто не был вызван) — применение идёт штатно, ничего не блокируется.
    Дублирует часть смысла `test_apply_staged_backs_up_replaces_and_demands_restart`,
    но здесь факт вызова детекта проверяется явным счётчиком, а не только автофикстурой.
    """
    state, ctx = env
    client, blob, _meta_dict = _client("0.307.0")
    releases.stage(client, state, ctx)

    calls: list = []

    def fake_no_mutex(*a, **k):
        calls.append(1)
        return False

    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", fake_no_mutex)

    result = releases.apply_staged(state, ctx)

    assert calls == [1], "детект обязан быть вызван ровно один раз"
    assert result["applied"] is True
    assert Path(ctx.binary_path).read_bytes() == blob


# ======================================================================================
# 14. GAP-212: тип артефакта обязан подходить цели подмены
# ======================================================================================
def test_apply_staged_refuses_bundle_instead_of_binary(env):
    """Издательский конвейер до 02.09.2026 публиковал `.mcpb` -- ZIP-бандл, -- а канал
    подменяет им бинарь MCP. Подпись такого артефакта СОВЕРШЕННО ЧЕСТНАЯ, поэтому все
    проверки подлинности проходят: единственное, что стоит между клиентом и архивом на
    месте `bpmkit.exe`, -- этот гард."""
    state, ctx = env
    version = "0.307.0"
    blob = _blob(version)
    meta = _meta(version, blob, filename=f"bpmkit-{version}.mcpb")
    sidecar = _sidecar(meta["filename"], blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "artifact_type_mismatch", (
        "отдельный kind обязателен: это не файловая ошибка и не подделка, а «издатель "
        "выложил не тот артефакт»")
    assert Path(ctx.binary_path).read_bytes() == old_bytes, "установленная версия не тронута"
    assert not list(_backups(ctx).iterdir()) if _backups(ctx).exists() else True, (
        "отказ обязан случиться ДО бэкапа")
    assert releases.staged_info(state) is not None, (
        "подготовленное обновление не выбрасывается: чинить нечего на стороне клиента")


def test_apply_staged_refuses_archive_renamed_to_exe(env):
    """Расширение «правильное», содержимое -- нет: подпись и здесь сошлась бы, потому что
    подписывают файл, а не его смысл."""
    state, ctx = env
    version = "0.307.0"
    blob = b"PK\x03\x04" + _blob(version)[2:]
    meta = _meta(version, blob)
    sidecar = _sidecar(meta["filename"], blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    old_bytes = Path(ctx.binary_path).read_bytes()
    releases.stage(client, state, ctx)

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_staged(state, ctx)

    assert excinfo.value.kind == "artifact_type_mismatch"
    assert Path(ctx.binary_path).read_bytes() == old_bytes


def test_artifact_type_mismatch_is_not_retriable_and_visible(env):
    """Повтор на следующем тике бессмыслен (скачается тот же файл), а молчать нельзя:
    молча выключенный канал обновлений -- ровно то состояние, которое не замечают."""
    from standkit_companion import errors as companion_errors

    title, retriable, user_visible = companion_errors.KIND_TITLES["artifact_type_mismatch"]
    assert title
    assert retriable is False
    assert user_visible is True
