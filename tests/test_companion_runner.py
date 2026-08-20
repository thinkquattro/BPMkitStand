# -*- coding: utf-8 -*-
"""Тесты планировщика канала обновлений: `standkit_companion.runner` и его CLI.

Зачем файл. Планировщик — единственное место канала, где ошибка НИКОГДА не видна сразу:
цикл тикает раз в 30 минут (релизы — раз в сутки), в фоне, без человека рядом. Поэтому
каждое свойство, ради которого он написан именно так, закрыто отдельным регресс-тестом —
иначе его поломка обнаружится не сегодня, а через месяц как «обновления перестали
приезжать»:

* выключенный цикл — не ошибка и НЕ ходит в сеть (иначе «выключил» ничего не значит);
* ручное действие человека сильнее флага расписания, но не сильнее главного рубильника;
* исключение цикла не роняет пробуждение и не роняет поток — соседние циклы отрабатывают;
* не-retriable отказ гасит СВОЙ цикл до вмешательства (повтор 401 каждые 30 минут со
  стороны издателя неотличим от перебора ключа), а `poke()` блокировку снимает;
* порядок пробуждения — отзыв первым: при отозванной лицензии остальные циклы всё равно
  получат 401, и честнее показать причину, а не два невнятных отказа;
* `apply_staged` планировщиком не вызывается НИКОГДА (`SECURITY.md` §4.1) — подмена
  исполняемого файла только по команде человека;
* конверт лицензии не попадает в `status()` (ответ статуса уходит в браузер и в логи);
* джиттер ±10% считается заново на каждый срок (парк клиентов не должен приходить к
  издателю одной секундой), интервал перечитывается из конфига без перезапуска.

Сеть, диск издателя и CLI клиентского MCP не поднимаются: функции циклов подменяются
записывающими заглушками (`Recorder`), время — управляемыми часами, генератор джиттера —
сценарием. Живой поток стартует только там, где предмет теста — сам поток.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from standkit_companion import patterns, releases, revocations, runner
from standkit_companion.errors import ChannelError, ContextUnavailable
from standkit_companion.runner import (
    CompanionRunner,
    available_actions,
    build_runner,
    status_snapshot,
)
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionCycle, CompanionSettings

ENVELOPE = "BPMKIT1.SEKRETNYJ-KONVERT-LICENZII.podpis"


@pytest.fixture(autouse=True)
def _allow_background_thread(monkeypatch):
    """Снять общий рубильник `STANDKIT_COMPANION_DISABLED` из корневого `conftest.py`.

    Он выключает фоновый планировщик на всём прогоне, чтобы сотня тестов хаба не заводила
    настоящий канал на машине с живой лицензией. Но ЭТОТ файл тестирует сам планировщик —
    здесь поток обязан подниматься. Снимаем переменную точечно, а не убираем из conftest:
    иначе защита исчезнет для всех остальных файлов.
    """
    monkeypatch.delenv(runner.DISABLE_ENV, raising=False)



# --------------------------------------------------------------------------------------
# Подставные соседи
# --------------------------------------------------------------------------------------
class Clock:
    """Управляемые монотонные часы: сроки планировщика обязаны быть проверяемы точно,
    а не «примерно, если тест не тормознул»."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class ScriptedRandom:
    """Генератор джиттера по сценарию: `uniform(a, b)` берёт очередную ДОЛЮ отрезка.

    Возвращать заранее заданное число нельзя: раннер зовёт `uniform` с разными отрезками
    (сдвиг интервала ±10% и разброс первого тика), и доля — единственная форма, годная для
    обоих вызовов.
    """

    def __init__(self, fractions) -> None:
        self.fractions = list(fractions)
        self.calls = 0

    def uniform(self, low: float, high: float) -> float:
        fraction = self.fractions[self.calls % len(self.fractions)]
        self.calls += 1
        return low + (high - low) * fraction


