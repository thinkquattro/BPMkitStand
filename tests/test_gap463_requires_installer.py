# -*- coding: utf-8 -*-
"""Тесты GAP-463 — «requires_installer»: обновление, которое каналом доставить нельзя.

Издатель объявляет в `GET /v1/version/latest` (тот же попутный запрос, что уже несёт
`release_notes`/`known_issues`, GAP-442) булево `requires_installer` для версии `latest`.
Смысл флага — канал подменяет РОВНО ОДИН файл (бинарь сервера) и не трогает ни блок
запуска в конфиге хоста, ни требования рантайма, ни состав поставки вне бинаря; релиз,
меняющий что-то из этого, тихой подменой доставлен быть не может. Пять свойств, ради
которых написан именно такой код, а не более простой:

1. **чтение симметрично соседям** (`release_notes`/`known_issues`) и той же строгости:
   отсутствие поля, отсутствие записи для `latest`, мусор в значении — везде `False`,
   а не «неизвестно» (обратная совместимость со старым бэкендом);
2. **`stage` отказывает ДО скачивания**, тем же приёмом, что `signed: false` — трафик
   пользователя не тратится на файл, который всё равно не будет применён;
3. **`apply_staged` проверяет ЗАНОВО**, потому что флаг мог появиться уже ПОСЛЕ того, как
   файл лёг в стейджинг (та же гонка, из-за которой подпись в `apply_staged` тоже
   проверяется повторно, а не переиспользуется из `stage`); уже подготовленный файл при
   этом НЕ отзывается — подпись доказана, данные целы;
4. **автоматика (планировщик, «check_update») молча пропускает шаг**, а не падает
   typed-ошибкой: `requires_installer` — не отказ цикла проверки обновлений, который
   обязан продолжать тикать;
5. **явная команда человека получает честный typed-отказ** (`kind="requires_installer"`,
   тот же класс, что у `artifact_type_mismatch`/`signature_invalid` в `errors.KIND_TITLES`)
   и кнопка UI гаснет штатным механизмом `available_actions`, а не спецслучаем в вёрстке.

Подставные соседи (`FakeCtx`, `_client`, `env`, `_no_real_mutex_probe`) — из
`tests/test_companion_releases.py`; обёртка `_VersionAwareClient` и фикстура `marker_path`
для попутного запроса `/v1/version/latest` — из `tests/test_gap447_437_442_hub_truth.py`
(GAP-442, тот же эндпоинт). Раннер (`Recorder`/`patch_cycles`/`make_runner`/
`settings_all_on`) — из `tests/test_companion_runner.py`. Общий приём проекта: одна
реализация подставных соседей, а не расходящиеся копии в каждом файле тестов.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from standkit_companion import releases
from standkit_companion.errors import ChannelError, KIND_TITLES
from standkit_companion.runner import available_actions
from standkit_companion.state import CompanionState

from tests.test_companion_releases import FakeCtx, _client, _no_real_mutex_probe, env
from tests.test_companion_runner import Recorder, make_runner, patch_cycles, settings_all_on
from tests.test_gap447_437_442_hub_truth import _VersionAwareClient, marker_path

# Заглушаем предупреждение линтера "импортирован, но не используется явно" — обе фикстуры
# подключаются ЧЕРЕЗ ИМЯ параметра теста, это штатный для этого проекта приём переиспользования
# (см. докстринг test_gap447_437_442_hub_truth.py).
_ = (_no_real_mutex_probe, marker_path)


def _payload(version: str = "0.310.0", *, requires_installer=True, **extra) -> dict:
    """Ответ `GET /v1/version/latest` в объёме, который читает `_update_release_notes`."""
    payload = {
        "latest": version, "min_supported": "0.1.0",
        "release_notes": [], "known_issues": [],
        "requires_installer": requires_installer,
    }
    payload.update(extra)
    return payload


# ==========================================================================================
# 1. Чтение флага — симметрично release_notes/known_issues (GAP-442), та же деградация
# ==========================================================================================
def test_check_reads_requires_installer_true(marker_path, tmp_path):
    inner, _blob, _meta = _client(version="0.310.0")
    client = _VersionAwareClient(inner, _payload("0.310.0", requires_installer=True))
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    result = releases.check(client, state, ctx)

    assert state.releases["requires_installer"] is True
    assert result["requires_installer"] is True


@pytest.mark.parametrize("extra", [
    {"requires_installer": None},
    {"requires_installer": "true"},
    {"requires_installer": "false"},
    {"requires_installer": 1},
    {"requires_installer": 0},
    {"requires_installer": {}},
    {"requires_installer": []},
])
def test_check_treats_garbage_requires_installer_as_false(marker_path, tmp_path, extra):
    """Мусор в поле — `False`, а не «неизвестно»: дословно то же правило, что у
    отсутствующего поля и у отсутствующей записи для `latest` (обратная совместимость)."""
    inner, _blob, _meta = _client(version="0.310.0")
    payload = _payload("0.310.0")
    payload.update(extra)
    client = _VersionAwareClient(inner, payload)
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    result = releases.check(client, state, ctx)

    assert state.releases["requires_installer"] is False
    assert result["requires_installer"] is False


def test_check_missing_field_entirely_is_false(marker_path, tmp_path):
    """Старый бэкенд поля не пришлёт вовсе — тот же путь, что уже проверен для
    `release_notes`/`known_issues` (GAP-442): поведение обязано остаться прежним."""
    inner, _blob, _meta = _client(version="0.310.0")
    payload = _payload("0.310.0")
    del payload["requires_installer"]
    client = _VersionAwareClient(inner, payload)
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    result = releases.check(client, state, ctx)

    assert state.releases["requires_installer"] is False
    assert result["requires_installer"] is False


def test_check_degrades_requires_installer_to_false_when_endpoint_unavailable(
        marker_path, tmp_path):
    """ГЛАВНЫЙ тест на обратную совместимость: ручка `/v1/version/latest` недоступна
    (старый бэкенд без эндпоинта, сеть лежит) — ровно то же поведение, что и раньше, у
    ОБЫЧНОГО FakeClient релизов, который про этот путь ничего не знает."""
    client, _blob, _meta = _client(version="0.310.0")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    result = releases.check(client, state, ctx)

    assert result["available"] is True, "деградация попутного запроса не роняет проверку"
    assert state.releases["requires_installer"] is False
    assert result["requires_installer"] is False


def test_summary_exposes_requires_installer(marker_path, tmp_path):
    inner, _blob, _meta = _client(version="0.310.0")
    client = _VersionAwareClient(inner, _payload("0.310.0", requires_installer=True))
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    releases.check(client, state, ctx)
    summary = state.summary()["releases"]

    assert summary["requires_installer"] is True


def test_check_detail_text_differs_from_plain_available(marker_path, tmp_path):
    """Текст исхода для карточки/лога — не тот же «Доступна версия X», что у обычного
    обновления (иначе UI, читающий `rel.detail`, не мог бы их различить)."""
    inner, _blob, _meta = _client(version="0.310.0")
    flagged_client = _VersionAwareClient(inner, _payload("0.310.0", requires_installer=True))
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    releases.check(flagged_client, state, ctx)
    flagged_detail = state.releases["last_detail"]

    assert "ставится установщиком" in flagged_detail
    assert "Доступна версия 0.310.0 (установлена" not in flagged_detail, (
        "обычная формулировка про доступную версию не должна проскакивать в этом "
        "состоянии — иначе владелец продукта не отличит его от штатного обновления")


# ==========================================================================================
# 2. `stage` отказывает ДО скачивания — трафик не тратится
# ==========================================================================================
def test_stage_refuses_when_target_version_requires_installer(env):
    state, ctx = env
    client, _blob, _meta = _client(version="0.310.0")
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "0.310.0"

    with pytest.raises(ChannelError) as exc_info:
        releases.stage(client, state, ctx, version="0.310.0")

    assert exc_info.value.kind == "requires_installer"
    assert client.file_calls == [], (
        "версия ставится установщиком — канал не вправе тратить трафик пользователя "
        "на файл, который он всё равно не применит")
    assert state.releases["staged"] is None


def test_stage_refuses_even_without_a_known_version_number(env):
    """Сравнить нечем (нет `release_notes_version` при поднятом флаге) — тоже отказ:
    канал не вправе истолковать «не знаю» как разрешение."""
    state, ctx = env
    client, _blob, _meta = _client(version="0.310.0")
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = None

    with pytest.raises(ChannelError) as exc_info:
        releases.stage(client, state, ctx, version="0.310.0")

    assert exc_info.value.kind == "requires_installer"


def test_stage_still_works_for_a_different_version_when_latest_is_flagged(env):
    """Флаг относится к КОНКРЕТНОЙ версии (`release_notes_version`) — подготовка ДРУГОЙ,
    явно запрошенной версии им не блокируется (`stage_update` умеет адресоваться к
    произвольной версии, не только к `latest`)."""
    state, ctx = env
    client, _blob, _meta = _client(version="0.305.0")
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "0.310.0"

    result = releases.stage(client, state, ctx, version="0.305.0")

    assert result["version"] == "0.305.0"
    assert state.releases["staged"]["version"] == "0.305.0"


def test_stage_backward_compatible_when_flag_is_false(env):
    """Флаг `False` (дефолт нового состояния) — поведение канала ровно прежнее."""
    state, ctx = env
    client, _blob, _meta = _client(version="0.310.0")

    result = releases.stage(client, state, ctx, version="0.310.0")

    assert result["version"] == "0.310.0"
    assert state.releases["staged"] is not None
    assert client.file_calls, "обычная версия по-прежнему скачивается как раньше"


# ==========================================================================================
# 3. `apply_staged` проверяет ЗАНОВО (гонка: флаг появился уже после stage);
#    уже подготовленный файл при этом НЕ отзывается
# ==========================================================================================
def test_apply_staged_refuses_when_staged_version_becomes_flagged_afterwards(env):
    state, ctx = env
    client, _blob, _meta = _client(version="0.310.0")
    releases.stage(client, state, ctx, version="0.310.0")
    assert state.releases["staged"] is not None
    old_binary_bytes = Path(ctx.binary_path).read_bytes()

    # Гонка: издатель поднял флаг ПОСЛЕ того, как файл уже лежит в стейджинге проверенным.
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "0.310.0"

    with pytest.raises(ChannelError) as exc_info:
        releases.apply_staged(state, ctx)

    assert exc_info.value.kind == "requires_installer"
    assert state.releases["staged"] is not None, (
        "решение: уже скачанный и проверенный файл НЕ отзывается — подпись доказана, "
        "данные целы, единственная причина отказа — политика доставки, а не порча "
        "содержимого (см. докстринг apply_staged, GAP-463)")
    assert Path(state.releases["staged"]["path"]).is_file(), (
        "файл в стейджинге остаётся на диске")
    assert Path(ctx.binary_path).read_bytes() == old_binary_bytes, (
        "установленная версия не должна быть тронута отказавшим применением")


def test_apply_staged_ignores_flag_for_a_different_staged_version(env):
    """Стейдж — версия, отличная от флагованной `release_notes_version»: применение
    разрешено (флаг про НЕЁ, а не про канал вообще)."""
    state, ctx = env
    client, _blob, _meta = _client(version="0.305.0")
    releases.stage(client, state, ctx, version="0.305.0")

    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "0.310.0"

    result = releases.apply_staged(state, ctx)

    assert result["applied"] is True
    assert result["version"] == "0.305.0"


# ==========================================================================================
# 4. `available_actions` гасит «Установить» ТОЛЬКО для флагованной версии — тем же
#    механизмом, что и «нечего применять»/«некуда откатываться»
# ==========================================================================================
def test_available_actions_disables_apply_update_for_flagged_staged_version(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "bpmkit-1.2.3.exe"
    binary.write_bytes(b"MZ")
    state.releases["staged"] = {"version": "1.2.3", "path": str(binary)}
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "1.2.3"

    allowed = available_actions(settings_all_on(), state)

    assert allowed["apply_update"] is False, (
        "подготовленная версия ставится установщиком — кнопка «Установить» обязана "
        "погаснуть штатным механизмом available_actions, а не спецслучаем в вёрстке")
    assert allowed["stage_update"] is True, (
        "stage_update не привязан к ОДНОЙ версии — им адресуются к другой; решение по "
        "конкретной версии принимает releases.stage в момент вызова")


def test_available_actions_keeps_apply_update_for_a_different_staged_version(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "bpmkit-1.2.2.exe"
    binary.write_bytes(b"MZ")
    state.releases["staged"] = {"version": "1.2.2", "path": str(binary)}
    state.releases["requires_installer"] = True
    state.releases["release_notes_version"] = "1.2.3"

    allowed = available_actions(settings_all_on(), state)

    assert allowed["apply_update"] is True, (
        "подготовленная версия — НЕ та, что помечена установщиком; блокировать уже "
        "проверенный и не флагованный файл было бы избыточно")


def test_available_actions_false_by_default_matches_previous_behaviour(tmp_path):
    """Флаг не установлен вовсе (свежее состояние) — то же поведение, что до GAP-463."""
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "bpmkit-1.2.3.exe"
    binary.write_bytes(b"MZ")
    state.releases["staged"] = {"version": "1.2.3", "path": str(binary)}

    allowed = available_actions(settings_all_on(), state)

    assert allowed["apply_update"] is True


# ==========================================================================================
# 5. Автоматика (планировщик, «check_update») пропускает шаг МОЛЧА — не typed-ошибкой
# ==========================================================================================
def test_scheduler_auto_stage_skips_flagged_version_without_failing_the_cycle(
        tmp_path, monkeypatch):
    log: list = []
    stubs = patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "0.310.0",
                                 "requires_installer": True}))
    runner_obj = make_runner(tmp_path, settings_all_on(auto_stage_release=True))

    outcome = runner_obj.run_cycle("releases")

    assert stubs["releases_stage"].calls == 0, (
        "версия ставится установщиком — авто-подготовка обязана промолчать")
    assert outcome["status"] == "ok", (
        "requires_installer — НЕ отказ цикла проверки обновлений: он не retriable "
        "(errors.KIND_TITLES), и просочись исключение сюда, цикл встал бы намертво "
        "('halted') даже для БУДУЩИХ версий без этого флага")


def test_scheduler_auto_stage_off_is_unaffected_by_the_flag(tmp_path, monkeypatch):
    log: list = []
    stubs = patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "0.310.0",
                                 "requires_installer": True}))
    runner_obj = make_runner(tmp_path, settings_all_on(auto_stage_release=False))

    outcome = runner_obj.run_cycle("releases")

    assert stubs["releases_stage"].calls == 0
    assert outcome["status"] == "ok"


def test_check_update_action_skips_staging_when_requires_installer(tmp_path, monkeypatch):
    """GAP-241 «Проверить обновление»: проверка остаётся информативной (номер версии,
    нотсы), попутная подготовка молча пропускается."""
    log: list = []
    stubs = patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "0.310.0",
                                 "requires_installer": True}))
    r = make_runner(tmp_path, settings_all_on())

    result = r.run_action("check_update")

    assert stubs["releases_stage"].calls == 0
    assert result["staged"] is None
    assert result["available"] is True
    assert result["requires_installer"] is True


# ==========================================================================================
# 6. Явная команда человека («stage_update») получает честный typed-отказ —
#    сквозь весь путь runner → releases, без исключений и спецслучаев
# ==========================================================================================
def test_run_action_stage_update_raises_typed_error_end_to_end(tmp_path):
    client, _blob, _meta = _client(version="0.310.0")
    r = make_runner(
        tmp_path, settings_all_on(),
        context_resolver=lambda s: FakeCtx(workdir=str(tmp_path / "companion")),
        client_factory=lambda ctx, s: client,
    )
    r._state.releases["requires_installer"] = True
    r._state.releases["release_notes_version"] = "0.310.0"

    with pytest.raises(ChannelError) as exc_info:
        r.run_action("stage_update", version="0.310.0")

    assert exc_info.value.kind == "requires_installer"
    assert r._state.releases["staged"] is None
    assert client.file_calls == []


# ==========================================================================================
# 7. Класс отказа — тот же, что у существующих fail-closed kind'ов
# ==========================================================================================
def test_requires_installer_kind_is_not_retriable_and_user_visible():
    title, retriable, user_visible = KIND_TITLES["requires_installer"]
    assert retriable is False, (
        "следующий тик найдёт ТОТ ЖЕ флаг у ТОЙ ЖЕ версии — повтор ничего не изменит, "
        "та же семантика, что у artifact_type_mismatch/signature_invalid")
    assert user_visible is True
    assert title


# ==========================================================================================
# 8. UI хаба — текст карточки отличается от обычного «доступна X» (тот же лёгкий
#    текстовый guard, что GAP-437/442 используют для app.js/index.html/style.css)
# ==========================================================================================
def _web_dir() -> Path:
    import standkit_hub.server as server_module

    return Path(server_module.__file__).parent / "web"


def test_ui_reads_requires_installer_flag_from_the_snapshot():
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "requires_installer" in js, (
        "карточка обязана читать флаг из снимка канала, а не только release_notes/"
        "known_issues (GAP-442)")


def test_ui_shows_distinct_text_for_the_installer_required_state():
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "нужен установщик" in js, "бейдж карточки — не «доступна X» дословно"
    assert "ставится установщиком" in js
    assert "installerRequired" in js


def test_ui_install_button_gets_a_specific_reason_when_installer_required():
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "actionUnavailableReason" in js, (
        "причина недоступности кнопки должна вычисляться динамически (штатный "
        "механизм title/tooltip уже существующих причин), а не быть спецслучаем")
