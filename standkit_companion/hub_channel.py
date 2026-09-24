# -*- coding: utf-8 -*-
"""Канал самообновления диспетчера (kind=hub) — GAP-523.

Второй узкий поток по образцу установщика (ADR-0048), симметричный
`releases.check_installer`/`stage_installer`/`apply_installer`: своя пара
`meta`/`signature` под `/v1/content/hub/...`, свой стейджинг, свой слот
состояния (`state.hub`), ОБЯЗАТЕЛЬНЫЙ `kind="hub"` в сайдкаре (fail-closed —
симметрично `expected_kind="installer"` у потока установщика).

Предмет здесь — БИНАРЬ ДИСПЕТЧЕРА (`bpmkit-hub-<standkit_version>.exe`), не
бинарь MCP (тот остаётся за `releases.py`) и не установщик (тот запускается,
а не подменяет себя). Версия в имени файла — версия пакета `standkit`
(`standkit.__version__`), НЕ версия MCP: два номера расходятся, и сравнивать
их друг с другом было бы категорической ошибкой.

**Два режима установки, и это НЕ деталь UI, а развилка уже в `check_hub`.**
Самообновляющийся exe (`getattr(sys, "frozen", False)`, PyInstaller) умеет
скачать, проверить и подменить сам себя; pip-установка — нет, и канал не
имеет права попытаться: `pip install` от имени диспетчера — незапрошенная
установка стороннего пакета из процесса без террминала, на котором сидит
пользователь, за его спиной. Поэтому `stage_hub`/`apply_self_update` в
pip-режиме отказывают `self_update_unsupported` ДО любого сетевого похода
или мутации — ровно тот же принцип «отказ раньше действия», что у
`requires_installer` в `releases.py`.

**`apply_self_update` не завершает процесс диспетчера сам.** Он готовит и
ЗАПУСКАЕТ помощника (`--apply-self-update --target <exe> --wait-pid <pid>`,
см. `standkit_hub/__main__.py`), но останавливать себя — забота вызывающего
слоя (`standkit_hub.server`), у которого есть доступ к HTTP-серверу и его
штатному пути выключения (`request_self_shutdown`, тот же, что у
`POST /api/hub/shutdown`). Смешать эти две ответственности в одной функции
означало бы протащить HTTP-объект в модуль, который сегодня прекрасно
тестируется без единого сокета.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from standkit import __version__ as _standkit_version
from standkit.platform import ProcessError, spawn_hidden

from . import fsutil, signature
from .backend import CONTENT_PREFIX
from .errors import ChannelError
from .releases import (
    LATEST,
    PART_SUFFIX,
    _PE_MAGIC,
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
    is_numeric_version,
)
from .state import utc_now_iso

__all__ = [
    "HUB_PREFIX",
    "PYPI_PACKAGE_URL",
    "is_frozen_hub",
    "check_hub",
    "check_hub_pypi",
    "stage_hub",
    "staged_hub_info",
    "hub_status",
    "apply_self_update",
]

#: Префикс канала диспетчера — та же схема, что у `INSTALLER_PREFIX`.
HUB_PREFIX = f"{CONTENT_PREFIX}/hub"

#: `bpmkit-hub-<standkit_version>.exe` — контракт имени файла (см. CONTRACT
#: серии, раздел "kind hub"), версия — числовая, ТА ЖЕ форма, что у сервера.
_HUB_FILENAME_RE = re.compile(r"^bpmkit-hub-\d+(?:\.\d+)*\.exe\Z")

#: Публичный JSON-эндпоинт PyPI для best-effort проверки версии в pip-режиме.
#: НЕ лицензионный бэкенд издателя — отдельный хост, без авторизации, поэтому
#: идёт напрямую через `urllib`, а не через `BackendClient`.
PYPI_PACKAGE_URL = "https://pypi.org/pypi/standkit/json"

#: Таймаут запроса к PyPI. Best-effort проверка не имеет права подвесить тик
#: канала на системный таймаут сети (десятки секунд).
_PYPI_TIMEOUT_S = 6.0


def is_frozen_hub() -> bool:
    """Запущен ли ЭТОТ процесс из самообновляемой сборки (PyInstaller), а не
    из pip-установки/исходников.

    Отдельная функция, а не инлайновый `getattr`, — чтобы тесты могли
    подменить её одной точкой (`monkeypatch.setattr(hub_channel,
    "is_frozen_hub", ...)`), не трогая `sys.frozen` глобально."""
    return bool(getattr(sys, "frozen", False))


# ======================================================================================
# Проверка
# ======================================================================================
def _fetch_hub_meta(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{HUB_PREFIX}/{target}/meta")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Метаданные диспетчера пришли не объектом JSON — обновление не применяется",
            kind="bad_response",
        )
    return payload


def _fetch_hub_sidecar(client, target: str) -> dict:
    payload, _headers = client.get_json(f"{HUB_PREFIX}/{target}/signature")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Сайдкар подписи диспетчера пришёл не объектом JSON — обновление не применяется",
            kind="signature_not_available",
        )
    return payload


def check_hub(client, state, version: str = LATEST) -> dict:
    """Дешёвая проверка «есть ли новый диспетчер» для FROZEN-сборки — `GET .../meta`
    без скачивания тела. Симметрично `releases.check_installer`.

    `404 hub not configured` — издатель ещё не выложил диспетчер для этой версии
    (или канал не сконфигурирован на бэкенде) — штатный молчаливый пропуск, не
    ошибка.
    """
    hub = state.hub
    meta = None
    try:
        meta = _fetch_hub_meta(client, version)
    except ChannelError as exc:
        if exc.kind == "http_error" and exc.http_status == 404:
            hub["mode"] = "exe"
            state.mark("hub", "skipped", "Диспетчер для этой версии не опубликован издателем")
            state.save()
            return {
                "mode": "exe", "available": False, "latest": None,
                "current": _standkit_version, "signed": None, "reason": "hub_not_available",
            }
        raise
    latest = str(meta.get("version") or "").strip()
    signed = bool(meta.get("signed"))
    available = bool(latest) and compare_versions(latest, _standkit_version) > 0
    hub["mode"] = "exe"
    hub["known_latest"] = latest or None
    detail = (f"Доступна версия {latest} (установлена {_standkit_version})" if available
              else f"Установлена актуальная версия диспетчера ({_standkit_version})")
    state.mark("hub", "ok", detail)
    state.save()
    return {
        "mode": "exe",
        "available": available,
        "latest": latest or None,
        "current": _standkit_version,
        "signed": signed,
        "size_bytes": _int_or_none(meta.get("size_bytes")),
        "filename": str(meta.get("filename") or "") or None,
        "sha256": _norm_hex(meta.get("sha256")) or None,
        "target": version,
        "reason": "update_available" if available else "up_to_date",
    }


def check_hub_pypi(*, timeout: float = _PYPI_TIMEOUT_S) -> dict:
    """Best-effort проверка версии `standkit` на PyPI для PIP-режима.

    ЛУЧШЕЕ СТАРАНИЕ намеренно: недоступность PyPI, отсутствие сети, HTTP-ошибка
    — всё это НЕ поднимается как `ChannelError` и не роняет остальные циклы
    канала, а превращается в честное `available: False, reason: "offline"`.
    Причина — тот же принцип, что у `releases._update_release_notes`: попутная
    проверка не имеет права остановить канал целиком за отказ стороннего,
    неуправляемого издателем сервиса.

    Ключ ответа PyPI — `info.version` (контракт `GET /pypi/<name>/json`, тот же
    у самого PyPI, не BPMkit-специфика)."""
    try:
        req = Request(PYPI_PACKAGE_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - фиксированный https-хост
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, OSError, UnicodeDecodeError) as exc:
        return {
            "mode": "pip", "available": False, "latest": None,
            "current": _standkit_version, "reason": "offline",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    latest = str((payload.get("info") or {}).get("version") or "").strip()
    available = bool(latest) and compare_versions(latest, _standkit_version) > 0
    return {
        "mode": "pip",
        "available": available,
        "latest": latest or None,
        "current": _standkit_version,
        "reason": "update_available" if available else "up_to_date",
        "pip_command": "python -m pip install -U standkit",
    }


# ======================================================================================
# Подготовка (только FROZEN)
# ======================================================================================
def stage_hub(client, state, ctx, version: str = LATEST) -> dict:
    """Скачать `bpmkit-hub-<v>.exe` в стейджинг и полностью проверить (сайдкар,
    `kind="hub"`, PE-заголовок). Дословный порядок `releases.stage_installer`.

    Отказывает СРАЗУ, БЕЗ единого запроса, если процесс не самообновляемая
    сборка (`self_update_unsupported`) — тот же принцип «отказ раньше
    действия», что у `requires_installer`: pip-режим не умеет применить
    скачанное, и качать десятки мегабайт ради этого незачем.
    """
    if not is_frozen_hub():
        raise ChannelError(
            "Диспетчер запущен не из exe-сборки (pip/исходники) — самообновление "
            "недоступно, используйте команду pip",
            kind="self_update_unsupported",
        )
    hub = state.hub
    target = str(version or LATEST).strip() or LATEST
    if target != LATEST and not is_numeric_version(target):
        target = LATEST

    meta = _fetch_hub_meta(client, target)
    filename = _safe_filename(meta.get("filename"))
    if not _HUB_FILENAME_RE.match(filename):
        raise ChannelError(
            f"Издатель выложил файл {filename!r} с именем, не подходящим под шаблон "
            f"диспетчера bpmkit-hub-<version>.exe — обновление не применяется.",
            kind="artifact_kind_mismatch",
        )
    expected_sha = _norm_hex(meta.get("sha256"))
    if not _SHA256_RE.match(expected_sha):
        raise ChannelError(
            "Метаданные диспетчера не содержат корректной контрольной суммы sha256 — "
            "обновление не применяется",
            kind="bad_response",
        )
    size_bytes = _int_or_none(meta.get("size_bytes")) or 0
    meta_version = str(meta.get("version") or "").strip()
    if not bool(meta.get("signed")):
        raise ChannelError(
            f"Сервер не подтвердил подпись диспетчера {filename} (signed: false) — "
            f"файл не скачивается и не применяется",
            kind="signature_not_available",
        )
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))

    staging = companion_workdir(ctx) / "hub"
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось создать каталог подготовки диспетчера {staging}: {exc}",
            kind="local_io",
        ) from None
    part = staging / (filename + PART_SUFFIX)
    resume_from = _resume_offset(hub, part, expected_sha, size_bytes, slot="partial")

    resumed = False
    if size_bytes and resume_from == size_bytes:
        resumed = True
    else:
        try:
            result = client.download(f"{HUB_PREFIX}/{target}", part,
                                     resume_from=resume_from,
                                     expected_size=size_bytes or None)
        except ChannelError as exc:
            if exc.kind == "range_invalid":
                hub["partial"] = None
                state.save()
                raise
            hub["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
            state.save()
            raise
        resumed = bool(isinstance(result, dict) and result.get("resumed"))

    actual_size = _size_on_disk(part)
    if size_bytes and actual_size < size_bytes:
        hub["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise ChannelError(
            f"Файл диспетчера скачан не полностью: {actual_size} из {size_bytes} байт — "
            f"докачаем на следующей проверке",
            kind="offline",
        )
    if size_bytes and actual_size > size_bytes:
        _unlink_quietly(part)
        hub["partial"] = None
        state.save()
        raise ChannelError(
            f"Размер скачанного диспетчера больше объявленного ({actual_size} против "
            f"{size_bytes} байт) — данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        actual_sha = fsutil.sha256_file(part)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось посчитать контрольную сумму скачанного диспетчера {part}: {exc}",
            kind="local_io",
        ) from None
    if actual_sha != expected_sha:
        _unlink_quietly(part)
        hub["partial"] = None
        state.save()
        raise ChannelError(
            f"Контрольная сумма скачанного диспетчера не сошлась с метаданными "
            f"(ожидался sha256 …{expected_sha[-8:]}, получен …{actual_sha[-8:]}) — "
            f"данные отброшены",
            kind="integrity_mismatch",
        )

    try:
        sidecar = _fetch_hub_sidecar(client, target)
        verified = signature.verify_artifact(
            part, sidecar, pubkey_raw,
            expected_name=filename, expected_sha256=expected_sha,
            expected_kind="hub")
    except ChannelError:
        hub["partial"] = _partial_record(target, filename, expected_sha, size_bytes, part)
        state.save()
        raise

    try:
        with open(part, "rb") as fh:
            head = fh.read(len(_PE_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Скачанный диспетчер {part} не читается ({exc}) — обновление не применяется.",
            kind="local_io",
        ) from None
    if head != _PE_MAGIC:
        _unlink_quietly(part)
        hub["partial"] = None
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
            f"Не удалось поместить проверенный диспетчер в стейджинг ({final}): {exc}",
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
        "sidecar": sidecar,
    }
    hub["staged"] = record
    hub["partial"] = None
    state.mark("hub", "ok", f"Диспетчер {record['version'] or 'latest'} подготовлен и "
                            f"проверен; применение — по явной команде")
    state.save()

    out = {key: value for key, value in record.items() if key != "sidecar"}
    out["resumed"] = resumed
    out["reason"] = "hub_staged"
    return out


def staged_hub_info(state) -> Optional[dict]:
    """Подготовленный диспетчер (без сайдкара), или `None`, если записи нет
    либо файл с диска исчез — симметрично `releases.staged_installer_info`."""
    record = state.hub.get("staged")
    if not isinstance(record, dict):
        return None
    path = str(record.get("path") or "")
    if not path or not Path(path).is_file():
        return None
    return {key: value for key, value in record.items() if key != "sidecar"}


def hub_status(state) -> dict:
    """Карточка канала диспетчера для `/api/companion/status`."""
    return {
        "mode": state.hub.get("mode"),
        "frozen": is_frozen_hub(),
        "current": _standkit_version,
        "known_latest": state.hub.get("known_latest"),
        "staged": staged_hub_info(state),
        "self_update_launched": state.hub.get("self_update_launched"),
    }


# ======================================================================================
# Применение — запуск помощника самообновления
# ======================================================================================
#: см. `releases.INSTALLER_ELEVATION_WINERROR` — то же значение, тот же смысл
#: (WinAPI отказала запустить процесс без повышения прав), но своя константа:
#: модули развязаны намеренно (см. докстринг модуля про `apply_self_update`).
ELEVATION_WINERROR = 740

#: Флаги CLI хаба, которыми запускается ПОМОЩНИК самообновления (тот же exe,
#: см. `standkit_hub/__main__.py::_maybe_apply_self_update`).
_APPLY_FLAG = "--apply-self-update"
_TARGET_FLAG = "--target"
_WAIT_PID_FLAG = "--wait-pid"


def apply_self_update(state, ctx) -> dict:
    """Запустить подготовленный диспетчер КАК ПОМОЩНИКА самообновления.

    Симметрично `releases.apply_installer` по духу («не подменяем — запускаем
    отдельным процессом»), но развилка иная: здесь новый бинарь запускается с
    флагом `--apply-self-update`, ждёт выхода ТЕКУЩЕГО процесса (по pid) и
    ТОЛЬКО ПОТОМ подменяет target и перезапускает его штатно — см. докстринг
    `standkit_hub/__main__.py::run_self_update_helper` за точным порядком
    (бэкап → retry на sharing violation → запуск).

    Порядок проверок — тот же fail-closed принцип, что у `apply_installer`:
    1. не FROZEN → `self_update_unsupported` (та же причина, что у `stage_hub`:
       подменить себя умеет только exe-сборка);
    2. подготовленного файла нет/пропал с диска → `nothing_staged`;
    3. подпись — ЗАНОВО, по сохранённому сайдкару (между `stage_hub` и этим
       вызовом файл в стейджинге мог быть подменён любым процессом
       пользователя — тот же довод, что у `apply_staged`/`apply_installer`);
    4. PE-заголовок MZ — тем же порядком.

    Этот вызов НЕ останавливает текущий процесс диспетчера — это делает
    вызывающий слой (`standkit_hub.server`) СРАЗУ ПОСЛЕ успешного возврата,
    тем же путём, что и `POST /api/hub/shutdown` (см. докстринг модуля).
    """
    if not is_frozen_hub():
        raise ChannelError(
            "Диспетчер запущен не из exe-сборки — самообновление недоступно, "
            "используйте команду pip",
            kind="self_update_unsupported",
        )
    hub = state.hub
    record = hub.get("staged")
    if not isinstance(record, dict) or not record.get("path"):
        raise ChannelError(
            "Подготовленного диспетчера нет — сначала выполните проверку и подготовку "
            "обновления",
            kind="nothing_staged",
        )
    src = Path(record["path"])
    if not src.is_file():
        hub["staged"] = None
        state.save()
        raise ChannelError(
            f"Подготовленный диспетчер исчез с диска ({src}) — подготовьте обновление "
            f"заново",
            kind="nothing_staged",
        )

    sidecar = record.get("sidecar")
    pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))
    verified = signature.verify_artifact(
        src, sidecar, pubkey_raw,
        expected_name=record.get("filename"), expected_sha256=record.get("sha256"),
        expected_kind="hub")

    try:
        with open(src, "rb") as fh:
            head = fh.read(len(_PE_MAGIC))
    except OSError as exc:
        raise ChannelError(
            f"Подготовленный диспетчер {src.name} не читается ({exc}) — самообновление "
            f"не запущено.",
            kind="local_io",
        ) from None
    if head != _PE_MAGIC:
        raise ChannelError(
            f"Подготовленный диспетчер {src.name} потерял PE-заголовок — самообновление "
            f"не запущено, обратитесь к издателю.",
            kind="artifact_type_mismatch",
        )

    target_exe = str(getattr(sys, "executable", "") or "")
    if not target_exe:
        raise ChannelError(
            "Не удалось определить путь текущего исполняемого файла диспетчера — "
            "самообновление не запущено.",
            kind="local_io",
        )
    current_pid = os.getpid()

    log_path = companion_workdir(ctx) / "hub_self_update.log"
    try:
        pid = spawn_hidden(
            [str(src), _APPLY_FLAG, _TARGET_FLAG, target_exe,
             _WAIT_PID_FLAG, str(current_pid)],
            cwd=src.parent, log_path=log_path)
    except ProcessError as exc:
        cause = exc.__cause__
        if getattr(cause, "winerror", None) == ELEVATION_WINERROR:
            raise ChannelError(
                "Диспетчеру нужны права администратора для самообновления (установлен "
                "«для всех пользователей»). Перезапустите диспетчер с правами "
                f"администратора и повторите, либо обновите его установщиком: {src}",
                kind="elevation_required",
            ) from None
        raise ChannelError(
            f"Не удалось запустить помощника самообновления {src}: {exc}",
            kind="local_io",
        ) from None

    launched_at = utc_now_iso()
    hub["self_update_launched"] = {
        "version": record.get("version"),
        "path": str(src),
        "pid": pid,
        "target": target_exe,
        "waited_pid": current_pid,
        "launched_at": launched_at,
        "key_id": verified.get("key_id"),
        "log": str(log_path),
    }
    state.mark("hub", "ok", f"Помощник самообновления диспетчера запущен (pid={pid}) — "
                           f"диспетчер сейчас завершится и перезапустится новой версией")
    state.save()

    return {
        "launched": True,
        "version": record.get("version"),
        "pid": pid,
        "target": target_exe,
        "waited_pid": current_pid,
        "launched_at": launched_at,
        "log": str(log_path),
        "reason": "hub_self_update_launched",
    }