class FakeContext:
    """Лицензионный контекст: каналу нужны только адреса и пути (duck-typing).

    Конверт здесь ЗАВЕДОМО узнаваемая строка — тест «конверта нет в статусе» ищет её
    буквально в сериализованном ответе.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.envelope = ENVELOPE
        self.license_status = "active"
        self.backend_url = "https://backend.example"
        self.mcp_version = "0.305.0"
        self.package_root = str(tmp_path / "mcp")
        self.shipped_patterns_root = str(tmp_path / "mcp" / "references")
        self.override_patterns_root = str(tmp_path / "override")
        self.patterns_env_registered = True
        self.revocations_target = str(tmp_path / "revocations.json")
        self.revocations_env_registered = True
        self.artifact_pubkey = ""
        self.binary_path = str(tmp_path / "mcp" / "bpmkit.exe")
        self.cli = [str(tmp_path / "mcp" / "bpmkit.exe")]
        self.raw = {}


class FakeClient:
    """Транспорт-пустышка: до реальных запросов в этих тестах дело не доходит."""

    def __init__(self) -> None:
        self.calls: list = []


class Recorder:
    """Заглушка функции цикла: журналирует вызовы, при необходимости бросает.

    `raises` — список исходов по порядку вызовов (`None` = успех). Именно список, а не
    один флаг: половина тестов проверяет ВТОРОЙ вызов (после блокировки, после `poke`).
    """

    def __init__(self, name: str, log: list, *, raises=None, result=None) -> None:
        self.name = name
        self.log = log
        self.raises = list(raises or [])
        self.result = result if result is not None else {"cycle": name}
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.log.append(self.name)
        if self.raises:
            exc = self.raises.pop(0)
            if exc is not None:
                raise exc
        return dict(self.result)


def settings_all_on(**overrides) -> CompanionSettings:
    """Настройки со ВСЕМИ включёнными циклами — база большинства тестов.

    Конструктор, а не `from_dict`: тестам нужны короткие интервалы, а `from_dict`
    справедливо поднимает их до минимумов (5 минут / час).
    """
    values = {
        "enabled": True,
        "patterns": CompanionCycle(enabled=True, interval_sec=1800),
        "releases": CompanionCycle(enabled=True, interval_sec=86400),
        "revocations": CompanionCycle(enabled=True, interval_sec=1800),
        "auto_stage_release": False,
    }
    values.update(overrides)
    return CompanionSettings(**values)


def make_runner(tmp_path: Path, settings, *, clock=None, rng=None,
                context_resolver=None, client_factory=None) -> CompanionRunner:
    """Раннер на подставных соседях. Настройки читаются из ЯЧЕЙКИ (list), чтобы тест мог
    подменить их на лету — ровно так же, как это делает пользователь в UI."""
    cell = settings if isinstance(settings, list) else [settings]
    return CompanionRunner(
        tmp_path / "standkit-hub.json",
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: cell[0],
        context_resolver=context_resolver or (lambda s: FakeContext(tmp_path)),
        client_factory=client_factory or (lambda ctx, s: FakeClient()),
        monotonic=clock or Clock(),
        rng=rng if rng is not None else ScriptedRandom([0.0]),
    )


def patch_cycles(monkeypatch, log, *, patterns_stub=None, releases_check=None,
                 releases_stage=None, revocations_stub=None):
    """Подмена всех трёх функций циклов сразу.

    Подменяются АТРИБУТЫ модулей (`patterns.sync` и т.д.), потому что раннер зовёт их
    именно так — через модуль, а не по сохранённой ссылке. Это же и проверяется: сохрани
    он ссылку при импорте, ни один из этих тестов не увидел бы своей заглушки.
    """
    stubs = {
        "patterns_sync": patterns_stub or Recorder("patterns", log),
        "releases_check": releases_check or Recorder(
            "releases", log, result={"available": False, "target": "latest"}),
        "releases_stage": releases_stage or Recorder("stage", log),
        "revocations_refresh": revocations_stub or Recorder("revocations", log),
    }
    monkeypatch.setattr(patterns, "sync", stubs["patterns_sync"])
    monkeypatch.setattr(releases, "check", stubs["releases_check"])
    monkeypatch.setattr(releases, "stage", stubs["releases_stage"])
    monkeypatch.setattr(revocations, "refresh", stubs["revocations_refresh"])
    return stubs


# --------------------------------------------------------------------------------------
# 1. Выключенный цикл
# --------------------------------------------------------------------------------------
def test_disabled_cycle_is_status_disabled_and_makes_no_calls(tmp_path, monkeypatch):
    """Выключенный цикл — не ошибка и НЕ повод ходить в сеть.

    Проверяется не только статус, но и то, что лицензионный контекст даже не резолвился:
    резолв — это ЗАПУСК процесса CLI клиентского MCP, и делать его ради цикла, который
    пользователь выключил, значит платить за выключенную функцию.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    resolved: list = []

    def spy_resolver(settings_arg):
        resolved.append(settings_arg)
        return FakeContext(tmp_path)

    settings = settings_all_on(patterns=CompanionCycle(enabled=False, interval_sec=1800))
    runner_obj = make_runner(tmp_path, settings, context_resolver=spy_resolver)

    outcome = runner_obj.run_cycle("patterns")

    assert outcome["status"] == "disabled", "выключенный цикл обязан быть 'disabled', не ошибкой"
    assert outcome["error"] is None, "выключение пользователем — не отказ канала"
    assert stubs["patterns_sync"].calls == 0, "выключенный цикл не должен работать"
    assert resolved == [], "выключенный цикл не должен запускать CLI за лицензией"


