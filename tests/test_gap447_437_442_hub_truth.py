# -*- coding: utf-8 -*-
"""Тесты GAP-447/437/442 — честная карточка «Обновления BPMkit».

Три претензии реального клиента (обращение 21.09.2026, LIC-2026-1B9C9123) закрыты одним
набором, потому что все три — про один порок: карточка канала показывала СОСТОЯНИЕ, а
не ФАКТ.

* **GAP-447** — `restart_required` выставлялся подменой файла (`apply_staged`/
  `rollback`) и не снимался НИГДЕ: плашка «перезапустите» висела вечно, даже после
  честного перезапуска, а у клиента вдобавок процесс реально работал на СТАРОЙ версии
  (маркер должен это показать, а не спрятать за общим «перезапустите»). Тесты ниже —
  все четыре комбинации маркер/`applied_at`/версия, требуемые заказчиком: маркера нет,
  маркер старше подмены, маркер свежий и версия совпала, маркер свежий но версия другая
  (ровно сценарий клиента: `running_version` 1.1.1 при установленном 1.1.90).
* **GAP-437** — `renderPatternsRow` (см. `standkit_hub/web/app.js`) сводило четыре разных
  состояния канала паттернов к одной фразе; здесь проверяется ДАННЫЕ, на которых эта
  фраза строится: пустая дельта — это `ok`, а не «ещё не синхронизировались», и счётчик
  обязан отражать фактически доступную базу (поставочная + применённая дельта), а не
  только дельту канала.
* **GAP-442** — `release_notes`/`known_issues` из `GET /v1/version/latest` теперь
  попадают в состояние канала попутно проверке релиза, ЛУЧШЕЕ СТАРАНИЕ: недоступность
  эндпоинта не имеет права уронить саму проверку обновления.

Подставные соседи (`FakeClient`, `FakeCtx`, `_client`, `make_env`, `page`, `pattern`)
переиспользуются из `tests/test_companion_releases.py` и `tests/test_companion_patterns.py`
— тот же приём, которым `test_companion_releases.py` берёт подписыватель Ed25519 из
`test_companion_signature.py` (одна реализация подставного клиента, а не три расходящиеся
копии).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standkit_companion import patterns as pm
from standkit_companion import releases
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionSettings

from tests.test_companion_patterns import (
    FakeClient as PatternsFakeClient,
    make_env,
    page,
    pattern,
)
from tests.test_companion_releases import FakeCtx, _client


# ==========================================================================================
# GAP-447.0: read_runtime_marker — чтение маркера, best-effort
# ==========================================================================================
@pytest.fixture()
def marker_path(tmp_path, monkeypatch):
    """Путь маркера подменён явно (а не через APPDATA/HOME песочницы conftest.py) —
    тест не обязан знать детали барьера, а маркеру достаточно СВОЕГО файла в tmp_path."""
    path = tmp_path / "mcp_runtime.json"
    monkeypatch.setattr(releases, "_runtime_marker_path", lambda: path)
    return path


def _write_marker(path: Path, *, version: str, started_at: str, pid: int = 4242,
                  frozen: bool = True, binary: str = "bpmkit.exe") -> None:
    path.write_text(json.dumps({
        "version": version, "started_at": started_at, "pid": pid,
        "frozen": frozen, "binary": binary,
    }), encoding="utf-8")


def test_read_runtime_marker_missing_file_is_none(marker_path):
    assert releases.read_runtime_marker() is None, (
        "старый сервер, ни разу не перезапускавшийся после апгрейда с GAP-447, "
        "маркера не пишет вовсе — это НЕ ошибка канала")


def test_read_runtime_marker_reads_valid_json(marker_path):
    _write_marker(marker_path, version="1.1.90", started_at="2026-09-21T10:05:00Z")
    assert releases.read_runtime_marker() == {
        "version": "1.1.90", "started_at": "2026-09-21T10:05:00Z",
        "pid": 4242, "frozen": True, "binary": "bpmkit.exe",
    }


def test_read_runtime_marker_broken_json_is_treated_as_absent(marker_path):
    marker_path.write_text("{не json совсем", encoding="utf-8")
    assert releases.read_runtime_marker() is None


def test_read_runtime_marker_without_version_or_started_at_is_absent(marker_path):
    marker_path.write_text('{"pid": 1, "frozen": true}', encoding="utf-8")
    assert releases.read_runtime_marker() is None, (
        "без version/started_at сверить нечего — запись считается отсутствующей")


# ==========================================================================================
# GAP-447.1: _reconcile_restart_required — снятие вечной плашки
# ==========================================================================================
def _state_with_current(tmp_path, *, installed_version: str, applied_at: str,
                        restart_required: bool = True) -> CompanionState:
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["restart_required"] = restart_required
    state.releases["current"] = {"version": installed_version, "applied_at": applied_at}
    return state


def test_reconcile_no_marker_keeps_flag_and_clears_running_version(marker_path, tmp_path):
    state = _state_with_current(tmp_path, installed_version="1.1.90",
                                applied_at="2026-09-21T10:00:00Z")
    releases._reconcile_restart_required(state)
    assert state.releases["restart_required"] is True, (
        "маркера нет — снимать плашку нечем, флаг обязан остаться True")
    assert state.releases["running_version"] is None
    assert state.releases["running_started_at"] is None


def test_reconcile_marker_older_than_applied_at_keeps_flag(marker_path, tmp_path):
    """Маркер СТАРШЕ подмены — это процесс, поднятый ДО обновления. Совпадение версии
    было бы случайным (плейсхолдер/старый бинарь с тем же номером), а не доказательством
    перезапуска."""
    _write_marker(marker_path, version="1.1.90", started_at="2026-09-21T09:59:00Z")
    state = _state_with_current(tmp_path, installed_version="1.1.90",
                                applied_at="2026-09-21T10:00:00Z")
    releases._reconcile_restart_required(state)
    assert state.releases["restart_required"] is True
    assert state.releases["running_version"] == "1.1.90", (
        "running_version — информационное поле независимо от того, снят ли флаг")


def test_reconcile_marker_fresh_and_version_matches_clears_flag(marker_path, tmp_path):
    _write_marker(marker_path, version="1.1.90", started_at="2026-09-21T10:05:00Z")
    state = _state_with_current(tmp_path, installed_version="1.1.90",
                                applied_at="2026-09-21T10:00:00Z")
    releases._reconcile_restart_required(state)
    assert state.releases["restart_required"] is False, (
        "маркер свежий (после applied_at) и версия совпала — перезапуск подтверждён")
    assert state.releases["running_version"] == "1.1.90"
    assert state.releases["running_started_at"] == "2026-09-21T10:05:00Z"


def test_reconcile_marker_fresh_but_different_version_keeps_flag(marker_path, tmp_path):
    """Ровно сценарий обращения клиента 21.09.2026 (LIC-2026-1B9C9123): сервер честно
    перезапустился (маркер свежее applied_at), но поднялся на СТАРОЙ версии —
    `product_version 1.1.1` при установленном бинаре 1.1.90. Плашка обязана остаться:
    обновление реально не подействовало, и снять флаг здесь значило бы соврать."""
    _write_marker(marker_path, version="1.1.1", started_at="2026-09-21T10:05:00Z")
    state = _state_with_current(tmp_path, installed_version="1.1.90",
                                applied_at="2026-09-21T10:00:00Z")
    releases._reconcile_restart_required(state)
    assert state.releases["restart_required"] is True
    assert state.releases["running_version"] == "1.1.1", (
        "хабу есть что показать: сейчас работает 1.1.1, а установлена 1.1.90")


def test_reconcile_never_raises_flag_on_its_own(marker_path, tmp_path):
    """Маркер свежий и версия совпадает, но плашки не было — reconcile НЕ поднимает флаг
    сам: поднимает его только сама подмена (apply_staged/rollback)."""
    _write_marker(marker_path, version="1.1.90", started_at="2026-09-21T10:05:00Z")
    state = _state_with_current(tmp_path, installed_version="1.1.90",
                                applied_at="2026-09-21T10:00:00Z", restart_required=False)
    releases._reconcile_restart_required(state)
    assert state.releases["restart_required"] is False


# ==========================================================================================
# GAP-442: release_notes/known_issues из GET /v1/version/latest — попутно проверке релиза
# ==========================================================================================
def test_check_degrades_gracefully_when_version_endpoint_unavailable(marker_path, tmp_path):
    """Обычный FakeClient релизов не знает пути `/v1/version/latest` (падает
    AssertionError на неожиданном запросе) — ИМЕННО ЭТИМ и проверяется деградация: любой
    сбой попутного запроса поглощается, а сама проверка обновления отрабатывает как
    обычно."""
    client, _blob, _meta = _client(version="0.307.0")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    result = releases.check(client, state, ctx)

    assert result["available"] is True, (
        "недоступность /v1/version/latest не имеет права уронить проверку обновления")
    assert state.releases["release_notes"] == []
    assert state.releases["known_issues"] == []
    assert state.releases["release_notes_version"] is None


class _VersionAwareClient:
    """Обёртка над FakeClient релизов, которая ДОПОЛНИТЕЛЬНО отвечает на
    `GET /v1/version/latest` (GAP-442) — остальные пути делегируются оригиналу без
    изменений, поэтому существующий FakeClient не трогается."""

    def __init__(self, inner, version_payload: dict) -> None:
        self._inner = inner
        self.version_payload = dict(version_payload)
        self.version_calls: list = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def head(self, *args, **kwargs):
        return self._inner.head(*args, **kwargs)

    def get_json(self, path, *, params=None, authorized=True, etag=None):
        if path == releases._VERSION_INFO_PATH:
            self.version_calls.append({"params": dict(params or {}), "authorized": authorized})
            return dict(self.version_payload), {}
        return self._inner.get_json(path, params=params, authorized=authorized, etag=etag)

    def download(self, *args, **kwargs):
        return self._inner.download(*args, **kwargs)


def test_check_fetches_release_notes_and_known_issues(marker_path, tmp_path):
    inner, _blob, _meta = _client(version="0.307.0")
    client = _VersionAwareClient(inner, {
        "latest": "0.307.0",
        "min_supported": "0.1.0",
        "release_notes": ["Ускорена синхронизация паттернов", "Починена докачка релиза"],
        "known_issues": ["Известна проблема с прокси в корпоративной сети"],
    })
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    releases.check(client, state, ctx)

    assert state.releases["release_notes_version"] == "0.307.0"
    assert state.releases["release_notes"] == [
        "Ускорена синхронизация паттернов", "Починена докачка релиза"]
    assert state.releases["known_issues"] == [
        "Известна проблема с прокси в корпоративной сети"]
    # Эндпоинт публичный (без лицензии тоже должен отвечать) — запрос идёт БЕЗ авторизации.
    assert client.version_calls, "канал обязан был сходить за /v1/version/latest"
    assert client.version_calls[0]["authorized"] is False
    assert client.version_calls[0]["params"] == {"current": "0.300.0"}, (
        "known_issues сервер фильтрует по УСТАНОВЛЕННОЙ версии — она обязана уйти "
        "параметром current")


def test_check_summary_exposes_release_notes_via_state_summary(marker_path, tmp_path):
    inner, _blob, _meta = _client(version="0.307.0")
    client = _VersionAwareClient(inner, {
        "latest": "0.307.0", "min_supported": "0.1.0",
        "release_notes": ["Пункт состава обновления"], "known_issues": [],
    })
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"))

    releases.check(client, state, ctx)
    summary = state.summary()["releases"]

    assert summary["release_notes_version"] == "0.307.0"
    assert summary["release_notes"] == ["Пункт состава обновления"]
    assert summary["known_issues"] == []


# ==========================================================================================
# GAP-437: честные состояния канала паттернов
# ==========================================================================================
def test_seed_override_root_reports_shipped_count_even_when_skipped(tmp_path):
    ctx, _state, override = make_env(tmp_path)
    shipped = ctx.shipped_patterns_root

    first = pm.seed_override_root(shipped, override)
    assert first["skipped"] is False
    assert first["shipped_count"] == 4, (
        "поставочный корень фикстуры make_shipped несёт index + js_ui + csharp + "
        "snippets/esq.md — 4 файла")

    second = pm.seed_override_root(shipped, override)
    assert second["skipped"] is True
    assert second["shipped_count"] == 4, (
        "пропуск seed'а на обычном тике (индекс уже валиден) не должен терять размер "
        "поставочной базы — иначе счётчик пропадал бы после первого же тика")


def test_seed_override_root_unknown_shipped_root_gives_none_not_zero(tmp_path):
    override = tmp_path / "override"
    override.mkdir()
    result = pm.seed_override_root("", override)
    assert result["shipped_count"] is None, (
        "не смогли посчитать честно — None, а не 0: ноль читался бы как «база пуста»")


def test_sync_stores_shipped_count_and_total_available(tmp_path):
    ctx, state, _override = make_env(tmp_path)
    client = PatternsFakeClient([page([pattern(1, title="Поле на карточке",
                                               body="## Задача\nТекст решения.\n")])])

    pm.sync(client, state, ctx, CompanionSettings())
    summary = state.summary()["patterns"]

    assert summary["shipped_count"] == 4
    assert summary["applied_count"] == 1
    assert summary["total_available"] == 5, (
        "фактически доступная база — поставочная + применённая дельта, а не только "
        "дельта канала")


def test_summary_total_available_is_none_when_shipped_count_unknown(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    summary = state.summary()["patterns"]
    assert summary["shipped_count"] is None
    assert summary["total_available"] is None, (
        "размер поставочной базы ещё не посчитан ни разу — счётчик не показывается "
        "вовсе, а не 0")


def test_empty_delta_after_seed_is_ok_not_never_synced(tmp_path):
    """GAP-437: у издателя нет новых паттернов — статус `ok`, `last_run_at` заполнен, а
    уже применённые паттерны остаются на месте (пустая дельта ничего не стирает). Именно
    эти три факта отличают «новых нет» от «ни разу не синхронизировались» в
    `renderPatternsRow` (standkit_hub/web/app.js)."""
    ctx, state, _override = make_env(tmp_path)
    client = PatternsFakeClient([page([pattern(1, title="Поле на карточке",
                                               body="## Задача\nТекст решения.\n")])])
    pm.sync(client, state, ctx, CompanionSettings())
    assert state.patterns["last_status"] == "ok"
    first_run_at = state.patterns["last_run_at"]
    assert first_run_at is not None

    empty_client = PatternsFakeClient([page([], has_more=False)])
    pm.sync(empty_client, state, ctx, CompanionSettings())

    assert state.patterns["last_status"] == "ok", (
        "пустая дельта — успех, а не ошибка и не повод показывать «не синхронизировались»")
    assert state.patterns["last_run_at"] is not None
    assert len(state.patterns["applied"]) == 1, (
        "ранее применённый паттерн не должен исчезать из-за пустой страницы дельты")


def test_never_synced_state_has_no_last_run_at(tmp_path):
    """Контрольный случай для сравнения с предыдущим тестом: свежее состояние канала
    (ни разу не тикал) не имеет `last_run_at` — это и есть данные для состояния «первая
    синхронизация ещё не проходила», отдельного от «новых нет» (GAP-437)."""
    state = CompanionState(tmp_path / "companion-state.json")
    assert state.patterns["last_run_at"] is None
    assert state.summary()["patterns"]["applied_count"] == 0


# ==========================================================================================
# Лёгкие текстовые guard'ы на UI (тот же приём, что test_hub_companion_api.py: app.js/
# index.html/style.css — обычный текст, полноценного DOM-рендера в наборе нет).
# ==========================================================================================
def _web_dir() -> Path:
    import standkit_hub.server as server_module
    return Path(server_module.__file__).parent / "web"


def test_ui_has_four_distinct_pattern_row_states():
    """GAP-528: окно переверстано по компактному макету («иконка | название +
    статус-строка | кнопки» — updates_mockup_v2.html), и два прежних текстовых
    состояния «новых нет»/«дельта применена» слились в одну строку с чипом
    «актуально» — макет владельца не оставляет места под абзац-объяснение. Тест
    по-прежнему держит различимость состояний «остановлен», «ни разу не
    отрабатывал» и «отработал» (см. renderPatternsRow), просто по новым
    текстам/чипу, а не по старой дословной фразе."""
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "Первая синхронизация паттернов ещё не проходила" in js
    assert "Синхронизация паттернов остановлена" in js
    # Состояние «отработал успешно» — единая строка с чипом «актуально», а не
    # прежний развёрнутый текст (см. renderPatternsRow, ветки 3/4).
    assert 'setChip("upd-patterns-chip", "ok", "актуально"' in js
    assert "· автоматически, ${whenChecked}" in js


def test_ui_restart_note_names_running_version_when_known():
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "running_version" in js, (
        "плашка перезапуска обязана читать версию РЕАЛЬНО работающего процесса")


def test_ui_has_whatsnew_spoiler_collapsed_by_default():
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    css = (_web_dir() / "style.css").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert '<details class="upd-whatsnew" id="upd-whatsnew" hidden>' in html, (
        "свёрнут по умолчанию — нативный <details> без атрибута open, и по умолчанию "
        "ещё и hidden (нечего показывать, пока не пришли данные)")
    assert ".upd-whatsnew" in css
    assert "renderWhatsNew" in js


# ==========================================================================================
# GAP-528 — переверстка окна «Обновления» по макету владельца (updates_mockup_v2.html):
# компактная сетка «иконка 34px | название + статус-строка | кнопки», один блок разметки
# на редакцию (`#upd-paid-rows` / `#upd-free-rows`), кнопки только когда есть что нажать.
# Те же текстовые guard'ы, что и остальной набор этого файла — без DOM-рендера.
# ==========================================================================================

def test_gap528_updates_button_is_always_visible():
    """Кнопка «Обновления» видна в обеих редакциях (GAP-528) — прежний `hidden`
    по умолчанию и его снятие только для платной редакции убраны."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    # Кнопка в разметке без атрибута hidden.
    assert '<button id="btn-updates" class="updates-btn" type="button" title="Обновления">' in html
    assert 'byId("btn-updates").hidden = !known' not in js, (
        "кнопка «Обновления» больше не прячется по лицензии — свободная редакция "
        "показывает свою карточку (self-version)")


