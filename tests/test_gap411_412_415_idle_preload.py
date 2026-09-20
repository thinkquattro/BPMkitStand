# -*- coding: utf-8 -*-
"""
GAP-411 / GAP-412 / GAP-415 (аудит 19–20.09.2026) — сторож простоя, который
можно разобрать, снапшот с возрастом, загрузка модулей при старте и гарды
канала обновлений.

ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Живой прогон на VM 20.09 (поставка 1.1.116,
`BPMkit-hub.exe`, `idle_shutdown_min=1`, ПУСТОЙ реестр): через 4 минуты
диспетчер жив, а в `hub.log` — две строки «старт standkit-hub» и больше
НИЧЕГО. Из этого факта одинаково следовали четыре взаимоисключающих вывода:
сторож не запустился; конфиг не прочитан; снапшот считает стенды живыми;
дашборд открыт в браузере (exe без `--no-browser` открывает его сам, а
открытая страница держит SSE — и это штатная причина НЕ выходить). Воспроизве-
дение сторожа на HEAD сабмодуля показало, что сам предикат исправен, — то
есть чинить надо было не решение, а невозможность узнать, каким оно было.
Отсюда INFO при старте сторожа и DEBUG на каждом тике, и отсюда же тесты ниже.
"""
from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path

import pytest

from standkit.models import ProbeState, Stand, StandStatus, Transport
from standkit_hub import preload
from standkit_hub.config import HubConfig
from standkit_hub.server import _IdleShutdownWatcher, _stand_entry


# --------------------------------------------------------------------------
# двойники
# --------------------------------------------------------------------------


class _Snapshot:
    """Снапшот В ТОЙ ЖЕ ФОРМЕ, что строит поллер, — со ВСЕМИ полями оригинала.

    Класс 63 («двойник беднее оригинала») здесь не абстракция: ровно на
    отсутствии `probed`/`error` у прежнего двойника набор оставался зелёным,
    пока предикат в бою отвечал неверно (GAP-383). `generated_at` — то же
    самое поле той же природы.
    """

    def __init__(self, *, stands=None, probed=True, error=None, generated_at=None):
        self.stands = [] if stands is None else stands
        self.probed = probed
        self.error = error
        self.generated_at = time.time() if generated_at is None else generated_at


class _Poller:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


class _Server:
    def __init__(self, *, sse=0, snapshot=None):
        self._sse = sse
        self.status_poller = _Poller(snapshot) if snapshot is not None else None
        self.shutdown_calls = 0

    def sse_client_count(self):
        return self._sse

    def request_self_shutdown(self):
        self.shutdown_calls += 1
        return True


class _Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _watcher(tmp_path, server, *, minutes=1, refresh=10, desktop=False,
             clock=None, wall=None):
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(run_dir=str(tmp_path / "run"), idle_shutdown_min=minutes,
              refresh_interval_sec=refresh).save(config_path)
    return _IdleShutdownWatcher(server, config_path, desktop=desktop,
                               now=clock or _Clock(), wall_now=wall)


# --------------------------------------------------------------------------
# GAP-411 — сторож объясняет себя: INFO при старте, DEBUG на каждом тике
# --------------------------------------------------------------------------


def test_watcher_announces_itself_on_start(tmp_path, caplog):
    """Старт сторожа обязан оставить след с таймаутом И путём конфига.

    Без этой строки «сторож не запустился» и «сторож работает, но условие не
    выполнено» в hub.log неразличимы — а это ровно те два вывода, между
    которыми выбирал разбор VM-прогона 20.09."""
    server = _Server(snapshot=_Snapshot())
    watcher = _watcher(tmp_path, server, minutes=7)
    with caplog.at_level(logging.INFO, logger="standkit_hub"):
        watcher.start()
        watcher.stop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "сторож простоя" in text
    assert "7 мин" in text
    assert str(watcher._config_path) in text
    assert "включён" in text