# --------------------------------------------------------------------------------------
# 2. force
# --------------------------------------------------------------------------------------
def test_force_runs_disabled_cycle(tmp_path, monkeypatch):
    """`force=True` прогоняет выключенный цикл: ручное действие сильнее РАСПИСАНИЯ.

    Решение осознанное: `enabled` отвечает на вопрос «ходить ли по таймеру», а не «можно ли
    вообще». Безопасность релизов держится не на этом флаге, а на fail-closed проверке
    подписи внутри `releases.stage`, которую `force` не отключает и отключить не может.
    Без этого кнопка «Синхронизировать сейчас» в UI молча ничего бы не делала у всех, кто
    выключил расписание.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    settings = settings_all_on(patterns=CompanionCycle(enabled=False, interval_sec=1800))
    runner_obj = make_runner(tmp_path, settings)

    outcome = runner_obj.run_cycle("patterns", force=True)

    assert outcome["status"] == "ok", "ручной прогон выключенного цикла обязан выполниться"
    assert stubs["patterns_sync"].calls == 1


def test_force_does_not_override_master_switch(tmp_path, monkeypatch):
    """А вот главный рубильник `enabled=False` `force` НЕ перебивает.

    «Канал выключен» обязано означать ровно это: иначе выключить канал по-настоящему
    нельзя, и любая кнопка UI оставалась бы дырой в решении пользователя.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    settings = settings_all_on(enabled=False)
    runner_obj = make_runner(tmp_path, settings)

    outcome = runner_obj.run_cycle("patterns", force=True)

    assert outcome["status"] == "disabled"
    assert stubs["patterns_sync"].calls == 0, "выключенный целиком канал не работает и по кнопке"


# --------------------------------------------------------------------------------------
# 3. Исключение цикла
# --------------------------------------------------------------------------------------
def test_exception_in_cycle_does_not_break_the_pass(tmp_path, monkeypatch):
    """Незнакомое исключение одного цикла не отменяет остальные и приводится к типу.

    `RuntimeError` из недр цикла (битый диск, чужая библиотека) обязан стать таким же
    типизированным исходом, как отказ бэкенда: у UI один разбор ошибок, а не два.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log, revocations_stub=Recorder(
        "revocations", log, raises=[RuntimeError("диск отвалился")]))
    runner_obj = make_runner(tmp_path, settings_all_on())

    report = runner_obj.run_due()

    assert report["results"]["revocations"]["status"] == "error"
    assert report["results"]["revocations"]["error"]["kind"] == "unknown", (
        "непредвиденный сбой обязан приезжать типизированным, а не голой строкой")
    assert report["results"]["patterns"]["status"] == "ok", (
        "сбой одного цикла не отменяет соседние — они независимы")
    assert stubs["patterns_sync"].calls == 1
    assert stubs["releases_check"].calls == 1


def test_background_thread_survives_failing_cycle(tmp_path, monkeypatch):
    """Тот же случай, но на ЖИВОМ потоке: поток обязан пережить падение тика.

    Здесь используются настоящие часы и нулевой разброс старта (сценарный генератор),
    чтобы первый тик случился сразу; предмет теста — не расписание, а живучесть потока.
    """
    log: list = []
    ticked = threading.Event()

    def boom(*args, **kwargs):
        log.append("revocations")
        ticked.set()
        raise RuntimeError("падаем на каждом тике")

    patch_cycles(monkeypatch, log, revocations_stub=boom)
    runner_obj = CompanionRunner(
        tmp_path / "standkit-hub.json",
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: settings_all_on(),
        context_resolver=lambda s: FakeContext(tmp_path),
        client_factory=lambda ctx, s: FakeClient(),
        rng=ScriptedRandom([0.0]),
    )
    runner_obj.start()
    try:
        assert ticked.wait(3.0), "фоновый поток не выполнил ни одного тика"
        time.sleep(0.05)
        assert runner_obj.is_running(), "поток умер от исключения внутри цикла"
        assert runner_obj.status()["cycles"]["revocations"]["last_status"] == "error"
    finally:
        runner_obj.stop(timeout=2.0)


# --------------------------------------------------------------------------------------
# 4. Не-retriable отказ
# --------------------------------------------------------------------------------------
def test_non_retriable_error_halts_only_its_cycle_until_poke(tmp_path, monkeypatch):
    """Отзыв лицензии гасит СВОЙ цикл до вмешательства; `poke()` блокировку снимает.

    Повторять запрос с отозванным ключом каждые 30 минут бессмысленно (без действий
    человека ответ не изменится) и вредно: со стороны издателя поток одинаковых 401 с
    одним ключом неотличим от перебора. При этом соседние циклы обязаны продолжать
    работать — блокировка персональная.
    """
    log: list = []
    revoked = ChannelError("лицензия отозвана", kind="revoked")
    stubs = patch_cycles(monkeypatch, log, patterns_stub=Recorder(
        "patterns", log, raises=[revoked]))
    clock = Clock()
    runner_obj = make_runner(tmp_path, settings_all_on(), clock=clock)

    first = runner_obj.run_due()
    assert first["results"]["patterns"]["status"] == "error"
    assert first["results"]["patterns"]["halted"] is True, "не-retriable отказ обязан гасить цикл"
    assert first["results"]["revocations"]["status"] == "ok", "гаснет только свой цикл"

    clock.advance(100_000)  # заведомо больше любого интервала
    second = runner_obj.run_due()
    assert second["results"]["patterns"]["status"] == "halted"
    assert stubs["patterns_sync"].calls == 1, "заблокированный цикл не должен повторять запрос"
    assert stubs["revocations_refresh"].calls == 2, "соседний цикл продолжает тикать"

    runner_obj.poke("patterns")
    third = runner_obj.run_due()
    assert third["results"]["patterns"]["status"] == "ok", "poke() обязан снимать блокировку"
    assert stubs["patterns_sync"].calls == 2


def test_retriable_error_does_not_halt_cycle(tmp_path, monkeypatch):
    """Retriable-отказ (бэкенд недоступен) блокировку НЕ ставит — просто ждёт срока.

    Обратная половина предыдущего правила: спутать эти два случая значит либо навсегда
    погасить канал из-за минутного обрыва связи, либо вечно долбить издателя отозванным
    ключом.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log, patterns_stub=Recorder(
        "patterns", log, raises=[ChannelError("нет связи", kind="offline")]))
    clock = Clock()
    runner_obj = make_runner(tmp_path, settings_all_on(), clock=clock)

    first = runner_obj.run_due()
    assert first["results"]["patterns"]["status"] == "error"
    assert first["results"]["patterns"]["halted"] is False

    clock.advance(100_000)
    second = runner_obj.run_due()
    assert second["results"]["patterns"]["status"] == "ok"
    assert stubs["patterns_sync"].calls == 2, (
        "retriable-отказ обязан повториться на следующем сроке")