def test_gap528_paid_and_free_row_containers_exist():
    """Разметка держит ОБА варианта окна: 4 канала (Companion) и одну строку
    «Диспетчер стендов» по self-version (свободная редакция), скрытые/показанные
    через app.js::applyUpdatesEditionView по факту ответа /api/companion/status."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert 'id="upd-paid-rows"' in html
    assert 'id="upd-free-rows" hidden' in html
    assert 'id="upd-self-current"' in html
    assert 'id="upd-self-chip"' in html
    assert "applyUpdatesEditionView" in js
    assert '"/api/hub/self-version"' in js
    assert '"/api/hub/self-version/check"' in js


def test_gap528_buttons_hidden_unless_actionable():
    """«Установить»/«Обновить скиллы» — только по делу (update_available), а не
    всегда доступной кнопкой (макет владельца убрал абзацы-объяснения, поэтому
    видимость кнопки — единственный сигнал «есть что поставить»)."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert '<button type="button" id="upd-install-btn" data-companion-action="apply_update" hidden>' in html
    assert "install.hidden = installerRequired || !hasNew" in js
    assert "applyBtn.hidden = !(hasNew && staged)" in js  # renderSkillsRow


def test_gap528_chip_helper_and_colors():
    """Чип статус-строки — общий хелпер setChip с тремя цветами (ok/new/err), а
    не разные ad-hoc бейджи на каждый канал; цвета берутся из переменных темы
    диспетчера (--bpmkit-ok/--bpmkit-primary/--bpmkit-down), не хардкодом макета."""
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    css = (_web_dir() / "style.css").read_text(encoding="utf-8")
    assert "function setChip(id, kind, text, title)" in js
    assert ".upd-chip.ok" in css and "var(--bpmkit-ok)" in css
    assert ".upd-chip.new" in css and "var(--bpmkit-primary)" in css
    assert ".upd-chip.err" in css and "var(--bpmkit-down)" in css