def test_watcher_start_says_timer_is_disabled(tmp_path, caplog):
    """``idle_shutdown_min=0`` — самая частая причина «диспетчер не выходит».

    Она обязана быть написана словами при старте, а не вычисляться человеком
    из отсутствия событий."""
    server = _Server(snapshot=_Snapshot())
    watcher = _watcher(tmp_path, server, minutes=0)
    with caplog.at_level(logging.INFO, logger="standkit_hub"):
        watcher.start()
        watcher.stop()
    assert "ВЫКЛЮЧЕН" in "\n".join(r.getMessage() for r in caplog.records)


def test_tick_logs_decision_facts_at_debug(tmp_path, caplog):
    """DEBUG-строка тика содержит ВСЕ входы решения, а не его исход.

    Разбор начинается с вопроса «что именно сторож видел», и ответ обязан
    лежать рядом с каждым тиком: счётчик SSE, живость стендов, рассинхрон
    версий, признаки снапшота и накопленный простой."""
    server = _Server(sse=2, snapshot=_Snapshot(stands=[]))
    watcher = _watcher(tmp_path, server, minutes=1)
    with caplog.at_level(logging.DEBUG, logger="standkit_hub"):
        watcher._tick()
    line = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "тик сторожа простоя" in line
    for token in ("sse_client_count=2", "has_running_stands=False",
                  "version_desynced=", "snapshot_probed=True", "snapshot_age_sec="):
        assert token in line, token


def test_tick_is_silent_and_cheap_without_debug(tmp_path, caplog):
    """При выключенном DEBUG тик не пишет ничего и не собирает факты.

    Сторож тикает раз в 15 с круглые сутки: лог ради лога здесь стоил бы
    ротации боевого файла и лишнего снапшота на каждый тик."""
    server = _Server(sse=0, snapshot=_Snapshot(stands=[]))
    watcher = _watcher(tmp_path, server, minutes=1)
    with caplog.at_level(logging.INFO, logger="standkit_hub"):
        watcher._tick()
    assert [r for r in caplog.records if r.levelno == logging.DEBUG] == []


def test_open_dashboard_holds_the_hub_and_says_so(tmp_path, caplog):
    """Открытая вкладка — ШТАТНАЯ причина не выходить, и она обязана быть видна.

    Именно этот случай объясняет живой прогон VM 20.09: exe без
    ``--no-browser`` открывает дашборд сам, страница держит ``/api/events``,
    счётчик SSE не нулевой — и таймер простоя не начинает отсчёт вовсе."""
    clock = _Clock()
    server = _Server(sse=1, snapshot=_Snapshot(stands=[]))
    watcher = _watcher(tmp_path, server, minutes=1, clock=clock)
    with caplog.at_level(logging.DEBUG, logger="standkit_hub"):
        watcher._tick()
        clock.advance(3600)
        watcher._tick()
    assert server.shutdown_calls == 0
    assert "idle=False" in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# GAP-412 — возраст снапшота: знание протухает
# --------------------------------------------------------------------------


def test_stale_snapshot_is_not_knowledge(tmp_path, caplog):
    """Снапшот старше 3×refresh_interval = «о стендах НИЧЕГО не известно».

    Зависший поллер и уснувшая машина оставляют последний успешный снапшот
    как есть. Пустой список трёхминутной давности неотличим от «стендов нет»
    — и стоил бы ровно того же, что стоил рецидив 18.09."""
    now = [10_000.0]
    server = _Server(snapshot=_Snapshot(stands=[], generated_at=9_900.0))
    watcher = _watcher(tmp_path, server, minutes=1, refresh=10,
                       wall=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="standkit_hub"):
        assert watcher.has_running_stands() is True
        assert watcher.is_idle() is False
    assert "протух" in "\n".join(r.getMessage() for r in caplog.records)


