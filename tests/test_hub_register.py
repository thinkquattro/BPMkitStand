"""
Тесты ``POST /api/stand/register`` (standkit_hub.server::_api_stand_register)
— кнопка "Зарегистрировать стенд" веб-дашборда. Пишет запись УЖЕ существующего
стенда в общий реестр (projects.json), НЕ провижининг.

Переиспользует лёгкие хелперы поднятия хаба в daemon-потоке из
tests/test_hub_server.py (тот же паттерн, без реальной сети/БД).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import standkit_hub.server as server_module
from standkit.registry import Registry
from standkit_hub.server import _REGISTER_ALLOWED_FIELDS

from tests.test_hub_server import _request, _start_hub, _write_config


WEB_DIR = Path(server_module.__file__).parent / "web"

# Поля реестра, доехавшие до формы регистрации по GAP-001/GAP-003. Без них
# стенд за TLS и адрес Redis настраивались только правкой projects.json руками
# (и, соответственно, перезапуском агента).
_NEW_REGISTER_FIELDS = ("stand_scheme", "verify_tls", "redis_host", "redis_port", "logs_dir")


def _app_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _client_register_field_names() -> list[str]:
    """
    Имена из ``app.js::_REGISTER_FIELD_NAMES`` — читаем исходник статически
    (тот же приём, что в tests/test_hub_pwa_and_compact.py для style.css:
    JS-движка в тестах нет, а рассинхрон списков ловить надо).
    """
    match = re.search(r"const _REGISTER_FIELD_NAMES = \[(.*?)\];", _app_js(), re.S)
    assert match, "в app.js не найден список _REGISTER_FIELD_NAMES"
    return re.findall(r'"([^"]+)"', match.group(1))


def _collect_register_payload_source() -> str:
    """Тело ``app.js::collectRegisterPayload`` — для регрессий по его логике."""
    match = re.search(
        r"function collectRegisterPayload\(form\) \{(.*?)\n  \}", _app_js(), re.S
    )
    assert match, "в app.js не найдена функция collectRegisterPayload"
    return match.group(1)


# --- успех: запись реально появляется в файле реестра ---


def test_register_minimal_local_stand_succeeds_and_persists(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "new-stand",
        "transport": "local",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "new-stand"),
        "stand_host": "127.0.0.1",
        "stand_port": "5050",
        "db_type": "postgres",
        "db_host": "127.0.0.1",
        "db_port": "5432",
        "db_name": "new_stand_db",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )

    assert status == 200
    assert body == {"ok": True, "name": "new-stand"}

    reloaded = Registry.load(registry_path)
    assert "new-stand" in reloaded
    stand = reloaded.get("new-stand")
    assert stand.stand_dir == str(tmp_path / "new-stand")
    assert stand.stand_port == 5050
    assert stand.db_name == "new_stand_db"
    # исходная запись "existing" не пострадала
    assert "existing" in reloaded


def test_register_docker_stand_with_all_conditional_fields(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "docker-stand",
        "transport": "local",
        "host_kind": "docker",
        "stand_dir": str(tmp_path / "docker-stand"),
        "docker_container": "bpmsoft-docker-stand",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("docker-stand")
    assert stand.host_kind.value == "docker"
    assert stand.docker_container == "bpmsoft-docker-stand"


def test_register_agent_stand_with_agent_fields(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "remote-stand",
        "transport": "agent",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "remote-stand"),
        "agent_url": "https://remote-host:8765",
        "agent_secret_ref": "standkit:remote-stand:agent-token",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("remote-stand")
    assert stand.transport.value == "agent"
    assert stand.agent_url == "https://remote-host:8765"
    assert stand.agent_secret_ref == "standkit:remote-stand:agent-token"


# --- 400: валидация ---


def test_register_missing_name_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={"stand_dir": str(tmp_path / "x")},
    )
    assert status == 400
    assert "name" in body.get("fields", [])


def test_register_agent_transport_without_agent_url_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-agent-stand",
            "transport": "agent",
            "stand_dir": str(tmp_path / "bad-agent-stand"),
        },
    )
    assert status == 400
    assert "error" in body


def test_register_docker_host_kind_without_container_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-docker-stand",
            "host_kind": "docker",
            "stand_dir": str(tmp_path / "bad-docker-stand"),
        },
    )
    assert status == 400
    assert "error" in body


def test_register_invalid_transport_value_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-transport-stand",
            "transport": "teleport",
            "stand_dir": str(tmp_path / "bad-transport-stand"),
        },
    )
    assert status == 400
    assert "transport" in body.get("fields", [])


def test_register_non_numeric_port_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-port-stand",
            "stand_dir": str(tmp_path / "bad-port-stand"),
            "stand_port": "not-a-number",
        },
    )
    assert status == 400
    assert "stand_port" in body.get("fields", [])


def test_register_password_field_is_rejected_with_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "pwd-stand",
            "stand_dir": str(tmp_path / "pwd-stand"),
            "db_password": "hunter2",
        },
    )
    assert status == 400
    assert "db_password" in body.get("fields", [])


def test_register_password_field_never_reaches_registry(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")
    _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "pwd-stand-2",
            "stand_dir": str(tmp_path / "pwd-stand-2"),
            "db_password": "hunter2",
        },
    )
    raw = registry_path.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    assert "pwd-stand-2" not in raw


# --- 409: дубликат имени ---


def test_register_duplicate_name_returns_409_and_does_not_overwrite(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={"name": "existing", "stand_dir": str(tmp_path / "some-other-dir")},
    )
    assert status == 409
    assert "error" in body

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("existing")
    # исходный stand_dir не перезаписан
    assert stand.stand_dir == str(tmp_path / "existing")


# --- 401/403: CSRF / Origin ---


def test_register_without_token_is_unauthorized_or_forbidden(tmp_path):
    base_url, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        method="POST",
        origin=base_url,
        body={"name": "no-token-stand", "stand_dir": str(tmp_path / "no-token-stand")},
    )
    assert status in (401, 403)


def test_register_with_cookie_only_no_header_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        cookie_token=token,
        method="POST",
        origin=base_url,
        body={"name": "cookie-only-stand", "stand_dir": str(tmp_path / "cookie-only-stand")},
    )
    assert status == 403


def test_register_with_wrong_origin_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin="http://evil.example.com",
        body={"name": "evil-origin-stand", "stand_dir": str(tmp_path / "evil-origin-stand")},
    )
    assert status == 403


def test_register_with_wrong_token_is_forbidden(tmp_path):
    base_url, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        token="wrong-token",
        method="POST",
        origin=base_url,
        body={"name": "wrong-token-stand", "stand_dir": str(tmp_path / "wrong-token-stand")},
    )
    assert status == 403


# --- согласованность формы, клиентского и серверного белых списков (GAP-001/GAP-003) ---


def test_new_registry_fields_are_in_both_client_and_server_field_lists():
    """
    Форма регистрации собирается по ДВУМ независимым белым спискам: что
    отправит клиент (``app.js::_REGISTER_FIELD_NAMES``) и что примет сервер
    (``server.py::_REGISTER_ALLOWED_FIELDS``). Именно рассинхрон этих списков и
    был причиной GAP-001: поля появились в модели, но ни в один список не
    попали, и стенд за TLS настраивался только правкой projects.json.
    """
    client = set(_client_register_field_names())
    for field in _NEW_REGISTER_FIELDS:
        assert field in client, f"{field} не собирается формой (app.js)"
        assert field in _REGISTER_ALLOWED_FIELDS, f"{field} не принимается сервером"


def test_client_register_field_names_are_a_subset_of_server_whitelist():
    """
    Всё, что клиент кладёт в тело запроса, сервер должен уметь принять — иначе
    поле молча выбрасывается и оператор считает, что значение сохранено.
    Исключение ровно одно: ``name`` — ключ записи реестра, сервер разбирает его
    отдельной веткой и в белом списке полей его нет.
    """
    client = set(_client_register_field_names())
    assert "name" in client
    assert "name" not in _REGISTER_ALLOWED_FIELDS
    unknown = client - {"name"} - set(_REGISTER_ALLOWED_FIELDS)
    assert not unknown, f"клиент шлёт поля, неизвестные серверу: {sorted(unknown)}"


def test_register_form_markup_has_controls_for_new_registry_fields():
    """Список в app.js бесполезен, если в разметке нет самих полей."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    form = html.split('<form id="register-form">', 1)[1].split("</form>", 1)[0]
    for field in _NEW_REGISTER_FIELDS:
        assert f'name="{field}"' in form, f"в форме регистрации нет поля {field}"

    # stand_scheme — именно select с двумя допустимыми значениями модели.
    assert re.search(r'<select name="stand_scheme">', form)
    for option in ('value="http"', 'value="https"'):
        assert option in form

    # verify_tls — чекбокс (булево поле), включённый по умолчанию, и живёт в
    # условном блоке: при stand_scheme=http флаг ничего не значит (GAP-001 п.3).
    assert re.search(r'<input type="checkbox" name="verify_tls" checked />', form)
    assert 'data-when-scheme="https"' in form