def test_gap528_skills_harnesses_joined_and_single_howto_link():
    """Список харнессов со скиллами — через « · » в статус-строке; ссылка на
    кукбук — ОДНА («как установить плагин и скиллы» → сводный раздел
    #plugin-skills кукбука BPMkit), правка владельца 24.09.2026."""
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    assert 'harnesses.map((c) => c.name || c.id).join(" · ")' in js
    assert 'id="upd-skills-howto-link"' in html
    assert '/bpmkit-cookbook#plugin-skills' in html
    assert 'upd-skills-harness-link' not in html and 'upd-skills-harness-link' not in js
    assert html.count('/bpmkit-cookbook#') >= 1


def test_gap528_version_fallback_fields_used_in_ui():
    """`current_version_source`/`installed_source` (фолбэк на running-версию,
    GAP-528 п.2а/2б) читаются фронтом и превращаются в подсказку title, а не
    теряются молча."""
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "rel.current_version_source" in js
    assert "skl.installed_source" in js
    assert "rel.update_available" in js
    assert "hub.update_available" in js
    assert "skl.update_available" in js


def test_gap528_updates_badge_includes_self_version():
    """Бейдж «есть что поставить» учитывает и self-version (свободная редакция и
    платная — диспетчер по PyPI), не только канал издателя."""
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "lastSelfVersion && lastSelfVersion.update_available" in js