def test_stale_warning_is_written_once_per_series(tmp_path, caplog):
    now = [10_000.0]
    server = _Server(snapshot=_Snapshot(stands=[], generated_at=9_900.0))
    watcher = _watcher(tmp_path, server, minutes=1, wall=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="standkit_hub"):
        for _ in range(5):
            watcher.has_running_stands()
    stale = [r for r in caplog.records if "протух" in r.getMessage()]
    assert len(stale) == 1, "зависший поллер не имеет права залить лог одной строкой"


def test_stale_threshold_follows_configured_refresh_interval(tmp_path):
    """Порог считается от НАСТРОЙКИ, а не от константы.

    У человека с опросом раз в минуту снапшот 100-секундной давности — норма;
    у дефолтных 10 с — уже авария."""
    now = [10_000.0]
    snapshot = _Snapshot(stands=[], generated_at=9_900.0)  # 100 с
    fast = _watcher(tmp_path / "fast", _Server(snapshot=snapshot), refresh=10,
                    wall=lambda: now[0])
    slow = _watcher(tmp_path / "slow", _Server(snapshot=snapshot), refresh=60,
                    wall=lambda: now[0])
    assert fast.snapshot_is_stale(snapshot) is True
    assert slow.snapshot_is_stale(snapshot) is False


def test_snapshot_without_generated_at_is_stale(tmp_path):
    """Снапшот без даты — «не знаем, когда собран», то есть протухший."""
    server = _Server(snapshot=_Snapshot(stands=[], generated_at=0.0))
    watcher = _watcher(tmp_path, server)
    assert watcher.has_running_stands() is True


def test_fresh_empty_snapshot_still_allows_idle(tmp_path):
    """Обратная сторона: свежий честный снапшот без живых стендов — простой.

    Без этой проверки «протухание» легко превратить в запрет автовыхода
    вообще, и GAP-411 вернулся бы с другой стороны."""
    server = _Server(snapshot=_Snapshot(stands=[]))
    watcher = _watcher(tmp_path, server)
    assert watcher.has_running_stands() is False
    assert watcher.is_idle() is True


# --------------------------------------------------------------------------
# GAP-412 — контракт «реальный _stand_entry → has_running_stands»
# --------------------------------------------------------------------------


def _entry(process_state: ProbeState) -> dict:
    """Строка снапшота, собранная НАСТОЯЩИМ ``_stand_entry``, а не руками.

    Класс 63: пока тест сам строит словарь, он проверяет своё представление о
    формате, а не формат. Именно так GAP-385 прожил до боя — предикат сравнивал
    состояние со строкой ``running``, которой ``_stand_entry`` не выдаёт."""
    stand = Stand(name="x", transport=Transport.LOCAL, stand_dir="C:/stands/x",
                  stand_host="127.0.0.1", stand_port=5000)
    status = StandStatus(name="x", process=process_state)
    return _stand_entry("x", stand, status)


def test_real_stand_entry_with_live_process_blocks_idle(tmp_path):
    server = _Server(snapshot=_Snapshot(stands=[_entry(ProbeState.OK)]))
    assert _watcher(tmp_path, server).has_running_stands() is True


@pytest.mark.parametrize("state", [ProbeState.DOWN, ProbeState.UNKNOWN, ProbeState.SKIPPED])
def test_real_stand_entry_without_live_process_allows_idle(tmp_path, state):
    server = _Server(snapshot=_Snapshot(stands=[_entry(state)]))
    assert _watcher(tmp_path, server).has_running_stands() is False