def test_collect_register_payload_has_dedicated_checkbox_branch():
    """
    Регрессия на GAP-001 п.2. У чекбокса ``input.value`` — это "on" независимо
    от состояния флажка, поэтому общая ветка «взять value, пустое пропустить»
    для него не годится: снятый verify_tls либо приехал бы как "on", либо
    исчез бы из тела и молча получил дефолт модели ``verify_tls=true``.
    JS-движка в тестах нет — проверяем наличие ветки по исходнику.
    """
    body = _collect_register_payload_source()
    assert 'input.type === "checkbox"' in body
    assert "input.checked" in body


def test_collect_register_payload_skips_hidden_conditional_blocks():
    """
    Скрытый условный блок не должен слать значения: при stand_scheme=http
    чекбокс verify_tls невидим, и его состояние не имеет смысла — на сервере
    должен остаться дефолт. Для текстовых полей это раньше выходило само собой
    (пустая строка отсекалась), для чекбокса — нет.
    """
    body = _collect_register_payload_source()
    assert ".register-conditional" in body
    assert "block.hidden" in body


def test_register_tls_stand_with_redis_and_logs_dir_persists_new_fields(tmp_path):
    """
    Критерий приёмки GAP-001/GAP-003: стенд за TLS с self-signed и адресом
    Redis регистрируется ЦЕЛИКОМ из формы. Тело — ровно то, что соберёт
    collectRegisterPayload: порты строками (значения ``<input type="number">``),
    verify_tls — настоящий JSON-bool, а не строка "on"/"off".
    """
    base_url, token, _config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "tls-stand",
        "transport": "local",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "tls-stand"),
        "logs_dir": str(tmp_path / "tls-stand" / "Logs"),
        "stand_scheme": "https",
        "verify_tls": False,
        "stand_host": "10.0.0.10",
        "stand_port": "5443",
        "redis_host": "10.0.0.11",
        "redis_port": "6379",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200, body

    stand = Registry.load(registry_path).get("tls-stand")
    assert stand.stand_scheme == "https"
    assert stand.verify_tls is False
    assert stand.stand_port == 5443
    assert stand.redis_host == "10.0.0.11"
    # redis_port приводится к int так же, как stand_port/db_port, — иначе
    # строка уехала бы в реестр и падала уже внутри пробы (GAP-003).
    assert stand.redis_port == 6379
    assert stand.logs_dir == str(tmp_path / "tls-stand" / "Logs")