def test_gap528_footer_and_lock_texts_from_mockup():
    """Подвал и плашка платных каналов — тексты владельца дословно."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    assert "Скачивание — заранее, установка — только по вашей кнопке" in html
    assert "Паттерны, обновления MCP и скиллов — в редакции с лицензией BPMkit" in html
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert "Проверяется по PyPI, без лицензии" in js


# ==========================================================================================
# GAP-528, продолжение — правки владельца после просмотра на живом хосте:
# чип своей колонкой, кнопка «Проверить» в шапке, кнопки по делу, «Перезапустить» убрана,
# копирование pip-команды и «Папка плагина» — иконками.
# ==========================================================================================

def test_gap528b_status_column_is_a_dedicated_grid_cell():
    """Чип каждой строки — в СВОЕЙ колонке грида (`.upd-status`), а не внутри
    `.upd-main` вместе с версией/метой: иначе колонка чипа «гуляет» вслед за
    длиной текста слева от него (жалоба владельца на живом хосте)."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    css = (_web_dir() / "style.css").read_text(encoding="utf-8")
    for chip_id in ("upd-patterns-chip", "upd-mcp-chip", "upd-hub-chip",
                    "upd-skills-chip", "upd-self-chip"):
        needle = f'<div class="upd-status">\n              <span class="upd-chip" id="{chip_id}"'
        assert needle in html, f"чип {chip_id} обязан лежать в своей колонке .upd-status"
    assert ".upd-status {" in css
    # 4 колонки фиксированной ширины — статус и действия не "плавают".
    assert "grid-template-columns: 34px minmax(0, 1fr) 112px 136px" in css