def test_http_stand_never_blocks_idle_and_it_is_documented(tmp_path):
    """Удалённый стенд без агента (GAP-277) в условие простоя не входит.

    Процесса у такой записи нет (проба ``process`` → SKIPPED), диспетчер его не
    запускал и остановить не может — терять при выходе нечего. Поведение
    задокументировано в докстринге класса: это решение, а не побочный эффект."""
    stand = Stand(name="r", transport=Transport.HTTP, stand_host="stand.example.com",
                  stand_port=443, stand_scheme="https")
    status = StandStatus(name="r", process=ProbeState.SKIPPED, http=ProbeState.OK)
    entry = _stand_entry("r", stand, status)
    server = _Server(snapshot=_Snapshot(stands=[entry]))
    assert _watcher(tmp_path, server).has_running_stands() is False
    doc = _IdleShutdownWatcher.__doc__ or ""
    assert "http" in doc and "SKIPPED" in doc


def test_class_docstring_does_not_promise_running_state():
    """Докстринг класса не имеет права называть состояние, которого нет.

    ``ProbeState`` — ok/down/unknown/skipped; слово ``running`` в описании
    условия простоя ровно один раз уже стало кодом (GAP-385)."""
    doc = _IdleShutdownWatcher.__doc__ or ""
    assert "нет ни одного стенда в состоянии ``running``" not in doc, (
        "докстринг снова описывает условие простоя через несуществующее состояние")
    assert "ProbeState.OK" in doc, "условие простоя обязано быть названо реальным состоянием"
    assert "running" not in {state.value for state in ProbeState}


# --------------------------------------------------------------------------
# GAP-412 — нечитаемая версия ПОСЛЕ прочитанной = рассинхрон
# --------------------------------------------------------------------------


def test_version_unreadable_after_being_read_is_desync(tmp_path, monkeypatch):
    """`pip install` снимает `__init__.py` и кладёт новый НЕ атомарно.

    Попасть тиком сторожа в это окно — вопрос времени, а цена та же: выход с
    живыми стендами посреди обновления."""
    server = _Server(snapshot=_Snapshot())
    watcher = _watcher(tmp_path, server)
    monkeypatch.setattr(watcher, "_version_in_memory", staticmethod(lambda: "0.12.6"))
    monkeypatch.setattr(watcher, "_version_on_disk", staticmethod(lambda: "0.12.6"))
    assert watcher.version_desynced() is False
    monkeypatch.setattr(watcher, "_version_on_disk", staticmethod(lambda: ""))
    assert watcher.version_desynced() is True
    assert watcher.is_idle() is False


def test_version_never_readable_is_not_desync(tmp_path, monkeypatch):
    """frozen-поставка: исходника пакета на диске нет ВООБЩЕ.

    Считать это рассинхроном значило бы запретить exe-диспетчеру автовыход
    навсегда — то есть заменить один GAP-411 другим."""
    server = _Server(snapshot=_Snapshot())
    watcher = _watcher(tmp_path, server)
    monkeypatch.setattr(watcher, "_version_in_memory", staticmethod(lambda: "0.12.6"))
    monkeypatch.setattr(watcher, "_version_on_disk", staticmethod(lambda: ""))
    assert watcher.version_desynced() is False
    assert watcher.is_idle() is True


# --------------------------------------------------------------------------
# GAP-412 — все модули пакетов в память при старте; гейт ленивых импортов
# --------------------------------------------------------------------------