def test_settings_change_clears_halt(tmp_path, monkeypatch):
    """Правка настроек снимает блокировку: человек чинил ровно то, на что жаловались.

    Без этого пользователь, вписавший новый лицензионный ключ, ждал бы эффекта до
    перезапуска хаба — то есть «ввёл ключ, и ничего не поменялось».
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log, patterns_stub=Recorder(
        "patterns", log, raises=[ChannelError("лицензия отозвана", kind="revoked")]))
    clock = Clock()
    cell = [settings_all_on()]
    runner_obj = make_runner(tmp_path, cell, clock=clock)

    runner_obj.run_due()
    assert runner_obj.status()["cycles"]["patterns"]["halted"] is True

    cell[0] = settings_all_on(mcp_cli=r"C:\BPMkit\server\bpmkit.exe")
    clock.advance(100_000)
    report = runner_obj.run_due()

    assert report["results"]["patterns"]["status"] == "ok"
    assert stubs["patterns_sync"].calls == 2


# --------------------------------------------------------------------------------------
# 5. Порядок пробуждения
# --------------------------------------------------------------------------------------
def test_cycle_order_in_one_wakeup(tmp_path, monkeypatch):
    """Отзыв → паттерны → релизы, именно в этом порядке.

    Отзыв первым намеренно: если лицензия отозвана, два других цикла всё равно получат
    401, и пользователю честнее увидеть настоящую причину, а не два невнятных отказа
    перед ней.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    runner_obj = make_runner(tmp_path, settings_all_on())

    report = runner_obj.run_due()

    assert log == ["revocations", "patterns", "releases"], (
        "порядок циклов в одном пробуждении изменился — отзыв обязан идти первым")
    assert report["order"] == ["revocations", "patterns", "releases"]
    assert list(report["results"]) == ["revocations", "patterns", "releases"]


