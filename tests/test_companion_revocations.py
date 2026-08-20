"""
Тесты цикла отзыва лицензий: `standkit_companion.revocations`.

Зачем этот файл существует. Цикл маленький, но у него два свойства, потеря которых
незаметна снаружи и катастрофична по последствиям:

1. **Запрос уходит БЕЗ конверта.** Отозванная лицензия не проходит аутентификацию — на ней
   основной канал отвечает `401 revoked`. Если бы список отзыва запрашивался авторизованно,
   единственный клиент, которому он адресован, никогда бы его не получил. «Случайно»
   добавленный `authorized=True` не сломает ни один сценарий у разработчика с валидной
   лицензией и сломает ровно тот, ради которого цикл написан. Поэтому тест смотрит на
   фактический аргумент запроса, а не на результат;
2. **На диск попадают БАЙТ В БАЙТ данные сервера.** Клиентский MCP проверяет подпись сам,
   по канонической сериализации; любая пере-сериализация (`json.dumps(json.loads(...))`)
   меняет порядок ключей, отступы и представление кириллицы — и подпись перестаёт
   сходиться. Дефект не воспроизводится на английских данных, поэтому в тестовом документе
   кириллица есть намеренно, а сравнение — побайтное.

Плюс поведение, которое обязано быть НЕ ошибкой: `404` (издатель ещё не выложил файл) и
`304` (файл не менялся). Ошибка ровно одна — не сошедшаяся подпись, и она обязана оставить
диск нетронутым: подделка списка означает СНЯТИЕ отзыва.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from standkit_companion import revocations
from standkit_companion import signature as sigmod
from standkit_companion.backend import BackendResponse
from standkit_companion.errors import ChannelError, NotModified
from standkit_companion.state import CompanionState
from tests.test_companion_signature import FAKE_SEED, OTHER_SEED, _public_key, _sign


# --------------------------------------------------------------------------------------
# Построение подписанного документа
# --------------------------------------------------------------------------------------
def _document(revoked, *, seed: bytes = FAKE_SEED, reason: str = "нарушение условий EULA",
              pretty: bool = True) -> bytes:
    """Подписанный список отзыва в том виде, в каком его отдаёт сервер.

    Отдаётся `pretty`-JSON с отступами и кириллицей — то есть НЕ в канонической форме.
    Это принципиально: канонизация нужна только для вычисления подписи, а на диск обязаны
    лечь исходные байты. Документ, совпадающий с канонической формой, скрыл бы ошибку
    пере-сериализации.
    """
    payload = {
        "version": 1,
        "issued_at": "2026-08-20T09:00:00Z",
        "reason": reason,
        "revoked": list(revoked),
    }
    raw = _sign(seed, sigmod.canonical_revocations_payload(payload))
    payload["signature"] = base64.b64encode(raw).decode("ascii")
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None) + "\n"
    return text.encode("utf-8")


@pytest.fixture(autouse=True)
def publisher_key(monkeypatch):
    """Ключ ЛИЦЕНЗИЙ издателя на время теста — наш тестовый.

    Приватного ключа издателя у нас нет и быть не может, поэтому подписать документ его
    ключом нельзя. Подменяется именно вшитая константа, а не логика проверки: проверяется
    настоящий `verify_revocations_document`, включая канонизацию.
    """
    monkeypatch.setattr(sigmod, "PUBLISHER_LICENSE_PUBKEY_B64",
                        base64.b64encode(_public_key(FAKE_SEED)).decode("ascii"))


# --------------------------------------------------------------------------------------
# Подставные клиент и контекст
# --------------------------------------------------------------------------------------
class FakeClient:
    """Заглушка `BackendClient.request` с записью фактических аргументов запроса."""

    def __init__(self, body: bytes = b"", *, etag: str = '"rev-1"',
                 error: BaseException | None = None) -> None:
        self.body = body
        self.etag = etag
        self.error = error
        self.requests: list = []

    def request(self, path, *, method="GET", params=None, authorized=True, etag=None,
                range_start=None, extra_headers=None, timeout=None):
        self.requests.append({"path": path, "method": method, "authorized": authorized,
                              "etag": etag})
        if self.error is not None:
            raise self.error
        return BackendResponse(status=200, headers={"etag": self.etag}, body=self.body)


@dataclass
class FakeCtx:
    """Лицензионный контекст в объёме, который читает `revocations` (duck-typing)."""

    revocations_target: str
    revocations_env_registered: bool = True
    envelope: str = "конверт-заглушка"
    backend_url: str = "https://updates.example.invalid"
    mcp_version: str = "0.300.0"
    package_root: str = ""
    binary_path: str = ""
    artifact_pubkey: str = ""
    raw: dict = field(default_factory=dict)


@pytest.fixture()
def env(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(revocations_target=str(tmp_path / "mcp" / "revocations.json"))
    return state, ctx


# ======================================================================================
# 1. Успешный путь: побайтная запись
# ======================================================================================
def test_signed_document_is_written_byte_for_byte(env):
    state, ctx = env
    body = _document(["LIC-0001", "LIC-0002"])
    client = FakeClient(body)

    result = revocations.refresh(client, state, ctx)

    written = Path(ctx.revocations_target).read_bytes()
    assert written == body, (
        "на диск обязаны лечь исходные байты сервера: MCP проверяет подпись по канонической "
        "сериализации, и любая пере-сериализация (порядок ключей, отступы, ensure_ascii) "
        "её ломает")
    assert result["changed"] is True
    assert result["revoked_count"] == 2
    assert result["reason"] == "updated"
    assert result["etag"] == '"rev-1"'
    assert state.revocations["revoked_ids"] == ["LIC-0001", "LIC-0002"]
    assert state.revocations["etag"] == '"rev-1"'
    assert state.revocations["last_status"] == "ok"


def test_written_bytes_are_not_a_reserialized_json(env):
    """Отдельная проверка того же свойства «от противного».

    Документ намеренно отдаётся в НЕканонической форме. Если бы модуль писал
    `json.dumps(json.loads(body))`, файл всё равно разбирался бы как правильный JSON — и
    ошибка вскрылась бы только у клиента, на проверке подписи. Здесь она видна сразу.
    """
    state, ctx = env
    body = _document(["LIC-Ф-01"], reason="отзыв по требованию правообладателя")
    assert body != sigmod.canonical_revocations_payload(json.loads(body)), (
        "тестовый документ обязан отличаться от канонической формы, иначе тест ничего "
        "не доказывает")
    client = FakeClient(body)

    revocations.refresh(client, state, ctx)

    written = Path(ctx.revocations_target).read_bytes()
    assert written == body
    assert b"\n  " in written, "отступы сервера обязаны сохраниться"
    assert "правообладателя".encode("utf-8") in written, (
        "кириллица обязана остаться UTF-8, а не превратиться в \\uXXXX")


def test_second_run_with_same_document_reports_unchanged(env):
    state, ctx = env
    client = FakeClient(_document(["LIC-0001"]))
    revocations.refresh(client, state, ctx)

    result = revocations.refresh(client, state, ctx)

    assert result["changed"] is False
    assert result["reason"] == "unchanged"
    assert result["revoked_count"] == 1


# ======================================================================================
# 2. Подпись не сошлась — на диске не появляется ничего
# ======================================================================================
def test_forged_document_is_rejected_and_nothing_is_written(env):
    state, ctx = env
    # Документ подписан ЧУЖИМ ключом — ровно то, как выглядела бы попытка снять отзыв.
    client = FakeClient(_document(["LIC-0001"], seed=OTHER_SEED))

    with pytest.raises(ChannelError) as excinfo:
        revocations.refresh(client, state, ctx)

    assert excinfo.value.kind == "revocations_signature_invalid"
    assert not Path(ctx.revocations_target).exists(), (
        "подделка списка отзыва = снятие отзыва; такой файл не должен оказаться на диске "
        "даже на мгновение — MCP может прочитать его между записью и откатом")
    assert state.revocations["revoked_ids"] == []


def test_tampered_body_does_not_overwrite_existing_copy(env):
    """Уже лежащая корректная копия обязана пережить попытку подсунуть подделку."""
    state, ctx = env
    good = _document(["LIC-0001"])
    revocations.refresh(FakeClient(good), state, ctx)

    forged = _document([], seed=OTHER_SEED)
    with pytest.raises(ChannelError):
        revocations.refresh(FakeClient(forged), state, ctx)

    assert Path(ctx.revocations_target).read_bytes() == good
    assert revocations.is_license_revoked(state, "LIC-0001") is True


# ======================================================================================
# 3. 404 — штатное состояние
# ======================================================================================
def test_not_configured_is_skipped_not_error(env):
    state, ctx = env
    client = FakeClient(error=ChannelError("Бэкенд издателя ответил 404",
                                           kind="revocations_not_configured",
                                           http_status=404,
                                           detail="revocations not configured"))

    result = revocations.refresh(client, state, ctx)

    assert result["reason"] == "not_configured"
    assert result["changed"] is False
    assert state.revocations["last_status"] == "skipped", (
        "сегодня файла у издателя может не быть вовсе — будить пользователя нечем")
    assert not Path(ctx.revocations_target).exists()


# ======================================================================================
# 4. 304 — файл не переписывается
# ======================================================================================
def test_not_modified_leaves_file_untouched(env):
    state, ctx = env
    body = _document(["LIC-0001"])
    revocations.refresh(FakeClient(body), state, ctx)
    path = Path(ctx.revocations_target)
    before_mtime = path.stat().st_mtime_ns
    # Гарантируем различимость mtime даже на файловых системах с грубым разрешением.
    time.sleep(0.01)

    client = FakeClient(error=NotModified("Данные не изменились с прошлой проверки"))
    result = revocations.refresh(client, state, ctx)

    assert result["reason"] == "not_modified"
    assert result["changed"] is False
    assert result["revoked_count"] == 1, "счётчик берётся из состояния, а не обнуляется"
    assert path.stat().st_mtime_ns == before_mtime, "файл не должен переписываться на 304"
    assert path.read_bytes() == body
    assert state.revocations["last_status"] == "ok", "304 — это успех, а не ошибка"


def test_stored_etag_is_sent_verbatim_on_next_request(env):
    state, ctx = env
    revocations.refresh(FakeClient(_document(["LIC-0001"]), etag='W/"rev-7"'), state, ctx)

    client = FakeClient(error=NotModified("не изменилось"))
    revocations.refresh(client, state, ctx)

    assert client.requests[-1]["etag"] == 'W/"rev-7"', (
        "ETag сравнивается сервером байт в байт: снятые кавычки или отброшенный W/ дают "
        "промах кэша и полную перекачку на каждом тике")


# ======================================================================================
# 5. Работа без конверта
# ======================================================================================
def test_refresh_works_without_envelope_and_asks_unauthorized(env):
    state, ctx = env
    ctx.envelope = ""  # лицензии нет либо она уже отозвана
    client = FakeClient(_document(["LIC-0001"]))

    result = revocations.refresh(client, state, ctx)

    assert client.requests[-1]["authorized"] is False, (
        "эндпоинт публичный именно затем, чтобы отозванный клиент узнал об отзыве: "
        "авторизованный запрос на отозванной лицензии вернёт 401 и цикл потеряет смысл")
    assert result["revoked_count"] == 1
    assert Path(ctx.revocations_target).exists()


def test_refresh_requests_the_public_endpoint(env):
    state, ctx = env
    client = FakeClient(_document([]))

    revocations.refresh(client, state, ctx)

    assert client.requests[-1]["path"] == "/v1/content/revocations.json"


# ======================================================================================
# 6. Запрос к состоянию
# ======================================================================================
def test_is_license_revoked_on_empty_state_is_false(env):
    state, _ctx = env

    assert revocations.is_license_revoked(state, "LIC-0001") is False, (
        "канал не имеет права объявлять лицензию отозванной на основании отсутствия данных")
    assert revocations.is_license_revoked(state, "") is False
    assert revocations.is_license_revoked(state, None) is False


def test_is_license_revoked_matches_case_insensitively(env):
    state, ctx = env
    revocations.refresh(FakeClient(_document(["LIC-ABC-001"])), state, ctx)

    assert revocations.is_license_revoked(state, "LIC-ABC-001") is True
    assert revocations.is_license_revoked(state, "  lic-abc-001  ") is True, (
        "цена ложного «не отозвана» выше цены ложного «отозвана», а идентификатор проходит "
        "через конфиги и копипасту")
    assert revocations.is_license_revoked(state, "LIC-ABC-002") is False


# ======================================================================================
# 7. Прочие защиты
# ======================================================================================
def test_broken_json_does_not_touch_the_file(env):
    state, ctx = env
    client = FakeClient(b"<html>502 Bad Gateway</html>")

    with pytest.raises(ChannelError) as excinfo:
        revocations.refresh(client, state, ctx)

    assert excinfo.value.kind == "bad_response"
    assert not Path(ctx.revocations_target).exists()


def test_missing_target_path_is_reported_not_guessed(env):
    """Пустой `revocations_target` — это «MCP не сказал, куда класть», а не повод
    придумать путь самостоятельно."""
    state, ctx = env
    ctx.revocations_target = ""
    client = FakeClient(_document(["LIC-0001"]))

    with pytest.raises(ChannelError) as excinfo:
        revocations.refresh(client, state, ctx)

    assert excinfo.value.kind == "local_io"


def test_unregistered_env_is_surfaced_not_hidden(env):
    """Если MCP не зарегистрировал переменную на этот файл, он его не читает — цикл
    работает вхолостую, и снаружи всё выглядит исправным. Об этом надо сказать."""
    state, ctx = env
    ctx.revocations_env_registered = False
    client = FakeClient(_document(["LIC-0001"]))

    result = revocations.refresh(client, state, ctx)

    assert result["env_registered"] is False
    assert "не зарегистрировал" in state.revocations["last_detail"]