#: Ленивые импорты МОДУЛЕЙ САМИХ ПАКЕТОВ, разрешённые с обоснованием.
#: Ключ — «<файл>:<функция> -> <что импортируется>». Всё, чего здесь нет,
#: обязано быть импортировано на уровне модуля: иначе замена файлов под живым
#: процессом (`pip install -U`) втащит новый модуль в старый процесс.
#: Стандартная библиотека и необязательные внешние пакеты (ctypes, webview,
#: getpass, datetime, http.client) под гейт не попадают: их подмена под
#: процессом — не тот класс отказа, ради которого он написан.
LAZY_IMPORT_ALLOWLIST = {
    # Циклы внутри standkit: hosting ↔ lifecycle ↔ health ↔ adopt. Разорвать
    # их на уровне модуля нельзя — только переписав разделение обязанностей.
    "standkit/adopt.py:_capture -> standkit.hosting": "цикл adopt ↔ hosting",
    "standkit/health.py:process_alive -> standkit.platform": "цикл health ↔ platform",
    "standkit/health.py:check_stand._probe_process -> standkit.hosting": "цикл health ↔ hosting",
    "standkit/hosting.py:_tcp_fallback -> standkit.health": "цикл hosting ↔ health",
    "standkit/hosting.py:start -> standkit.lifecycle": "цикл hosting ↔ lifecycle",
    "standkit/hosting.py:stop -> standkit.lifecycle": "цикл hosting ↔ lifecycle",
    "standkit/hosting.py:restart -> standkit.lifecycle": "цикл hosting ↔ lifecycle",
    "standkit/hosting.py:is_running -> standkit.lifecycle": "цикл hosting ↔ lifecycle",
    "standkit/hosting.py:kill_worker_processes -> standkit.platform": "цикл hosting ↔ platform",
    "standkit/hosting.py:read_logs -> standkit.logs": "цикл hosting ↔ logs",
    "standkit/lifecycle.py:start -> standkit.hosting": "цикл lifecycle ↔ hosting",
    "standkit/lifecycle.py:stop -> standkit.hosting": "цикл lifecycle ↔ hosting",
    "standkit/lifecycle.py:restart -> standkit.hosting": "цикл lifecycle ↔ hosting",
    "standkit/lifecycle.py:is_running -> standkit.hosting": "цикл lifecycle ↔ hosting",
    "standkit/lifecycle.py:_adoption_candidate -> standkit.adopt": "цикл lifecycle ↔ adopt",
    "standkit/lifecycle.py:_kestrel_stop -> standkit.health": "цикл lifecycle ↔ health",
    "standkit_hub/mutex.py:_mutex_security_descriptor_sddl -> standkit.platform":
        "mutex поднимается ДО пакета standkit в точке входа",
    "standkit_hub/instance.py:_process_looks_like_hub -> standkit.adopt":
        "цикл instance ↔ adopt (adopt тянет hosting)",
    "standkit_hub/instance.py:wait_port_released -> standkit_hub.server":
        "цикл instance ↔ server (server импортирует instance)",
    "standkit_hub/server.py:on_disk_standkit_version -> standkit":
        "импорт ради __file__: версия читается С ДИСКА, а не из памяти (ADR-0007 §4)",
    "standkit_hub/server.py:_is_companion_error -> standkit_companion.errors":
        "редакция без канала обновлений — штатная поставка",
    "standkit_hub/server.py:make_handler._api_iis_detect -> standkit.hosting":
        "цикл server ↔ hosting",
    "standkit_hub/__main__.py:main -> standkit_hub.elevated_op":
        "одноразовый режим «выполнить и выйти» до всякого bind/mutex",
    "standkit_companion/runner.py:_load_settings_from_config -> standkit_hub.config":
        "хаб импортирует канал МЯГКО (try/except ImportError) — встречный импорт "
        "на уровне модуля замкнул бы кольцо при старте хаба",
    "standkit_companion/runner.py:settings -> standkit_hub.config":
        "то же кольцо canal ↔ hub (фолбэк на дефолтные настройки при битом конфиге)",
    "standkit_companion/__main__.py:_config_path -> standkit_hub.config":
        "точка входа канала: пакет хаба может отсутствовать в этой редакции",
    "standkit_agent/server.py:make_handler._handle_logs -> standkit.logs":
        "агент — ОТДЕЛЬНЫЙ процесс, в адресном пространстве диспетчера не живёт "
        "и под preload не попадает (см. standkit_hub.preload.PACKAGES)",
}

