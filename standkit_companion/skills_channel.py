# -*- coding: utf-8 -*-
"""Канал обновления скиллов/плагина (kind=skills) — GAP-288.

Третий узкий поток по образцу установщика (ADR-0048): своя пара
`meta`/`signature` под `/v1/content/skills/...`, свой стейджинг, свой сайдкар
с ОБЯЗАТЕЛЬНЫМ `kind="skills"`. Предмет — `bpmkit-skills-<mcp_version>.plugin`
(ZIP, собирает `packaging/build_plugin.py` dev-репо), версия в имени —
версия САМОГО MCP (`manifest.json`), НЕ версия `standkit` (в отличие от
`hub_channel.py`).

**Два получателя, а не один.** Артефакт распаковывается в ДВА разных места
одним и тем же скачанным файлом:

1. **скиллы** — раскладываются каналом же (`apply_skills`) в файловые хосты,
   куда их когда-то положил мастер установки (`install_config.json`, ключ
   `skills_installed`), тем же движком, что и у самого CLI
   (`bpmkit setup skills-install <client> --app-dir <распакованный .plugin>`).
   Хостов, которые НЕ читают скиллы с диска (Claude Desktop/Cowork — только
   плагином через интерфейс приложения), это не касается — см. rc=0/skipped
   у самой подкоманды;
2. **`.plugin`** — просто кладётся в папку для РУЧНОЙ загрузки в Claude
   Desktop/Cowork (`%APPDATA%\\BPMkit\\plugin\\`). Программной установки
   плагина в приложение канал не делает и делать не может — только показывает
   путь, кнопку «Открыть папку» и ссылку на раздел кукбука.

**Распаковка — с гардом от zip-slip.** `.plugin` — обычный ZIP, и запись,
чьё имя внутри архива содержит `../`/абсолютный путь, не должна суметь
записать файл ВНЕ целевого каталога — только потому, что архив подписан
издателем, доверять его СОДЕРЖИМОМУ бесконтрольно нельзя (то же рассуждение,
что у `releases._ensure_artifact_applicable`, только для распаковки, а не
подмены одного файла).

**Установленная версия — маркер на диске, а не state канала.** `state.skills`
хранит только то, что канал сам сделал В ЭТОМ пробуждении (что скачано,
проверено); ФАКТ «что реально установлено» — отдельный файл
`%APPDATA%\\BPMkit\\skills\\installed.json`, который читают и пишут разные
процессы (CLI, установщик, канал) — тот же принцип, что у `mcp_runtime.json`
в `releases.py`.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Optional

from standkit.platform import run_console
from standkit.registry import bpmkit_config_dir

from . import fsutil, signature
from .backend import CONTENT_PREFIX
from .errors import ChannelError
from .releases import (
    LATEST,
    PART_SUFFIX,
    _SHA256_RE,
    _cleanup_staging,
    _int_or_none,
    _norm_hex,
    _partial_record,
    _resume_offset,
    _safe_filename,
    _size_on_disk,
    _unlink_quietly,
    companion_workdir,
    compare_versions,
    read_runtime_marker,
)
from .state import utc_now_iso

__all__ = [
    "SKILLS_PREFIX",
    "SKILLS_DIRNAME",
    "PLUGIN_DIRNAME",
    "DIST_DIRNAME",
    "check_skills",
    "stage_skills",
    "staged_skills_info",
    "skills_status",
    "apply_skills",
    "installed_marker_path",
    "read_installed_marker",
    "install_summary",
    "install_summary_lite",
]

#: Префикс канала скиллов/плагина.
SKILLS_PREFIX = f"{CONTENT_PREFIX}/skills"

#: `bpmkit-skills-<mcp_version>.plugin` (ZIP). Версия — числовая, форма сервера.
_SKILLS_FILENAME_RE = re.compile(r"^bpmkit-skills-\d+(?:\.\d+)*\.plugin\Z")

#: Первые два байта ZIP (`PK\x03\x04` у обычного архива, но магия ограничена
#: `PK`, чтобы не отвергать пустой/однозаписной ZIP с другим третьим байтом).
_ZIP_MAGIC = b"PK"

#: Подкаталог маркера установленной версии — `%APPDATA%\BPMkit\skills\`.
SKILLS_DIRNAME = "skills"
#: Подкаталог для РУЧНОЙ загрузки `.plugin` в Claude Desktop/Cowork.
PLUGIN_DIRNAME = "plugin"
#: Подкаталог распакованных `.plugin` версий (`skills-dist\<v>\`).
DIST_DIRNAME = "skills-dist"

#: Имя маркера установленной версии скиллов.
_INSTALLED_MARKER_NAME = "installed.json"

#: Таймаут одного вызова CLI (`setup skills-install`/`detect-clients`) —
#: та же величина, что у `context._CLI_TIMEOUT_S`: операция локальная
#: (копирование файлов), не сетевая, минут ждать нечего.
_CLI_TIMEOUT_S = 30.0


# ======================================================================================
# Пути
# ======================================================================================
def _skills_marker_dir() -> Path:
    return bpmkit_config_dir() / SKILLS_DIRNAME


def installed_marker_path() -> Path:
    """`%APPDATA%\\BPMkit\\skills\\installed.json` — единственный источник
    правды об установленной версии скиллов (пишет ТОЛЬКО `apply_skills`)."""
    return _skills_marker_dir() / _INSTALLED_MARKER_NAME


def read_installed_marker() -> Optional[dict]:
    """Маркер установленной версии, best-effort (см. `releases.read_runtime_marker`
    за тем же рассуждением: маркер пишет отдельный процесс/предыдущий тик, и
    его отсутствие/повреждение — НЕ ошибка канала)."""
    path = installed_marker_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    return {
        "version": version,
        "sha256": str(data.get("sha256") or "") or None,
        "applied_at": str(data.get("applied_at") or "") or None,
        "hosts": [str(h) for h in (data.get("hosts") or []) if isinstance(h, (str, int))],
    }


def _current_skills_version(ctx) -> Optional[str]:
    """Версия, относительно которой считается «есть ли обновление».

    Приоритет — у маркера `installed.json`: он про скиллы конкретно. Маркера
    нет (свежая установка, канал ещё ни разу не применял скиллы) — берётся
    версия ЗАПУЩЕННОГО MCP (`mcp_runtime.json`, тот же маркер, что у
    `releases.py`): установщик кладёт скиллы той же версии, что и сам MCP,
    так что до первого применения канала это разумное приближение. Ни того,
    ни другого — `None`, и `check_skills` честно считает, что обновление
    есть (сравнивать не с чем — то же решение, что у `releases.check`)."""
    marker = read_installed_marker()
    if marker:
        return marker["version"]
    runtime = read_runtime_marker()
    if runtime:
        return runtime.get("version") or None
    return str(getattr(ctx, "mcp_version", "") or "").strip() or None


# ======================================================================================
# Проверка
# ======================================================================================
def _fetch_skills_meta(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{SKILLS_PREFIX}/{target}/meta")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Метаданные скиллов/плагина пришли не объектом JSON — обновление не применяется",
            kind="bad_response",
        )
    return payload


def _fetch_skills_sidecar(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{SKILLS_PREFIX}/{target}/signature")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Сайдкар подписи скиллов/плагина пришёл не объектом JSON — обновление не "
            "применяется",
            kind="signature_not_available",
        )
    return payload


def check_skills(client, state, ctx, version: str = LATEST) -> dict:
    """Дешёвая проверка «есть ли новые скиллы/плагин» — `GET .../meta` без
    скачивания тела. Симметрично `releases.check_installer`/`hub_channel.check_hub`."""
    skl = state.skills
    current = _current_skills_version(ctx)
    try:
        meta = _fetch_skills_meta(client, version)
    except ChannelError as exc:
        if exc.kind == "http_error" and exc.http_status == 404:
            state.mark("skills", "skipped", "Скиллы/плагин для этой версии не "
                                            "опубликованы издателем")
            state.save()
            return {"available": False, "latest": None, "current": current,
                    "signed": None, "reason": "skills_not_available"}
        raise

    latest = str(meta.get("version") or "").strip()
    signed = bool(meta.get("signed"))
    if not latest:
        available, reason = False, "version_unknown"
    elif not current:
        available, reason = True, "current_version_unknown"
    elif compare_versions(latest, current) > 0:
        available, reason = True, "update_available"
    else:
        available, reason = False, "up_to_date"

    skl["known_latest"] = latest or None
    detail = (f"Доступна версия {latest} (установлена {current or 'неизвестно'})"
              if available else f"Установлена актуальная версия скиллов ({current or '—'})")
    state.mark("skills", "ok", detail)
    state.save()
    return {
        "available": available,
        "latest": latest or None,
        "current": current,
        "signed": signed,
        "size_bytes": _int_or_none(meta.get("size_bytes")),
        "filename": str(meta.get("filename") or "") or None,
        "sha256": _norm_hex(meta.get("sha256")) or None,
        "target": version,
        "reason": reason,
    }


# ======================================================================================
# Подготовка
# ======================================================================================
def stage_skills(client, state, ctx, version: str = LATEST) -> dict:
    """Скачать `.plugin` в `%APPDATA%\\BPMkit\\plugin\\bpmkit-skills-<v>.plugin`
    и полностью проверить (сайдкар, `kind="skills"`, магия ZIP). Дословный
    порядок `releases.stage_installer`/`hub_channel.stage_hub`."""
    skl = state.skills
    target = str(version or LATEST).strip() or LATEST

    meta = _fetch_skills_meta(client, target)
    filename = _safe_filename(meta.get("filename"))
    if not _SKILLS_FILENAME_RE.match(filename):
        raise ChannelError(
            f"Издатель выложил файл {filename!r} с именем, не подходящим под шаблон "
            f"скиллов/плагина bpmkit-skills-<version>.plugin — обновление не применяется.",
            kind="artifact_kind_mismatch",
        )
    expected_sha = _norm_hex(meta.get("sha256"))
    if not _SHA256_RE.match(expected_sha):
        raise ChannelError(
            "Метаданные скиллов/плагина не содержат корректной контрольной суммы sha256 — "
            "обновление не применяется",
            kind="bad_response",
        )
    size_bytes = _int_or_none(meta.get("size_bytes")) or 0
    meta_version = str(meta.get("version") or "").strip()
    if not bool(meta.get("signed")):
        raise ChannelError(
            f"Сервер не подтвердил подпись скиллов/плагина {filename} (signed: false) — "
            f"файл не скачивается и не применяется",
            kind="signature_not_available",
        )
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))

    plugin_dir = bpmkit_config_dir() / PLUGIN_DIRNAME
    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось создать папку плагина {plugin_dir}: {exc}",
            kind="local_io",
        ) from None
    part = plugin_dir / (filename + PART_SUFFIX)
    resume_from = _resume_offset(skl, part, expected_sha, size_bytes, slot="partial")

    resumed = False
    if size_bytes and resume_from == size_bytes:
        resumed = True
    else:
        try:
            result = client.download(f"{SKILLS_PREFIX}/{target}", part,
                                     resume_from=resume_from,
                                     expected_size=size_bytes or None)
        except ChannelError as exc:
            if exc.kind == "range_invalid":
                skl["partial"] = None
                state.save()
                raise
            skl["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
            state.save()
            raise
        resumed = bool(isinstance(result, dict) and result.get("resumed"))

    actual_size = _size_on_disk(part)
    if size_bytes and actual_size < size_bytes:
        skl["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise ChannelError(
            f"Файл скиллов/плагина скачан не полностью: {actual_size} из {size_bytes} байт "
            f"— докачаем на следующей проверке",
            kind="offline",
        )
    if size_bytes and actual_size > size_bytes:
        _unlink_quietly(part)
        skl["partial"] = None
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
        _unlink_quietly(part)
        skl["partial"] = None
        state.save()
        raise ChannelError(
            f"Контрольная сумма скачанного файла не сошлась с метаданными (ожидался "
            f"sha256 …{expected_sha[-8:]}, получен …{actual_sha[-8:]}) — данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        sidecar = _fetch_skills_sidecar(client, target)
        verified = signature.verify_artifact(
            part, sidecar, pubkey_raw,
            expected_name=filename, expected_sha256=expected_sha,
            expected_kind="skills")
    except ChannelError:
        skl["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise

    try:
        with open(part, "rb") as fh:
            head = fh.read(len(_ZIP_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Скачанный файл {part} не читается ({exc}) — обновление не применяется.",
            kind="local_io",
        ) from None
    if head != _ZIP_MAGIC:
        _unlink_quietly(part)
        skl["partial"] = None
        state.save()
        raise ChannelError(
            f"Скачанный файл {filename} не является ZIP-архивом (нет магии PK) — "
            f"под видом плагина выложено что-то другое, обновление не применяется.",
            kind="artifact_type_mismatch",
        )

    final = plugin_dir / filename
    try:
        fsutil.replace_with_retry(part, final)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось поместить проверенный файл в папку плагина ({final}): {exc}",
            kind="local_io",
        ) from None
    # Старые версии .plugin в этой же папке НЕ вычищаются (в отличие от
    # `staging` каналов hub/releases): файл здесь — не промежуточный
    # стейджинг, а КОНЕЧНОЕ место для ручной загрузки, и пользователь мог
    # ещё не забрать прошлую версию. Чистятся только осиротевшие `.part`.
    for entry in plugin_dir.glob(f"*{PART_SUFFIX}"):
        if entry.name != part.name:
            _unlink_quietly(entry)

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
        "sidecar": sidecar,
    }
    skl["staged"] = record
    skl["partial"] = None
    state.mark("skills", "ok", f"Скиллы/плагин {record['version'] or 'latest'} "
                               f"подготовлены и проверены; применение — по явной команде")
    state.save()

    out = {key: value for key, value in record.items() if key != "sidecar"}
    out["resumed"] = resumed
    out["reason"] = "skills_staged"
    return out


def staged_skills_info(state) -> Optional[dict]:
    """Подготовленный файл (без сайдкара), или `None` — симметрично `releases.staged_info`."""
    record = state.skills.get("staged")
    if not isinstance(record, dict):
        return None
    path = str(record.get("path") or "")
    if not path or not Path(path).is_file():
        return None
    return {key: value for key, value in record.items() if key != "sidecar"}


def skills_status(state) -> dict:
    """Карточка канала для `/api/companion/status`.

    Установленная версия (GAP-528 п.2б): если маркера `installed.json` ещё
    нет (свежая установка, канал ещё ни разу не применял скиллы), берём
    версию ЗАПУЩЕННОГО MCP из `mcp_runtime.json` — то же приближение, что
    `_current_skills_version` уже использует для сетевого `check_skills`, но
    до этой правки НИКОГДА не попадало в статус: UI видел `installed: null`
    и писал «неизвестна», хотя маркер запущенного MCP рядом был. Источник
    отмечается `installed_source` ("marker" — честный `installed.json`,
    "running" — приближение по `mcp_runtime.json"), чтобы UI мог показать
    это отличие, а не выдать приближение за точный факт.

    `update_available` (GAP-528 п.2в): подготовленная версия (`staged`) ЛИБО
    известная более новая, чем действующая (маркер или приближение выше).
    """
    installed = read_installed_marker()
    installed_source = "marker" if installed else None
    if installed is None:
        runtime = read_runtime_marker()
        runtime_version = str((runtime or {}).get("version") or "").strip()
        if runtime_version:
            installed = {"version": runtime_version, "sha256": None,
                         "applied_at": None, "hosts": []}
            installed_source = "running"
    known_latest = state.skills.get("known_latest")
    staged = staged_skills_info(state)
    installed_version = (installed or {}).get("version")
    update_available = bool(staged) or (
        bool(known_latest) and bool(installed_version)
        and compare_versions(known_latest, installed_version) > 0
    )
    return {
        "installed": installed,
        "installed_source": installed_source,
        "known_latest": known_latest,
        "staged": staged,
        "update_available": update_available,
    }


# ======================================================================================
# Безопасная распаковка (zip-slip гард)
# ======================================================================================
def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Распаковать ZIP в `dest`, отвергая ЛЮБУЮ запись, которая метила бы
    вне `dest` (абсолютный путь, `../`, диск на Windows).

    Проверка — по РЕЗОЛВНУТОМУ пути (`Path.resolve`), а не по строке имени:
    архив, честно подписанный издателем, всё равно не заслуживает доверия к
    СОДЕРЖИМОМУ путей внутри — тот же принцип, что у остальных fail-closed
    проверок канала (подпись доказывает подлинность файла, не безопасность
    того, что внутри).
    """
    dest = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if not name or name.startswith("/") or name.startswith("\\"):
            raise ChannelError(
                f"Архив плагина содержит недопустимый путь {name!r} — распаковка "
                f"остановлена (zip-slip).",
                kind="artifact_type_mismatch",
            )
        target = (dest / name).resolve()
        try:
            target.relative_to(dest)
        except ValueError:
            raise ChannelError(
                f"Архив плагина пытается записать файл вне целевой папки ({name!r}) — "
                f"распаковка остановлена (zip-slip).",
                kind="artifact_type_mismatch",
            ) from None
    zf.extractall(dest)


def _extract_plugin(archive: Path, dist_root: Path) -> Path:
    """Распаковать `.plugin` в `dist_root`, вернуть путь к дереву `skills/`
    внутри распакованного (контракт `packaging/build_plugin.py` dev-репо —
    архив несёт папку `skills/` на верхнем уровне)."""
    dist_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            _safe_extract(zf, dist_root)
    except zipfile.BadZipFile as exc:
        raise ChannelError(
            f"Файл плагина повреждён и не распаковывается как ZIP: {exc}",
            kind="integrity_mismatch",
        ) from None
    skills_dir = dist_root / "skills"
    if not skills_dir.is_dir():
        raise ChannelError(
            f"В распакованном плагине нет папки skills/ ({dist_root}) — поставка "
            f"неполна, применение остановлено.",
            kind="bad_response",
        )
    return dist_root


# ======================================================================================
# Применение
# ======================================================================================
def _run_cli_json(cli: list, args: list, *, timeout: float = _CLI_TIMEOUT_S) -> dict:
    """Один вызов CLI BPMkit → разобранный JSON stdout, best-effort устойчиво
    к постороннему выводу (см. `context._parse_stdout` — тот же приём:
    вырезаем от первой `{` до последней `}`)."""
    try:
        proc = run_console(list(cli) + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - один вид отказа наружу
        raise ChannelError(
            f"Не удалось запустить CLI BPMkit ({' '.join(args)}): {exc}",
            kind="local_io",
        ) from None
    text = (proc.stdout or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ChannelError(
                f"CLI BPMkit ({' '.join(args)}) не вернул разбираемый JSON",
                kind="bad_response",
                detail=(proc.stderr or "")[:500],
            ) from None
        try:
            return json.loads(text[start:end + 1])
        except ValueError as exc:
            raise ChannelError(
                f"CLI BPMkit ({' '.join(args)}) вернул неразбираемый JSON: {exc}",
                kind="bad_response",
            ) from None


def _app_dir_from_ctx(ctx) -> Optional[Path]:
    """Каталог установки MCP — `<app_dir>` контракта (родитель родителя
    бинаря из маркера `mcp_runtime.json`: `.../<app_dir>/bin/bpmkit.exe` →
    `<app_dir>`). Маркера нет — фолбэк на `ctx.package_root`/`ctx.binary_path`."""
    marker = read_runtime_marker()
    binary = str((marker or {}).get("binary") or getattr(ctx, "binary_path", "") or "").strip()
    if binary:
        try:
            candidate = Path(binary).resolve().parent.parent
            if candidate and str(candidate) not in ("", "."):
                return candidate
        except (OSError, RuntimeError, ValueError):
            pass
    root = str(getattr(ctx, "package_root", "") or "").strip()
    return Path(root) if root else None


def _install_config_path(ctx) -> Optional[Path]:
    """`install_config.json` установки — по контракту мастера он лежит рядом
    с каталогом установки (`<app_dir>/install_config.json`). Файла нет/каталог
    неизвестен — `None`, вызывающий код обязан трактовать это как «список
    клиентов не известен», а не падать."""
    app_dir = _app_dir_from_ctx(ctx)
    if app_dir is None:
        return None
    candidate = app_dir / "install_config.json"
    return candidate if candidate.is_file() else None


def _read_install_config(ctx) -> dict:
    path = _install_config_path(ctx)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _skills_installed_clients(ctx) -> list:
    """Список клиентов из `install_config.json`, ключ `skills_installed` —
    куда мастер уже клал скиллы (контракт `BPMkit/server/bpmkit/setup_cli.py`,
    подкоманда `skills-install`). Формат значения — по факту записи мастера:
    строка через запятую ИЛИ список; оба разбираются терпимо."""
    raw = _read_install_config(ctx).get("skills_installed")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    return []


def install_summary_lite() -> dict:
    """Дешёвая версия карточки «Скиллы и плагин» для `status()` — БЕЗ резолва
    лицензионного контекста и БЕЗ запуска CLI (`setup detect-clients`).

    `status()` канала обязан отвечать мгновенно и дёргается UI-поллером раз в
    несколько секунд (см. докстринг `CompanionRunner.status`) — резолв
    контекста запускает процесс CLI с таймаутом до 30 секунд, и звать его на
    каждый опрос значило бы то же нарушение, от которого `status()` уже
    защищён для лицензии. Поэтому здесь — только то, что читается с диска
    напрямую: `app_dir`/`install_config.json` через маркер `mcp_runtime.json`
    (файл, не процесс) и локальный список `skills_installed`.

    Отличие от полной `install_summary(ctx)`: список клиентов здесь — РОВНО
    те, что уже отмечены в `install_config.json` (без сверки «найден ли
    хост-каталог сейчас» через CLI); полную версию с детектом зовёт явное
    действие (`check_skills`/`apply_skills`), не пассивный опрос статуса."""
    marker = read_runtime_marker()
    binary = str((marker or {}).get("binary") or "").strip()
    app_dir: Optional[Path] = None
    if binary:
        try:
            candidate = Path(binary).resolve().parent.parent
            if str(candidate) not in ("", "."):
                app_dir = candidate
        except (OSError, RuntimeError, ValueError):
            app_dir = None
    plugin_dir = bpmkit_config_dir() / PLUGIN_DIRNAME
    names: list = []
    if app_dir is not None:
        config_path = app_dir / "install_config.json"
        if config_path.is_file():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raw = {}
            value = raw.get("skills_installed") if isinstance(raw, dict) else None
            if isinstance(value, list):
                names = [str(item).strip() for item in value if str(item).strip()]
            elif isinstance(value, str):
                names = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
    return {
        "app_dir": str(app_dir) if app_dir else None,
        "plugin_dir": str(plugin_dir),
        "clients": [{"id": name, "name": name, "skills_installed": True} for name in names],
    }


def install_summary(ctx) -> dict:
    """`{app_dir, plugin_dir, clients:[{id, name, skills_installed}]}` для
    карточки «Скиллы и плагин» (GAP-288, контракт серии).

    `clients` — пересечение того, что ЗНАЕТ CLI (`setup detect-clients`, best
    effort — отсутствие CLI не роняет карточку целиком, просто список пуст) с
    отметкой `skills_installed`, взятой из `install_config.json`. Любой отказ
    CLI здесь ПРОГЛАТЫВАЕТСЯ: карточка обязана показать хотя бы пути, даже
    если CLI рядом не нашёлся или упал."""
    app_dir = _app_dir_from_ctx(ctx)
    plugin_dir = bpmkit_config_dir() / PLUGIN_DIRNAME
    installed_names = set(_skills_installed_clients(ctx))
    clients: list = []
    cli = [str(item) for item in (getattr(ctx, "cli", None) or [])]
    if cli:
        try:
            payload = _run_cli_json(cli, ["setup", "detect-clients"])
            for entry in payload.get("clients") or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                clients.append({
                    "id": name,
                    "name": str(entry.get("label") or name),
                    "found": bool(entry.get("found")),
                    "skills_installed": name in installed_names,
                })
        except ChannelError:
            clients = []
    return {
        "app_dir": str(app_dir) if app_dir else None,
        "plugin_dir": str(plugin_dir),
        "clients": clients,
    }


def apply_skills(state, ctx) -> dict:
    """Распаковать подготовленный `.plugin` и разложить скиллы по клиентам из
    `install_config.json` (ключ `skills_installed`).

    Порядок, тот же fail-closed принцип, что у `apply_installer`/
    `apply_self_update`:
    1. подготовленного файла нет/пропал → `nothing_staged`;
    2. подпись — ЗАНОВО, по сохранённому сайдкару (см. докстринг модуля,
       раздел про `apply_self_update` — то же рассуждение про TOCTOU);
    3. распаковка в `%APPDATA%\\BPMkit\\skills-dist\\<v>\\` с zip-slip гардом
       (`_safe_extract`) — ДО обращения к CLI;
    4. `bpmkit setup skills-install <client> --app-dir <распакованное>` для
       каждого клиента из `skills_installed`. Клиент, который сам не читает
       скиллы с диска (rc=0, `skipped: true`), не считается отказом; НЕНУЛЕВОЙ
       код для остальных — копится в отчёте, а не роняет весь проход (один
       упавший клиент не должен блокировать остальных).

    Маркер `installed.json` пишется ТОЛЬКО если хотя бы распаковка прошла
    успешно (сама подстановка версии/sha) — отдельные отказы применения к
    конкретным клиентам в маркер не идут, они видны в возвращаемом отчёте.
    Полный отказ (все клиенты упали) НЕ бросает исключение — это частичный
    успех (плагин всё равно лежит в папке для ручной загрузки), но помечается
    `kind="skills_apply_failed"` в поле `error` отчёта, чтобы UI показал
    проблему явно.
    """
    skl = state.skills
    record = skl.get("staged")
    if not isinstance(record, dict) or not record.get("path"):
        raise ChannelError(
            "Подготовленных скиллов/плагина нет — сначала выполните проверку и "
            "подготовку обновления",
            kind="nothing_staged",
        )
    src = Path(str(record["path"]))
    if not src.is_file():
        skl["staged"] = None
        state.save()
        raise ChannelError(
            f"Подготовленный файл плагина исчез с диска ({src}) — подготовьте обновление "
            f"заново",
            kind="nothing_staged",
        )

    sidecar = record.get("sidecar")
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))
    signature.verify_artifact(
        src, sidecar, pubkey_raw,
        expected_name=record.get("filename"), expected_sha256=record.get("sha256"),
        expected_kind="skills")

    version = str(record.get("version") or "unknown")
    dist_root = companion_workdir(ctx).parent / DIST_DIRNAME / version
    _extract_plugin(src, dist_root)

    clients = _skills_installed_clients(ctx)
    cli = [str(item) for item in (getattr(ctx, "cli", None) or [])]
    results = []
    hosts_applied = []
    for client_name in clients:
        if not cli:
            results.append({"client": client_name, "ok": False,
                            "error": "рядом нет CLI BPMkit — применить нечем"})
            continue
        try:
            payload = _run_cli_json(
                cli, ["setup", "skills-install", client_name, "--app-dir", str(dist_root)])
        except ChannelError as exc:
            results.append({"client": client_name, "ok": False, "error": str(exc)})
            continue
        ok = bool(payload.get("ok"))
        results.append({"client": client_name, "ok": ok,
                        "skipped": bool(payload.get("skipped")),
                        "error": payload.get("error")})
        if ok and not payload.get("skipped"):
            hosts_applied.append(client_name)

    any_attempted = bool(clients)
    all_failed = any_attempted and not any(r["ok"] for r in results)

    marker = {
        "version": version,
        "sha256": record.get("sha256"),
        "applied_at": utc_now_iso(),
        "hosts": hosts_applied,
    }
    try:
        _skills_marker_dir().mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_text(
            installed_marker_path(), json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise ChannelError(
            f"Скиллы распакованы и применены, но маркер установленной версии не "
            f"сохранён: {exc}",
            kind="local_io",
        ) from None

    skl["installed"] = marker
    detail = (f"Скиллы {version} применены к {len(hosts_applied)} из {len(clients)} "
             f"клиент(ов)" if clients else
             f"Скиллы {version} распакованы; клиентов для применения не найдено "
             f"(install_config.json пуст или недоступен)")
    error = None
    if all_failed:
        error = {"kind": "skills_apply_failed",
                 "title": "Не удалось применить скиллы к найденным клиентам"}
    state.mark("skills", "error" if all_failed else "ok", detail)
    state.save()

    return {
        "version": version,
        "dist_root": str(dist_root),
        "plugin_path": str(src),
        "clients": results,
        "hosts": hosts_applied,
        "installed": marker,
        "error": error,
        "reason": "skills_applied",
    }
