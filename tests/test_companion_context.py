# -*- coding: utf-8 -*-
"""Тесты standkit_companion.context — резолва лицензионного контекста у CLI MCP.

Зачем эти тесты именно такие. Модуль сознательно НЕ проверяет лицензию сам: он спрашивает
готовый ответ у `bpmkit setup companion-context --json`. Значит проверять надо не логику
лицензирования, а границу с чужим процессом:

* разные отказы разводятся правильно — «CLI рядом нет» (чинит настройки) против «лицензии
  нет» (чинит ключ). Их регулярно путают, и пользователь чинит не то;
* явная настройка `backend_url` перебивает адрес из поставки;
* кэш действительно экономит запуск процесса (иначе тик трёх циклов = три запуска `.exe`);
* конверт не утекает НИ В ОДНО сообщение об ошибке — stdout CLI содержит лицензионный ключ,
  и попадание stdout в текст исключения означало бы ключ в логе хаба.

Реальный процесс не запускается нигде: `resolve` принимает `run` как точку инъекции.
"""

from __future__ import annotations

import json
import time

import pytest

from standkit_companion import context as context_module
from standkit_companion.context import (
    CONTEXT_ARGV_TAIL,
    LicenseContext,
    find_cli,
    invalidate_cache,
    resolve,
)
from standkit_companion.errors import ChannelError, ContextUnavailable
from standkit_hub.config import CompanionSettings

ENVELOPE = "BPMKIT1.eyJsaWMiOiJ0ZXN0In0.c2ln"

OK_PAYLOAD = {
    "ok": True,
    "envelope": ENVELOPE,
    "license_status": "valid",
    "backend_url": "https://publisher.example/",
    "mcp_version": "0.355.0",
    "package_root": "/opt/BPMkit",
    "shipped_patterns_root": "/opt/BPMkit/skills/bpmsoft-dev/references",
    "override_patterns_root": "/home/u/.config/BPMkit/patterns/references",
    "patterns_env_registered": False,
    "revocations_target": "/home/u/.config/BPMkit/revocations.json",
    "revocations_env_registered": False,
    "artifact_pubkey": "",
    "binary_path": "/opt/BPMkit/server/bpmkit.exe",
}


class _Runner:
    """Подставной запуск CLI: считает вызовы и отдаёт заранее заданный ответ.

    Счётчик — не украшение: на нём проверяется кэш (второй `resolve` не имеет права
    запускать процесс) и его сброс.
    """

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.rc, self.stdout, self.stderr


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш модульный — без уборки один тест кормил бы контекстом следующий."""
    invalidate_cache()
    yield
    invalidate_cache()


def _cli(tmp_path, name: str = "bpmkit.exe"):
    """Файл-заглушка CLI: `find_cli` проверяет существование, а не исполняемость."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _settings(cli_path, **kwargs) -> CompanionSettings:
    return CompanionSettings(mcp_cli=str(cli_path), **kwargs)


def _ok_runner(payload: dict | None = None) -> _Runner:
    return _Runner(stdout=json.dumps(payload or OK_PAYLOAD, ensure_ascii=False))


# --------------------------------------------------------------------------------------
# find_cli
# --------------------------------------------------------------------------------------
def test_find_cli_uses_configured_path_as_is(tmp_path):
    """Путь к файлу берётся целиком: в нём бывают пробелы, дробить его нельзя."""
    cli = _cli(tmp_path / "Program Files", "bpmkit.exe")

    assert find_cli(_settings(cli)) == [str(cli)]


def test_find_cli_splits_configured_command_line(tmp_path):
    """`python -m bpmkit` — не путь, а командная строка: её надо разобрать на argv."""
    settings = CompanionSettings(mcp_cli="python -m bpmkit")

    assert find_cli(settings) == ["python", "-m", "bpmkit"]