def test_gap528b_check_button_moved_to_header_no_footer():
    """Кнопка «Проверить обновления» — в шапке (рядом с «проверено N назад»),
    подвала `.modal-footer` у окна больше нет."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    overlay_start = html.index('id="updates-overlay"')
    overlay_html = html[overlay_start:html.index('id="license-crit-overlay"')]
    header_part, _, rest = overlay_html.partition('<div class="modal-body upd-body">')
    assert 'id="updates-check-btn"' in header_part, (
        "кнопка проверки обязана быть в шапке окна, до modal-body")
    assert 'class="modal-footer updates-footer"' not in overlay_html, (
        "подвал окна «Обновления» убран целиком"
    )


def test_gap528b_no_restart_button_in_updates_window():
    """Кнопки «Перезапустить» в окне «Обновления» больше нет ни у диспетчера
    (платная редакция), ни у self-version (свободная) — перезапуск живёт в
    настройках (`version-skew-restart-btn` и т.п.), а не в этом окне."""
    overlay_start_marker = 'id="updates-overlay"'
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    overlay_html = html[html.index(overlay_start_marker):
                         html.index('id="license-crit-overlay"')]
    assert "upd-hub-restart-btn" not in overlay_html
    assert "upd-self-restart-btn" not in overlay_html
    assert "data-hub-restart" not in overlay_html
    assert ">Перезапустить<" not in overlay_html


def test_gap528b_pip_copy_is_an_icon_button_always_visible_in_pip_mode():
    """Копирование pip-команды — иконка-кнопка 30×30 (не текст «⧉ pip»), лежит
    в колонке действий и не привязана к наличию новой версии: `hidden` в
    разметке (JS решает видимость по режиму hub/self, не по update_available)."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    for btn_id in ("upd-hub-pip-copy-btn", "upd-self-pip-copy-btn"):
        assert f'id="{btn_id}"' in html
        start = html.index(f'id="{btn_id}"')
        tag = html[html.rfind("<button", 0, start):html.index(">", start) + 1]
        assert "hidden" in tag, f"{btn_id} по умолчанию скрыта, показывает JS"
        assert "<svg" in html[start:start + 400], f"{btn_id} обязана быть SVG-иконкой"
        assert "⧉" not in html[start - 5:start], f"{btn_id} больше не текстовая кнопка"
    assert "Скопировать команду обновления: python -m pip install -U standkit" in html
    # Не завязано на конкретный <code>-контейнер по видимости — копия работает
    # ВСЕГДА в pip-режиме (см. renderHubRow/renderSelfVersionRow).
    assert "pipCopyBtn.hidden" in js or "PipCopyBtn.hidden" in js or \
        "hub-pip-copy-btn" in js