# --------------------------------------------------------------------------------------
# 6-7. Релизы: стейджинг по настройке, применение — никогда
# --------------------------------------------------------------------------------------
def test_auto_stage_release_off_does_not_download(tmp_path, monkeypatch):
    """`auto_stage_release=False` — только проверка, файл не качается.

    Дефолт именно такой: скачивание десятков мегабайт без спроса — не то, что пользователь
    ожидает от «проверять обновления».
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "1.2.3"}))
    runner_obj = make_runner(tmp_path, settings_all_on(auto_stage_release=False))

    runner_obj.run_cycle("releases")

    assert stubs["releases_check"].calls == 1
    assert stubs["releases_stage"].calls == 0, "без настройки канал не качает бинарь"


def test_auto_stage_release_on_downloads_but_does_not_apply(tmp_path, monkeypatch):
    """`auto_stage_release=True` — качает и проверяет, но останавливается перед подменой."""
    log: list = []
    stubs = patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "1.2.3"}))
    runner_obj = make_runner(tmp_path, settings_all_on(auto_stage_release=True))

    outcome = runner_obj.run_cycle("releases")

    assert stubs["releases_stage"].calls == 1, "с настройкой канал обязан подготовить обновление"
    assert outcome["result"]["staged"] is not None
    assert log == ["releases", "stage"], "стейджинг идёт ПОСЛЕ проверки, а не вместо неё"


@pytest.mark.parametrize("auto_stage", [False, True])
def test_scheduler_never_applies_update(tmp_path, monkeypatch, auto_stage):
    """ЗАПРЕТ: Планировщик не вызывает `apply_staged` ни при каких настройках.

    `SECURITY.md` §4.1 «никакого тихого действия»: подмена исполняемого файла MCP —
    всегда явное решение человека. Тест подменяет `apply_staged` на бросающую функцию,
    поэтому ЛЮБОЕ обращение к ней провалит прогон, а не будет замечено «по счётчику».
    """
    log: list = []
    patch_cycles(monkeypatch, log, releases_check=Recorder(
        "releases", log, result={"available": True, "target": "1.2.3"}))

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "планировщик вызвал apply_staged — тихая подмена исполняемого файла MCP "
            "запрещена SECURITY.md §4.1")

    monkeypatch.setattr(releases, "apply_staged", forbidden)
    runner_obj = make_runner(tmp_path, settings_all_on(auto_stage_release=auto_stage))

    report = runner_obj.run_due()

    assert report["results"]["releases"]["status"] == "ok"
    assert "apply" not in log


# --------------------------------------------------------------------------------------
# 8. Конверт в статусе
# --------------------------------------------------------------------------------------
def test_status_never_contains_license_envelope(tmp_path, monkeypatch):
    """Конверта лицензии в `status()` нет ни в каком виде.

    Проверка буквальная — поиск подстроки в сериализованном ответе: статус уходит по HTTP
    в браузер и оседает в логах хаба, и «случайно попавшее» поле контекста утащило бы туда
    лицензионный ключ. Заодно убеждаемся, что путь к CLI в ответе ЕСТЬ: он нужен для
    диагностики и секретом не является.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    runner_obj = make_runner(tmp_path, settings_all_on())
    runner_obj.run_due()  # контекст резолвится и запоминается — самый опасный момент

    snapshot = runner_obj.status()
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert ENVELOPE not in serialized, "конверт лицензии просочился в статус канала"
    assert "SEKRETNYJ" not in serialized, "фрагмент конверта просочился в статус канала"
    assert snapshot["context"]["ok"] is True
    assert snapshot["context"]["cli"], "путь к CLI в статусе нужен для диагностики"
    assert snapshot["edition"] == "companion"
    assert set(snapshot["cycles"]) == set(runner.CYCLES)


def test_status_reports_context_failure_without_killing_cycles(tmp_path, monkeypatch):
    """Нет CLI рядом — каждый цикл честно помечен `context_unavailable`, поток жив.

    Это retriable-отказ: пользователь может поставить MCP в любой момент, и гасить канал
    навсегда из-за его отсутствия нельзя.
    """
    log: list = []
    stubs = patch_cycles(monkeypatch, log)

    def no_cli(settings):
        raise ContextUnavailable("рядом нет CLI", kind="context_unavailable")

    runner_obj = make_runner(tmp_path, settings_all_on(), context_resolver=no_cli)
    report = runner_obj.run_due()

    for cycle in runner.CYCLES:
        assert report["results"][cycle]["error"]["kind"] == "context_unavailable"
        assert report["results"][cycle]["halted"] is False, "отсутствие CLI — поправимо"
    assert stubs["patterns_sync"].calls == 0
    assert runner_obj.status()["context"]["ok"] is False