def test_find_cli_autodetects_next_to_package(tmp_path):
    """Поставка кладёт BPMkitStand ВНУТРЬ корня пакета MCP — бинарь ищется в
    `<root>/server/bpmkit.exe` и рядом."""
    root = tmp_path / "BPMkit"
    (root / "server").mkdir(parents=True)
    binary = root / "server" / "bpmkit.exe"
    binary.write_text("", encoding="utf-8")

    assert find_cli(CompanionSettings(), extra_roots=[root]) == [str(binary)]


def test_find_cli_returns_none_when_nothing_found(tmp_path):
    """Пусто — значит пусто. Гадать «а вдруг он в PATH» нельзя: там может оказаться
    другая сборка MCP, и канал спросит лицензию не у того."""
    assert find_cli(CompanionSettings(), extra_roots=[tmp_path / "нет-такой-папки"]) is None


# --------------------------------------------------------------------------------------
# resolve: успешный путь
# --------------------------------------------------------------------------------------
def test_resolve_parses_context(tmp_path):
    cli = _cli(tmp_path)
    runner = _ok_runner()

    ctx = resolve(_settings(cli), run=runner, cache_ttl=0)

    assert isinstance(ctx, LicenseContext)
    assert ctx.envelope == ENVELOPE
    assert ctx.license_status == "valid"
    assert ctx.mcp_version == "0.355.0"
    assert ctx.shipped_patterns_root.endswith("references")
    assert ctx.patterns_env_registered is False
    assert ctx.revocations_env_registered is False
    assert ctx.binary_path.endswith("bpmkit.exe")
    assert ctx.cli == [str(cli)]
    assert ctx.raw["ok"] is True
    # Хвостовой слэш адреса срезан: иначе каждый путь склеится через двойной `//`.
    assert ctx.backend_url == "https://publisher.example"
    assert runner.calls == [[str(cli), *CONTEXT_ARGV_TAIL]]


def test_resolve_settings_backend_url_overrides_context(tmp_path):
    """Адрес, заданный человеком в настройках хаба, сильнее дефолта из поставки —
    иначе настройка молча игнорируется."""
    cli = _cli(tmp_path)
    settings = _settings(cli, backend_url="https://stand.local:8000/")

    ctx = resolve(settings, run=_ok_runner(), cache_ttl=0)

    assert ctx.backend_url == "https://stand.local:8000"


def test_resolve_tolerates_banner_before_json(tmp_path):
    """Чужой процесс имеет право напечатать предупреждение рантайма перед ответом —
    это не повод объявлять контекст недоступным."""
    cli = _cli(tmp_path)
    runner = _Runner(stdout="WARNING: устаревший конфиг\n"
                            + json.dumps(OK_PAYLOAD, ensure_ascii=False) + "\n")

    ctx = resolve(_settings(cli), run=runner, cache_ttl=0)

    assert ctx.license_status == "valid"


# --------------------------------------------------------------------------------------
# resolve: отказы
# --------------------------------------------------------------------------------------
def test_resolve_without_cli_says_what_to_fix(tmp_path, monkeypatch):
    """«CLI нет» — это не «лицензии нет»: чинится указанием пути в настройках, и
    сообщение обязано это называть. Процесс при этом не запускается вовсе."""
    # Автодетект уводим в заведомо пустой каталог: иначе на машине разработчика он мог бы
    # найти НАСТОЯЩИЙ bpmkit.exe рядом с репозиторием и тест перестал бы проверять отказ.
    monkeypatch.setattr(context_module, "_candidate_roots", lambda extra_roots=None: [tmp_path])
    runner = _ok_runner()

    with pytest.raises(ContextUnavailable) as info:
        resolve(CompanionSettings(mcp_cli=""), run=runner, cache_ttl=0)

    err = info.value
    assert err.kind == "context_unavailable"
    assert err.retriable is True
    assert "mcp_cli" in str(err), "сообщение не называет, что именно чинить"
    assert runner.calls == [], "процесс запущен, хотя запускать нечего"


