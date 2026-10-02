# -*- coding: utf-8 -*-
"""Гейт правила R18: у каждого выпуска в docs/CHANGELOG.md есть строка-ссылка на PyPI.

Формат Keep a Changelog: раздел `## [X.Y.Z] — дата` и внизу файла строка
`[X.Y.Z]: https://pypi.org/project/standkit/X.Y.Z/`. Ссылку раньше вносили руками после
выкладки и забывали (для 0.12.16 её не было); теперь её готовит `tools/publish_standkit.py`
(dev-репо BPMkit-dev, шаг docs-sync), а этот тест не даёт пропуску вернуться.

Граница `FIRST_COVERED`: разделы 0.12.0..0.12.9 ссылок никогда не имели (их ввели с 0.12.10).
Задним числом их не проставляем -- часть тех версий могла не выходить на PyPI, а ссылка на
несуществующую страницу хуже отсутствия. Правило действует для версий >= FIRST_COVERED.
"""
import os
import re

CHANGELOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "CHANGELOG.md")
FIRST_COVERED = (0, 12, 10)

_HEADER = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_LINK = re.compile(r"^\[(\d+\.\d+\.\d+)\]:\s*(\S+)\s*$", re.MULTILINE)


def _vt(version):
    return tuple(int(x) for x in version.split("."))


def _text():
    with open(CHANGELOG, encoding="utf-8-sig", newline="") as fh:
        return fh.read()


def test_every_release_since_first_covered_has_pypi_link():
    text = _text()
    links = dict(_LINK.findall(text))
    missing = [v for v in _HEADER.findall(text)
               if _vt(v) >= FIRST_COVERED and v not in links]
    assert not missing, (
        "в docs/CHANGELOG.md нет строк-ссылок для: {}. Добавьте внизу файла "
        "`[X.Y.Z]: https://pypi.org/project/standkit/X.Y.Z/` (их готовит docs-sync "
        "tools/publish_standkit.py после выкладки на PyPI).".format(", ".join(missing)))


def test_links_point_to_the_matching_pypi_page():
    bad = [(v, url) for v, url in _LINK.findall(_text())
           if url != "https://pypi.org/project/standkit/{}/".format(v)]
    assert not bad, "ссылка не на свою страницу PyPI: {}".format(bad)


def test_no_link_without_release_section():
    text = _text()
    headers = set(_HEADER.findall(text))
    orphans = [v for v, _u in _LINK.findall(text) if v not in headers]
    assert not orphans, "ссылки без раздела `## [X.Y.Z]`: {}".format(orphans)