def test_register_half_configured_redis_pair_returns_400(tmp_path):
    """
    GAP-003 п.2: половина пары redis_host/redis_port — явная ошибка формы, а не
    молчаливый ``unknown`` в дашборде.
    """
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "half-redis-stand",
            "stand_dir": str(tmp_path / "half-redis-stand"),
            "redis_host": "10.0.0.11",
        },
    )
    assert status == 400
    assert "redis" in body.get("error", "").lower()


# --- доверие к сертификату агента в форме регистрации (GAP-008) ---

# Пара полей канала «хаб → агент». Их отсутствие в форме и было половиной
# GAP-008: оператор находил verify_tls, снимал его и не понимал, почему агент
# с самоподписанным сертификатом всё равно недоступен.
_AGENT_TRUST_FIELDS = ("agent_ca", "agent_verify_tls")


def test_agent_trust_fields_are_in_both_client_and_server_field_lists():
    client = set(_client_register_field_names())
    for field in _AGENT_TRUST_FIELDS:
        assert field in client, f"{field} не собирается формой (app.js)"
        assert field in _REGISTER_ALLOWED_FIELDS, f"{field} не принимается сервером"


def test_register_form_markup_has_agent_trust_controls_in_the_agent_block():
    """
    Поля должны лежать ИМЕННО в условном блоке транспорта agent: при
    transport=local они бессмысленны, а скрытый блок клиент не отправляет.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    form = html.split('<form id="register-form">', 1)[1].split("</form>", 1)[0]
    agent_block = form.split('data-when-transport="agent"', 1)[1].split("</div>", 1)[0]

    assert 'name="agent_ca"' in agent_block
    assert '<input type="checkbox" name="agent_verify_tls" checked />' in agent_block
    # Различие «проба стенда / канал до агента» проговорено прямо в форме —
    # именно на нём спотыкается оператор.
    assert "verify_tls" in agent_block


def test_register_agent_stand_with_trust_fields_persists(tmp_path):
    """
    Критерий приёмки GAP-008 со стороны формы: стенд за агентом с
    самоподписанным сертификатом регистрируется целиком из дашборда.
    """
    base_url, token, _config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    ca_path = tmp_path / "agent.crt"
    payload = {
        "name": "trusted-agent-stand",
        "transport": "agent",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "trusted-agent-stand"),
        "agent_url": "https://remote-host:8765",
        "agent_secret_ref": "standkit:trusted-agent-stand:agent-token",
        "agent_ca": str(ca_path),
        "agent_verify_tls": True,
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200, body

    stand = Registry.load(registry_path).get("trusted-agent-stand")
    assert stand.agent_ca == str(ca_path)
    assert stand.agent_verify_tls is True
    # Доверие к агенту не имеет отношения к пробе стенда — её флаг остался дефолтным.
    assert stand.verify_tls is True


def test_register_agent_stand_with_verification_disabled_persists_false(tmp_path):
    """
    Снятый чекбокс приезжает настоящим JSON-bool и обязан сохраниться как
    false: молчаливый откат к дефолту true — это и есть «оператор думает, что
    выключил, а он нет».
    """
    base_url, token, _config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "insecure-agent-stand",
        "transport": "agent",
        "stand_dir": str(tmp_path / "insecure-agent-stand"),
        "agent_url": "https://remote-host:8765",
        "agent_verify_tls": False,
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200, body

    stand = Registry.load(registry_path).get("insecure-agent-stand")
    assert stand.agent_verify_tls is False


def test_register_rejects_non_boolean_agent_verify_tls(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-bool-stand",
            "transport": "agent",
            "stand_dir": str(tmp_path / "bad-bool-stand"),
            "agent_url": "https://remote-host:8765",
            "agent_verify_tls": "ага",
        },
    )
    assert status == 400
    assert "agent_verify_tls" in body.get("fields", [])


def test_register_rejects_agent_ca_over_plain_http(tmp_path):
    """
    Валидация модели доезжает до формы: agent_ca при agent_url на http:// не
    применится никогда, и молчать об этом нельзя.
    """
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "http-agent-stand",
            "transport": "agent",
            "stand_dir": str(tmp_path / "http-agent-stand"),
            "agent_url": "http://remote-host:8765",
            "agent_ca": str(tmp_path / "agent.crt"),
        },
    )
    assert status == 400
    assert "agent_ca" in body.get("error", "")