def test_resolve_no_license_is_a_different_problem(tmp_path):
    """`ok:false / no_license` при rc=0 — ШТАТНЫЙ ответ MCP: лицензии нет. Это другой
    отказ, чем «нет CLI», и чинится он другим действием."""
    cli = _cli(tmp_path)
    runner = _Runner(stdout=json.dumps(
        {"ok": False, "error": "no_license", "detail": "ключ не найден"}))

    with pytest.raises(ChannelError) as info:
        resolve(_settings(cli), run=runner, cache_ttl=0)

    err = info.value
    assert err.kind == "no_license"
    assert isinstance(err, ChannelError) and not isinstance(err, ContextUnavailable)
    assert err.user_visible is True
    assert err.retriable is False


def test_resolve_unknown_error_code_falls_back_to_unknown(tmp_path):
    """Неизвестный `error` не подменяется знакомым: расхождение контрактов должно быть
    видно, а не выглядеть как понятная ошибка."""
    cli = _cli(tmp_path)
    runner = _Runner(stdout=json.dumps({"ok": False, "error": "нечто-новое"}))

    with pytest.raises(ChannelError) as info:
        resolve(_settings(cli), run=runner, cache_ttl=0)

    assert info.value.kind == "unknown"


def test_resolve_known_error_code_is_kept(tmp_path):
    """А вот известный `kind` (например, истёкшая лицензия) сохраняется как есть."""
    cli = _cli(tmp_path)
    runner = _Runner(stdout=json.dumps({"ok": False, "error": "expired",
                                        "detail": "срок истёк"}))

    with pytest.raises(ChannelError) as info:
        resolve(_settings(cli), run=runner, cache_ttl=0)

    assert info.value.kind == "expired"


def test_resolve_nonzero_rc_reports_stderr(tmp_path):
    """Ненулевой код возврата — в detail идёт stderr (там причина), но не stdout."""
    cli = _cli(tmp_path)
    runner = _Runner(rc=2, stdout="", stderr="Traceback: не найден модуль bpmkit")

    with pytest.raises(ContextUnavailable) as info:
        resolve(_settings(cli), run=runner, cache_ttl=0)

    assert info.value.kind == "context_unavailable"
    assert "не найден модуль bpmkit" in info.value.detail


def test_resolve_empty_stdout_is_context_unavailable(tmp_path):
    cli = _cli(tmp_path)

    with pytest.raises(ContextUnavailable):
        resolve(_settings(cli), run=_Runner(rc=0, stdout="   \n"), cache_ttl=0)


def test_resolve_non_json_stdout_is_context_unavailable(tmp_path):
    """Не-JSON на stdout — поломка контракта с MCP, а не «нет лицензии»."""
    cli = _cli(tmp_path)

    with pytest.raises(ContextUnavailable) as info:
        resolve(_settings(cli), run=_Runner(stdout="это не json"), cache_ttl=0)

    assert info.value.kind == "context_unavailable"


# --------------------------------------------------------------------------------------
# Кэш
# --------------------------------------------------------------------------------------
def test_resolve_caches_context_between_ticks(tmp_path):
    """Контекст спрашивается на каждый тик трёх циклов — запускать процесс трижды подряд
    незачем."""
    cli = _cli(tmp_path)
    runner = _ok_runner()
    settings = _settings(cli)

    first = resolve(settings, run=runner, cache_ttl=300.0)
    second = resolve(settings, run=runner, cache_ttl=300.0)

    assert second is first
    assert len(runner.calls) == 1, "кэш не сработал — процесс запущен повторно"


def test_invalidate_cache_forces_new_call(tmp_path):
    """После ввода ключа/смены адреса канал обязан переспросить сразу, а не через TTL."""
    cli = _cli(tmp_path)
    runner = _ok_runner()
    settings = _settings(cli)

    resolve(settings, run=runner, cache_ttl=300.0)
    invalidate_cache()
    resolve(settings, run=runner, cache_ttl=300.0)

    assert len(runner.calls) == 2


