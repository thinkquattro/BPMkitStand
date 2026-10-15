# -*- coding: utf-8 -*-
"""Гигиена публичного репозитория: служебный контекст разработки не уезжает наружу.

Репозиторий публичный (MIT). Собственный реестр (docs/GAPs/GAP-001.., docs/adr/0001..)
легитимен; номера реестров, цитаты решений, имя и пути внутреннего репозитория, пути и логин
машины разработчика, имена его стендов и хоста -- нет.

Правила (сканируются tracked + новые untracked текстовые файлы, бинарники пропускаются):
  STRICT, всегда падение:
    win_user_path   -- C:\\Users\\<реальное имя> (плейсхолдеры Имя/User/tester/<user>/%USERNAME% -- нет);
    publisher_path  -- каталог разработки на C:, логин разработчика (по хэшу);
    integrator      -- название интегратора (слово и префикс, включая почтовый домен).
  BASELINE, падение только сверх tests/public_hygiene_baseline.json (файл -> правило -> число):
    dev_reg         -- номера внешнего реестра: GAP >= 100, OA-NNN, CAND-N, ADR-NNNN вне docs/adr/;
    owner_decision  -- цитаты решений;
    dev_term        -- служебные понятия процесса разработки;
    private_repo    -- имя внутреннего репозитория (рабочий ref секрета keyring -- допустим);
    publisher_stand / publisher_host -- имена стендов и хоста разработчика (по хэшам).
Названия клиентов проверяет гейт корневого репозитория BPMkit (стоп-лист там).

Бейзлайн только убывает. Пересобрать после чистки:
    python tests/test_public_hygiene.py --write-baseline
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_BASELINE = Path(__file__).resolve().parent / "public_hygiene_baseline.json"

_PRIVATE = "bpmsoft" + "-mcp"
_INTEGRATOR = "".join(map(chr, (103, 98, 99)))

_GAP = re.compile(r"(?<!\w)GAP-[1-9]\d{2,}(?![0-9])")
_OA = re.compile(r"(?<!\w)OA-\d{3}(?![0-9])")
_CAND = re.compile(r"(?<!\w)CAND-\d+")
_ADR = re.compile(r"(?<!\w)ADR-(\d{4})(?![0-9])")
_OWNER = re.compile("[\u0420\u0440]\u0435\u0448\u0435\u043d\u0438\\w*\\s+"
                    "\u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\\w*")
_DEV_TERM = re.compile("|".join((
    r"\b[Dd]ev-\u0440\u0435\u043f\u043e\w*", r"\bBPMkit" + "-dev\\b", r"\bDEV" + "LOG\\b",
    r"\bmerge" + "-steps\\b", "\\b[\u041c\u043c]\u0435\u0440\u0434\u0436\u0435\u0440\\w*",
    r"(?<![\w.])_wt" + r"[\\/]", r"\bRESERVATIONS" + r"\.md\b")))
_PRIVATE_RE = re.compile(r"(?i)(?<![\w-])" + re.escape(_PRIVATE)
                         + r"(?:(?::|%3A)[\w.]+)*(?![\w\\/-])")
_PRIVATE_OK = re.compile(r"(?i)^" + re.escape(_PRIVATE) + r"(?::|%3A)remote(?::|%3A)token$")
_PRIVATE_PATH = re.compile(r"(?i)" + re.escape(_PRIVATE) + r"[\\/]")
_DEV_DRIVE = re.compile(r"(?i)C:[\\/]+Anthropic\b")
_WIN_USER = re.compile(r"(?i)C:(?:\\|/)+Users(?:\\|/)+([^\\/\s\"'<>|:*?`,;)\]]+)")
_INTEGRATOR_RE = re.compile(r"(?i)(?<![A-Za-z0-9])" + _INTEGRATOR)
_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_]+")

#: sha256(имя)[:16] -- имена стендов разработчика (сами имена в публичный репо не пишутся).
_STAND_H = frozenset((
    "925bbaf6094520d2", "045cf37d4cb7c112", "6d99b031b1c70cf8", "42920c9e070bc7dd",
    "afc860b390259e01", "55dbb354deb155e4", "0f916789d300a986", "43c07660377c8bba",
    "ce82bb0ac4c10e81", "fad52f145aa267d6", "b771c7705895bfa0", "4bee8f198f525000",
    "d2bfc8025ea4935a", "ed882b45e7d23de0", "e02567745d563fe1", "1b4f0e9851971998",
    "1cd28b89c7adcd53"))
_HOST_H = frozenset(("88b0a9d93e6e0416",))
_LOGIN_H = frozenset(("80b86daf1cde6fa3",))

_USER_PLACEHOLDERS = frozenset((
    "\u0438\u043c\u044f", "\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c",
    "name", "user", "username", "user_name", "tester", "test", "testuser", "example", "you",
    "your_name", "yourname", "me", "someone", "alice", "bob", "public", "default", "all users",
    "...", "\u2026", "x", "u"))

STRICT = frozenset(("win_user_path", "publisher_path", "integrator"))

_SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".pytest_cache", ".venv", "node_modules"}
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".exe", ".dll", ".pyc",
             ".pyd", ".whl", ".gz", ".mcpb", ".sqlite", ".docx", ".xlsx", ".pptx", ".woff",
             ".woff2", ".ttf")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _own_adrs(repo: Path) -> frozenset:
    nums = {"%04d" % i for i in range(1, 8)}
    adr = repo / "docs" / "adr"
    if adr.is_dir():
        nums.update(m.group(1) for m in (re.match(r"(\d{4})-", p.name) for p in adr.iterdir()) if m)
    return frozenset(nums)


def _placeholder(name: str) -> bool:
    n = name.strip().casefold()
    return n in _USER_PLACEHOLDERS or n[:1] in "<%{$[" or n.startswith(("...", "\u2026"))


def _tokens(line: str):
    """Имена-токены (с подчёркиванием -- до 3 частей, жадно по длине)."""
    for run in _RUN.finditer(line):
        parts = run.group(0).split("_")
        i = 0
        while i < len(parts):
            for j in range(min(len(parts), i + 3), i, -1):
                tok = "_".join(parts[i:j])
                if tok and (_h(tok) in _STAND_H or _h(tok) in _HOST_H
                            or _h(tok.lower()) in _LOGIN_H):
                    yield tok
                    i = j
                    break
            else:
                i += 1


def scan_text(text: str, own_adrs: frozenset):
    """[(line, rule, fragment)]"""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        for rx in (_GAP, _OA, _CAND):
            out += [(no, "dev_reg", m.group(0)) for m in rx.finditer(line)]
        out += [(no, "dev_reg", m.group(0)) for m in _ADR.finditer(line)
                if m.group(1) not in own_adrs]
        out += [(no, "owner_decision", m.group(0)) for m in _OWNER.finditer(line)]
        out += [(no, "dev_term", m.group(0)) for m in _DEV_TERM.finditer(line)]
        out += [(no, "private_repo", m.group(0)) for m in _PRIVATE_RE.finditer(line)
                if not _PRIVATE_OK.match(m.group(0))]
        for rx in (_PRIVATE_PATH, _DEV_DRIVE):
            out += [(no, "publisher_path", m.group(0)) for m in rx.finditer(line)]
        out += [(no, "win_user_path", m.group(0)) for m in _WIN_USER.finditer(line)
                if not _placeholder(m.group(1))]
        out += [(no, "integrator", m.group(0)) for m in _INTEGRATOR_RE.finditer(line)]
        for tok in _tokens(line):
            rule = ("publisher_stand" if _h(tok) in _STAND_H else
                    "publisher_host" if _h(tok) in _HOST_H else "publisher_path")
            out.append((no, rule, tok))
    return out


def _git_exe() -> str:
    cand = r"C:\Program Files\Git\cmd\git.exe"
    return cand if os.path.isfile(cand) else "git"


def list_files(repo: Path):
    try:
        proc = subprocess.run([_git_exe(), "-C", str(repo), "ls-files", "-z", "--cached",
                               "--others", "--exclude-standard"], capture_output=True,
                              timeout=60, stdin=subprocess.DEVNULL)
        if proc.returncode == 0 and proc.stdout:
            return sorted({n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n})
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for dp, dns, fns in os.walk(repo):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        out += [Path(dp, f).relative_to(repo).as_posix() for f in fns]
    return sorted(out)


def scan_repo(repo: Path = _REPO):
    """{rel: [(line, rule, fragment)]} по текстовым файлам репозитория."""
    own = _own_adrs(repo)
    found = {}
    for rel in list_files(repo):
        if rel.lower().endswith(_SKIP_EXT) or rel == _BASELINE.relative_to(_REPO).as_posix():
            continue
        path = repo / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue
        hits = scan_text(data.decode("utf-8", "replace"), own)
        if hits:
            found[rel] = hits
    return found


def counts(found):
    out = {}
    for rel, hits in sorted(found.items()):
        c = collections.Counter(rule for _n, rule, _f in hits if rule not in STRICT)
        if c:
            out[rel] = dict(sorted(c.items()))
    return out


def _load_baseline():
    if not _BASELINE.is_file():
        return {}
    return json.loads(_BASELINE.read_text(encoding="utf-8")).get("files", {})


# --- правила на синтетике ------------------------------------------------------------------

def _rules(text):
    return sorted(rule for _n, rule, _f in scan_text(text, frozenset({"0001"})))


def test_own_registry_and_placeholders_are_clean():
    text = ("docs/GAPs/GAP-001.md, GAP-12, ADR-0001; https://github.com/thinkquattro/BPMkitStand\n"
            "C" ":\\Users\\\u0418\u043c\u044f\\AppData C" ":/Users/<user>/x C" ":\\Users\\tester\\x\n"
            'DEFAULT_REF = "' + _PRIVATE + ':remote:token"\n')
    assert _rules(text) == []


def test_baselined_rules_fire():
    text = ("GAP" "-999 OA" "-999 CAND" "-9 ADR" "-0999\n"
            "\u0420\u0435\u0448\u0435\u043d\u0438\u0435 \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430 15.10\n"
            "dev-\u0440\u0435\u043f\u043e; submodule " + _PRIVATE + "\n")
    assert _rules(text) == ["dev_reg"] * 4 + ["dev_term", "owner_decision", "private_repo"]


def test_strict_rules_fire():
    text = ("C" ":\\Users\\Ivanov\\AppData; C" ":\\" "Anthropic\\x; mail@" + _INTEGRATOR + "-team.com\n")
    assert set(_rules(text)) == {"win_user_path", "publisher_path", "integrator"}


# --- дерево репозитория -----------------------------------------------------------------------

def test_no_strict_findings_in_repo():
    bad = ["{}:{} [{}]".format(rel, n, rule) for rel, hits in scan_repo().items()
           for n, rule, _f in hits if rule in STRICT]
    assert not bad, "\n".join(bad)


def test_no_new_findings_over_baseline():
    base = _load_baseline()
    new = []
    for rel, rules in counts(scan_repo()).items():
        for rule, n in rules.items():
            b = base.get(rel, {}).get(rule, 0)
            if n > b:
                new.append("{} [{}] {} > {}".format(rel, rule, n, b))
    assert not new, ("новые вхождения сверх бейзлайна (служебный контекст разработки в "
                     "публичном репо):\n" + "\n".join(new))


if __name__ == "__main__":
    if "--write-baseline" in sys.argv:
        data = {"_comment": "Generated: python tests/test_public_hygiene.py --write-baseline. "
                            "File -> rule -> count; numbers only decrease.",
                "files": counts(scan_repo())}
        _BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                             encoding="utf-8", newline="\n")
        print("baseline written: {} files".format(len(data["files"])))