def test_gap528b_plugin_folder_button_is_icon_only():
    """«Папка плагина» — иконка-кнопка (SVG папки), не текстовая надпись, с
    title, называющим путь."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    assert ">Папка плагина<" not in html
    start = html.index('id="upd-skills-open-folder-btn"')
    tag_start = html.rfind("<button", 0, start)
    tag_end = html.index("</button>", start)
    button_html = html[tag_start:tag_end]
    assert "<svg" in button_html
    assert 'data-hub-open-folder="plugin"' in button_html
    assert "title=" in button_html


def test_gap528b_patterns_button_only_when_new_available():
    """«Загрузить новые» у паттернов — видна только когда канал сообщает
    `patterns.new_available` (см. `standkit_companion.state.CompanionState.summary`);
    в разметке кнопка по умолчанию `hidden`, JS решает по этому полю."""
    html = (_web_dir() / "index.html").read_text(encoding="utf-8")
    js = (_web_dir() / "app.js").read_text(encoding="utf-8")
    assert '<button type="button" class="secondary" id="upd-patterns-apply-btn" data-companion-action="sync_patterns" hidden>Загрузить новые</button>' in html
    assert "new_available" in js, (
        "renderPatternsRow обязан читать summary.patterns.new_available")


def test_gap528b_state_summary_exposes_new_available_field():
    """Бэкенд: `CompanionState.summary()["patterns"]["new_available"]` — поле
    для кнопки «Загрузить новые», отдельное от `applied_count`/`total_available`."""
    from standkit_companion.state import CompanionState
    state = CompanionState(_web_dir() / "нет-такого-файла.json")
    summary = state.summary()
    assert "new_available" in summary["patterns"]
    assert summary["patterns"]["new_available"] is False