# --------------------------------------------------------------------------------------
# 9. Жизненный цикл потока
# --------------------------------------------------------------------------------------
def test_start_is_idempotent_and_stop_is_quick(tmp_path, monkeypatch):
    """Поток стартует, повторный `start()` — no-op, `stop()` укладывается в таймаут.

    Ожидание в потоке — на `Condition` шагами не длиннее `MAX_WAIT_STEP_SEC`: со
    `sleep(interval)` на сутки этот тест ждал бы сутки, а закрытие хаба висло бы.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    runner_obj = CompanionRunner(
        tmp_path / "standkit-hub.json",
        state_path=tmp_path / "companion-state.json",
        settings_loader=lambda: settings_all_on(),
        context_resolver=lambda s: FakeContext(tmp_path),
        client_factory=lambda ctx, s: FakeClient(),
        rng=ScriptedRandom([0.0]),
    )
    assert runner_obj.is_running() is False

    runner_obj.start()
    first_thread = runner_obj._thread
    runner_obj.start()
    try:
        assert runner_obj.is_running() is True
        assert runner_obj._thread is first_thread, "повторный start() поднял второй поток"
        assert first_thread.daemon is True, "поток обязан быть daemon: он не держит процесс"
        assert runner_obj.status()["running"] is True
    finally:
        started = time.perf_counter()
        runner_obj.stop(timeout=2.0)
        elapsed = time.perf_counter() - started

    assert runner_obj.is_running() is False
    assert elapsed < 2.0, (
        f"stop() не уложился в таймаут ({elapsed:.2f} с) — значит поток спит длинными шагами")


def test_stop_without_start_is_safe(tmp_path):
    """`stop()` на неподнятом раннере — no-op: `server_close()` хаба зовёт его безусловно."""
    runner_obj = make_runner(tmp_path, settings_all_on())
    runner_obj.stop(timeout=0.1)
    assert runner_obj.is_running() is False


# --------------------------------------------------------------------------------------
# 10. Джиттер
# --------------------------------------------------------------------------------------
def test_jitter_is_within_ten_percent_and_changes_between_ticks(tmp_path, monkeypatch):
    """Срок гуляет в пределах ±10% и пересчитывается на КАЖДОМ тике.

    Джиттер нужен не «для красоты»: без него парк клиентов, обновлённый разом, приходит к
    единственному синхронному воркеру бэкенда издателя в одну и ту же секунду — и делает
    это вечно, потому что интервал фиксированный и расхождение не накапливается. Один
    сдвиг, вычисленный при старте, задачу не решает: он лишь переставит одинаковые тики на
    одинаковое новое место, поэтому проверяется именно РАЗЛИЧИЕ между тиками.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    clock = Clock()
    # Доли отрезка (-10%..+10%): 0.0 → -10%, 1.0 → +10%, 0.5 → номинал.
    runner_obj = make_runner(tmp_path, settings_all_on(), clock=clock,
                             rng=ScriptedRandom([0.0, 1.0, 0.5]))
    interval = 1800

    delays = []
    for _ in range(3):
        # Именно `run_cycle`, а не `run_due`: пробуждение планирует ВСЕ три цикла, и доли
        # сценарного генератора разошлись бы между ними, а тест — про сроки одного цикла.
        runner_obj.run_cycle("patterns")
        delays.append(runner_obj._runtime["patterns"].delay)
        clock.advance(100_000)

    for delay in delays:
        assert abs(delay - interval) <= interval * runner.JITTER_FRACTION + 1e-6, (
            f"сдвиг {delay} вышел за ±10% от интервала {interval}")
    assert len(set(delays)) > 1, "джиттер обязан пересчитываться на каждый срок, а не один раз"
    assert delays[0] < interval < delays[1], "сдвиг обязан работать в обе стороны"


def test_default_jitter_seed_is_stable_for_the_machine(tmp_path):
    """Зерно генератора стабильно между запусками и различается между машинами.

    Стабильность здесь — суть механизма: если бы фаза сбрасывалась при каждом старте,
    разброс парка обнулялся бы ровно в момент массового обновления, ради которого он и
    заведён. Встроенный `hash()` для этого не годится (рандомизирован по процессу), и тест
    поймает возврат к нему.
    """
    first = [runner._stable_seed(tmp_path / "standkit-hub.json") for _ in range(2)]
    other = runner._stable_seed(tmp_path / "other" / "standkit-hub.json")

    assert first[0] == first[1], "зерно джиттера обязано быть одинаковым при каждом расчёте"
    assert first[0] != other, "разные установки обязаны получать разную фазу"


