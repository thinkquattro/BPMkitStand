# -*- coding: utf-8 -*-
"""
GAP-780: ``/api/secret/<ref>`` отвечал 400 на любой ref с двоеточием.

Страница (``standkit_hub/web/app.js``) строит путь через
``encodeURIComponent(ref)`` — двоеточие уходит в сеть как ``%3A``
(живой симптом у клиента: ``GET /api/secret/bpmsoft-mcp%3Aremote%3Atoken 400``).
Сервер матчил регэксп по ``urlparse(self.path).path`` без ``unquote``, и
whitelist ``validate_secret_ref`` видел ``%``. Тесты в ``test_hub_server.py``
слали голое двоеточие — браузер так не делает, поэтому дефект жил с июля.

Здесь — ровно то, что шлёт браузер: ``%3A`` для GET/POST/DELETE, плюс
границы раскодирования (``%2F`` и двойное кодирование честно отсекаются).
"""

from __future__ import annotations

import re
from pathlib import Path

import standkit_hub.server as server_module
from tests.test_hub_server import _request, _start_hub

REF = "bpmsoft-mcp:remote:token"
ENCODED = "bpmsoft-mcp%3Aremote%3Atoken"


def test_get_secret_with_encoded_colon_is_200(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    seen = {}

    def _has(ref):
        seen["ref"] = ref
        return True

    monkeypatch.setattr(server_module, "has_secret", _has)
    status, body, _ = _request(base_url, f"/api/secret/{ENCODED}", token=token)
    assert status == 200, body
    assert body == {"ref": REF, "has_secret": True}
    assert seen["ref"] == REF, "в хранилище должен уйти РАСКОДИРОВАННЫЙ ref"


def test_post_secret_with_encoded_colon_saves_under_decoded_ref(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    captured = {}

    def _fake_set(ref, value):
        captured["ref"] = ref
        captured["value"] = value

    monkeypatch.setattr(server_module, "set_secret", _fake_set)
    status, body, _ = _request(
        base_url, f"/api/secret/{ENCODED}", token=token, method="POST",
        origin=base_url, body={"value": "s3cr3t"},
    )
    assert status == 200, body
    assert body == {"ok": True, "ref": REF}
    assert captured == {"ref": REF, "value": "s3cr3t"}


def test_delete_secret_with_encoded_colon_deletes_decoded_ref(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    deleted = []
    monkeypatch.setattr(server_module, "delete_secret", lambda ref: deleted.append(ref))
    status, body, _ = _request(
        base_url, f"/api/secret/{ENCODED}", token=token, method="DELETE", origin=base_url,
    )
    assert status == 200, body
    assert body == {"ok": True, "ref": REF}
    assert deleted == [REF]


def test_lowercase_percent_encoding_is_accepted(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    monkeypatch.setattr(server_module, "has_secret", lambda ref: False)
    status, body, _ = _request(base_url, "/api/secret/standkit%3ademo%3aagent-token", token=token)
    assert status == 200, body
    assert body == {"ref": "standkit:demo:agent-token", "has_secret": False}


def test_decoded_slash_is_still_rejected(tmp_path, monkeypatch):
    """``%2F`` раскрывается в ``/`` — whitelist обязан отсечь, раскодирование
    не расширяет множество допустимых ref'ов."""
    base_url, token, *_ = _start_hub(tmp_path)
    monkeypatch.setattr(server_module, "has_secret", lambda ref: True)
    status, body, _ = _request(base_url, "/api/secret/a%2Fb", token=token)
    assert status == 400
    assert body.get("error") == "invalid secret ref"


def test_double_encoding_is_decoded_once_and_rejected(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    monkeypatch.setattr(server_module, "has_secret", lambda ref: True)
    status, body, _ = _request(base_url, "/api/secret/a%253Ab", token=token)
    assert status == 400
    assert body.get("error") == "invalid secret ref"


def test_page_still_encodes_ref_so_server_must_decode():
    """Страница кодирует ref — сервер обязан раскодировать (контракт пары)."""
    js = (Path(server_module.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"/api/secret/\$\{encodeURIComponent\(ref\)\}", js)
    src = Path(server_module.__file__).read_text(encoding="utf-8")
    for verb in ("get", "post", "delete"):
        assert f"self._api_secret_{verb}(_secret_ref_from_path(m))" in src


def test_stand_names_survive_encode_uri_component_unchanged():
    """Соседние эндпоинты с именем в пути (``/api/stand/<name>/...``) этим
    дефектом не страдают: whitelist имени стенда ``[A-Za-z0-9_.-]`` целиком
    входит в множество символов, которые ``encodeURIComponent`` НЕ кодирует.
    Тест фиксирует это допущение: расширят whitelist — придётся раскодировать
    и имя стенда."""
    from standkit_hub import security

    unreserved = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
    pattern = security._STAND_NAME_RE.pattern
    m = re.match(r"^\^\[(?P<cls>[^\]]+)\]", pattern)
    assert m, pattern
    cls = m.group("cls")
    expanded = set()
    i = 0
    while i < len(cls):
        if i + 2 < len(cls) and cls[i + 1] == "-":
            expanded.update(chr(c) for c in range(ord(cls[i]), ord(cls[i + 2]) + 1))
            i += 3
        else:
            expanded.add(cls[i])
            i += 1
    assert expanded <= unreserved, sorted(expanded - unreserved)
