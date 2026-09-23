# -*- coding: utf-8 -*-
"""Канал релизов MCP: проверка, подготовка (стейджинг), применение, откат.

Цена ошибки здесь выше, чем в любом другом цикле канала: сюда приезжает **исполняемый**
бинарь, который потом запустит хост MCP. Поэтому модуль устроен вокруг пяти решений,
каждое из которых принято ПРОТИВ более простой альтернативы.

**1. Проверка обновления не качает файл.** «Есть ли что-то новое» выясняется парой
`HEAD /v1/content/releases/latest` + `GET .../latest/meta`, а не `GET` самого файла.
Разница — десятки мегабайт на каждом тике планировщика: цикл релизов тикает часами, и
клиент, качающий релиз ради сравнения версий, съест трафик пользователя и канал издателя.

**2. Версия из `/meta` НЕ подставляется в URL вслепую.** Сервер принимает в пути только
`^\\d+(\\.\\d+)*$`; всё прочее — 404. На легаси-раскладке издателя `/meta` при этом честно
отвечает `version: "unknown"` — по такой «версии» скачать через `/releases/{version}`
НЕВОЗМОЖНО. Клиент, который просто подставит `meta["version"]`, ломается на ровном месте и
выглядит как «сервер сломался». Поэтому нечисловая версия здесь означает ровно одно: работаем
через путь `latest`, а причина уезжает в состояние явным текстом (`_resolve_target`).

**3. `signed: false` — это не «издатель забыл подписать».** Сервер выставляет этот флаг
после сверки сайдкара с файлом (имя + sha256): `.sig`, оставшийся от прежнего бинаря под тем
же именем, для него подписи НЕ образует. То есть `signed: false` означает «подпись ЭТОГО
файла не подтверждена» — единственно возможная реакция на такое в канале доставки кода —
не качать вовсе. Политика fail-closed **без флага отключения**: в публичном API нет и не
должно появиться ни `skip_signature`, ни `force`. Единственный параметр-исключение —
`stage(..., allow_unsigned=True)` — существует ТОЛЬКО для тестов самого механизма докачки
и не пробрасывается ни в CLI, ни в API хаба; лазейку «подготовить без подписи, а потом
применить» закрывает `apply_staged`, который повторно проверяет подпись по сохранённому
сайдкару и без него отказывает.

**4. Публичный ключ берётся только из поставки** (`ctx.artifact_pubkey`, приезжает от CLI
самого MCP). Ключ, скачанный по тому же каналу, что и бинарь, не проверяет ничего: кто
подменил один ответ, подменит и второй.

**5. Занятость целевого файла выясняется ДО мутации, не ПОСЛЕ (GAP-161).** До этого канал
узнавал, что MCP-сервер работает, только косвенно — по `PermissionError` из
`fsutil.replace_with_retry`, уже ПОСЛЕ того, как сделан бэкап. `apply_staged` теперь
сначала спрашивает `mcp_mutex.server_mutex_exists()` (сервер BPMkit сам объявляет о себе
именованным Windows-мьютексом, GAP-155б в поставке BPMkit) и, если детект молчит,
дополнительно пробует неразрушающе открыть файл на запись (`fsutil.probe_writable`) — обе
проверки строго до бэкапа. См. docstring `apply_staged` для точного порядка и текстов
отказа.

**Никакого «тихо обновлено».** Подмена файла НЕ перезапускает работающий MCP-сервер (хост
не поднимает его заново при перезапуске плагина), поэтому после успешного `apply_staged`
обязательно выставляется `state.releases["restart_required"] = True` и человекочитаемое
«перезапустите Claude Desktop». Автоматическим может быть только `stage` (по настройке
`companion.auto_stage_release`); `apply_staged` вызывается исключительно явным действием
человека из CLI/UI.

**Куда пишем.** Рабочий каталог — `<bpmkit_config_dir>/companion` (`%APPDATA%\\BPMkit\\companion`
на Windows), а НЕ `package_root`: поставка MCP лежит в `Program Files`, запись туда требует
прав администратора, а Companion обязан работать без них.

Разделение исходов по `kind` (см. `errors.KIND_TITLES`) здесь не косметика: `offline`
означает «докачаем», `integrity_mismatch` — «данные испорчены, начнём заново»,
`artifact_signature_invalid` — «повторять бессмысленно, зовите человека». Один общий
«ошибка обновления» превратил бы подмену бинаря в мигающую сетевую ошибку.

**6. `requires_installer` (GAP-463) останавливает `stage`/`apply_staged` РАНЬШЕ, чем
успевает начаться скачивание или подмена.** Канал подменяет РОВНО ОДИН файл — бинарь
сервера; блок запуска в конфиге хоста, требования рантайма и состав поставки вне
бинаря он не трогает вовсе. Издатель, объявивший в `GET /v1/version/latest`
(попутный запрос `_update_release_notes`, симметрично `release_notes`/`known_issues`)
`requires_installer: true` для версии `release_notes_version`, тем самым говорит:
эту версию тихой подменой поставить нельзя. Тот же принцип, что у `signed: false`
(п.3) — отказ ДО скачивания, а не после: клиент, который сначала качает и только
потом решает, что делать с файлом, тратит трафик пользователя на артефакт, который
и так не будет применён. Планировщик (`runner._run_releases`) и опция
«check_update» (GAP-241) читают тот же признак и просто НЕ вызывают `stage` —
без исключения: это не отказ автоматики, а штатный пропуск шага, тем не менее
явная команда `stage_update`/`apply_update` человека получает честный typed-отказ
(`kind="requires_installer"`), а не молчание.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

from standkit.registry import bpmkit_config_dir
from standkit.platform import ProcessError, is_alive, spawn_hidden

from . import fsutil, mcp_mutex, signature
from .backend import CONTENT_PREFIX
from .errors import ChannelError, NotModified
from .state import utc_now_iso

__all__ = [
    "STAGING_DIRNAME",
    "BACKUP_DIRNAME",
    "COMPANION_DIRNAME",
    "RELEASES_PREFIX",
    "RESTART_MESSAGE",
    "parse_version",
    "compare_versions",
    "is_numeric_version",
    "companion_workdir",
    "check",
    "stage",
    "apply_staged",
    "rollback",
    "prune_backups",
    "staged_info",
    "staged_requires_installer",
    "read_runtime_marker",
    "INSTALLER_PREFIX",
    "check_installer",
    "stage_installer",
    "apply_installer",
    "staged_installer_info",
    "installer_status",
    "INSTALLER_ELEVATION_WINERROR",
]

#: Подкаталог скачанного, но ещё не применённого бинаря.
STAGING_DIRNAME = "staging"

#: Подкаталог копий заменённых бинарей — единственный источник для отката.
BACKUP_DIRNAME = "backups"

#: Рабочий каталог канала внутри `<bpmkit_config_dir>`.
COMPANION_DIRNAME = "companion"

#: Префикс релизных эндпоинтов. Собирается из общего `CONTENT_PREFIX`, чтобы путь не
#: расползался по модулю строковыми литералами.
RELEASES_PREFIX = f"{CONTENT_PREFIX}/releases"

#: Псевдо-версия в пути — единственный способ скачать релиз, когда номер нечисловой.
LATEST = "latest"

#: Префикс канала установщика (ADR-0048, GAP-279) — ОТДЕЛЬНЫЙ от RELEASES_PREFIX,
#: раздельность адреса — часть контракта ADR-0048 п.1/п.3, не деталь реализации.
INSTALLER_PREFIX = f"{CONTENT_PREFIX}/installer"

#: Имя файла установщика — `bpmkit-setup-<version>.exe`, СОЗНАТЕЛЬНО отличное от
#: релизного `bpmkit-<version>.<ext>` (см. `app.installer` дословно, репозиторий
#: BPMkit-backend — тот же шаблон обязан совпасть на обеих сторонах канала).
_INSTALLER_FILENAME_RE = re.compile(r"^bpmkit-setup-\d+(?:\.\d+)*\.exe$")

#: Суффикс частично скачанного файла. Отдельное имя обязательно: файл без суффикса в
#: стейджинге означает «проверен и готов к применению», и недокачанный кусок под этим
#: именем однажды был бы применён.
PART_SUFFIX = ".part"

#: Ровно то, что принимает сервер в сегменте `{version}` (`BPMkit-backend`, content.py).
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")

#: Первые цифры сегмента версии — всё остальное (`rc1`, `dev`) считается нулём.
_SEGMENT_RE = re.compile(r"\d+")

#: sha256 в hex.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Символы, допустимые в имени файла бэкапа. Имя строится из версии, приехавшей ОТ СЕРВЕРА,
#: поэтому фильтруется: `../` в версии не должен превращаться в путь.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Текст, который обязан увидеть человек после подмены бинаря. Константой — потому что его
#: показывают три места (результат вызова, состояние, UI хаба), и разъехаться они не должны.
RESTART_MESSAGE = (
    "Обновление MCP установлено. Перезапустите Claude Desktop: подмена файла не "
    "перезапускает уже работающий MCP-сервер, и до перезапуска продолжает работать "
    "прежняя версия."
)


# ======================================================================================
# Версии
# ======================================================================================
def parse_version(value) -> tuple:
    """Версия → кортеж целых для сравнения.

    Сравнивать версии строкой нельзя: `"0.10.0" < "0.9.0"` лексикографически, то есть
    клиент со строковым сравнением перестанет видеть обновления ровно на десятом минорном
    релизе — молча и надолго.

    Нечисловой сегмент даёт `0` (`"1.2.0-rc1"` → `(1, 2, 0)`): предрелизы издатель в канал
    не выкладывает, а падать на неожиданной строке модуль обновлений не имеет права.
    Ведущее `v` срезается — оно встречается в тегах git и человеческом вводе.
    """
    text = str(value or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        return ()
    out = []
    for chunk in text.split("."):
        found = _SEGMENT_RE.match(chunk.strip())
        out.append(int(found.group()) if found else 0)
    return tuple(out)


def compare_versions(a, b) -> int:
    """`-1` / `0` / `+1` — обычная трёхзначная сверка версий.

    Кортежи выравниваются нулями: `1.2` и `1.2.0` — одна и та же версия, а не разные.
    """
    va, vb = parse_version(a), parse_version(b)
    width = max(len(va), len(vb))
    va = va + (0,) * (width - len(va))
    vb = vb + (0,) * (width - len(vb))
    return (va > vb) - (va < vb)


def is_numeric_version(value) -> bool:
    """Годится ли строка как сегмент `{version}` в URL сервера.

    Это НЕ «похоже на версию»: сервер принимает строго `^\\d+(\\.\\d+)*$`, остальное —
    404. Поэтому проверка дословно повторяет его регэксп, а не смягчает его.
    """
    return bool(_VERSION_RE.match(str(value or "").strip()))


# ======================================================================================
# Пути
# ======================================================================================
def companion_workdir(ctx) -> Path:
    """Рабочий каталог канала: `<bpmkit_config_dir>/companion`.

    Почему не `ctx.package_root`. Поставка MCP живёт в `Program Files`; запись туда требует
    прав администратора, а Companion обязан работать без них (иначе служба под LocalSystem,
    пишущая скачанное из сети в системный каталог, — прямое нарушение SECURITY.md).

    Атрибут `ctx.workdir` (если он есть и непуст) перекрывает путь — этим пользуются
    портативная установка и тесты. Обычный `LicenseContext` такого поля не имеет, поэтому
    штатное поведение не меняется.
    """
    override = str(getattr(ctx, "workdir", "") or "").strip()
    if override:
        return Path(override)
    return Path(bpmkit_config_dir()) / COMPANION_DIRNAME


def _staging_dir(ctx) -> Path:
    return companion_workdir(ctx) / STAGING_DIRNAME


def _backup_dir(ctx) -> Path:
    return companion_workdir(ctx) / BACKUP_DIRNAME


# ======================================================================================
# Мелкие помощники
# ======================================================================================
def _int_or_none(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _norm_hex(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _safe_component(value: str, fallback: str = "unknown") -> str:
    """Строка от сервера → безопасный кусок имени файла.

    Версия приезжает из сети и участвует в имени бэкапа. Без фильтрации `../..` в этом поле
    превратился бы в запись мимо каталога бэкапов.
    """
    cleaned = _SAFE_NAME_RE.sub("_", str(value or "").strip()).strip("._-")
    return cleaned or fallback


def _safe_filename(value) -> str:
    """Имя файла релиза от сервера, пригодное для склейки с локальным путём.

    Имя приходит из сети и используется как имя файла на диске. Разделители пути и `..`
    здесь означают попытку записи за пределы стейджинга, а не «необычное имя», поэтому это
    отказ, а не санитизация: молча переименовать чужой файл — значит спрятать атаку.
    """
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or Path(name).name != name:
        raise ChannelError(
            f"Бэкенд прислал недопустимое имя файла релиза: {name!r} — обновление не применяется",
            kind="bad_response",
        )
    return name


def _unlink_quietly(path: Path) -> None:
    """Удаление, которое не может стать второй ошибкой поверх первой."""
    try:
        path.unlink()
    except OSError:
        pass


def _current_version(state, ctx) -> Optional[str]:
    """Версия, относительно которой считается «есть ли обновление».

    Приоритет у состояния, а не у `ctx.mcp_version`: после `apply_staged` бинарь на диске
    уже новый, а РАБОТАЮЩИЙ MCP (который и отвечает на `companion-context`) до перезапуска
    продолжает докладывать старую версию. Взять её — значит на каждом тике заново
    предлагать уже установленное обновление и качать его повторно.
    """
    current = (state.releases.get("current") or {}).get("version")
    text = str(current or getattr(ctx, "mcp_version", "") or "").strip()
    return text or None


def _fetch_meta(client, target: str) -> dict:
    """`GET .../{target}/meta` — дешёвая карточка релиза (версия, имя, размер, sha, signed)."""
    payload, _headers = client.get_json(f"{RELEASES_PREFIX}/{target}/meta")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Метаданные релиза пришли не объектом JSON — обновление не применяется",
            kind="bad_response",
        )
    return payload


def _fetch_sidecar(client, target: str) -> dict:
    """`GET .../{target}/signature` — сайдкар подписи как есть.

    404 здесь бэкенд отдаёт в двух случаях: подписи нет вовсе ИЛИ она не от этого файла
    (сервер сверяет её с артефактом по имени и sha256). Различать их клиенту нечем и не
    нужно: последствие одно — fail-closed отказ.
    """
    payload, _headers = client.get_json(f"{RELEASES_PREFIX}/{target}/signature")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Сайдкар подписи пришёл не объектом JSON — обновление не применяется",
            kind="signature_not_available",
        )
    return payload


def _resolve_target(version) -> tuple:
    """`(сегмент пути, пояснение)` — куда идти за файлом.

    Возвращает `latest` для всего, что сервер не примет в URL. Это не «мягкость»: сервер
    отвечает 404 на любой нечисловой сегмент, а `/meta` легаси-раскладки издателя вполне
    легально отдаёт `version: "unknown"`. Пояснение — не для лога ради лога: без него
    состояние показывало бы «обновлено до unknown» без единого намёка, почему номер не
    известен.
    """
    text = str(version or "").strip()
    if not text or text.lower() == LATEST:
        return LATEST, ""
    if is_numeric_version(text):
        return text, ""
    return LATEST, (
        f"Номер версии {text!r} не подходит для адреса релиза (сервер принимает только "
        f"числовой вид вроде 0.307.0), поэтому файл берётся по пути «latest»"
    )


# ======================================================================================
# Маркер работающего процесса MCP (GAP-447)
# ======================================================================================
#: Имя файла маркера — одинаковое на обеих ОС.
_RUNTIME_MARKER_FILENAME = "mcp_runtime.json"

#: Каталог маркера на POSIX. НЕ `bpmkit_config_dir()` (там `$XDG_CONFIG_HOME/BPMkit`) —
#: контракт маркера зафиксирован серверной веткой (BPMkit/server) раньше этого модуля, и
#: путь у него СВОЙ: `~/.bpmkit/mcp_runtime.json`. Переопределять точку контракта в
#: одностороннем порядке нельзя — разойдись пути, маркер не нашёлся бы НИ НА ОДНОЙ
#: не-Windows машине.
_RUNTIME_MARKER_POSIX_DIRNAME = ".bpmkit"


def _runtime_marker_path() -> Path:
    """Путь файла-маркера работающего процесса MCP.

    Windows: `%APPDATA%\\BPMkit\\mcp_runtime.json` — та же папка, что и у самого
    Companion (`bpmkit_config_dir()`), потому что там же лежит `companion-state.json` и
    туда же пишет клиентский MCP. На прочих ОС — `~/.bpmkit/mcp_runtime.json` (см.
    докстринг константы `_RUNTIME_MARKER_POSIX_DIRNAME` про то, почему НЕ
    `bpmkit_config_dir()`).
    """
    if sys.platform == "win32":
        return bpmkit_config_dir() / _RUNTIME_MARKER_FILENAME
    return Path.home() / _RUNTIME_MARKER_POSIX_DIRNAME / _RUNTIME_MARKER_FILENAME


def read_runtime_marker() -> Optional[dict]:
    """Прочитать маркер работающего процесса MCP, best-effort.

    `None` — маркера нет, ПО ЛЮБОЙ причине: файла не существует (старый сервер, который
    ещё ни разу не перезапускался после апгрейда с GAP-447), битый JSON, нет прав на
    чтение, каталог недоступен. Это НЕ ошибка канала обновлений — маркер пишет чужой
    процесс, best-effort, и его отсутствие ничего не доказывает (кроме того, что сверить
    «какая версия реально работаетһ сейчас нечем).

    Формат — контракт серверной ветки (GAP-447): `{"version": str, "started_at":
    ISO-8601 UTC с суффиксом "Z", "pid": int, "frozen": bool, "binary": str}`. Запись без
    `version`/`started_at` считается отсутствующей: остальные поля справочные, а без этих
    двух сверить нечего.
    """
    path = _runtime_marker_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    started_at = str(data.get("started_at") or "").strip()
    if not version or not started_at:
        return None
    return {
        "version": version,
        "started_at": started_at,
        "pid": data.get("pid"),
        "frozen": bool(data.get("frozen")),
        "binary": str(data.get("binary") or ""),
    }


def _reconcile_restart_required(state) -> None:
    """Снять вечную плашку «перезапустите», когда рабочий процесс это уже подтвердил
    (GAP-447). Раньше `restart_required` выставлялся в `True` подменой файла
    (`apply_staged`/`rollback`) и не снимался НИГДЕ — плашка висела и после честного
    перезапуска, потому что канал не имел способа узнать, что MCP-сервер поднялся заново.

    Условие снятия — ВСЕ три сразу, иначе флаг не трогается (ни в одну, ни в другую
    сторону — поднимает его только сама подмена):

    1. маркер вообще есть (сервер хоть раз объявил о себе после апгрейда до версии,
       которая его пишет);
    2. `started_at` маркера СТРОГО позже `applied_at` последней подмены — сравнение
       лексикографическое, ЧТО КОРРЕКТНО: обе метки — `utc_now_iso()`, один и тот же
       секундно-точный формат `%Y-%m-%dT%H:%M:%SZ`. Без этого условия старый маркер (от
       процесса, поднятого ДО обновления) совпал бы версией случайно и снял бы плашку,
       хотя перезапуска не было;
    3. версия маркера совпадает с версией, которую канал считает установленной
       (`state.releases["current"]["version"]`) — сравнение через `compare_versions`, а не
       строкой: `"1.1.90"` и `"1.1.90.0"` в маркере и в `current` не обязаны совпадать
       посимвольно.

    Заодно каждый вызов обновляет `running_version`/`running_started_at` в состоянии — это
    отдельный от `restart_required` факт («что сейчас реально работает»), и UI показывает
    его даже когда условия снятия не выполнены (расхождение версий — само по себе полезная
    информация, см. `standkit_hub/web/app.js::renderMcpRow`).
    """
    rel = state.releases
    marker = read_runtime_marker()
    if marker is None:
        rel["running_version"] = None
        rel["running_started_at"] = None
        return

    rel["running_version"] = marker["version"]
    rel["running_started_at"] = marker["started_at"]

    if not rel.get("restart_required"):
        return

    current = rel.get("current") or {}
    applied_at = str(current.get("applied_at") or "").strip()
    installed_version = str(current.get("version") or "").strip()
    if not applied_at or not installed_version:
        return
    if marker["started_at"] <= applied_at:
        return
    if compare_versions(marker["version"], installed_version) != 0:
        return
    rel["restart_required"] = False


# ======================================================================================
# Нотсы и известные проблемы (GAP-442)
# ======================================================================================
#: `GET /v1/version/latest` — эндпоинт БЕЗ авторизации (`BPMkit-backend/app/routers/
#: version.py`), не под `CONTENT_PREFIX`: он не про доставку файла, а про то, что о нём
#: сказать. `current` в query — версия, для которой сервер отфильтрует `known_issues`;
#: `release_notes` в ответе — ВСЕГДА про версию `latest` бэкенда (симметрия файла
#: `version_info.json`, но не симметрия фильтрации — так устроен сам эндпоинт).
_VERSION_INFO_PATH = "/v1/version/latest"


def _update_release_notes(state, client, current_version: Optional[str]) -> None:
    """Подтянуть состав обновления и известные проблемы — попутно к проверке релиза,
    ЛУЧШЕЕ СТАРАНИЕ (GAP-442).

    Факт «есть ли новая версия» первичен, «что в ней нового» — нет: недоступность этого
    эндпоинта (сеть, бэкенд лежит, у издателя не настроен `version_info_file` — 404) НЕ
    имеет права уронить проверку обновления, поэтому исключение ЛЮЁОГО типа (не только
    `ChannelError`) здесь проглатывается молча, а результат просто остаётся тем, что был
    (пустым — на первом тике). Отдельно от логики `check()` намеренно: смысл «что нового»
    вторичен по отношению к «есть ли новое», и падение одного не должно маскировать успех
    другого.
    """
    rel = state.releases
    params = {"current": current_version} if current_version else None
    try:
        payload, _headers = client.get_json(_VERSION_INFO_PATH, params=params,
                                            authorized=False)
    except Exception:  # noqa: BLE001 - попутный запрос не имеет права уронить проверку
        return
    if not isinstance(payload, dict):
        return
    notes = payload.get("release_notes")
    issues = payload.get("known_issues")
    rel["release_notes_version"] = str(payload.get("latest") or "").strip() or None
    rel["release_notes"] = [str(item) for item in notes] if isinstance(notes, list) else []
    rel["known_issues"] = [str(item) for item in issues] if isinstance(issues, list) else []
    # GAP-463: поле — булево для `latest` ВСЕГДА (контракт бэкенда, симметрично
    # `release_notes`). `is True` — намеренно строгая проверка ТИПА, а не истинности:
    # отсутствие поля (`None`), отсутствие записи для `latest` (эндпоинт всё равно
    # отдаёт ключ со значением по умолчанию) и мусор (строка `"true"`, `1`, `{}`) —
    # всё это ОДНО И ТО ЖЕ «false» по контракту (обратная совместимость со старым
    # бэкендом, который поля не пришлёт вовсе). `bool(1)`/`bool("false")` здесь дали
    # бы `True` на мусоре — ровно то, чего контракт запрещает.
    rel["requires_installer"] = payload.get("requires_installer") is True


def _flagged_version_requires_installer(rel: dict, version: str) -> bool:
    """`True`, если `version` — ТА САМАЯ версия, для которой издатель поднял
    `requires_installer` (GAP-463). Общая проверка `stage`/`apply_staged`/
    `staged_requires_installer` — три места должны видеть ОДНО решение, а не разные
    сравнения версий.

    Флаг в состоянии — булев и БЕЗ версии внутри себя (контракт бэкенда: булево для
    `latest`, см. `_update_release_notes`), поэтому «та самая версия» — это
    `rel["release_notes_version"]`, снятый ПОПУТНО, тем же запросом. Сравнение —
    `compare_versions`, не строкой (`"0.310.0"` и `"0.310"` — одна версия).

    Сравнить НЕЧЕМ (пустая `version` или пустой `release_notes_version`) — тоже
    `True`: канал не вправе истолковать «не знаю» как разрешение подменить бинарь,
    когда издатель уже сказал «эту тихо не ставь».
    """
    if not rel.get("requires_installer"):
        return False
    flagged = str(rel.get("release_notes_version") or "").strip()
    version = str(version or "").strip()
    if not flagged or not version:
        return True
    return compare_versions(version, flagged) == 0


# ======================================================================================
# Проверка обновления
# ======================================================================================
def check(client, state, ctx) -> dict:
    """Есть ли новый релиз. Файл НЕ качается — только `HEAD` и `/meta`.

    `If-None-Match` здесь намеренно НЕ отправляется. `HEAD` и так не тянет тело, а `304` на
    него означал бы «файл не менялся с прошлой проверки» — и скрыл бы уже известное
    обновление, которое пользователь ещё не применил. ETag из ответа при этом запоминается:
    он пригодится другим потребителям состояния.

    `404 release not configured` ловится здесь и превращается в штатный пропуск тика
    (`skipped`), а не в ошибку: владелец просто ещё не выложил релиз, чинить пользователю
    нечего и будить его нечем.

    Прочие отказы поднимаются наверх как есть — записью `status=error` занимается тик
    планировщика, у которого объект ошибки со всеми полями уже в руках.
    """
    rel = state.releases
    current = _current_version(state, ctx)

    # GAP-447/GAP-442: локальная сверка маркера и попутный запрос нотсов — ДО сетевого
    # похода за самим релизом и НЕЗАВИСИМО от его исхода (см. докстринги обеих функций).
    _reconcile_restart_required(state)
    _update_release_notes(state, client, current)

    try:
        headers = client.head(f"{RELEASES_PREFIX}/{LATEST}")
        meta = _fetch_meta(client, LATEST)
    except NotModified:
        # Сервер (или прокси) ответил 304 без нашего If-None-Match. Ничего нового.
        state.mark("releases", "ok", "Релиз не изменился с прошлой проверки")
        state.save()
        return {
            "available": False, "latest": rel.get("known_latest"), "current": current,
            "signed": None, "size_bytes": None, "etag": rel.get("etag"),
            "reason": "not_modified", "target": LATEST, "filename": None, "sha256": None,
            "requires_installer": bool(rel.get("requires_installer")),
        }
    except ChannelError as exc:
        # 404 БЕЗ разобранного `detail` — не «неизвестная ошибка», а тот же самый
        # «релиз не выложен». Живой прогон 20.08.2026 против бэкенда издателя: `HEAD`
        # по определению отдаёт ответ БЕЗ ТЕЛА, поэтому классификатор, который читает
        # `detail` из JSON, на нём слеп и падает в общий `http_error`. Под
        # `/v1/content/releases/*` других 404 не бывает: `signature not available`
        # возможен только на `.../signature`, куда `check` не ходит вовсе. Поэтому
        # доклассифицируем по коду и пути — иначе штатное «владелец ещё не выложил
        # релиз» показывалось бы пользователю как поломка канала.
        if exc.kind == "http_error" and exc.http_status == 404:
            exc = ChannelError(str(exc), kind="release_not_configured",
                               http_status=404, detail=exc.detail)
        if exc.kind == "release_not_configured":
            state.mark("releases", "skipped", exc.title())
            state.save()
            return {
                "available": False, "latest": None, "current": current,
                "signed": None, "size_bytes": None, "etag": None,
                "reason": "release_not_configured", "target": LATEST,
                "filename": None, "sha256": None,
                "requires_installer": bool(rel.get("requires_installer")),
            }
        raise

    headers = headers if isinstance(headers, dict) else {}
    meta_version = str(meta.get("version") or "").strip()
    head_version = str(headers.get("x-bpmkit-version") or "").strip()
    latest = meta_version or head_version
    numeric = is_numeric_version(latest)
    if not numeric and is_numeric_version(head_version):
        # Заголовок HEAD оказался информативнее `/meta` — берём его: числовая версия даёт
        # и сравнение, и адресуемый путь.
        latest, numeric = head_version, True

    signed = bool(meta.get("signed"))
    size_bytes = _int_or_none(meta.get("size_bytes"))
    sha256 = _norm_hex(meta.get("sha256")) or _norm_hex(headers.get("x-bpmkit-sha256"))
    etag = headers.get("etag")
    target = latest if numeric else LATEST

    if not numeric:
        # Сравнить нечего: номера нет. Единственный доступный признак «то же самое» —
        # контрольная сумма установленного бинаря.
        current_sha = _norm_hex((rel.get("current") or {}).get("sha256"))
        if current_sha and sha256 and current_sha == sha256:
            available, reason = False, "up_to_date"
        else:
            available, reason = True, "version_unknown_use_latest"
    elif not current:
        # Версия установленного неизвестна (MCP рядом не ответил) — считаем, что обновление
        # есть: fail-closed политика касается подписи, а не отказа показать релиз.
        available, reason = True, "current_version_unknown"
    elif compare_versions(latest, current) > 0:
        available, reason = True, "update_available"
    else:
        available, reason = False, "up_to_date"

    rel["known_latest"] = latest or None
    rel["etag"] = etag

    requires_installer = bool(rel.get("requires_installer"))
    detail = _check_detail(available, reason, latest, current, signed, requires_installer)
    state.mark("releases", "ok", detail)
    state.save()

    return {
        "available": available,
        "latest": latest or None,
        "current": current,
        "signed": signed,
        "size_bytes": size_bytes,
        "etag": etag,
        "reason": reason,
        # Ниже — то, что нужно `stage`, чтобы не ходить за метаданными второй раз вслепую.
        "target": target,
        "filename": str(meta.get("filename") or "") or None,
        "sha256": sha256 or None,
        # GAP-463: та же версия, о которой отчитывается `latest` — установщик или
        # тихая подмена файла. Читают `runner._run_releases` (гасит авто-стейдж) и
        # UI хаба (карточка «MCP-сервер BPMkit»).
        "requires_installer": requires_installer,
    }


def _check_detail(available: bool, reason: str, latest: str,
                  current: Optional[str], signed: bool,
                  requires_installer: bool = False) -> str:
    """Человеческая строка исхода проверки для состояния и UI."""
    if reason == "version_unknown_use_latest":
        return ("Издатель не сообщил номер версии релиза — обновление доступно только по "
                "пути «latest»")
    if not available:
        return f"Установлена актуальная версия ({current or 'неизвестно'})"
    # GAP-463: проверяется РАНЬШЕ подписи — версия, которая ставится установщиком,
    # тихой подменой не будет доставлена независимо от того, подтверждена подпись
    # или нет (сама подпись артефакта тут ни при чём: канал его и не скачает).
    if requires_installer:
        return (f"Доступна версия {latest}, но она ставится установщиком — тихим "
                f"обновлением её доставить нельзя. Скачайте новую поставку и "
                f"запустите установку")
    if not signed:
        return (f"Доступна версия {latest}, но её подпись сервером не подтверждена — "
                f"обновление не будет скачано")
    return f"Доступна версия {latest} (установлена {current or 'неизвестно'})"


# ======================================================================================
# Подготовка (стейджинг)
# ======================================================================================
def stage(client, state, ctx, version: str = "latest", *,
          allow_unsigned: bool = False) -> dict:
    """Скачать релиз в стейджинг и полностью его проверить. Ничего не применяет.

    Порядок шагов не произволен — сначала всё, что позволяет НЕ качать десятки мегабайт:

    1. `/meta` (дёшево) → имя, размер, sha256, `signed`;
    1б. `requires_installer` (GAP-463) у версии, которую называет `/meta`, → отказ
        **до** скачивания, ещё раньше проверки подписи: версия, которую издатель велел
        ставить установщиком, каналом не доставляется независимо от того, подписана
        она или нет;
    2. `signed: false` → отказ **до** скачивания. Это не «издатель забыл подписать», а
       «подпись этого файла не подтверждена» (сервер сверяет сайдкар с файлом);
    3. публичный ключ из поставки (`ctx.artifact_pubkey`) → плейсхолдер/пусто даёт
       `pubkey_missing` тоже **до** скачивания: проверить подпись всё равно будет нечем;
    4. скачивание с докачкой в `<workdir>/staging/<filename>.part`;
    5. размер и sha256;
    6. сайдкар `GET .../signature` и `signature.verify_artifact`;
    7. и только теперь переименование в `<workdir>/staging/<filename>`.

    **`allow_unsigned` существует ТОЛЬКО для тестов самого механизма** (докачка, 416,
    сброс состояния) и НЕ пробрасывается ни в CLI, ни в API хаба. Подготовленный с ним
    артефакт применить нельзя: `apply_staged` требует сохранённый сайдкар и проверяет
    подпись заново, а его в таком стейдже нет.

    Судьба `.part` при отказе (осознанно разная):

    * обрыв связи, отказ подписи → файл ОСТАЁТСЯ, следующая попытка продолжит с места
      обрыва (или сразу перейдёт к проверке, если тело уже целиком на диске);
    * `416` и несошедшийся sha256 → файл УДАЛЯЕТСЯ. Докачивать нечего: сервер отверг наш
      диапазон либо содержимое доказано неверное, и сохранённый кусок обрёк бы клиента на
      вечный повтор одного и того же битого запроса.
    """
    rel = state.releases
    target, note = _resolve_target(version)

    meta = _fetch_meta(client, target)
    filename = _safe_filename(meta.get("filename"))
    expected_sha = _norm_hex(meta.get("sha256"))
    if not _SHA256_RE.match(expected_sha):
        raise ChannelError(
            "Метаданные релиза не содержат корректной контрольной суммы sha256 — "
            "обновление не применяется",
            kind="bad_response",
        )
    size_bytes = _int_or_none(meta.get("size_bytes")) or 0
    meta_version = str(meta.get("version") or "").strip()
    signed_flag = bool(meta.get("signed"))

    # --- 1б. Установщик (GAP-463): ещё раньше «signed», не тратим трафик вовсе ----------
    # Издатель объявил `requires_installer: true` для версии `release_notes_version`
    # (попутный запрос `_update_release_notes`, всегда ходит раньше сетевого похода за
    # самим релизом — см. `check`). Совпадение версий сравнивается ЧИСЛОМ
    # (`compare_versions`), а не строкой: `meta_version` приходит из `/meta` ЭТОГО
    # похода, `release_notes_version` — из отдельного эндпоинта, и лишний ноль в
    # хвосте одной из них не должен превратить совпадающие версии в разные. Если
    # сверить нечем (сервер не назвал номер ни там, ни там) — отказ тоже: канал не
    # имеет права положиться на «наверное, это другая версия» там, где издатель уже
    # сказал «эту тихо не ставь».
    if _flagged_version_requires_installer(rel, meta_version or target):
        raise ChannelError(
            f"Версия {meta_version or target} ставится установщиком — обновление "
            f"не скачивается и не применяется каналом. Скачайте новую поставку и "
            f"запустите установку",
            kind="requires_installer",
        )

    # --- 2. Подпись не подтверждена сервером: не тратим трафик вовсе --------------------
    if not signed_flag and not allow_unsigned:
        raise ChannelError(
            f"Сервер не подтвердил подпись файла релиза {filename} (signed: false) — "
            f"файл не скачивается и не применяется",
            kind="signature_not_available",
        )

    # --- 3. Ключ проверки должен быть в поставке ДО скачивания -------------------------
    pubkey_raw = None
    if not allow_unsigned:
        # Ключ берётся ТОЛЬКО из поставки. Скачанный по тому же каналу, что и бинарь, он не
        # проверял бы ничего: кто подменил файл, подменит и ключ рядом с ним.
        pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))

    staging = _staging_dir(ctx)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось создать каталог подготовки обновления {staging}: {exc}",
            kind="local_io",
        ) from None
    part = staging / (filename + PART_SUFFIX)

    resume_from = _resume_offset(rel, part, expected_sha, size_bytes)

    # --- 4. Скачивание ------------------------------------------------------------------
    resumed = False
    if size_bytes and resume_from == size_bytes:
        # Тело уже целиком на диске (прошлая попытка упала на проверке подписи). Повторно
        # качать его нечего — сразу к проверкам.
        resumed = True
    else:
        try:
            result = client.download(f"{RELEASES_PREFIX}/{target}", part,
                                     resume_from=resume_from,
                                     expected_size=size_bytes or None)
        except ChannelError as exc:
            if exc.kind == "range_invalid":
                # Клиент уже удалил `.part`. Состояние докачки обязано уехать вместе с ним,
                # иначе следующий заход снова пошлёт тот же битый Range.
                rel["partial"] = None
                state.save()
                raise
            rel["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
            state.save()
            raise
        resumed = bool(isinstance(result, dict) and result.get("resumed"))

    actual_size = _size_on_disk(part)

    # --- 5. Размер и контрольная сумма --------------------------------------------------
    if size_bytes and actual_size < size_bytes:
        # Тело кончилось раньше времени — это обрыв, а не порча. `.part` остаётся.
        rel["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise ChannelError(
            f"Файл релиза скачан не полностью: {actual_size} из {size_bytes} байт — "
            f"докачаем на следующей проверке",
            kind="offline",
        )
    if size_bytes and actual_size > size_bytes:
        _unlink_quietly(part)
        rel["partial"] = None
        state.save()
        raise ChannelError(
            f"Размер скачанного файла больше объявленного ({actual_size} против "
            f"{size_bytes} байт) — данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        actual_sha = fsutil.sha256_file(part)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось посчитать контрольную сумму скачанного файла {part}: {exc}",
            kind="local_io",
        ) from None
    if actual_sha != expected_sha:
        # Содержимое доказано не то. Сохранять его ради «докачки» бессмысленно — докачивать
        # нечего, а сохранённый кусок заставил бы клиента вечно падать на этом же месте.
        _unlink_quietly(part)
        rel["partial"] = None
        state.save()
        raise ChannelError(
            f"Контрольная сумма скачанного релиза не сошлась с метаданными "
            f"(ожидался sha256 …{expected_sha[-8:]}, получен …{actual_sha[-8:]}) — "
            f"данные отброшены",
            kind="integrity_mismatch",
        )

    # --- 6. Подпись ---------------------------------------------------------------------
    sidecar: Optional[dict] = None
    verified: dict = {}
    if not allow_unsigned:
        try:
            sidecar = _fetch_sidecar(client, target)
            verified = signature.verify_artifact(
                part, sidecar, pubkey_raw,
                expected_name=filename, expected_sha256=expected_sha)
        except ChannelError:
            # Файл на диске цел и полон — виноват сайдкар. Оставляем `.part`: повторная
            # попытка не потратит трафик, а издателю достаточно перевыложить подпись.
            rel["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
            state.save()
            raise

    # --- 7. Готово: имя без `.part` означает «проверено» --------------------------------
    final = staging / filename
    try:
        fsutil.replace_with_retry(part, final)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось поместить проверенный файл в стейджинг ({final}): {exc}",
            kind="local_io",
        ) from None
    _cleanup_staging(staging, keep=final.name)

    record = {
        "version": meta_version or (target if target != LATEST else ""),
        "filename": filename,
        "path": str(final),
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "signed": sidecar is not None,
        "key_id": verified.get("key_id"),
        "signed_at": verified.get("signed_at"),
        "target": target,
        "staged_at": utc_now_iso(),
        # Сайдкар хранится вместе с записью намеренно: `apply_staged` проверяет подпись
        # ЗАНОВО, уже перед подменой. Между подготовкой и применением проходит время, и
        # файл в стейджинге за это время могли подменить.
        "sidecar": sidecar,
    }
    rel["staged"] = record
    rel["partial"] = None
    detail = (f"Обновление {record['version'] or 'latest'} подготовлено и проверено; "
              f"применение — по явной команде")
    state.mark("releases", "ok", (note + ". " if note else "") + detail)
    state.save()

    out = {key: value for key, value in record.items() if key != "sidecar"}
    out["resumed"] = resumed
    out["reason"] = "staged"
    out["note"] = note
    return out


def _partial_record(target: str, filename: str, sha256: str,
                    size_bytes: int, part: Path) -> dict:
    """Запись о недокачанном файле.

    Хранится не только число байт, но и sha256 РЕЛИЗА: если издатель перевыложил файл, к
    старому куску дописывать новый нельзя — получится мусор, который вскроется только на
    финальной проверке. Сверка sha даёт отказ от докачки сразу.
    """
    return {
        "target": target,
        "filename": filename,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "bytes": _size_on_disk(part),
        "path": str(part),
        "updated_at": utc_now_iso(),
    }


def _resume_offset(rel: dict, part: Path, expected_sha: str, size_bytes: int,
                   *, slot: str = "partial") -> int:
    """Сколько байт уже лежит на диске и можно ли им доверять.

    Докачка разрешена, если частичный файл существует И (записи о нём нет ЛИБО она про этот
    же релиз). Расхождение sha означает «релиз перевыложили» — кусок удаляется, качаем с
    нуля. Кусок больше объявленного размера — тоже мусор.

    `slot` (ADR-0048/GAP-279) — ключ состояния для записи о недокачанном файле: релизный
    поток использует `"partial"` (умолчание, поведение НЕ меняется), поток установщика —
    `"installer_partial"` (свой слот, НЕ пересекается с релизным)."""
    existing = _size_on_disk(part)
    if existing <= 0:
        return 0
    partial = rel.get(slot) or {}
    known_sha = _norm_hex(partial.get("sha256"))
    if known_sha and known_sha != expected_sha:
        _unlink_quietly(part)
        rel[slot] = None
        return 0
    if size_bytes and existing > size_bytes:
        _unlink_quietly(part)
        rel[slot] = None
        return 0
    return existing


def _size_on_disk(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _cleanup_staging(staging: Path, keep: str) -> None:
    """Убрать из стейджинга всё, кроме готового файла.

    Иначе каталог копит недокачанные хвосты старых релизов по десятку мегабайт каждый —
    невидимо для пользователя, который про этот каталог не знает.
    """
    try:
        entries = list(staging.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == keep:
            continue
        if entry.is_file():
            _unlink_quietly(entry)


def staged_info(state) -> Optional[dict]:
    """Что подготовлено к применению, или `None`.

    Наличие записи в состоянии проверяется наличием ФАЙЛА: запись без файла (пользователь
    почистил каталог, антивирус унёс `.exe` в карантин) — это «нечего применять», и UI
    обязан показать именно это, а не предлагать кнопку, которая упадёт.

    Сайдкар наружу не отдаётся: он нужен только `apply_staged` и в карточке состояния —
    лишние килобайты.
    """
    record = state.releases.get("staged")
    if not isinstance(record, dict):
        return None
    path = str(record.get("path") or "")
    if not path or not Path(path).is_file():
        return None
    return {key: value for key, value in record.items() if key != "sidecar"}


def staged_requires_installer(state) -> bool:
    """Подготовленный файл — та самая версия, которую издатель объявил ставящейся
    установщиком (GAP-463)? Только для `available_actions`/UI: решает, гасить ли
    кнопку «Установить» заранее, а не после честного отказа `apply_staged`.

    `staged_info` при этом НЕ вызывается и файл на диске никак не трогает — это
    отдельный, более дешёвый вопрос («можно ли ЭТО применить»), а не «есть ли что
    применять» (за это отвечает `staged_info`, и вызывающий обязан проверить его
    первым — см. `runner.available_actions`).
    """
    rel = state.releases
    record = rel.get("staged")
    if not isinstance(record, dict):
        return False
    return _flagged_version_requires_installer(rel, str(record.get("version") or ""))


# ======================================================================================
# Применение
# ======================================================================================
#: Первые два байта исполняемого файла Windows (PE/MZ-заголовок, DOS-заглушка). Проверяем
#: именно их, а не «файл больше N байт»: ZIP начинается с `PK`, а `.mcpb`, который канал чуть
#: не получил вместо бинаря (GAP-212), -- это ZIP.
_PE_MAGIC = b"MZ"

#: Имя артефакта-УСТАНОВЩИКА (`bpmkit-setup-1.1.120.exe`, `BPMkit-setup-...`). Регистр не
#: учитывается: имя приходит от бэкенда, и «BPMkit-setup» против «bpmkit-setup» — не тот
#: признак, по которому гард имеет право промахнуться. См. _ensure_artifact_applicable.
_INSTALLER_NAME_RE = re.compile(r"^bpmkit[-_]setup[-_.]", re.IGNORECASE)


def _ensure_artifact_applicable(src: Path, dest: Path) -> None:
    """Fail-closed по ПРИМЕНИМОСТИ артефакта (GAP-212) -- в дополнение к fail-closed по
    подлинности, которая уже проверена подписью.

    Зачем отдельная проверка, если подпись сходится. Подпись доказывает, что файл ровно тот,
    который выложил издатель, и НИЧЕГО не говорит о том, что этот файл вообще можно положить
    на место бинаря MCP. Издательский конвейер (`tools/release_publish.py`) до 02.09.2026
    собирал и публиковал `.mcpb` -- ZIP-бандл, -- а `apply_staged` подменяет
    `ctx.binary_path`, то есть `bpmkit.exe`. Подпись бандла была бы честной, `/meta` вернул
    бы `signed: true`, sha256 сошёлся бы, повторная проверка перед подменой прошла бы -- и
    канал сам, аккуратно и «успешно», положил бы архив на место исполняемого файла. Клиент
    получил бы не запускающийся MCP, а диагностика указывала бы куда угодно, только не на
    тип артефакта.

    Две проверки, обе дешёвые и обе ДО бэкапа (порядок как у GAP-161: сначала отказ, потом
    любые мутации):
      1. расширение подготовленного файла совпадает с расширением цели (без учёта регистра) --
         ловит `.mcpb`/`.zip`/`.msi` на месте `.exe`;
      2. если цель -- `.exe`, у файла обязан быть PE-заголовок `MZ` -- ловит переименованный
         архив, у которого расширение «правильное», а содержимое нет.
    Заголовок не читается целиком: нужны два байта. Ошибка чтения -- НЕ повод пропустить
    проверку (fail-closed): нечитаемый файл применять всё равно нельзя.

    ТРЕТЬЯ проверка -- ИМЯ (GAP-415). Обе проверки выше пропускают УСТАНОВЩИК
    `bpmkit-setup-<версия>.exe`: расширение у него `.exe`, PE-заголовок на месте, подпись
    издателя честная. А делает он совсем не то, что подмена бинаря: это Inno Setup, который
    сам останавливает диспетчер и MCP, раскладывает поставку, правит реестр и перезапускает
    хаб. Положить его НА МЕСТО `bpmkit.exe` -- значит получить процесс, который при каждом
    запуске MCP-хоста показывает мастер установки. Отдельным типом артефакта установщик
    станет по ADR-0048 (канал должен научиться ЗАПУСКАТЬ его, а не подменять им бинарь);
    до тех пор клиентская сторона отказывается его применять -- независимо от того, что
    решит выдать бэкенд. Гард именно клиентский и именно здесь: канал обязан быть безопасен
    против ОШИБКИ ИЗДАТЕЛЯ, а не только против подделки.
    """
    if _INSTALLER_NAME_RE.match(src.name):
        raise ChannelError(
            f"Подготовленный файл {src.name} — УСТАНОВЩИК BPMkit, а не бинарь MCP: "
            f"подменять им {dest.name} нельзя (установщик надо запускать, а не класть на "
            f"место программы). Обновление НЕ применено, установленная версия не тронута. "
            f"Поддержка установщика отдельным типом артефакта — ADR-0048 (GAP-415).",
            kind="artifact_type_mismatch",
        )
    src_suffix = src.suffix.lower()
    dest_suffix = dest.suffix.lower()
    if src_suffix != dest_suffix:
        raise ChannelError(
            f"Подготовленный файл {src.name} не подходит для замены {dest.name}: издатель "
            f"выложил артефакт типа {src_suffix or '<без расширения>'}, а обновляется "
            f"{dest_suffix or '<без расширения>'}. Обновление НЕ применено, установленная "
            f"версия не тронута — сообщите издателю (GAP-212).",
            kind="artifact_type_mismatch",
        )
    if dest_suffix != ".exe":
        return
    try:
        with open(src, "rb") as fh:
            head = fh.read(len(_PE_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Подготовленный файл {src.name} не читается ({exc}) — обновление не применяется.",
            kind="local_io",
        ) from None
    if head != _PE_MAGIC:
        raise ChannelError(
            f"Подготовленный файл {src.name} не является исполняемым (нет заголовка "
            f"{_PE_MAGIC.decode('ascii')}) — под видом бинаря MCP выложено что-то другое. "
            f"Обновление НЕ применено, установленная версия не тронута (GAP-212).",
            kind="artifact_type_mismatch",
        )


def apply_staged(state, ctx, *, target: Optional[str] = None) -> dict:
    """Подменить бинарь MCP подготовленным. Только по ЯВНОЙ команде человека.

    Сигнатура намеренно бедна: у функции нет и не должно появиться параметров вида
    `force`/`skip_signature`/`allow_unsigned`. Применение неподписанного бинаря не должно
    быть выразимо в публичном API — это проверяется отдельным регресс-тестом.

    Подпись проверяется ЗАНОВО, по сайдкару из состояния. Проверка при подготовке не
    заменяет эту: между `stage` и `apply_staged` проходит произвольное время, в течение
    которого файл в стейджинге доступен на запись любому процессу пользователя.

    Порядок «бэкап → подмена» обязателен: копия делается ДО, и если подмена не удалась
    (типичный случай на Windows — файл занят работающим MCP), старая версия остаётся на
    месте нетронутой, а наружу уходит `local_io` с текстом, прямо говорящим, что надо
    закрыть Claude Desktop.

    **Ещё раньше — GAP-463.** Прежде мьютекса и подписи — гонка «версию объявили
    установщиком уже ПОСЛЕ того, как файл лёг в стейджинг» (штатный путь эту версию
    туда и не пустит, см. `stage`, шаг «1б»). Подготовленный файл при этом не трогается
    и не отзывается: подпись доказана, данные целы, единственная причина отказа —
    политика доставки, а не порча.

    **До бэкапа и до любой мутации (GAP-161)** — две дополнительные проверки, ОБЕ
    fail-fast (отказывают раньше, backup/подмена не трогаются) и не заменяют, а
    дополняют друг друга и финальный `replace_with_retry`:

    1. **Детект по мьютексу** (`mcp_mutex.server_mutex_exists`, только Windows) — сервер
       BPMkit с GAP-155б сам объявляет о себе именованным системным объектом. Если он
       виден — канал честно говорит «сервер работает», НЕ выясняя того же самого через
       неудачную файловую операцию; kind `mcp_running`, отдельный от `local_io`, чтобы UI
       не путал «сервер точно занят» с «файловая ошибка неизвестной природы». Мьютекса не
       видно (в т.ч. на не-Windows или при ошибке WinAPI, детект fail-open) — это НЕ
       доказательство простоя, проверка идёт дальше.
    2. **Файловая проба** (`fsutil.probe_writable`, прямо перед бэкапом) — неразрушающая
       попытка открыть целевой бинарь на запись. Ловит и старые серверы без мьютекса, и
       посторонние процессы, держащие файл по другой причине. Отказывает С ТЕМ ЖЕ текстом,
       которым закончилась бы настоящая подмена (kind `local_io`), но раньше — до того, как
       сделан бэкап.

    **Третья проверка (GAP-212), тоже до бэкапа** — `_ensure_artifact_applicable`: тип
    подготовленного файла обязан подходить цели подмены. Подпись доказывает ПОДЛИННОСТЬ, но
    не ПРИМЕНИМОСТЬ: издательский конвейер публиковал `.mcpb`, и честно подписанный ZIP-бандл
    прошёл бы здесь всё до единой проверки, после чего лёг бы архивом на место `bpmkit.exe`.

    После успеха обязательно выставляется `restart_required`: подмена файла НЕ
    перезапускает работающий MCP-сервер, и без явного сообщения пользователь считал бы, что
    обновление уже действует.
    """
    rel = state.releases
    record = rel.get("staged")
    if not isinstance(record, dict):
        raise ChannelError(
            "Нет подготовленного обновления — сначала выполните подготовку (stage)",
            kind="nothing_staged",
        )

    src = Path(str(record.get("path") or ""))
    if not src.is_file():
        rel["staged"] = None
        state.save()
        raise ChannelError(
            f"Подготовленный файл обновления не найден ({src}) — подготовьте его заново",
            kind="nothing_staged",
        )

    # --- GAP-463, проверка 0: установщик — РАНЬШЕ мьютекса и подписи ---------------------
    # `stage` (см. её докстринг, шаг «1б») уже отказывает СКАЧИВАТЬ версию с флагом —
    # то есть в обычном ходе канала этот код мёртв: файл с таким флагом просто не
    # окажется в `staged`. Проверка здесь — не дублирование, а защита от ГОНКИ: между
    # `stage` (файл подготовлен, флага ещё не было) и нажатием «Установить» издатель
    # мог объявить `requires_installer` для этой самой версии. `staged_info`
    # (см. её докстринг) файл при этом НЕ трогает и НЕ удаляет — незачем: подпись
    # уже проверена, данные не испорчены, применить их нельзя ровно по ЭТОЙ причине,
    # и она может исчезнуть так же, как появилась (издатель снял флаг).
    staged_version = str(record.get("version") or "").strip()
    if _flagged_version_requires_installer(rel, staged_version):
        raise ChannelError(
            f"Версия {staged_version or 'обновления'} ставится установщиком — "
            f"подмена бинаря каналом запрещена. Подготовленный файл сохранён, "
            f"установленная версия не тронута. Скачайте новую поставку и "
            f"запустите установку",
            kind="requires_installer",
        )

    # --- GAP-161, проверка 1: детект по именованному мьютексу сервера (Windows) ----------
    # Раньше и дешевле файловой пробы ниже: если сервер сам объявил о себе, качать
    # подпись/делать бэкап незачем — отказ формулируется как факт, а не как файловая
    # ошибка неясной природы. Fail-open ДЕТЕКТА (см. docstring mcp_mutex) — `False` здесь
    # значит «мьютекса не видно», а не «сервер точно не работает»; последнее слово всё
    # равно за пробой ниже и за самой подменой.
    if mcp_mutex.server_mutex_exists():
        raise ChannelError(
            "Обнаружен запущенный MCP-сервер BPMkit (по именованному системному объекту) "
            "— закройте Claude Desktop и повторите. Установленная версия не тронута, "
            "подготовленное обновление сохранено.",
            kind="mcp_running",
        )

    # --- fail-closed: подпись проверяется повторно, непосредственно перед подменой -------
    sidecar = record.get("sidecar")
    if not isinstance(sidecar, dict):
        raise ChannelError(
            "У подготовленного обновления нет подтверждённой подписи — применение "
            "запрещено политикой канала",
            kind="signature_not_available",
        )
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))
    verified = signature.verify_artifact(
        src, sidecar, pubkey_raw,
        expected_name=str(record.get("filename") or "") or None,
        expected_sha256=_norm_hex(record.get("sha256")) or None)

    dest = Path(str(target or getattr(ctx, "binary_path", "") or ""))
    if not str(dest):
        raise ChannelError(
            "Не известен путь к устанавливаемому бинарю MCP — обновление не применяется",
            kind="local_io",
        )

    # --- GAP-212: тип артефакта обязан подходить цели подмены (ДО бэкапа и мутаций) ------
    _ensure_artifact_applicable(src, dest)

    previous_version = _current_version(state, ctx)
    new_version = str(record.get("version") or "").strip()

    # --- GAP-161, проверка 2: неразрушающая файловая проба, ДО бэкапа --------------------
    # Ловит занятый файл ЛЮБОЙ природы (старый сервер без мьютекса, чужой процесс) — тем
    # же текстом, каким закончилась бы настоящая подмена (см. except OSError ниже по
    # `replace_with_retry`), но раньше: ни бэкап, ни установленная версия ещё не тронуты.
    try:
        fsutil.probe_writable(dest)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось заменить файл MCP ({dest}): {exc}. Скорее всего, MCP-сервер сейчас "
            f"запущен и держит файл — закройте Claude Desktop и повторите. Установленная "
            f"версия не тронута, подготовленное обновление сохранено.",
            kind="local_io",
        ) from None

    # --- бэкап ДО подмены ---------------------------------------------------------------
    stamp = _safe_component(utc_now_iso())
    backup_name = f"{dest.stem}-{_safe_component(previous_version)}-{stamp}{dest.suffix}"
    try:
        backup_path = fsutil.backup_copy(dest, _backup_dir(ctx), backup_name)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось сохранить резервную копию текущего бинаря ({dest}): {exc} — "
            f"обновление не применяется",
            kind="local_io",
        ) from None
    backup_sha = fsutil.sha256_file(backup_path) if backup_path else None

    # --- подмена -------------------------------------------------------------------------
    try:
        fsutil.replace_with_retry(src, dest)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось заменить файл MCP ({dest}): {exc}. Скорее всего, MCP-сервер сейчас "
            f"запущен и держит файл — закройте Claude Desktop и повторите. Установленная "
            f"версия не тронута, подготовленное обновление сохранено.",
            kind="local_io",
        ) from None

    applied_at = utc_now_iso()
    state.push_history({
        "version": new_version,
        "previous_version": previous_version,
        "backup": str(backup_path) if backup_path else "",
        "backup_sha256": backup_sha,
        "sha256": record.get("sha256"),
        "binary": str(dest),
        "applied_at": applied_at,
    })
    rel["current"] = {
        "version": new_version,
        "sha256": record.get("sha256"),
        "size_bytes": record.get("size_bytes"),
        "key_id": verified.get("key_id"),
        "signed_at": verified.get("signed_at"),
        "path": str(dest),
        "applied_at": applied_at,
    }
    rel["staged"] = None
    rel["restart_required"] = True
    state.mark("releases", "ok", RESTART_MESSAGE)
    state.save()

    return {
        "applied": True,
        "version": new_version or None,
        "previous_version": previous_version,
        "binary": str(dest),
        "backup": str(backup_path) if backup_path else None,
        "sha256": record.get("sha256"),
        "key_id": verified.get("key_id"),
        "restart_required": True,
        "message": RESTART_MESSAGE,
        "reason": "applied",
    }


# ======================================================================================
# Канал установщика (ADR-0048, GAP-279, 23.09.2026)
# ======================================================================================
#
# Узкий отдельный поток по образцу канала релизов — СВОЙ адрес (INSTALLER_PREFIX), СВОЙ
# слот состояния (`rel["installer_staged"]`, releases["staged"] не трогается вовсе), но
# ПРИНЦИПИАЛЬНО другое "apply": установщик не ПОДМЕНЯЕТ бинарь, а ЗАПУСКАЕТСЯ (ADR-0048
# п.4/п.5). `_ensure_artifact_applicable` (GAP-415, выше) уже отказывает применить файл с
# именем установщика через `apply_staged` -- этот блок даёт ему ЗАКОННЫЙ путь вместо
# запрещённого.
#
# Три гарда, все ДО запуска процесса:
#   1. `kind` сайдкара ОБЯЗАН быть "installer" (`signature.verify_artifact(...,
#      expected_kind="installer")`, ADR-0048 п.2/п.4) -- симметрично серверной проверке
#      `app.installer.signature_of` (BPMkit-backend): у канала установщика нет легаси
#      сайдкаров без поля, послабления "отсутствует -> подразумевается" здесь НЕТ (в
#      отличие от `stage()`/`apply_staged` выше, где отсутствие трактуется как "server"
#      ради обратной совместимости с уже опубликованными релизами);
#   2. имя файла обязано подходить под `_INSTALLER_FILENAME_RE`
#      (`bpmkit-setup-<version>.exe`) -- та же защита, что у `_ensure_artifact_applicable`,
#      но на СВОЁМ канале: сервер публикует установщик под этим шаблоном, файл с любым
#      другим именем в этом слоте -- признак путаницы каналов, не гипотетика;
#   3. PE-заголовок `MZ` -- установщик тоже исполняемый Windows-файл, и переименованный
#      архив с "правильным" именем не должен пройти молча (тот же принцип, что у GAP-212).


def _fetch_installer_meta(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{INSTALLER_PREFIX}/{target}/meta")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Метаданные установщика пришли не объектом JSON — обновление не применяется",
            kind="bad_response",
        )
    return payload


def _fetch_installer_sidecar(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{INSTALLER_PREFIX}/{target}/signature")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Сайдкар подписи установщика пришёл не объектом JSON — обновление не применяется",
            kind="signature_not_available",
        )
    return payload


def check_installer(client, version: str = LATEST) -> dict:
    """Дешёвая проверка «есть ли опубликованный установщик» — `GET .../meta`, БЕЗ
    скачивания тела. `404 installer not configured` (издатель не выложил установщик
    для этой версии, ЛИБО канал вовсе не сконфигурирован на бэкенде) превращается в
    typed-отказ `installer_not_available` — штатный молчаливый пропуск, не ошибка
    (симметрично `release not configured` у `check()` выше)."""
    target, note = _resolve_target(version)
    try:
        meta = _fetch_installer_meta(client, target)
    except ChannelError as exc:
        if exc.kind == "http_error" and exc.http_status == 404:
            raise ChannelError(
                "Установщик для этой версии не опубликован издателем",
                kind="installer_not_available",
            ) from None
        raise
    meta["note"] = note
    return meta


def stage_installer(client, state, ctx, version: str = LATEST) -> dict:
    """Скачать установщик в стейджинг и полностью его проверить (сайдкар, `kind`,
    PE-заголовок). Дословный порядок шагов `stage()` выше (meta → signed → pubkey →
    скачивание с докачкой → размер/sha256 → сайдкар → атомарное переименование), с
    единственным содержательным отличием — обязательный `expected_kind="installer"`
    при проверке подписи (ADR-0048 п.2/п.4) и своё имя слота состояния
    (`rel["installer_staged"]`, НЕ трогает `rel["staged"]` релизного канала)."""
    rel = state.releases
    target, note = _resolve_target(version)

    meta = _fetch_installer_meta(client, target)
    filename = _safe_filename(meta.get("filename"))
    if not _INSTALLER_FILENAME_RE.match(filename):
        raise ChannelError(
            f"Издатель выложил файл {filename!r} с именем, не подходящим под шаблон "
            f"установщика bpmkit-setup-<version>.exe — обновление не применяется "
            f"(ADR-0048 п.3).",
            kind="artifact_kind_mismatch",
        )
    expected_sha = _norm_hex(meta.get("sha256"))
    if not _SHA256_RE.match(expected_sha):
        raise ChannelError(
            "Метаданные установщика не содержат корректной контрольной суммы sha256 — "
            "обновление не применяется",
            kind="bad_response",
        )
    size_bytes = _int_or_none(meta.get("size_bytes")) or 0
    meta_version = str(meta.get("version") or "").strip()
    signed_flag = bool(meta.get("signed"))

    if not signed_flag:
        raise ChannelError(
            f"Сервер не подтвердил подпись установщика {filename} (signed: false) — "
            f"файл не скачивается и не применяется",
            kind="signature_not_available",
        )

    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))

    staging = _staging_dir(ctx) / "installer"
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось создать каталог подготовки установщика {staging}: {exc}",
            kind="local_io",
        ) from None
    part = staging / (filename + PART_SUFFIX)

    resume_from = _resume_offset(rel, part, expected_sha, size_bytes, slot="installer_partial")

    resumed = False
    if size_bytes and resume_from == size_bytes:
        resumed = True
    else:
        try:
            result = client.download(f"{INSTALLER_PREFIX}/{target}", part,
                                     resume_from=resume_from,
                                     expected_size=size_bytes or None)
        except ChannelError as exc:
            if exc.kind == "range_invalid":
                rel["installer_partial"] = None
                state.save()
                raise
            rel["installer_partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
            state.save()
            raise
        resumed = bool(isinstance(result, dict) and result.get("resumed"))

    actual_size = _size_on_disk(part)

    if size_bytes and actual_size < size_bytes:
        rel["installer_partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise ChannelError(
            f"Файл установщика скачан не полностью: {actual_size} из {size_bytes} байт — "
            f"докачаем на следующей проверке",
            kind="offline",
        )
    if size_bytes and actual_size > size_bytes:
        _unlink_quietly(part)
        rel["installer_partial"] = None
        state.save()
        raise ChannelError(
            f"Размер скачанного установщика больше объявленного ({actual_size} против "
            f"{size_bytes} байт) — данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        actual_sha = fsutil.sha256_file(part)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось посчитать контрольную сумму скачанного установщика {part}: {exc}",
            kind="local_io",
        ) from None
    if actual_sha != expected_sha:
        _unlink_quietly(part)
        rel["installer_partial"] = None
        state.save()
        raise ChannelError(
            f"Контрольная сумма скачанного установщика не сошлась с метаданными "
            f"(ожидался sha256 …{expected_sha[-8:]}, получен …{actual_sha[-8:]}) — "
            f"данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        sidecar = _fetch_installer_sidecar(client, target)
        verified = signature.verify_artifact(
            part, sidecar, pubkey_raw,
            expected_name=filename, expected_sha256=expected_sha,
            expected_kind="installer")
    except ChannelError:
        rel["installer_partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise

    try:
        with open(part, "rb") as fh:
            head = fh.read(len(_PE_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Скачанный установщик {part} не читается ({exc}) — обновление не применяется.",
            kind="local_io",
        ) from None
    if head != _PE_MAGIC:
        _unlink_quietly(part)
        rel["installer_partial"] = None
        state.save()
        raise ChannelError(
            f"Скачанный файл {filename} не является исполняемым Windows-файлом "
            f"(нет PE-заголовка MZ) — обновление не применяется.",
            kind="artifact_type_mismatch",
        )

    final = staging / filename
    try:
        fsutil.replace_with_retry(part, final)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось поместить проверенный установщик в стейджинг ({final}): {exc}",
            kind="local_io",
        ) from None
    _cleanup_staging(staging, keep=final.name)

    record = {
        "version": meta_version or (target if target != LATEST else ""),
        "filename": filename,
        "path": str(final),
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "signed": True,
        "key_id": verified.get("key_id"),
        "signed_at": verified.get("signed_at"),
        "target": target,
        "staged_at": utc_now_iso(),
        # GAP-279 (предпросмотр «диспетчер C→D»): версия BPMkitStand внутри установщика,
        # если издатель её сообщил в `/meta` (необязательное поле). Нет поля — `None`,
        # и UI честно говорит «диспетчер обновится из комплекта установщика», а не
        # выдумывает номер.
        "standkit_version": _optional_meta_version(meta.get("standkit_version")),
        "sidecar": sidecar,
    }
    rel["installer_staged"] = record
    rel["installer_partial"] = None
    detail = (f"Установщик {record['version'] or 'latest'} подготовлен и проверен; "
              f"установка — по явной команде пользователя")
    state.mark("releases", "ok", (note + ". " if note else "") + detail)
    state.save()

    out = {key: value for key, value in record.items() if key != "sidecar"}
    out["resumed"] = resumed
    out["reason"] = "installer_staged"
    out["note"] = note
    return out


def _optional_meta_version(value) -> Optional[str]:
    """Необязательная строка версии из `/meta`: только непустая строка разумной длины,
    всё прочее — `None` (поле приходит от издателя и в UI показывается как есть)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64:
        return None
    return value