_PACKAGE_ROOTS = ("standkit", "standkit_hub", "standkit_companion", "standkit_agent")
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _lazy_package_imports():
    """Все ``import`` ВНУТРИ функций, нацеленные на модули САМИХ пакетов."""
    found = {}
    for root in _PACKAGE_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            stack = []

            def walk(node):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        stack.append(child.name)
                        walk(child)
                        stack.pop()
                        continue
                    if stack and isinstance(child, (ast.Import, ast.ImportFrom)):
                        if isinstance(child, ast.Import):
                            targets = [alias.name for alias in child.names]
                        else:
                            # `from standkit import hosting` — цель именно
                            # `standkit.hosting`, а не пакет `standkit`:
                            # заменяется под процессом ИМЕННО файл модуля.
                            module = child.module or ""
                            if not module:
                                targets = []
                            elif module.split(".")[0] in _PACKAGE_ROOTS and "." not in module:
                                targets = [f"{module}.{alias.name}" for alias in child.names]
                            else:
                                targets = [module]
                        for target in targets:
                            head = target.split(".")[0]
                            if head in _PACKAGE_ROOTS:
                                key = f"{rel}:{'.'.join(stack)} -> {target}"
                                found[key] = child.lineno
                    walk(child)

            walk(tree)
    return found


def test_no_unjustified_lazy_package_imports():
    """Новый ленивый импорт модуля пакета обязан быть обоснован в allowlist.

    Смысл гейта не в чистоте стиля: модуль, не импортированный к моменту
    `pip install -U`, подтягивается ПОСЛЕ замены файлов, и в одном процессе
    оказывается половина старой версии и половина новой (GAP-412)."""
    found = _lazy_package_imports()
    unexpected = sorted(set(found) - set(LAZY_IMPORT_ALLOWLIST))
    assert not unexpected, (
        "ленивые импорты модулей пакета без обоснования:\n  "
        + "\n  ".join(f"{k} (строка {found[k]})" for k in unexpected)
    )


def test_allowlist_has_no_dead_entries():
    """Протухший allowlist хуже отсутствующего: он разрешает то, чего нет."""
    found = _lazy_package_imports()
    dead = sorted(set(LAZY_IMPORT_ALLOWLIST) - set(found))
    assert not dead, f"в allowlist остались несуществующие импорты: {dead}"


def test_allowlist_entries_carry_a_reason():
    for key, reason in LAZY_IMPORT_ALLOWLIST.items():
        assert reason.strip(), key


def test_preload_covers_every_module_on_disk():
    """Перебор модулей обязан видеть КАЖДЫЙ файл пакета.

    Иначе «все модули загружены при старте» превращается в «почти все», а
    смысл гейта — именно в отсутствии исключений."""
    names = set(preload.package_module_names())
    for root in ("standkit", "standkit_hub", "standkit_companion"):
        base = _REPO_ROOT / root
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "__main__.py":
                continue
            rel = path.relative_to(_REPO_ROOT).with_suffix("")
            parts = [p for p in rel.parts if p != "__init__"]
            module = ".".join(parts)
            if module == root:
                continue
            assert module in names, module


def test_preload_imports_everything_without_failures():
    loaded, failed = preload.preload()
    assert failed == [], failed
    assert len(loaded) >= 30


def test_preload_skips_entry_points():
    assert not any(name.endswith(".__main__") for name in preload.package_module_names())


# --------------------------------------------------------------------------
# GAP-415 — effective_transport во ВСЕХ методах клиента
# --------------------------------------------------------------------------


def _legacy_remote_registry(tmp_path):
    """Реестр с записью `transport=agent` БЕЗ `agent_url` — так регистрировали
    удалённый стенд до появления транспорта http (GAP-277)."""
    from standkit.registry import Registry

    path = tmp_path / "projects.json"
    path.write_text('{"default": "r", "projects": {"r": {"transport": "agent", '
                    '"stand_host": "stand.example.com", "stand_port": 443, '
                    '"stand_scheme": "https"}}}', encoding="utf-8")
    return Registry.load(path)


