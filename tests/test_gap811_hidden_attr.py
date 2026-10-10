# -*- coding: utf-8 -*-
"""
Атрибут `hidden` в разметке диспетчера обязан реально скрывать элемент.

Класс, у которого в CSS задан `display: ...`, перебивает UA-правило для `[hidden]`:
скрытый элемент остаётся видимым. Так предупреждение про адрес не loopback
(`#mcpremote-query-warn`, класс `field-hint`) было видно всегда. Тест статический:
для каждого элемента из `index.html` с атрибутом `hidden` проверяет, что у каждого из его
классов, получивших `display` в style.css, есть парное правило `<селектор>[hidden]` с
`display: none` (или общее `[hidden] { display: none !important }`).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import standkit_hub.server as server_module

WEB_DIR = Path(server_module.__file__).parent / "web"

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_DISPLAY = re.compile(r"(?:^|[;\s])display\s*:\s*([^;]+)", re.IGNORECASE)


def _css_rules():
    css = _COMMENT.sub("", (WEB_DIR / "style.css").read_text(encoding="utf-8"))
    for sel, body in _RULE.findall(css):
        m = _DISPLAY.search(body)
        display = m.group(1).strip().lower() if m else None
        for one in sel.split(","):
            one = one.strip()
            if one:
                yield one, display


def _last_compound(selector):
    return re.split(r"[\s>+~]+", selector.strip())[-1]


def _classes_of(compound):
    return set(re.findall(r"\.([\w-]+)", compound))


class _HiddenCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = []  # (id, [classes])

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "hidden" in d:
            self.hidden.append((d.get("id") or tag, (d.get("class") or "").split()))


def _hidden_elements():
    parser = _HiddenCollector()
    parser.feed((WEB_DIR / "index.html").read_text(encoding="utf-8"))
    return parser.hidden


def test_field_hint_hidden_rule_present():
    rules = list(_css_rules())
    assert (".field-hint[hidden]", "none") in rules


def test_mcpremote_query_warn_is_hidden_field_hint():
    by_id = dict(_hidden_elements())
    assert "field-hint" in by_id["mcpremote-query-warn"]


def test_every_hidden_element_class_with_display_has_hidden_rule():
    rules = list(_css_rules())
    if any(sel == "[hidden]" and disp and disp.startswith("none") for sel, disp in rules):
        return  # общее правило закрывает всё
    hide_classes = set()
    for sel, disp in rules:
        if "[hidden]" in sel and disp == "none":
            hide_classes |= _classes_of(_last_compound(sel))
    display_classes = set()
    for sel, disp in rules:
        if "[hidden]" not in sel and disp not in (None, "none"):
            display_classes |= _classes_of(_last_compound(sel))
    # Элемент закрыт, если хотя бы один его класс имеет парное `[hidden]`-правило
    # (`.elevation-btn[hidden]` закрывает и `.topbar-btn` на том же узле).
    problems = sorted({
        (el_id, cls)
        for el_id, classes in _hidden_elements()
        if not any(c in hide_classes for c in classes)
        for cls in classes if cls in display_classes})
    assert not problems, (
        "элементы с атрибутом hidden, у класса которых задан display, но нет правила "
        "`.<класс>[hidden] {{ display: none; }}`: {}".format(problems))