#: Сколько после запуска установщика запись `installer_launched` считается «установка
#: идёт» (если процесс жив). Страховка от переиспользования pid: через полчаса живой
#: процесс с тем же номером — почти наверняка уже не наш установщик.
INSTALLER_RUNNING_WINDOW_SEC = 1800

#: `ERROR_ELEVATION_REQUIRED` (740): Windows отказала запустить исполняемый файл без
#: повышения прав. Установщик собран с `PrivilegesRequired=lowest`, но при установке
#: «для всех пользователей» (Program Files) повторный запуск наследует режим прошлой
#: установки и требует UAC, а `CreateProcess` из непривилегированного хаба UAC не
#: показывает — только эту ошибку.
INSTALLER_ELEVATION_WINERROR = 740


def installer_status(state, *, now_iso: Optional[str] = None) -> dict:
    """Карточка канала установщика для `/api/companion/status` (GAP-279): что подготовлено
    (без сайдкара) и что запущено, с признаком «установщик ещё работает».

    Файл подготовленного установщика проверяется на диске (как `staged_info` у
    релизного канала): запись без файла — это «нечего устанавливать»."""
    rel = state.releases
    staged = staged_installer_info(state)
    if staged is not None:
        try:
            if not Path(str(staged.get("path") or "")).is_file():
                staged = None
        except OSError:
            staged = None
    launched = rel.get("installer_launched")
    launched_out = None
    if isinstance(launched, dict):
        launched_out = dict(launched)
        running = False
        try:
            pid = int(launched.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and _launched_recently(launched.get("launched_at"), now_iso):
            try:
                running = bool(is_alive(pid))
            except Exception:  # noqa: BLE001 - статус не имеет права упасть из-за OS-проверки
                running = False
        launched_out["running"] = running
    return {"staged": staged, "launched": launched_out}


def _launched_recently(launched_at, now_iso: Optional[str]) -> bool:
    from datetime import datetime, timezone

    def _parse(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    started = _parse(launched_at)
    if started is None:
        return False
    now = _parse(now_iso) if now_iso else datetime.now(timezone.utc)
    if now is None:
        return False
    return 0 <= (now - started).total_seconds() <= INSTALLER_RUNNING_WINDOW_SEC


def staged_installer_info(state) -> Optional[dict]:
    """Карточка подготовленного установщика для UI/CLI — сайдкар наружу не отдаётся
    (симметрично `staged_info` выше)."""
    record = state.releases.get("installer_staged")
    if not isinstance(record, dict):
        return None
    return {key: value for key, value in record.items() if key != "sidecar"}


def apply_installer(state, ctx, *, target: Optional[str] = None) -> dict:
    """ЗАПУСТИТЬ подготовленный установщик (ADR-0048 п.4/п.5) — НЕ подменить им
    файл. Решение владельца 19.09.2026: тихая установка, но кнопку нажимает
    человек (SECURITY.md §4.1 не нарушается — см. ADR-0048 «Решение» п.5): этот
    вызов — явное действие пользователя из CLI/UI, после предпросмотра версий,
    точно так же, как `apply_staged` вызывается только явной командой.

    Порядок проверок, все ДО запуска процесса (тот же fail-closed принцип, что
    у `apply_staged`):
      1. подготовленный установщик вообще есть (`nothing_staged`);
      2. `kind` сайдкара — ПОВТОРНО, ЗАНОВО, тем же путём, что при `stage_installer`
         (между подготовкой и запуском проходит время, файл в стейджинге могли
         подменить — та же логика, что перепроверка подписи в `apply_staged`);
      3. PE-заголовок MZ (файл на диске за это время не подменили на нечто другое);
      4. установленный публичный ключ ещё соответствует ожидаемому (переиспользуется
         тот же сайдкар, сохранённый при подготовке).

    Установщик запускается В ФОНЕ (`standkit.platform.spawn_hidden`, НЕ `subprocess.run` — вызывающая
    сторона не обязана ждать конца Inno Setup) с флагами тихой установки; он сам
    останавливает и поднимает хаб (ADR-0048 п.7, GAP-276 п.1) и обновляет MCP/скиллы —
    канал здесь его только ЗАПУСКАЕТ, дальше это ответственность установщика."""
    rel = state.releases
    record = rel.get("installer_staged")
    if not isinstance(record, dict) or not record.get("path"):
        raise ChannelError(
            "Подготовленного установщика нет — сначала выполните проверку и подготовку "
            "обновления",
            kind="nothing_staged",
        )
    if target is not None and str(record.get("version") or "") != str(target):
        raise ChannelError(
            f"Подготовлен установщик версии {record.get('version')!r}, а запрошено "
            f"применение версии {target!r} — подготовьте нужную версию заново",
            kind="nothing_staged",
        )

    src = Path(record["path"])
    if not src.is_file():
        rel["installer_staged"] = None
        state.save()
        raise ChannelError(
            f"Подготовленный установщик исчез с диска ({src}) — подготовьте обновление "
            f"заново",
            kind="nothing_staged",
        )

    sidecar = record.get("sidecar")
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))
    verified = signature.verify_artifact(
        src, sidecar, pubkey_raw,
        expected_name=record.get("filename"), expected_sha256=record.get("sha256"),
        expected_kind="installer")

    try:
        with open(src, "rb") as fh:
            head = fh.read(len(_PE_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Подготовленный установщик {src.name} не читается ({exc}) — установка не "
            f"запущена.",
            kind="local_io",
        ) from None
    if head != _PE_MAGIC:
        raise ChannelError(
            f"Подготовленный установщик {src.name} потерял PE-заголовок — установка не "
            f"запущена, обратитесь к издателю.",
            kind="artifact_type_mismatch",
        )

    # Тихий фоновый запуск БЕЗ консольного окна — через `standkit.platform.spawn_hidden`,
    # ЕДИНУЮ точку запуска процессов пакета (GAP-138): голый `subprocess.Popen` вне
    # `standkit/platform.py` запрещён и стережётся `tests/test_no_window.py` статически
    # (родитель — `pythonw.exe`/служба без своей консоли, и без CREATE_NO_WINDOW каждый
    # дочерний процесс мигнул бы чёрным окном). Лог установщика — рядом со стейджингом,
    # НЕ теряется между тиками (используется при диагностике «установка не завершилась»).
    log_path = companion_workdir(ctx) / "installer_install.log"
    # GAP-279: Inno Setup — GUI-процесс, в stdout он не пишет ничего; настоящий журнал
    # установки (почему отказал: запущенный MCP-сервер, занятый диспетчер и т.п.) даёт
    # только ключ /LOG. Именно этот путь UI показывает человеку, если диспетчер после
    # установки не перезапустился.
    setup_log = companion_workdir(ctx) / "installer_setup.log"
    try:
        pid = spawn_hidden(
            [str(src), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/LOG={setup_log}"],
            cwd=src.parent, log_path=log_path)
    except ProcessError as exc:
        cause = exc.__cause__
        if getattr(cause, "winerror", None) == INSTALLER_ELEVATION_WINERROR:
            # GAP-279: прежняя установка сделана «для всех пользователей» — установщику
            # нужны права администратора, а хаб без них UAC показать не может. Отдельный
            # kind, чтобы UI предложил понятный выход, а не «локальную ошибку».
            raise ChannelError(
                "Установщику нужны права администратора (BPMkit установлен для всех "
                "пользователей). Перезапустите диспетчер с правами администратора и "
                f"повторите установку, либо запустите установщик вручную: {src}",
                kind="elevation_required",
            ) from None
        raise ChannelError(
            f"Не удалось запустить установщик {src}: {exc}",
            kind="local_io",
        ) from None

    launched_at = utc_now_iso()
    rel["installer_launched"] = {
        "version": record.get("version"),
        "standkit_version": record.get("standkit_version"),
        "path": str(src),
        "pid": pid,
        "launched_at": launched_at,
        "key_id": verified.get("key_id"),
        "log": str(setup_log),
        "stdout_log": str(log_path),
    }
    state.mark("releases", "ok",
               f"Установщик {record.get('version') or ''} запущен (pid={pid}) — "
               f"MCP, диспетчер и скиллы обновит он сам; диспетчер перезапустится "
               f"автоматически (ADR-0048).")
    state.save()

    return {
        "launched": True,
        "version": record.get("version"),
        "pid": pid,
        "path": str(src),
        "key_id": verified.get("key_id"),
        "launched_at": launched_at,
        "log": str(setup_log),
        "stdout_log": str(log_path),
        "reason": "installer_launched",
    }


# ======================================================================================
# Откат
# ======================================================================================
def rollback(state, ctx, *, version: Optional[str] = None) -> dict:
    """Вернуть предыдущий бинарь из бэкапа.

    `version` — версия, НА которую откатываемся (то есть `previous_version` записи истории).
    Без неё берётся самая свежая запись, то есть шаг назад ровно на одно обновление.

    Бэкап не переносится, а КОПИРУЕТСЯ на место бинаря: перенос сделал бы откат
    одноразовым, а вторая попытка отката (или диагностика «чем именно это было») осталась
    бы без исходника.

    История укорачивается до точки отката включительно: записи о версиях, которые мы только
    что откатили, больше не описывают реальность, и оставлять их — значит однажды
    «откатиться» на файл, которого на диске давно нет.
    """
    rel = state.releases
    history = rel.get("history") or []
    if not history:
        raise ChannelError(
            "Откатываться не на что: в истории канала нет ни одного применённого "
            "обновления",
            kind="nothing_to_rollback",
        )

    index = 0
    if version:
        wanted = str(version).strip()
        index = next((i for i, entry in enumerate(history)
                      if str(entry.get("previous_version") or "").strip() == wanted), -1)
        if index < 0:
            raise ChannelError(
                f"В истории канала нет резервной копии версии {wanted} — откат невозможен",
                kind="nothing_to_rollback",
            )

    entry = history[index]
    backup = Path(str(entry.get("backup") or ""))
    if not backup.is_file():
        raise ChannelError(
            f"Резервная копия {backup} недоступна — откат невозможен",
            kind="nothing_to_rollback",
        )

    dest = Path(str(entry.get("binary") or getattr(ctx, "binary_path", "") or ""))
    if not str(dest):
        raise ChannelError(
            "Не известен путь к бинарю MCP — откат невозможен",
            kind="local_io",
        )

    # Копия рядом с целью: `os.replace` атомарен только в пределах одного тома, а каталог
    # бэкапов вполне может оказаться на другом диске.
    try:
        staged_copy = fsutil.backup_copy(backup, dest.parent, dest.name + ".rollback")
    except OSError as exc:
        raise ChannelError(
            f"Не удалось подготовить откат рядом с {dest}: {exc}",
            kind="local_io",
        ) from None
    if staged_copy is None:
        raise ChannelError(
            f"Резервная копия {backup} исчезла во время отката",
            kind="local_io",
        )

    try:
        fsutil.replace_with_retry(staged_copy, dest)
    except OSError as exc:
        _unlink_quietly(staged_copy)
        raise ChannelError(
            f"Не удалось вернуть прежнюю версию на место ({dest}): {exc}. Скорее всего, "
            f"MCP-сервер запущен и держит файл — закройте Claude Desktop и повторите.",
            kind="local_io",
        ) from None

    restored = str(entry.get("previous_version") or "").strip()
    rolled_from = str(entry.get("version") or "").strip()
    rel["current"] = {
        "version": restored,
        "sha256": entry.get("backup_sha256"),
        "path": str(dest),
        "applied_at": utc_now_iso(),
        "rolled_back_from": rolled_from or None,
    }
    del history[:index + 1]
    rel["restart_required"] = True
    message = (f"Возвращена версия {restored or 'предыдущая'}. Перезапустите Claude Desktop, "
               f"чтобы MCP-сервер запустился из вернувшегося файла.")
    state.mark("releases", "ok", message)
    state.save()

    return {
        "rolled_back": True,
        "version": restored or None,
        "from_version": rolled_from or None,
        "binary": str(dest),
        "backup": str(backup),
        "restart_required": True,
        "message": message,
        "reason": "rolled_back",
    }


def prune_backups(state, ctx) -> int:
    """Удалить бэкапы, на которые больше не ссылается история. Возвращает число удалённых.

    Единственный критерий — ссылка из `state.releases["history"]`, которую `state`
    подрезает до `RELEASE_HISTORY_KEEP` записей. Поэтому копии текущей и предыдущей версий
    переживают уборку по построению, а на диске не копятся десятки мегабайт от релизов,
    откатиться на которые уже нельзя.

    Уборка сознательно НЕ трогает состояние: это операция над диском, и её неудача не
    должна влиять на то, что канал считает установленным.
    """
    directory = _backup_dir(ctx)
    if not directory.is_dir():
        return 0
    keep = {Path(str(entry.get("backup") or "")).name
            for entry in (state.releases.get("history") or [])
            if entry.get("backup")}
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file() or entry.name in keep:
            continue
        before = entry.exists()
        _unlink_quietly(entry)
        if before and not entry.exists():
            removed += 1
    return removed
