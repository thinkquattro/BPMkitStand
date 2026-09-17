"""
Статические тесты подсказок-«?» у полей «Настройки → Основные» (GAP-311,
решение владельца 16.09.2026).

Контекст: поле «CLI BPMkit» и его длинное описание владельцу не понравились;
«Каталог pid-файлов» и «Каталог логов» были пустыми («не задано») и никак не
объясняли, зачем они нужны. Решение — маленькая иконка-вопрос рядом с
подписью КАЖДОГО поля «Основные» с подсказкой по наведению и по клавиатурному
фокусу, вместо длинных серых пояснений под полями.

Тесты не поднимают хаб и не ходят в сеть — только читают web/index.html и
web/style.css, как test_hub_elevation_ui.py и test_hub_companion_api.py.
"""

from __future__ import annotations

import re

import standkit_hub.server as server_module

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _general_pane_html(html: str) -> str:
    """Вырезает разметку панели «Основные» (data-pane="general") до начала
    следующей панели («Лицензия»), чтобы не цепляться за подсказки в других
    разделах настроек."""
    # Рейка разделов настроек тоже несёт data-pane="general"/"license" на
    # своих кнопках — берём разметку самой ПАНЕЛИ (settings-pane), а не
    # первое совпадение атрибута в документе.
    start = html.index('<div class="settings-pane active" data-pane="general">')
    end = html.index('data-pane="license"', start)
    return html[start:end]


#: Поля панели «Основные», как их видит форма (см. app.js::SETTINGS_FIELDS) —
#: каждое обязано получить свою иконку-подсказку.
GENERAL_FIELDS = (
    "registry_path",
    "companion_mcp_cli",
    "run_dir",
    "log_dir",
    "refresh_interval_sec",
    # Д-3/GAP-276: автовыход по простою. Подсказка обязана назвать оба условия
    # простоя (вкладка закрыта И нет стендов) и что 0 выключает — иначе поле
    # читается как «через сколько диспетчер убьёт мои стенды».
    "idle_shutdown_min",
)


def test_every_general_field_has_field_help_icon():
    pane = _general_pane_html(_read("index.html"))
    for field in GENERAL_FIELDS:
        # Подпись поля и .field-help должны идти внутри одного <label>, ДО
        # самого input — иначе аудит попадёт на чужое поле.
        idx = pane.index(f'name="{field}"')
        before = pane[:idx]
        label_start = before.rindex("<label>")
        between = pane[label_start:idx]
        assert 'class="field-help"' in between, (
            f"у поля {field} нет иконки-подсказки field-help"
        )


def test_field_help_icons_have_nonempty_tip_and_aria_label():
    pane = _general_pane_html(_read("index.html"))
    matches = re.findall(
        r'<span class="field-help"[^>]*?aria-label="([^"]*)"[^>]*?data-tip="([^"]*)"',
        pane,
        re.S,
    )
    assert len(matches) == len(GENERAL_FIELDS), (
        f"ожидали {len(GENERAL_FIELDS)} иконок field-help в «Основные», "
        f"нашли {len(matches)}"
    )
    for aria_label, data_tip in matches:
        assert aria_label.strip(), "aria-label пустой — экранный диктор промолчит"
        assert data_tip.strip(), "data-tip пустой — подсказка будет пустой"
        # aria-label и data-tip — один и тот же текст (см. комментарий в CSS:
        # один источник текста, чтобы не разъехались).
        assert aria_label == data_tip


def test_field_help_icons_are_keyboard_focusable_and_have_inline_svg():
    pane = _general_pane_html(_read("index.html"))
    icon_blocks = re.findall(r'<span class="field-help"[^>]*>(.*?)</span>', pane, re.S)
    assert len(icon_blocks) == len(GENERAL_FIELDS)
    for block, opening in zip(
        icon_blocks,
        re.findall(r'<span class="field-help"[^>]*>', pane),
    ):
        assert 'tabindex="0"' in opening, "иконка недоступна с клавиатуры (нет tabindex)"
        assert 'role="img"' in opening
        assert "<svg" in block, "нет инлайн-SVG «?» внутри иконки"


