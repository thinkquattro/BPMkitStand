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


@pytest.fixture(autouse=True)
def _never_run_real_cli(monkeypatch):
    """Статус токена спрашивает CLI BPMkit. Набор не запускает настоящий процесс:
    по умолчанию «токена нет» (rc=1); тесты хранилища подставляют свой двойник."""
    rm.invalidate_token_cache()
    monkeypatch.setattr(rm, "default_cli_runner", lambda argv, env=None, timeout=None: (1, "", ""))
    yield
    rm.invalidate_token_cache()
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
        self.image = "BPMkit.exe"

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
            image_of=lambda pid: self.image,
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


def test_reused_pid_of_foreign_process_is_not_stopped(tmp_path):
    # pid переиспользован чужим процессом: порт молчит, образ не BPMkit -- не убиваем, чистим состояние
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    w.health = None
    w.image = "notepad.exe"
    assert c.stop() is True
    assert w.stopped == []
    assert not (tmp_path / "run" / "standkit-hub-remote.json").exists()


def test_status_treats_foreign_pid_as_stopped(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    w.health = None
    w.image = "chrome.exe"
    assert c.status()["state"] == "stopped"


def test_unknown_image_is_treated_as_foreign(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    w.health = None
    w.image = None
    assert c.stop() is True and w.stopped == []


def test_healthz_confirms_ownership_even_if_image_unknown(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    w.image = None                      # образ узнать не удалось, но /healthz BPMkit отвечает
    assert c.stop() is True and w.stopped == [4321]


def test_python_source_launch_is_recognised(tmp_path):
    w = _World()
    c = w.controller(_cfg(tmp_path))
    c.start()
    w.health = None
    w.image = "python.exe"
    assert c.status()["running"] is True


def test_default_image_of_unknown_pid_is_none():
    assert rm.default_image_of(2 ** 30) is None


def test_concurrent_starts_spawn_once(tmp_path):
    import threading as th
    w = _World()
    cfg = _cfg(tmp_path)
    results = []

    def go():
        try:
            w.controller(cfg).start()
            results.append("ok")
        except RemoteModeError as exc:
            results.append(str(exc))

    threads = [th.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(w.spawned) == 1
    assert results.count("ok") == 1


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
    assert 'id="mcpremote-query-warn"' in html and "updateRemoteQueryWarning" in js
    assert "после закрытия диспетчера" in html


# ====================================================================================
# токен проверяется и задаётся в ТОМ хранилище, что читает serve-http
# ====================================================================================


class _FakeBpmkitCli:
    """Подставной ``BPMkit.exe setup remote-token``: хранилище секретов BPMkit -- словарь.

    Повторяет контракт CLI: ``--status`` rc 0/1, ``--generate`` не перезаписывает без
    ``--force``, ``--from-env`` берёт значение из ПЕРЕМЕННОЙ ОКРУЖЕНИЯ и требует >= 16 символов."""

    def __init__(self, prefix=CLI):
        self.prefix = list(prefix)
        self.store: dict = {}
        self.calls: list = []
        self.envs: list = []
        self.returncode_override = None

    def __call__(self, argv, env=None, timeout=None):
        self.calls.append(list(argv))
        self.envs.append(dict(env) if env else None)
        if self.returncode_override is not None:
            return self.returncode_override, "", "usage: error"
        tail = list(argv)[len(self.prefix):]
        assert tail[:2] == ["setup", "remote-token"], tail
        ref = tail[tail.index("--ref") + 1]
        force = "--force" in tail
        if "--status" in tail:
            return (0 if ref in self.store else 1), json.dumps({"ok": ref in self.store}), ""
        if ref in self.store and not force:
            return 1, json.dumps({"ok": False, "error": "токен уже задан. Перезапись -- только осознанно, флагом --force"}), ""
        if "--generate" in tail:
            self.store[ref] = "generated-%d" % len(self.calls)
            return 0, json.dumps({"ok": True, "ref": ref}), ""
        if "--from-env" in tail:
            name = tail[tail.index("--from-env") + 1]
            value = (env or {}).get(name, "")
            if len(value) < 16:
                return 1, json.dumps({"ok": False, "error": "значение переменной короче 16 символов"}), ""
            self.store[ref] = value
            return 0, json.dumps({"ok": True, "ref": ref}), ""
        raise AssertionError(tail)


@pytest.fixture(autouse=False)
def fresh_token_cache():
    rm.invalidate_token_cache()
    yield
    rm.invalidate_token_cache()


def _ctl(tmp_path, cli, world=None, **cfg_over):
    w = world or _World()
    return w, RemoteModeController(
        _cfg(tmp_path, **cfg_over), cli_resolver=lambda: w.cli, spawn=w.spawn,
        alive=lambda pid: pid in w.alive_pids, stopper=w.stop, probe=lambda h, p: w.health,
        sleep=lambda s: None, image_of=lambda pid: w.image, startup_wait=0.5, cli_runner=cli)


class TestGap612TokenInBpmkitStore:
    def test_status_asks_bpmkit_store_not_standkit_store(self, tmp_path, monkeypatch, fresh_token_cache):
        """Круг, найденный приёмкой 13.10: секрет в хранилище standkit хаб считал токеном."""
        monkeypatch.setattr(rm, "_default_has_secret", lambda ref: True)     # есть ТОЛЬКО у standkit
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli)

        assert c.status()["token_present"] is False

    def test_token_created_by_hubs_own_command_is_seen(self, tmp_path, monkeypatch, fresh_token_cache):
        monkeypatch.setattr(rm, "_default_has_secret", lambda ref: False)    # standkit пуст
        cli = _FakeBpmkitCli()
        cli.store[DEFAULT_REMOTE_TOKEN_REF] = "x" * 32                       # `setup remote-token --generate`
        _w, c = _ctl(tmp_path, cli)

        assert c.status()["token_present"] is True

    def test_status_is_cached_but_start_is_fresh(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        cli.store[DEFAULT_REMOTE_TOKEN_REF] = "x" * 32
        w, c = _ctl(tmp_path, cli)

        c.status()
        c.status()
        assert len(cli.calls) == 1                      # опрос UI не плодит процессы BPMkit.exe
        c.start()
        assert len(cli.calls) == 2                      # а перед запуском проверка свежая

    def test_generate_then_start_end_to_end(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        w, c = _ctl(tmp_path, cli)
        with pytest.raises(RemoteModeError, match="Сгенерировать токен"):
            c.start()
        assert w.spawned == []

        ok, message = c.generate_token()
        assert ok is True and "создан" in message
        assert c.status()["token_present"] is True       # кэш сброшен созданием токена

        result = c.start()
        argv = w.spawned[0][0]
        assert argv[:len(CLI) + 1] == CLI + ["serve-http"]
        assert argv[argv.index("--token-ref") + 1] == DEFAULT_REMOTE_TOKEN_REF
        assert result.healthy is True
        # значение токена не показано и в argv запуска его нет
        assert cli.store[DEFAULT_REMOTE_TOKEN_REF] not in " ".join(argv)

    def test_generate_does_not_show_token(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli)
        c.generate_token()

        assert "--show" not in cli.calls[-1]

    def test_generate_does_not_overwrite_existing_token_without_force(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        cli.store[DEFAULT_REMOTE_TOKEN_REF] = "old-token-value-0000"
        _w, c = _ctl(tmp_path, cli)

        ok, message = c.generate_token()
        assert ok is False and "уже задан" in message
        assert cli.store[DEFAULT_REMOTE_TOKEN_REF] == "old-token-value-0000"
        ok, _message = c.generate_token(force=True)
        assert ok is True and cli.store[DEFAULT_REMOTE_TOKEN_REF] != "old-token-value-0000"

    def test_set_token_goes_to_bpmkit_store_via_environment_not_argv(self, tmp_path, fresh_token_cache):
        value = "ValueTypedByHuman-0123456789"
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli, remote_token_ref="my:ref")

        ok, message = c.set_token(value)

        assert ok is True
        assert cli.store["my:ref"] == value
        argv, env = cli.calls[-1], cli.envs[-1]
        assert value not in " ".join(argv)                  # в argv значения нет
        assert env == {rm.TOKEN_ENV_VAR: value}             # оно уходит переменной окружения
        assert "--force" in argv                            # человек сознательно меняет токен
        assert value not in message

    def test_set_token_reports_cli_refusal(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli)

        ok, message = c.set_token("short")

        assert ok is False and "короче 16" in message
        assert DEFAULT_REMOTE_TOKEN_REF not in cli.store

    def test_without_cli_nothing_is_present_and_messages_name_the_cli(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli, world=_World(cli=None))

        assert c.status()["token_present"] is False
        assert "BPMkit.exe" in c.generate_token()[1]
        assert "BPMkit.exe" in c.set_token("v" * 20)[1]
        assert cli.calls == []

    def test_old_cli_without_status_command_falls_back_to_standkit_store(
            self, tmp_path, monkeypatch, fresh_token_cache):
        monkeypatch.setattr(rm, "_default_has_secret", lambda ref: True)
        cli = _FakeBpmkitCli()
        cli.returncode_override = 2                         # argparse: неизвестная подкоманда
        _w, c = _ctl(tmp_path, cli)

        assert c.status()["token_present"] is True

    def test_missing_cli_is_reported_before_missing_token(self, tmp_path, fresh_token_cache):
        cli = _FakeBpmkitCli()
        _w, c = _ctl(tmp_path, cli, world=_World(cli=None))

        with pytest.raises(RemoteModeError, match="BPMkit.exe"):
            c.start()

    def test_module_never_passes_token_value_in_argv(self):
        src = Path(rm.__file__).read_text(encoding="utf-8")
        assert '"--show"' not in src                        # значение хаб не просит показать


class TestGap612TokenApi:
    @pytest.fixture()
    def hub(self, tmp_path, monkeypatch, fresh_token_cache):
        cli = _FakeBpmkitCli()
        monkeypatch.setattr(rm, "default_cli_runner", cli)
        monkeypatch.setattr(rm.license_api, "find_cli", lambda settings, **kw: list(CLI))
        monkeypatch.setattr(rm, "_default_has_secret", lambda ref: False)
        base_url, token, config_path, *_ = _start_hub(tmp_path)
        return base_url, token, cli

    def test_secret_status_of_remote_ref_comes_from_bpmkit_store(self, hub):
        base_url, token, cli = hub
        ref = DEFAULT_REMOTE_TOKEN_REF
        status, body, _ = _request(base_url, "/api/secret/" + ref.replace(":", "%3A"), token=token)
        assert status == 200 and body["has_secret"] is False
        cli.store[ref] = "z" * 32
        rm.invalidate_token_cache()
        status, body, _ = _request(base_url, "/api/secret/" + ref.replace(":", "%3A"), token=token)
        assert body["has_secret"] is True

    def test_set_button_writes_remote_token_to_bpmkit_store(self, hub):
        base_url, token, cli = hub
        value = "HumanChosenToken-0123456789"
        status, body, _ = _request(base_url, "/api/secret/" + DEFAULT_REMOTE_TOKEN_REF.replace(":", "%3A"),
                                   token=token, method="POST", origin=base_url, body={"value": value})
        assert status == 200 and body["ok"] is True
        assert cli.store[DEFAULT_REMOTE_TOKEN_REF] == value
        assert value not in json.dumps(body)

    def test_set_button_shows_cli_refusal(self, hub):
        base_url, token, cli = hub
        status, body, _ = _request(base_url, "/api/secret/" + DEFAULT_REMOTE_TOKEN_REF.replace(":", "%3A"),
                                   token=token, method="POST", origin=base_url, body={"value": "tiny"})
        assert status == 400 and "короче 16" in body["error"]

    def test_generate_endpoint_requires_mutation_auth_and_hides_value(self, hub):
        base_url, token, cli = hub
        status, _b, _ = _request(base_url, "/api/remote/token/generate", token=token, method="POST", body={})
        assert status in (401, 403)
        status, body, _ = _request(base_url, "/api/remote/token/generate", token=token, method="POST",
                                   origin=base_url, body={})
        assert status == 200 and body["token_present"] is True
        assert cli.store[DEFAULT_REMOTE_TOKEN_REF] not in json.dumps(body)
        status, body, _ = _request(base_url, "/api/remote/token/generate", token=token, method="POST",
                                   origin=base_url, body={})
        assert status == 400 and "уже задан" in body["error"]
        status, body, _ = _request(base_url, "/api/remote/token/generate", token=token, method="POST",
                                   origin=base_url, body={"force": True})
        assert status == 200

    def test_ui_has_generate_token_button(self):
        web = Path(server_module.__file__).parent / "web"
        assert 'id="mcpremote-generate-token-btn"' in (web / "index.html").read_text(encoding="utf-8")
        assert "/api/remote/token/generate" in (web / "app.js").read_text(encoding="utf-8")
