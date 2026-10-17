"""
Тесты standkit_hub.config.HubConfig — БЕЗ веб-слоя (модуль config.py
намеренно не импортирует http.server, чтобы быть тестируемым в изоляции).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from standkit.registry import default_registry_path
from standkit_hub.config import CompanionSettings, HubConfig, RemoteAgent


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = HubConfig.load(tmp_path / "does_not_exist.json")

    assert cfg.registry_path == str(default_registry_path())
    assert cfg.run_dir == ""
    assert cfg.log_dir == ""
    assert cfg.refresh_interval_sec == 10
    assert cfg.agents == []
    assert cfg.agent_host == "127.0.0.1"
    assert cfg.agent_port == 8765
    assert cfg.token_ref == ""
    assert cfg.readonly_token_ref == ""
    assert cfg.tls_cert == ""
    assert cfg.tls_key == ""
    assert cfg.tls_client_ca == ""
    assert cfg.insecure is False
    assert cfg.audit_log == ""
    assert cfg.lockout_max_failures == 5
    assert cfg.lockout_window_sec == 300.0


def test_save_load_roundtrip_preserves_all_fields(tmp_path):
    path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(
        registry_path=str(tmp_path / "projects.json"),
        run_dir=str(tmp_path / "run"),
        log_dir=str(tmp_path / "logs"),
        refresh_interval_sec=30,
        agents=[
            RemoteAgent(name="remote-a", url="https://a:8765", token_ref="standkit:a:agent-token"),
            RemoteAgent(name="remote-b", url="https://b:8765", token_ref="standkit:b:agent-token"),
        ],
        agent_host="0.0.0.0",
        agent_port=9999,
        token_ref="standkit:local:agent-token",
        readonly_token_ref="standkit:local:agent-readonly-token",
        tls_cert=str(tmp_path / "agent.crt"),
        tls_key=str(tmp_path / "agent.key"),
        tls_client_ca=str(tmp_path / "clients-ca.crt"),
        insecure=True,
        audit_log=str(tmp_path / "audit.log"),
        lockout_max_failures=10,
        lockout_window_sec=120.5,
    )
    cfg.save(path)

    reloaded = HubConfig.load(path)

    assert reloaded == cfg


def test_save_writes_only_token_refs_not_secret_values(tmp_path):
    path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(
        token_ref="standkit:local:agent-token",
        readonly_token_ref="standkit:local:agent-readonly-token",
        agents=[RemoteAgent(name="remote", url="https://remote:8765", token_ref="standkit:remote:agent-token")],
    )
    cfg.save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))

    # Только ссылки на секреты (*_ref) — ни одного поля с "живым" значением токена.
    assert raw["token_ref"] == "standkit:local:agent-token"
    assert raw["readonly_token_ref"] == "standkit:local:agent-readonly-token"
    assert raw["agents"][0]["token_ref"] == "standkit:remote:agent-token"
    assert "token" not in {k for k in raw.keys()} - {"token_ref", "readonly_token_ref"}
    assert set(raw["agents"][0].keys()) == {"name", "url", "token_ref"}


def test_load_tolerates_bom(tmp_path):
    path = tmp_path / "standkit-hub.json"
    payload = {"refresh_interval_sec": 42, "agent_host": "127.0.0.1"}
    path.write_text(json.dumps(payload), encoding="utf-8-sig")

    cfg = HubConfig.load(path)

    assert cfg.refresh_interval_sec == 42
    assert cfg.agent_host == "127.0.0.1"


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "standkit-hub.json"
    payload = {"refresh_interval_sec": 5, "future_field_from_newer_hub": "whatever"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.refresh_interval_sec == 5
    assert not hasattr(cfg, "future_field_from_newer_hub")


def test_load_corrupt_json_falls_back_to_defaults(tmp_path):
    path = tmp_path / "standkit-hub.json"
    path.write_text("{not valid json", encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.registry_path == str(default_registry_path())


def test_config_path_lives_under_bpmkit_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = HubConfig.config_path()

    assert result == tmp_path / ".config" / "BPMkit" / "standkit-hub.json"


def test_save_creates_parent_directory(tmp_path):
    nested = tmp_path / "a" / "b" / "standkit-hub.json"
    cfg = HubConfig()

    cfg.save(nested)

    assert nested.exists()


def test_ensure_registry_dir_creates_missing_folder(tmp_path):
    # Свежая машина: папки реестра (напр. %APPDATA%\BPMkit) ещё нет.
    reg_path = tmp_path / "does_not_exist" / "BPMkit" / "projects.json"
    cfg = HubConfig(registry_path=str(reg_path))

    created = cfg.ensure_registry_dir()

    assert created == reg_path.parent
    assert reg_path.parent.is_dir()
    # Идемпотентно: повторный вызов не падает.
    assert cfg.ensure_registry_dir() == reg_path.parent


def test_to_dict_from_dict_roundtrip():
    cfg = HubConfig(refresh_interval_sec=99, agents=[RemoteAgent(name="x", url="http://x", token_ref="r")])

    rebuilt = HubConfig.from_dict(cfg.to_dict())

    assert rebuilt == cfg


# --- секция канала обновлений (GAP-241) --------------------------------------


def test_companion_defaults_match_gap241_policy(tmp_path):
    """Дефолты канала: паттерны раз в 6 часов, релизы включены и раз в сутки.

    Раньше паттерны опрашивались каждые полчаса (бессмысленно часто для markdown,
    который издатель правит раз в недели), а релизы приходилось включать руками —
    ключ подписи артефактов и HTTPS бэкенда тогда ещё не были в строю.
    """
    cfg = HubConfig.load(tmp_path / "нет-файла.json")

    assert cfg.companion.enabled is True
    assert cfg.companion.patterns.enabled is True
    assert cfg.companion.patterns.interval_sec == 21600
    assert cfg.companion.releases.enabled is True
    assert cfg.companion.releases.interval_sec == 86400


def test_old_config_without_companion_section_still_loads(tmp_path):
    """Конфиг, написанный до появления секции канала, читается как есть."""
    path = tmp_path / "standkit-hub.json"
    path.write_text(json.dumps({"refresh_interval_sec": 15, "agent_port": 8765}),
                    encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.refresh_interval_sec == 15
    assert cfg.companion == CompanionSettings()


def test_old_companion_section_is_read_field_by_field(tmp_path):
    """Старый файл со ВСЕМИ прежними полями обязан читаться без потерь.

    Поля, которых больше нет в интерфейсе (`backend_url`, `revocations`,
    `require_pattern_signature`), остаются в конфиге и продолжают действовать —
    это override издателя, а не мусор: молча их обнулить значило бы сменить адрес
    бэкенда и режим подписи у тех, кто их задал.
    """
    path = tmp_path / "standkit-hub.json"
    path.write_text(json.dumps({"companion": {
        "enabled": True,
        "backend_url": "https://updates.example",
        "mcp_cli": "C:/BPMkit/bpmkit.exe",
        "patterns": {"enabled": True, "interval_sec": 1800},
        "releases": {"enabled": False, "interval_sec": 86400},
        "revocations": {"enabled": False, "interval_sec": 900},
        "auto_stage_release": True,
        "require_pattern_signature": True,
    }}), encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.companion.backend_url == "https://updates.example"
    assert cfg.companion.mcp_cli == "C:/BPMkit/bpmkit.exe"
    # Прежний интервал паттернов сохранён: новый дефолт не перебивает явный выбор.
    assert cfg.companion.patterns.interval_sec == 1800
    # Как и явно выключенные релизы — «включено по умолчанию» относится к НОВЫМ
    # установкам, а не к чужому сохранённому решению.
    assert cfg.companion.releases.enabled is False
    assert cfg.companion.revocations.interval_sec == 900
    assert cfg.companion.require_pattern_signature is True


def test_companion_section_roundtrips_through_save(tmp_path):
    path = tmp_path / "standkit-hub.json"
    cfg = HubConfig()
    cfg.companion = CompanionSettings(enabled=True, backend_url="https://updates.example",
                                      mcp_cli="python -m bpmkit")
    cfg.save(path)

    assert HubConfig.load(path).companion == cfg.companion


# --- цикл релизов включён по умолчанию, явное «выключено» уважается ----------------


class TestReleasesDefaultOn:
    """С 0.12.26 документы приведены к факту кода: проверка новых версий включена по умолчанию.

    Три обещания, которые дают документы (докстринг канала, SECURITY.md, кукбуки):
    (1) у новой установки цикл релизов включён; докуда идёт автоматика, решает режим
    обновлений (`update_mode`, по умолчанию `auto` — см. ``TestUpdateMode``);
    (2) явное ``releases.enabled = false`` в конфиге клиента — его решение, и ни чтение
    конфига, ни частичное сохранение формы настроек, ни пересохранение его не перетирают;
    (3) тексты в репозитории не утверждают обратного.
    """

    _REPO = Path(__file__).resolve().parent.parent

    def test_new_install_checks_releases(self, tmp_path):
        cfg = HubConfig.load(tmp_path / "нет-файла.json")

        assert cfg.companion.releases.enabled is True
        assert cfg.companion.update_mode == "auto"

    def test_section_without_enabled_key_gets_default_on(self):
        cfg = CompanionSettings.from_dict({"releases": {"interval_sec": 7200}})

        assert cfg.releases.enabled is True
        assert cfg.releases.interval_sec == 7200

    def test_explicit_false_survives_save_and_reload(self, tmp_path):
        path = tmp_path / "standkit-hub.json"
        path.write_text(json.dumps({"companion": {"releases": {"enabled": False}}}),
                        encoding="utf-8")

        HubConfig.load(path).save(path)
        cfg = HubConfig.load(path)

        assert cfg.companion.releases.enabled is False
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["companion"]["releases"]["enabled"] is False

    def test_partial_settings_post_keeps_explicit_false(self):
        """Форма прислала только интервал или только главный рубильник — флаг цикла цел."""
        from standkit_hub.server import merge_companion_section

        current = CompanionSettings.from_dict({"releases": {"enabled": False}}).to_dict()

        for incoming in ({"releases": {"interval_sec": 7200}},
                         {"enabled": True},
                         {"auto_stage_release": True}):
            merged = merge_companion_section(current, incoming)
            assert CompanionSettings.from_dict(merged).releases.enabled is False, incoming

    def test_docs_do_not_claim_releases_are_off_by_default(self):
        """Формулировки прежнего дефолта: «(`releases.enabled = false`)» в скобках —
        так документы называли умолчание, «дефолт `false`», «релизов выключен по
        умолчанию». Фраза «явное `releases.enabled = false` уважается» — не про дефолт."""
        stale = (re.compile(r"\(`?releases\.enabled\s*=\s*false`?\)"),
                 re.compile(r"дефолт\s+`?false"),
                 re.compile(r"(?i)(релиз\w*|цикл)[^.;]{0,40}выключен\w*\W+по умолчанию"))
        offenders = []
        for rel in ("standkit_companion/__init__.py", "SECURITY.md", "docs/ROADMAP.md",
                    "docs/BACKLOG.md", "README.md"):
            text = (self._REPO / rel).read_text(encoding="utf-8-sig")
            for line in text.splitlines():
                if any(rx.search(line) for rx in stale):
                    offenders.append("{}: {}".format(rel, line.strip()))
        assert not offenders, "\n".join(offenders)

    def test_companion_docstring_names_the_default(self):
        import standkit_companion

        assert "releases.enabled=true" in (standkit_companion.__doc__ or "")


# --- режим обновлений MCP (update_mode) и кольцо раскатки (update_ring) -----------------


class TestUpdateMode:
    """Режим `auto`/`notify`/`off` — одно поле вместо пары флажков; кольцо `early`/`stable`.

    Обещания: (1) установка без поля режима получает `auto`, кроме явного «не проверять
    релизы» — тогда `off`; (2) `releases.enabled` — зеркало режима (`off` ⇔ цикл не
    тикает); (3) запись секции — read-modify-write: незнакомые ключи переживают
    пересохранение; (4) прежний флажок, присланный без режима, переводится в режим.
    """

    def test_new_install_is_auto_stable(self, tmp_path):
        cfg = HubConfig.load(tmp_path / "нет-файла.json")

        assert cfg.companion.update_mode == "auto"
        assert cfg.companion.update_ring == "stable"
        assert cfg.companion.stage_releases is True

    def test_existing_config_without_mode_is_auto(self):
        cfg = CompanionSettings.from_dict({"releases": {"enabled": True},
                                           "auto_stage_release": False})

        assert cfg.update_mode == "auto"
        assert cfg.releases.enabled is True

    def test_explicit_releases_off_without_mode_is_off(self):
        cfg = CompanionSettings.from_dict({"releases": {"enabled": False}})

        assert cfg.update_mode == "off"
        assert cfg.releases.enabled is False
        assert cfg.stage_releases is False

    def test_mode_is_primary_and_mirrors_releases_flag(self):
        on = CompanionSettings.from_dict({"update_mode": "notify",
                                          "releases": {"enabled": False}})
        off = CompanionSettings.from_dict({"update_mode": "off",
                                           "releases": {"enabled": True}})

        assert (on.update_mode, on.releases.enabled) == ("notify", True)
        assert (off.update_mode, off.releases.enabled) == ("off", False)

    def test_garbage_mode_is_notify_with_problem(self):
        """Незнакомый режим — не «поля нет»: что выбрано, не понять, поэтому `notify`
        (или `off` при явно выключенной проверке) и признак `settings_unreadable`."""
        from standkit_hub.config import SETTINGS_UNREADABLE

        always = CompanionSettings.from_dict({"update_mode": "always"})
        assert (always.update_mode, always.load_problem) == ("notify", SETTINGS_UNREADABLE)
        off = CompanionSettings.from_dict({"update_mode": 1, "releases": {"enabled": False}})
        assert (off.update_mode, off.load_problem) == ("off", SETTINGS_UNREADABLE)
        clean = CompanionSettings.from_dict({"update_mode": " NOTIFY "})
        assert (clean.update_mode, clean.load_problem) == ("notify", "")
        assert CompanionSettings.from_dict({}).load_problem == "", "поля нет — не проблема"
        assert "load_problem" not in always.to_dict(), "факт чтения в файл не пишется"

    def test_missing_file_is_auto_without_problem(self, tmp_path):
        cfg = HubConfig.load(tmp_path / "нет-файла.json").companion
        assert (cfg.update_mode, cfg.load_problem) == ("auto", "")

    @pytest.mark.parametrize("content", [
        '{"companion": {"update_mode": "auto", "releases": {"enabled": tru',
        "",
        "[1, 2]",
        '{"companion": "мусор"}',
    ])
    def test_broken_config_is_notify_with_problem(self, tmp_path, content):
        """Битый файл (обрезанный, пустой, не объект, секция не объект) — дефолты, но не
        выбор человека: `notify` и `settings_unreadable` (автоматика не ставит)."""
        from standkit_hub.config import SETTINGS_UNREADABLE

        path = tmp_path / "standkit-hub.json"
        path.write_text(content, encoding="utf-8")
        cfg = HubConfig.load(path).companion
        assert cfg.load_problem == SETTINGS_UNREADABLE
        assert cfg.update_mode == "notify"

    def test_ring_normalized_to_stable(self):
        assert CompanionSettings.from_dict({"update_ring": "early"}).update_ring == "early"
        assert CompanionSettings.from_dict({"update_ring": "canary"}).update_ring == "stable"
        assert CompanionSettings.from_dict({}).update_ring == "stable"

    def test_mode_and_ring_roundtrip_and_unknown_keys_survive(self, tmp_path):
        path = tmp_path / "standkit-hub.json"
        path.write_text(json.dumps({"companion": {
            "update_mode": "notify", "update_ring": "early",
            "future_key": {"x": 1}}}), encoding="utf-8")

        HubConfig.load(path).save(path)
        on_disk = json.loads(path.read_text(encoding="utf-8"))["companion"]

        assert on_disk["update_mode"] == "notify"
        assert on_disk["update_ring"] == "early"
        assert on_disk["future_key"] == {"x": 1}, "запись — read-modify-write"
        assert on_disk["releases"]["enabled"] is True

    def test_constructor_with_releases_off_is_saved_as_off(self, tmp_path):
        """Настройки, собранные конструктором с выключенным циклом, — это `off` и после
        записи/чтения (действующий режим, а не сырое поле)."""
        from standkit_hub.config import CompanionCycle, effective_update_mode

        settings = CompanionSettings(releases=CompanionCycle(enabled=False))

        assert effective_update_mode(settings) == "off"
        assert CompanionSettings.from_dict(settings.to_dict()).update_mode == "off"

    def test_legacy_toggle_sent_without_mode_becomes_mode(self):
        from standkit_hub.config import sync_legacy_releases_toggle

        auto = CompanionSettings().to_dict()
        off = CompanionSettings.from_dict({"update_mode": "off"}).to_dict()

        assert sync_legacy_releases_toggle(
            auto, {"releases": {"enabled": False}})["update_mode"] == "off"
        assert sync_legacy_releases_toggle(
            off, {"releases": {"enabled": True}})["update_mode"] == "notify", (
            "флажок означал «проверять», а не «устанавливать без меня»")
        assert sync_legacy_releases_toggle(
            auto, {"releases": {"enabled": True}})["update_mode"] == "auto"
        assert sync_legacy_releases_toggle(
            auto, {"update_mode": "notify", "releases": {"enabled": False}}
        ).get("update_mode") == "auto", "присланный режим главнее флажка — его ставит merge"