# --------------------------------------------------------------------------------------
# 11. Перечитывание настроек
# --------------------------------------------------------------------------------------
def test_interval_change_is_picked_up_without_restart(tmp_path, monkeypatch):
    """Правка `interval_sec` в конфиге подхватывается со следующего тика.

    Настройки читаются ПЕРЕД каждым тиком, а не запоминаются при старте: пользователь
    меняет период в UI и ждёт эффекта, а не перезапуска хаба. Тест намеренно ходит через
    НАСТОЯЩИЙ загрузчик конфига (файл на диске) — подставной вернул бы что угодно и ничего
    бы не доказал.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    config_path = tmp_path / "standkit-hub.json"

    def write_config(interval: int) -> None:
        config_path.write_text(json.dumps({
            "companion": {"enabled": True,
                          "patterns": {"enabled": True, "interval_sec": interval}},
        }, ensure_ascii=False), encoding="utf-8")

    write_config(1800)
    clock = Clock()
    runner_obj = CompanionRunner(config_path, state_path=tmp_path / "companion-state.json",
                                 context_resolver=lambda s: FakeContext(tmp_path),
                                 client_factory=lambda ctx, s: FakeClient(),
                                 monotonic=clock, rng=ScriptedRandom([0.5]))

    runner_obj.run_due()
    assert runner_obj._runtime["patterns"].interval == 1800

    write_config(600)
    clock.advance(100_000)
    runner_obj.run_due()

    assert runner_obj._runtime["patterns"].interval == 600, (
        "новый интервал не подхвачен — настройки запомнились при старте")
    assert runner_obj.status()["cycles"]["patterns"]["interval_sec"] == 600


def test_broken_config_does_not_stop_the_channel(tmp_path, monkeypatch):
    """Битый конфиг не роняет тик: берутся дефолты, отказ виден в `status()`.

    `HubConfig.load` сам откатывается на дефолты при неразобранном JSON — раннер обязан
    это пережить, а не остановиться: конфиг правят руками, и одна лишняя запятая не должна
    выключать канал молча.
    """
    log: list = []
    patch_cycles(monkeypatch, log)
    config_path = tmp_path / "standkit-hub.json"
    config_path.write_text("{ это не json", encoding="utf-8")
    runner_obj = CompanionRunner(config_path, state_path=tmp_path / "companion-state.json",
                                 context_resolver=lambda s: FakeContext(tmp_path),
                                 client_factory=lambda ctx, s: FakeClient(),
                                 monotonic=Clock(), rng=ScriptedRandom([0.5]))

    report = runner_obj.run_due()

    assert report["results"]["patterns"]["status"] == "ok", (
        "дефолты конфига включают паттерны — тик обязан состояться")
    assert report["results"]["releases"]["status"] == "disabled", (
        "релизы по умолчанию выключены (ADR-0022, блокеры Б1/Б2)")


# --------------------------------------------------------------------------------------
# 12. Снимок без раннера
# --------------------------------------------------------------------------------------
def test_status_snapshot_on_empty_directory(tmp_path):
    """`status_snapshot` на пустом каталоге не падает и честно говорит `running: False`.

    Это состояние первого запуска и состояние «канал выключен настройкой»: UI обязан
    показать в них картину, а не пустой экран или ошибку.
    """
    snapshot = status_snapshot(tmp_path / "net-takogo-fajla.json")

    assert snapshot["running"] is False
    assert snapshot["edition"] == "companion"
    assert set(snapshot["cycles"]) == set(runner.CYCLES)
    for info in snapshot["cycles"].values():
        assert info["next_run_in_sec"] is None, (
            "у неподнятого планировщика нет следующего запуска — ноль читался бы как "
            "«прямо сейчас»")
        assert info["last_status"] == "never"
    assert snapshot["actions"]["apply_update"] is False
    assert snapshot["actions"]["rollback"] is False
    assert json.dumps(snapshot, ensure_ascii=False)  # снимок обязан быть сериализуем


def test_available_actions_follow_state_and_master_switch(tmp_path):
    """Кнопка предлагается, только если действие ВОЗМОЖНО, и только при включённом канале.

    `apply_update` проверяется по ФАЙЛУ на диске: запись о подготовленном обновлении без
    файла (антивирус унёс `.exe` в карантин) — это «нечего применять», и кнопка, которая
    гарантированно упадёт, хуже отсутствующей.
    """
    state = CompanionState(tmp_path / "companion-state.json")
    assert available_actions(settings_all_on(), state)["apply_update"] is False

    binary = tmp_path / "bpmkit-1.2.3.exe"
    binary.write_bytes(b"MZ")
    state.releases["staged"] = {"version": "1.2.3", "path": str(binary)}
    state.push_history({"version": "1.2.3", "previous_version": "1.2.2",
                        "backup": str(tmp_path / "backup.exe")})

    allowed = available_actions(settings_all_on(), state)
    assert allowed["apply_update"] is True
    assert allowed["rollback"] is True

    binary.unlink()
    assert available_actions(settings_all_on(), state)["apply_update"] is False, (
        "исчезнувший файл обязан убирать кнопку применения")

    off = available_actions(settings_all_on(enabled=False), state)
    assert not any(off.values()), "выключенный канал не предлагает ничего"

    assert available_actions(settings_all_on())["apply_update"] is False, (
        "без состояния «не знаю» обязано означать «не предлагать»")


def test_build_runner_does_not_start_thread(tmp_path):
    """`build_runner` собирает, но не запускает: хаб поднимает поток только после bind."""
    runner_obj = build_runner(tmp_path / "standkit-hub.json")
    assert isinstance(runner_obj, CompanionRunner)
    assert runner_obj.is_running() is False


def test_unknown_cycle_is_a_programming_error(tmp_path):
    """Опечатка в имени цикла — `ValueError`, а не тихий пропуск.

    Имена циклов совпадают с секциями состояния и полями настроек; молча проигнорированное
    имя означало бы цикл, который «включён», но не тикает никогда.
    """
    runner_obj = make_runner(tmp_path, settings_all_on())
    with pytest.raises(ValueError):
        runner_obj.run_cycle("patternz")


# --------------------------------------------------------------------------------------
# 13. CLI
# --------------------------------------------------------------------------------------
def test_cli_status_json_is_machine_readable(tmp_path, capsys):
    """`status --json` печатает валидный JSON и возвращает 0."""
    from standkit_companion.__main__ import main

    rc = main(["status", "--json", "--config", str(tmp_path / "standkit-hub.json")])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["running"] is False
    assert payload["edition"] == "companion"
    assert set(payload["cycles"]) == set(runner.CYCLES)


def test_cli_status_human_output(tmp_path, capsys):
    """Человеческий вывод — по умолчанию, на русском и без конверта."""
    from standkit_companion.__main__ import main

    rc = main(["status", "--config", str(tmp_path / "standkit-hub.json")])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Канал обновлений издателя BPMkit" in out
    assert "Циклы:" in out
    assert ENVELOPE not in out


def test_cli_unknown_command_returns_two(tmp_path, capsys):
    """Неизвестная команда — код 2 (ошибка использования), а не исключение наружу.

    `main` обязана ВОЗВРАЩАТЬ код: её зовут и как функцию, а argparse «выходит»
    исключением `SystemExit`.
    """
    from standkit_companion.__main__ import main

    assert main(["nesuschestvuyuschaya-komanda"]) == 2
    assert main([]) == 2, "без команды печатается справка и возвращается код использования"


def test_cli_apply_update_without_staged_returns_one(tmp_path, capsys):
    """`apply-update` без подготовленного обновления — код 1 и человеческий текст.

    Именно 1, а не 2: канал отработал и отвечает по существу («применять нечего»). И
    именно эта причина, а не «рядом нет CLI BPMkit»: локальная проверка идёт ДО резолва
    лицензионного контекста, иначе человек получал бы правдивую, но не относящуюся к делу
    жалобу.
    """
    from standkit_companion.__main__ import main

    rc = main(["apply-update", "--config", str(tmp_path / "standkit-hub.json")])
    err = capsys.readouterr().err

    assert rc == 1
    assert err.startswith("ОШИБКА: ")
    assert "Подготовленного обновления нет" in err
    assert "Что делать:" in err
    assert "Traceback" not in err, "стек-трейс наружу не летит никогда"


def test_cli_reports_missing_cli_as_environment_error(tmp_path, capsys, monkeypatch):
    """Нет CLI BPMkit рядом — код 2 (окружение), с подсказкой, что делать.

    Разводить 1 и 2 обязательно: в скриптах установки они ведут к разным действиям, а
    слипшись в «ненулевой код» превращают диагностику в гадание.
    """
    from standkit_companion import __main__ as cli
    from standkit_companion import context as context_module

    def no_cli(settings, **kwargs):
        raise ContextUnavailable("рядом нет CLI BPMkit", kind="context_unavailable",
                                 detail="автодетект не дал результата")

    monkeypatch.setattr(context_module, "resolve", no_cli)
    rc = cli.main(["sync", "--config", str(tmp_path / "standkit-hub.json")])
    err = capsys.readouterr().err

    assert rc == 2
    assert "ОШИБКА: Лицензионный контекст недоступен" in err
    assert "companion.mcp_cli" in err, "подсказка обязана называть то, что человек чинит"


def test_cli_run_once_makes_single_pass(tmp_path, capsys, monkeypatch):
    """`run --once` делает один проход и выходит, не поднимая фонового потока."""
    from standkit_companion import __main__ as cli
    from standkit_companion import context as context_module

    log: list = []
    patch_cycles(monkeypatch, log)
    monkeypatch.setattr(context_module, "resolve", lambda s, **kw: FakeContext(tmp_path))

    config_path = tmp_path / "standkit-hub.json"
    config_path.write_text(json.dumps({"companion": {"enabled": True}}), encoding="utf-8")
    rc = cli.main(["run", "--once", "--config", str(config_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert log == ["revocations", "patterns"], (
        "релизы выключены по умолчанию — их в проходе быть не должно")
    assert "отзыв лицензий" in out


def test_environment_switch_blocks_background_thread(tmp_path, monkeypatch):
    """Рубильник среды не даёт планировщику стартовать вообще.

    Это защита прогона тестов и CI: конфиг по умолчанию включает цикл паттернов, и без
    рубильника каждый поднятый тестом хаб на машине с живой лицензией заводил бы настоящий
    фоновый канал. Проверяем и то, что «выключено средой» видно в статусе явно, — иначе
    `running: false` в CI читалось бы как поломка.
    """
    monkeypatch.setenv(runner.DISABLE_ENV, "1")
    r = CompanionRunner(tmp_path / "standkit-hub.json", state_path=tmp_path / "state.json")
    r.start()
    try:
        assert not r.is_running(), (
            "при выставленном STANDKIT_COMPANION_DISABLED фоновый поток подниматься не "
            "должен — иначе рубильник бесполезен")
        status = r.status()
        assert status["environment_disabled"] is True, (
            "статус обязан честно отличать «выключено средой» от «выключено пользователем»")
    finally:
        r.stop(timeout=1.0)


def test_environment_switch_ignores_empty_and_falsy_values(tmp_path, monkeypatch):
    """Пустая или явно ложная переменная НЕ выключает канал.

    Случайно выставленная пустая переменная окружения не должна тихо лишить пользователя
    обновлений — молчаливое выключение платной функции хуже лишнего потока.
    """
    for value in ("", "0", "false", "NO", "off"):
        monkeypatch.setenv(runner.DISABLE_ENV, value)
        assert runner.environment_disabled() is False, (
            f"значение {value!r} не должно трактоваться как запрет")
    for value in ("1", "true", "yes", "on", "да"):
        monkeypatch.setenv(runner.DISABLE_ENV, value)
        assert runner.environment_disabled() is True, (
            f"значение {value!r} должно запрещать старт")
