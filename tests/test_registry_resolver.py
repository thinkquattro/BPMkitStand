"""
Тесты резолвера пути реестра (standkit.registry.default_registry_path /
bpmkit_config_dir) — единый реестр с BPMkit MCP.

Цепочка (см. registry.py):
    1. env BPMSOFT_PROJECTS_FILE, если путь существует;
    2. канонический путь кита (%APPDATA%\\BPMkit\\projects.json на Windows,
       $XDG_CONFIG_HOME/BPMkit/projects.json либо ~/.config/BPMkit/projects.json
       на POSIX);
    3. фолбэк ./projects.json (текущая рабочая директория), если существует;
    4. иначе — канонический путь п.2 (даже если файла там ещё нет).
"""

from __future__ import annotations

from pathlib import Path

from standkit.registry import bpmkit_config_dir, default_registry_path


def _clear_registry_env(monkeypatch):
    monkeypatch.delenv("BPMSOFT_PROJECTS_FILE", raising=False)


# --- bpmkit_config_dir ---

def test_bpmkit_config_dir_windows(monkeypatch):
    monkeypatch.setattr("standkit.registry.sys.platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\tester\AppData\Roaming")

    result = bpmkit_config_dir()

    assert result == Path(r"C:\Users\tester\AppData\Roaming") / "BPMkit"


def test_bpmkit_config_dir_windows_missing_appdata_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr("standkit.registry.sys.platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = bpmkit_config_dir()

    assert result == tmp_path / "AppData" / "Roaming" / "BPMkit"


def test_bpmkit_config_dir_posix_uses_xdg_config_home(monkeypatch):
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/tester/.config")

    result = bpmkit_config_dir()

    assert result == Path("/home/tester/.config") / "BPMkit"


def test_bpmkit_config_dir_posix_missing_xdg_falls_back_to_dot_config(monkeypatch, tmp_path):
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = bpmkit_config_dir()

    assert result == tmp_path / ".config" / "BPMkit"


# --- default_registry_path: приоритет env ---

def test_env_var_takes_priority_when_path_exists(monkeypatch, tmp_path):
    _clear_registry_env(monkeypatch)
    env_registry = tmp_path / "env_projects.json"
    env_registry.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BPMSOFT_PROJECTS_FILE", str(env_registry))

    result = default_registry_path()

    assert result == env_registry


def test_env_var_ignored_when_path_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    monkeypatch.setenv("BPMSOFT_PROJECTS_FILE", str(tmp_path / "does_not_exist.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconf"))

    result = default_registry_path()

    # env указывает на несуществующий файл — падаем на канонический путь кита.
    assert result == tmp_path / "xdgconf" / "BPMkit" / "projects.json"


# --- default_registry_path: канонический путь кита ---

def test_canonical_path_used_when_it_exists(monkeypatch, tmp_path):
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    xdg = tmp_path / "xdgconf"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    canonical = xdg / "BPMkit" / "projects.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}", encoding="utf-8")

    # cwd-фолбэк тоже существует, но канонический путь должен победить (шаг 2 раньше шага 3).
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "projects.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(cwd)

    result = default_registry_path()

    assert result == canonical


def test_returns_canonical_path_even_if_nothing_exists(monkeypatch, tmp_path):
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    xdg = tmp_path / "xdgconf"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(tmp_path)  # ./projects.json тоже не существует

    result = default_registry_path()

    assert result == xdg / "BPMkit" / "projects.json"
    assert not result.exists()


# --- default_registry_path: фолбэк на ./projects.json ---

def test_cwd_fallback_used_when_canonical_missing(monkeypatch, tmp_path):
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    xdg = tmp_path / "xdgconf"  # канонического файла тут не будет
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    cwd = tmp_path / "standalone"
    cwd.mkdir()
    (cwd / "projects.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(cwd)

    result = default_registry_path()

    assert result == cwd / "projects.json"