def test_cache_key_accounts_for_backend_url_override(tmp_path):
    """Разный `backend_url` — разный контекст: один кэш на оба означал бы, что смена
    адреса в настройках до пяти минут не действует."""
    cli = _cli(tmp_path)
    runner = _ok_runner()

    first = resolve(_settings(cli), run=runner, cache_ttl=300.0)
    second = resolve(_settings(cli, backend_url="https://other.local"),
                     run=runner, cache_ttl=300.0)

    assert len(runner.calls) == 2
    assert first.backend_url != second.backend_url


def test_expired_cache_entry_is_refetched(tmp_path):
    """TTL действительно истекает, а не кэширует навсегда. Проверяется коротким реальным
    TTL: подменять часы в модуле `time` ради этого дороже, чем подождать 60 мс."""
    cli = _cli(tmp_path)
    runner = _ok_runner()
    settings = _settings(cli)

    resolve(settings, run=runner, cache_ttl=0.05)
    time.sleep(0.06)
    resolve(settings, run=runner, cache_ttl=0.05)

    assert len(runner.calls) == 2


# --------------------------------------------------------------------------------------
# Конверт не утекает
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("stdout", [
    "мусор " + ENVELOPE + " хвост",
    json.dumps({"ok": False, "error": "revoked", "detail": ENVELOPE[:10] + "…"}),
])
def test_envelope_never_appears_in_error_text(tmp_path, stdout):
    """stdout CLI содержит лицензионный ключ. Ни одно сообщение об ошибке не имеет права
    протащить его в лог хаба — поэтому в detail идёт только stderr и коды."""
    cli = _cli(tmp_path)
    runner = _Runner(stdout=stdout, stderr="stderr без секретов")

    with pytest.raises((ContextUnavailable, ChannelError)) as info:
        resolve(_settings(cli), run=runner, cache_ttl=0)

    err = info.value
    assert ENVELOPE not in str(err)
    assert ENVELOPE not in err.detail
    assert ENVELOPE not in json.dumps(err.to_dict(), ensure_ascii=False)


def test_context_repr_hides_envelope(tmp_path):
    """Контекст естественно попадает в отладочный вывод и трассировку — конверта там
    быть не должно."""
    ctx = resolve(_settings(_cli(tmp_path)), run=_ok_runner(), cache_ttl=0)

    assert ENVELOPE not in repr(ctx)
    assert "envelope=<скрыт>" in repr(ctx)
    assert ctx.has_envelope is True


def test_default_run_goes_through_run_console(monkeypatch, tmp_path):
    """Прямой subprocess в пакете запрещён (GAP-138): без CREATE_NO_WINDOW каждый тик
    мигал бы чёрным окном. Проверяем и сам факт вызова, и обязательные параметры."""
    captured: dict = {}

    def _fake_run_console(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs

        class _Completed:
            returncode = 0
            stdout = json.dumps(OK_PAYLOAD)
            stderr = ""

        return _Completed()

    monkeypatch.setattr(context_module, "run_console", _fake_run_console)
    cli = _cli(tmp_path)

    ctx = resolve(_settings(cli), cache_ttl=0)

    assert captured["cmd"] == [str(cli), *CONTEXT_ARGV_TAIL]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["timeout"] == 30
    assert ctx.license_status == "valid"


def test_default_run_converts_exceptions_to_failure(monkeypatch, tmp_path):
    """Зоопарк исключений subprocess (нет файла, таймаут, отказ в доступе) наружу не
    выходит — вызывающий разбирает ОДИН вид отказа."""
    def _boom(cmd, **kwargs):
        raise OSError("файл не найден")

    monkeypatch.setattr(context_module, "run_console", _boom)

    with pytest.raises(ContextUnavailable) as info:
        resolve(_settings(_cli(tmp_path)), cache_ttl=0)

    assert "файл не найден" in info.value.detail