@pytest.mark.parametrize("call", [
    lambda c: c.adopt("r"),
    lambda c: c.stop("r"),
    lambda c: c.start("r"),
    lambda c: c.restart("r"),
    lambda c: c.logs("r"),
])
def test_legacy_agent_record_answers_like_http_everywhere(tmp_path, call):
    """Три метода клиента читали ``transport`` буквально и уходили к агенту,
    которого нет, — отвечая «не задан agent_url» вместо честного «управлять
    процессом удалённого стенда нечем». Ответ обязан быть ОДИН и тот же,
    откуда бы его ни получили."""
    from standkit_hub.client import FederatedClient

    client = FederatedClient(_legacy_remote_registry(tmp_path))
    with pytest.raises(NotImplementedError) as exc:
        call(client)
    assert "без агента управление процессом недоступно" in str(exc.value)


def test_client_has_no_literal_transport_comparisons():
    """Тест-сторож: сравнение с полем ``transport`` возвращает дефект целиком.

    Разница между ``transport`` и ``effective_transport`` — ровно одна запись
    реестра (legacy agent без URL), и именно она ломалась молча."""
    text = (_REPO_ROOT / "standkit_hub" / "client.py").read_text(encoding="utf-8")
    assert "stand.transport ==" not in text


# --------------------------------------------------------------------------
# GAP-415 — клиентский гард канала обновлений: установщик не бинарь
# --------------------------------------------------------------------------


def _pe_file(path: Path, name: str) -> Path:
    target = path / name
    target.write_bytes(b"MZ" + b"\x00" * 64)
    return target


def test_installer_artifact_is_refused_by_the_client(tmp_path):
    """`bpmkit-setup-*.exe` проходит обе прежние проверки (расширение и MZ) и
    подпись издателя — и при этом класть его на место `bpmkit.exe` нельзя:
    это Inno Setup, который сам останавливает диспетчер и раскладывает
    поставку. Отдельным типом артефакта он станет по ADR-0048."""
    from standkit_companion.errors import ChannelError
    from standkit_companion.releases import _ensure_artifact_applicable

    src = _pe_file(tmp_path, "bpmkit-setup-1.1.120.exe")
    dest = tmp_path / "bpmkit.exe"
    with pytest.raises(ChannelError) as exc:
        _ensure_artifact_applicable(src, dest)
    assert exc.value.kind == "artifact_type_mismatch"
    assert "ADR-0048" in str(exc.value)


@pytest.mark.parametrize("name", ["BPMkit-Setup-1.1.120.exe", "bpmkit_setup_1.1.120.exe"])
def test_installer_guard_ignores_case_and_separator(tmp_path, name):
    from standkit_companion.errors import ChannelError
    from standkit_companion.releases import _ensure_artifact_applicable

    with pytest.raises(ChannelError):
        _ensure_artifact_applicable(_pe_file(tmp_path, name), tmp_path / "bpmkit.exe")


def test_normal_binary_still_applies(tmp_path):
    """Гард не имеет права отказывать обычному бинарю — иначе канал обновлений
    остановится целиком."""
    from standkit_companion.releases import _ensure_artifact_applicable

    _ensure_artifact_applicable(_pe_file(tmp_path, "bpmkit-1.1.120.exe"),
                                tmp_path / "bpmkit.exe")


# --------------------------------------------------------------------------
# GAP-415 — версия пакета в двух местах обязана совпадать
# --------------------------------------------------------------------------


def test_package_version_matches_pyproject():
    """`standkit/__init__.__version__` == `pyproject.version`.

    Расхождение стоило целого ложного вывода аудита: exe 1.1.116 сообщал
    «standkit 0.12.2» при указателе сабмодуля на 0.12.5, и это прочли как
    «сборка унесла старый диспетчер». Старым был не диспетчер, а строка
    версии: 0.12.3/0.12.4/0.12.5 бампали только pyproject."""
    import re

    import standkit

    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "в pyproject.toml не найдена версия"
    assert standkit.__version__ == m.group(1)
