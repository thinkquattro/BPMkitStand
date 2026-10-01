"""
Удалённый режим BPMkit в диспетчере -- запуск/остановка ``BPMkit.exe serve-http``
(``standkit_hub.remote_mode``), настройки ``remote_*`` в ``HubConfig`` и маршруты
``/api/remote/*``. Реальные процессы не запускаются: spawn/alive/stop/probe/секреты --
подставные.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import standkit_hub.remote_mode as rm
import standkit_hub.server as server_module
from standkit_hub.config import (
    DEFAULT_REMOTE_PORT,
    DEFAULT_REMOTE_TOKEN_REF,
    HubConfig,
)
from standkit_hub.remote_mode import (
    RemoteModeController,
    RemoteModeError,
    build_serve_argv,
    connection_url,
    validate_remote_config,
)
from tests.test_hub_server import _close_hub_servers, _request, _start_hub  # noqa: F401

CLI = ["C:/BPMkit/server/BPMkit.exe"]
HEALTH = {"ok": True, "version": "1.2.16", "profile": "default", "tools": 40,
          "transport": "streamable-http", "auth": "bearer"}


def _cfg(tmp_path, **over) -> HubConfig:
    cfg = HubConfig(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class _World:
    """Подставной мир: процессы, порт, секреты."""

    def __init__(self, *, secret=True, cli=CLI, pid=4321):
        self.secret = secret
        self.cli = cli
        self.pid = pid
        self.alive_pids = set()
        self.health = None          # что отвечает /healthz
        self.health_after_spawn = HEALTH
        self.spawned = []
        self.stopped = []
        self.die_on_spawn = False

    def spawn(self, argv, cwd, log_path):
        self.spawned.append((list(argv), Path(log_path)))
        if self.die_on_spawn:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("error: token not found\n", encoding="utf-8")
        else:
            self.alive_pids.add(self.pid)
            self.health = self.health_after_spawn
        return self.pid

    def stop(self, pid, timeout=10.0):
        self.stopped.append(pid)
        self.alive_pids.discard(pid)
        self.health = None
        return True

    def controller(self, cfg, **kw):
        return RemoteModeController(
            cfg,
            cli_resolver=lambda: self.cli,
            spawn=self.spawn,
            alive=lambda pid: pid in self.alive_pids,
            stopper=self.stop,
            probe=lambda host, port: self.health,
            has_secret=lambda ref: self.secret,
            sleep=lambda s: None,
            startup_wait=0.5,
            **kw,
        )


# --- настройки ---


def test_config_defaults_and_roundtrip(tmp_path):
    cfg = HubConfig()
    assert cfg.remote_host == "127.0.0.1"
    assert cfg.remote_port == DEFAULT_REMOTE_PORT == 8766   # не 8765: его занимает локальный агент
    assert cfg.remote_profile == "default"
    assert cfg.remote_token_ref == DEFAULT_REMOTE_TOKEN_REF
    assert cfg.remote_allow_query_token is False and cfg.remote_autostart is False
    path = tmp_path / "hub.json"
    HubConfig(remote_host="0.0.0.0", remote_port=9000, remote_profile="cursor",
              remote_token_ref="my:ref", remote_allow_query_token=True,
              remote_autostart=True).save(path)
    loaded = HubConfig.load(path)
    assert (loaded.remote_host, loaded.remote_port, loaded.remote_profile) == ("0.0.0.0", 9000, "cursor")
    assert loaded.remote_token_ref == "my:ref"
    assert loaded.remote_allow_query_token and loaded.remote_autostart


def test_config_normalizes_garbage():
    cfg = HubConfig.from_dict({"remote_port": "abc", "remote_host": "  ", "remote_profile": " CURSOR ",
                               "remote_token_ref": "", "remote_autostart": 1})
    assert cfg.remote_port == DEFAULT_REMOTE_PORT
    assert cfg.remote_host == "127.0.0.1"
    assert cfg.remote_profile == "cursor"
    assert cfg.remote_token_ref == DEFAULT_REMOTE_TOKEN_REF
    assert cfg.remote_autostart is True
    assert HubConfig.from_dict({"remote_port": 70000}).remote_port == DEFAULT_REMOTE_PORT
    assert HubConfig.from_dict({"remote_port": True}).remote_port == DEFAULT_REMOTE_PORT


def test_validate_remote_config():
    assert validate_remote_config(HubConfig()) == []
    bad = HubConfig()
    bad.remote_host, bad.remote_port, bad.remote_profile, bad.remote_token_ref = "a b", 0, "Bad Profile", ""
    assert len(validate_remote_config(bad)) == 4


# --- argv ---


def test_build_serve_argv_has_ref_but_no_secret():
    cfg = HubConfig(remote_host="0.0.0.0", remote_port=9001, remote_profile="cursor",
                    remote_token_ref="bpmsoft-mcp:remote:token")
    argv = build_serve_argv(cfg, CLI)
    assert argv[:2] == CLI + ["serve-http"]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "9001"
    assert argv[argv.index("--profile") + 1] == "cursor"
    assert argv[argv.index("--token-ref") + 1] == "bpmsoft-mcp:remote:token"
    assert "--allow-query-token" not in argv and "--token" not in argv
    cfg.remote_allow_query_token = True
    assert build_serve_argv(cfg, CLI)[-1] == "--allow-query-token"


def test_connection_url_wildcard_uses_hostname(monkeypatch):
    assert connection_url(HubConfig(remote_host="10.0.0.5", remote_port=9000)) == "http://10.0.0.5:9000/mcp"
    monkeypatch.setattr(rm.socket, "gethostname", lambda: "srv1")
    assert connection_url(HubConfig(remote_host="0.0.0.0", remote_port=8766)) == "http://srv1:8766/mcp"
    assert connection_url(HubConfig(remote_host="::1", remote_port=1)) == "http://[::1]:1/mcp"


# --- контроллер ---


def test_start_spawns_hidden_and_persists_state(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    res = c.start()
    assert res.pid == 4321 and res.healthy is True
    argv, log = w.spawned[0]
    assert argv[:2] == CLI + ["serve-http"]
    assert log == tmp_path / "logs" / "standkit-hub-remote.log"
    state = json.loads((tmp_path / "run" / "standkit-hub-remote.json").read_text(encoding="utf-8"))
    assert state["pid"] == 4321 and state["port"] == 8766
    st = c.status()
    assert st["state"] == "running" and st["healthy"] and st["pid"] == 4321
    assert st["auth"] == "bearer" and st["url"].endswith(":8766/mcp")


def test_start_uses_platform_spawn_hidden_by_default():
    # единая точка запуска без консоли (без мигающих окон): по умолчанию -- standkit.platform.spawn_hidden
    import standkit.platform as plat
    ctrl = RemoteModeController(HubConfig())
    assert ctrl._spawn is plat.spawn_hidden
    assert ctrl._stopper is plat.stop


def test_start_refuses_without_token_secret_and_gives_hint(tmp_path):
    w = _World(secret=False)
    with pytest.raises(RemoteModeError) as ei:
        w.controller(_cfg(tmp_path)).start()
    assert "setup remote-token --generate --show" in str(ei.value)
    assert w.spawned == []


def test_start_hint_adds_ref_for_custom_ref(tmp_path):
    w = _World(secret=False)
    st = w.controller(_cfg(tmp_path, remote_token_ref="my:ref")).status()
    assert st["token_present"] is False
    assert st["token_hint"].endswith("--ref my:ref")


def test_start_refuses_without_cli(tmp_path):
    w = _World(cli=None)
    with pytest.raises(RemoteModeError, match="BPMkit.exe"):
        w.controller(_cfg(tmp_path)).start()
    assert w.spawned == []


def test_start_refuses_invalid_settings(tmp_path):
    w = _World()
    with pytest.raises(RemoteModeError, match="remote_profile"):
        w.controller(_cfg(tmp_path, remote_profile="bad profile")).start()


def test_start_refuses_when_already_running(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    with pytest.raises(RemoteModeError, match="уже запущен"):
        c.start()
    assert len(w.spawned) == 1


def test_start_refuses_when_foreign_server_answers_on_port(tmp_path):
    w = _World()
    w.health = HEALTH   # кто-то уже отвечает на этом порту
    with pytest.raises(RemoteModeError, match="не из диспетчера"):
        w.controller(_cfg(tmp_path)).start()
    assert w.spawned == []


def test_start_reports_instant_exit_with_log_tail(tmp_path):
    w = _World()
    w.die_on_spawn = True
    c = w.controller(_cfg(tmp_path))
    with pytest.raises(RemoteModeError, match="token not found"):
        c.start()
    assert not (tmp_path / "run" / "standkit-hub-remote.json").exists()


def test_stop_terminates_and_clears_state(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    assert c.stop() is True
    assert w.stopped == [4321]
    assert c.status()["state"] == "stopped"
    assert not (tmp_path / "run" / "standkit-hub-remote.json").exists()


def test_stop_without_process_is_noop(tmp_path):
    w = _World()
    assert w.controller(_cfg(tmp_path)).stop() is True
    assert w.stopped == []


def test_state_survives_new_controller_instance(tmp_path):
    w = _World()
    cfg = _cfg(tmp_path)
    w.controller(cfg).start()
    fresh = w.controller(cfg)
    assert fresh.status()["state"] == "running"
    fresh.stop()
    assert w.stopped == [4321]


def test_status_states_and_dead_pid_cleanup(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    assert c.status()["state"] == "stopped"
    c.start()
    w.health = None                      # процесс жив, порт молчит
    assert c.status()["state"] in ("starting", "unresponsive")
    st = json.loads((tmp_path / "run" / "standkit-hub-remote.json").read_text(encoding="utf-8"))
    st["started"] = 1.0                  # давно запущен
    (tmp_path / "run" / "standkit-hub-remote.json").write_text(json.dumps(st), encoding="utf-8")
    assert c.status()["state"] == "unresponsive"
    w.alive_pids.clear()                 # процесс умер
    assert c.status()["state"] == "stopped"
    assert not (tmp_path / "run" / "standkit-hub-remote.json").exists()


def test_status_never_contains_secret_value(tmp_path):
    secret = "S3CR3T-VALUE-NEVER-SHOWN"
    w = _World()
    c = RemoteModeController(
        _cfg(tmp_path), cli_resolver=lambda: CLI, spawn=w.spawn, alive=lambda p: p in w.alive_pids,
        stopper=w.stop, probe=lambda h, p: w.health, sleep=lambda s: None,
        has_secret=lambda ref: True)
    c.start()
    dumped = json.dumps(c.status())
    assert secret not in dumped
    assert "token_present" in dumped and c.status()["token_present"] is True


def test_remote_mode_module_does_not_read_secret_values():
    src = Path(rm.__file__).read_text(encoding="utf-8")
    assert "get_secret" not in src          # только has_secret: значение токена хаб не читает
    assert "subprocess.run(" not in src and "Popen(" not in src   # запуск -- через standkit.platform


# --- автозапуск ---


def test_autostart_disabled_does_nothing(tmp_path):
    w = _World()
    assert rm.autostart(_cfg(tmp_path), cli_resolver=lambda: CLI, spawn=w.spawn) is None
    assert w.spawned == []


def test_autostart_starts_and_reports_refusal(tmp_path):
    w = _World()
    cfg = _cfg(tmp_path, remote_autostart=True)
    kw = dict(cli_resolver=lambda: CLI, spawn=w.spawn, alive=lambda p: p in w.alive_pids,
              probe=lambda h, p: w.health, has_secret=lambda r: True, sleep=lambda s: None,
              startup_wait=0.5)
    assert rm.autostart(cfg, **kw) is None
    assert len(w.spawned) == 1
    assert rm.autostart(cfg, **kw) is None          # уже запущен -- не трогаем
    assert len(w.spawned) == 1
    w2 = _World(secret=False)
    msg = rm.autostart(_cfg(tmp_path / "x", remote_autostart=True), cli_resolver=lambda: CLI,
                       spawn=w2.spawn, has_secret=lambda r: False, probe=lambda h, p: None)
    assert msg and "remote-token" in msg and w2.spawned == []


# --- HTTP API хаба ---


def test_api_remote_status_and_auth(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _body, _ = _request(base_url, "/api/remote/status")
    assert status in (401, 403)
    status, body, _ = _request(base_url, "/api/remote/status", token=token)
    assert status == 200
    assert body["state"] == "stopped" and body["running"] is False
    assert body["url"].endswith(":8766/mcp") and "token_hint" in body


def test_api_remote_start_requires_mutation_auth_and_reports_missing_token(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _b, _ = _request(base_url, "/api/remote/start", token=token, method="POST", body={})
    assert status in (401, 403)         # без Origin -- мутация не проходит
    status, body, _ = _request(base_url, "/api/remote/start", token=token, method="POST",
                               origin=base_url, body={})
    assert status == 400                # секрета нет (двойник хранилища пуст) -- честный отказ
    assert "remote-token" in body["error"] or "BPMkit.exe" in body["error"]
    status, body, _ = _request(base_url, "/api/remote/stop", token=token, method="POST",
                               origin=base_url, body={})
    assert status == 200 and body["ok"] is True and body["state"] == "stopped"


def test_settings_post_roundtrips_remote_fields(tmp_path):
    base_url, token, config_path, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/settings", token=token, method="POST", origin=base_url,
                               body={"remote_port": 9100, "remote_autostart": True,
                                     "remote_profile": "cursor"})
    assert status == 200 and body["remote_port"] == 9100
    loaded = HubConfig.load(config_path)
    assert loaded.remote_port == 9100 and loaded.remote_autostart and loaded.remote_profile == "cursor"
    status, body, _ = _request(base_url, "/api/settings", token=token)
    assert body["defaults"]["remote_port"] == 8766


# --- UI (статически) ---


def test_ui_has_remote_pane_and_wiring():
    web = Path(server_module.__file__).parent / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert 'data-pane="mcp-remote"' in html
    for name in ("remote_host", "remote_port", "remote_profile", "remote_token_ref",
                 "remote_allow_query_token", "remote_autostart"):
        assert f'name="{name}"' in html
    for ident in ("mcpremote-start-btn", "mcpremote-stop-btn", "mcpremote-url", "mcpremote-token-hint"):
        assert f'id="{ident}"' in html
    assert "/api/remote/start" in js and "/api/remote/stop" in js and "/api/remote/status" in js
    assert '"remote_token_ref"' in js
