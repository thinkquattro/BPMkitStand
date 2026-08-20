# -*- coding: utf-8 -*-
"""Канал релизов MCP: проверка, подготовка (стейджинг), применение, откат.

Цена ошибки здесь выше, чем в любом другом цикле канала: сюда приезжает **исполняемый**
бинарь, который потом запустит хост MCP. Поэтому модуль устроен вокруг четырёх решений,
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
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from standkit.registry import bpmkit_config_dir

from . import fsutil, signature
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
        }
    except ChannelError as exc:
        if exc.kind == "release_not_configured":
            state.mark("releases", "skipped", exc.title())
            state.save()
            return {
                "available": False, "latest": None, "current": current,
                "signed": None, "size_bytes": None, "etag": None,
                "reason": "release_not_configured", "target": LATEST,
                "filename": None, "sha256": None,
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

    detail = _check_detail(available, reason, latest, current, signed)
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
    }


def _check_detail(available: bool, reason: str, latest: str,
                  current: Optional[str], signed: bool) -> str:
    """Человеческая строка исхода проверки для состояния и UI."""
    if reason == "version_unknown_use_latest":
        return ("Издатель не сообщил номер версии релиза — обновление доступно только по "
                "пути «latest»")
    if not available:
        return f"Установлена актуальная версия ({current or 'неизвестно'})"
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


def _resume_offset(rel: dict, part: Path, expected_sha: str, size_bytes: int) -> int:
    """Сколько байт уже лежит на диске и можно ли им доверять.

    Докачка разрешена, если частичный файл существует И (записи о нём нет ЛИБО она про этот
    же релиз). Расхождение sha означает «релиз перевыложили» — кусок удаляется, качаем с
    нуля. Кусок больше объявленного размера — тоже мусор.
    """
    existing = _size_on_disk(part)
    if existing <= 0:
        return 0
    partial = rel.get("partial") or {}
    known_sha = _norm_hex(partial.get("sha256"))
    if known_sha and known_sha != expected_sha:
        _unlink_quietly(part)
        rel["partial"] = None
        return 0
    if size_bytes and existing > size_bytes:
        _unlink_quietly(part)
        rel["partial"] = None
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


# ======================================================================================
# Применение
# ======================================================================================
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

    previous_version = _current_version(state, ctx)
    new_version = str(record.get("version") or "").strip()

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