def test_cli_field_keeps_only_short_status_hint():
    """Длинное описание под «CLI BPMkit» убрано; остаётся только короткая
    строка статуса cli-current-hint, которую рендерит renderCliHint (app.js) —
    её ломать нельзя."""
    pane = _general_pane_html(_read("index.html"))
    idx = pane.index('name="companion_mcp_cli"')
    label_start = pane.rindex("<label>", 0, idx)
    label_end = pane.index("</label>", idx)
    label_html = pane[label_start:label_end]

    assert 'id="cli-current-hint"' in label_html
    # Старое длинное пояснение под полем убрано целиком — текст переехал в
    # data-tip/aria-label иконки field-help, а под полем (в <small
    # class="field-hint"> без id) больше ничего не осталось.
    small_hints = re.findall(r'<small class="field-hint"[^>]*>', label_html)
    assert len(small_hints) == 1, f"под CLI BPMkit осталось {len(small_hints)} field-hint, ожидали 1"
    assert small_hints[0] == '<small class="field-hint" id="cli-current-hint">'


def test_no_old_long_hints_under_run_dir_and_log_dir_and_registry():
    """Раньше у «Реестр стендов» и «Каталог логов» были короткие field-hint,
    а у «Каталог pid-файлов» не было вовсе никакого объяснения — все три
    заменены иконкой field-help, под полем ничего не остаётся."""
    pane = _general_pane_html(_read("index.html"))
    for field in ("registry_path", "run_dir", "log_dir", "refresh_interval_sec"):
        idx = pane.index(f'name="{field}"')
        label_start = pane.rindex("<label>", 0, idx)
        label_end = pane.index("</label>", idx)
        label_html = pane[label_start:label_end]
        assert "field-hint" not in label_html, (
            f"у поля {field} остался старый field-hint под полем"
        )


def test_field_help_tip_texts_mention_real_defaults():
    """Тексты подсказок сверены с кодом (не выдуманы): run_dir/log_dir по
    умолчанию — ``~/.standkit/run`` и ``~/.standkit/logs`` (см.
    HubConfig.resolve_run_dir и standkit_agent.__main__.resolve_agent_paths),
    а не «не задано» и не «рядом со стендом»."""
    pane = _general_pane_html(_read("index.html"))

    run_dir_idx = pane.index('name="run_dir"')
    run_dir_label = pane[pane.rindex("<label>", 0, run_dir_idx):pane.index("</label>", run_dir_idx)]
    assert r".standkit\run" in run_dir_label or ".standkit/run" in run_dir_label

    log_dir_idx = pane.index('name="log_dir"')
    log_dir_label = pane[pane.rindex("<label>", 0, log_dir_idx):pane.index("</label>", log_dir_idx)]
    assert r".standkit\logs" in log_dir_label or ".standkit/logs" in log_dir_label
    # Прежняя (неверная) формулировка не должна вернуться.
    assert "рядом со стендом" not in log_dir_label


def test_field_help_css_has_hover_and_focus_visible_rules():
    css = _read("style.css")
    assert ".field-help" in css
    assert ".field-help:hover::after" in css
    assert ".field-help:focus-visible::after" in css
    # Подсказка не завязана на конкретную тему — цвета только через переменные.
    field_help_block = css[css.index(".field-help {"):]
    assert "var(--bpmkit-" in field_help_block


def test_field_help_ui_has_no_external_resources():
    """Как и весь дашборд — новая разметка не тащит внешних ресурсов."""
    for name in ("index.html", "style.css"):
        text = _read(name)
        for forbidden in ("<script src=\"http", "@import", "fonts.googleapis",
                          "cdn.jsdelivr", "unpkg.com"):
            assert forbidden not in text, f"{name}: внешний ресурс {forbidden}"
