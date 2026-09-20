# -*- coding: utf-8 -*-
"""Доставка кукбука пользователя (GAP-361, 17.09.2026) — УЗКИЙ отдельный поток
канала, рядом с паттернами и релизами.

**Почему отдельный модуль, а не ветка `releases.py`.** Релизный канал
одноартефактный по всей длине: ОДИН слот `state.releases["staged"]`, ОДИН адрес
`latest`, `_ensure_artifact_applicable` требует PE-заголовок `MZ` (GAP-212),
`apply_staged` останавливается на запущенном MCP (GAP-161) и требует
перезапуска Claude Desktop. Кукбук — HTML-документ: ему не нужен ни `MZ`, ни
остановка сервера, ни перезапуск, а его стейджинг не должен вытеснять
подготовленный к установке бинарь из единственного слота. Решение владельца
17.09.2026 — свой поток со своим состоянием (`state.cookbook`), инварианты
релизного канала не трогаются вовсе.

**Куда кладётся документ.** `%APPDATA%\\BPMkit\\docs\\cookbook.html`
(`bpmkit_config_dir()/docs`), а НЕ `{app}\\docs\\`. Причина фактическая:
установщик по умолчанию ставит per-user, но ключ `/ALLUSERS` (и диалог UAC,
`PrivilegesRequiredOverridesAllowed`) дают установку в `Program Files`, куда
Companion, работающий БЕЗ повышения прав, писать не может. Запись в профиль
работает при обеих раскладках. `{app}\\docs\\cookbook.html` остаётся
фолбэком ТОЛЬКО на чтение — им пользуется свежеустановленная система, до
которой канал ещё не доехал.

**Подпись обязательна (fail-closed).** Тот же ключ издателя и тот же формат
сайдкара `bpmkit-artifact-sig-v1`, что у релизов. Не сошлась — документ не
применяется, прежняя копия цела.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from standkit.registry import bpmkit_config_dir

from . import signature
from .backend import CONTENT_PREFIX
from .errors import ChannelError, NotModified
from .state import utc_now_iso

__all__ = [
    "COOKBOOK_PREFIX",
    "COOKBOOK_FILENAME",
    "cookbook_dir",
    "cookbook_path",
    "installed_version",
    "installed_copies",
    "read_version",
    "check",
    "sync",
]

#: Адреса потока. Версии в пути нет: на бэкенде лежит ровно одна текущая редакция.
COOKBOOK_PREFIX = f"{CONTENT_PREFIX}/cookbook"

COOKBOOK_FILENAME = "cookbook.html"

#: Тот же тег, что проставляет сборка в dev-репо и читает бэкенд
#: (`app/cookbook.py`). Держим ДВА регэкспа (атрибуты в обоих порядках) по той
#: же причине, что и там: документ собирается генератором, но читается здесь, и
#: привязка к точному написанию тега сделала бы канал хрупким.
_VERSION_META_RE = re.compile(
    r"""<meta\s+[^>]*name\s*=\s*["']bpmkit-cookbook-version["'][^>]*"""
    r"""content\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_VERSION_META_RE_REVERSED = re.compile(
    r"""<meta\s+[^>]*content\s*=\s*["']([^"']+)["'][^>]*"""
    r"""name\s*=\s*["']bpmkit-cookbook-version["']""",
    re.IGNORECASE,
)

_VERSION_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Сколько байт от начала файла читать в поисках `<meta>` — тег живёт в `<head>`.
_VERSION_SCAN_BYTES = 64 * 1024

#: Потолок документа. Тот же порядок, что у приёмника издателя: документ —
#: самодостаточный HTML (~200 КБ), 8 МБ — запас, но не безлимит.
#:
#: GAP-414: до 20.09.2026 константа была ОБЪЯВЛЕНА И НИ РАЗУ НЕ ИСПОЛЬЗОВАНА —
#: `sync` тянул документ любого размера. Теперь применяется ДВАЖДЫ и fail-closed:
#: по обещанному размеру из `/meta` (до сети) и по фактически прочитанному
#: (`client.download(max_bytes=...)`) — заголовку сервера доверять нельзя.
MAX_COOKBOOK_BYTES = 8 * 1024 * 1024


def cookbook_dir(config_dir: Optional[Path] = None) -> Path:
    """`%APPDATA%\\BPMkit\\docs` (`config_dir` переопределяется в тестах и при
    песочном прогоне — именно этот параметр позволяет применить документ в
    temp-каталог, не трогая боевую установку)."""
    base = Path(config_dir) if config_dir else bpmkit_config_dir()
    return Path(base) / "docs"


def cookbook_path(config_dir: Optional[Path] = None) -> Path:
    return cookbook_dir(config_dir) / COOKBOOK_FILENAME


def read_version(path: Path) -> Optional[str]:
    """Версия из `<meta>` внутри файла, либо `None` (файла нет/тега нет/значение
    не проходит алфавит). Читается ТОЛЬКО голова файла."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_VERSION_SCAN_BYTES)
    except OSError:
        return None
    text = head.decode("utf-8", errors="replace")
    for regex in (_VERSION_META_RE, _VERSION_META_RE_REVERSED):
        match = regex.search(text)
        if match:
            value = match.group(1).strip()
            if _VERSION_VALUE_RE.match(value):
                return value
    return None


#: Числовой префикс строки версии (`1.1.149-bb912a67` → `1.1.149`). Дальше `-sha8`
#: от СОДЕРЖИМОГО — он сравнению не подлежит (не порядковый).
_VERSION_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)")


def _version_order_key(version: Optional[str]):
    """Кортеж чисел для сравнения «кто новее», либо None, если версия не разбирается.

    Разбирается ТОЛЬКО числовой префикс, посегментно и ЦЕЛЫМИ числами: строковое
    сравнение здесь дало бы `1.1.9 > 1.1.149`, а сравнение по последнему числу —
    класс ошибки GAP-400 (`16.2` < `9.6`)."""
    if not version:
        return None
    match = _VERSION_PREFIX_RE.match(version.strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:  # pragma: no cover — регэксп уже гарантирует цифры
        return None


def _mtime_or_zero(path: Optional[Path]) -> float:
    try:
        return Path(path).stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def installed_copies(ctx=None, config_dir: Optional[Path] = None) -> list:
    """Все копии кукбука на машине: [{origin, path, version, mtime}] в порядке
    поиска (профиль, затем поставка). Только СУЩЕСТВУЮЩИЕ файлы.

    Отдельная функция, потому что диагностике (`self_check`, отчёт канала) нужны
    ОБЕ копии с их версиями, а не один «победивший» ответ."""
    out = []
    for origin, path in (("профиль", cookbook_path(config_dir)),
                          ("поставка", _shipped_path(ctx))):
        if path is None:
            continue
        version = read_version(path)
        if version is None and not Path(path).exists():
            continue
        out.append({"origin": origin, "path": Path(path), "version": version,
                     "mtime": _mtime_or_zero(path)})
    return out


def installed_version(ctx=None, config_dir: Optional[Path] = None) -> Optional[str]:
    """Версия САМОГО СВЕЖЕГО кукбука на машине (профиль либо поставка).

    GAP-429 (ОСТАТОК GAP-361/423). Раньше здесь безусловно побеждала копия в
    профиле: `read_version(профиль)` и, только если её нет, фолбэк на поставку.
    Дефект тот же, что чинил PR #837 в `_cookbook_report` dev-репо: после
    установки НОВОЙ поставки в `{app}\\docs` ложится свежий документ, а в профиле
    остаётся редакция, доставленная каналом раньше, — и канал считает
    «установленной» СТАРУЮ. Следствие: `check` сравнивает бэкенд со старой
    строкой, отчёт называет пользователю версию, которой у него уже нет, а
    «обновление применено» может рапортоваться при открытом старом документе.

    Теперь сравниваются ОБЕ копии, и побеждает НОВЕЙШАЯ:
      * по числовому префиксу версии ВНУТРИ файла (`<версия поставки>-<sha8>`),
        посегментно целыми числами;
      * при равном префиксе (та же поставка, другой `sha8` содержимого) — по
        времени файла, свежее берёт верх;
      * если версия не разбирается ни у одной копии — порядок прежний (профиль
        первым): это ровно тот случай, когда сравнивать нечем.

    Порядок ПОИСКА (профиль, затем поставка) не меняется — он остаётся
    тай-брейком, поэтому согласованность с ярлыком и `self_check` (GAP-361)
    сохраняется: при одинаковых версиях ответ прежний."""
    copies = installed_copies(ctx, config_dir)
    known = [c for c in copies if c["version"] is not None]
    if not known:
        return None
    if len(known) == 1:
        return known[0]["version"]

    def _key(entry):
        order = _version_order_key(entry["version"])
        # Копии без разбираемой версии не могут «победить» разбираемую.
        return (0, (), 0.0) if order is None else (1, order, entry["mtime"])

    best = known[0]
    for candidate in known[1:]:
        if _key(candidate) > _key(best):
            best = candidate
    return best["version"]


def _shipped_path(ctx) -> Optional[Path]:
    """`{app}\\docs\\cookbook.html` рядом с установленным бинарём MCP — фолбэк
    ТОЛЬКО на чтение. Канал сюда не пишет никогда (в режиме «для всех» каталог
    недоступен на запись непривилегированному процессу)."""
    binary = str(getattr(ctx, "binary_path", "") or "")
    if not binary:
        return None
    candidate = Path(binary).parent.parent / "docs" / COOKBOOK_FILENAME
    return candidate


def _fetch_meta(client) -> dict:
    payload, _headers = client.get_json(f"{COOKBOOK_PREFIX}/meta")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Метаданные кукбука пришли не объектом JSON — документ не обновляется",
            kind="bad_response",
        )
    return payload


def _fetch_sidecar(client) -> dict:
    payload, _headers = client.get_json(f"{COOKBOOK_PREFIX}/signature")
    if not isinstance(payload, dict):
        raise ChannelError(
            "Сайдкар подписи кукбука пришёл не объектом JSON — документ не обновляется",
            kind="signature_not_available",
        )
    return payload


def check(client, state, ctx, *, config_dir: Optional[Path] = None) -> dict:
    """Есть ли на бэкенде более свежий кукбук. Документ НЕ качается.

    Сравнение СТРОКОВОЕ, а не по номеру версии: строка имеет вид
    `<версия поставки>-<sha8>`, где `sha8` — от содержимого. Любое изменение
    текста меняет строку, совпадение строк означает побайтово тот же документ.
    Порядковое сравнение («больше/меньше») здесь было бы ложной точностью:
    редакция кукбука не обязана расти вместе с версией MCP.

    404 — штатный пропуск (`skipped`), как `release_not_configured` у релизов:
    издатель просто ещё не выложил документ, чинить пользователю нечего."""
    cb = state.cookbook
    current = installed_version(ctx, config_dir)

    try:
        meta = _fetch_meta(client)
    except NotModified:
        state.mark("cookbook", "ok", "Кукбук не изменился с прошлой проверки")
        state.save()
        return {"available": False, "latest": cb.get("known_latest"), "current": current,
                "signed": None, "reason": "not_modified"}
    except ChannelError as exc:
        if exc.http_status == 404:
            state.mark("cookbook", "skipped", "Издатель не выложил кукбук в канал")
            state.save()
            return {"available": False, "latest": None, "current": current,
                    "signed": None, "reason": "cookbook_not_configured"}
        raise

    latest = str(meta.get("version") or "").strip() or None
    signed = bool(meta.get("signed"))

    if latest is None:
        available, reason = False, "version_unknown"
    elif current is None:
        available, reason = True, "current_version_unknown"
    elif latest == current:
        available, reason = False, "up_to_date"
    else:
        available, reason = True, "update_available"

    cb["known_latest"] = latest
    detail = _check_detail(available, reason, latest, current, signed)
    # GAP-429: если СВЕЖАЯ копия лежит в поставке, а не в профиле, пользователь по
    # ярлыку откроет ПРОФИЛЬНУЮ (старую). Это не мешает сравнению с бэкендом, но
    # молчать об этом нельзя -- иначе «актуальная инструкция» в отчёте и документ
    # перед глазами пользователя снова расходятся (симптом GAP-361).
    stale = _stale_profile_copy(ctx, config_dir)
    if stale:
        detail += (" | [WARN] свежая копия лежит в поставке ({shipped}), а ярлык "
                    "открывает копию в профиле ({profile}) -- обновите документ в "
                    "профиле".format(**stale))
    state.mark("cookbook", "ok", detail)
    state.save()
    return {"available": available, "latest": latest, "current": current,
            "signed": signed, "reason": reason,
            "stale_profile_copy": stale,
            "sha256": str(meta.get("sha256") or "") or None,
            "size_bytes": meta.get("size_bytes")}


def _stale_profile_copy(ctx, config_dir) -> Optional[dict]:
    """{'profile': версия, 'shipped': версия}, если в ПОСТАВКЕ копия НОВЕЕ, чем в
    профиле; иначе None. Основание -- те же правила сравнения, что у
    `installed_version` (GAP-429)."""
    by_origin = {c["origin"]: c for c in installed_copies(ctx, config_dir)}
    profile, shipped = by_origin.get("профиль"), by_origin.get("поставка")
    if not profile or not shipped:
        return None
    p_key, s_key = _version_order_key(profile["version"]), _version_order_key(shipped["version"])
    if p_key is None or s_key is None or s_key <= p_key:
        return None
    return {"profile": profile["version"], "shipped": shipped["version"]}


def _check_detail(available: bool, reason: str, latest: Optional[str],
                  current: Optional[str], signed: bool) -> str:
    if reason == "version_unknown":
        return "Издатель не сообщил версию кукбука — документ не обновляется"
    if not available:
        return f"Установлена актуальная инструкция ({current or 'неизвестно'})"
    if not signed:
        return (f"Доступна инструкция {latest}, но её подпись сервером не подтверждена — "
                f"документ не будет скачан")
    return f"Доступна инструкция {latest} (установлена {current or 'неизвестно'})"


def sync(client, state, ctx, *, config_dir: Optional[Path] = None, force: bool = False) -> dict:
    """Скачать и применить кукбук, если он новее установленного.

    Одношаговая операция, БЕЗ пары `stage`/`apply_staged`, в отличие от релизов.
    Причина в природе артефакта: подмена бинаря может не удаться из-за занятого
    файла и обязана быть отделена от скачивания, чтобы человек выбрал момент.
    HTML-документ никем не держится, применяется атомарной заменой и не требует
    ни остановки MCP, ни перезапуска Claude Desktop — разделять тут нечего.

    Порядок fail-closed: проверили обещанный размер -> скачали во временный
    файл рядом с целью (с потолком по факту) -> проверили подпись -> проверили,
    что внутри есть та самая версия -> атомарно заменили. Любой отказ раньше
    последнего шага оставляет прежний документ нетронутым.

    ПОТОЛОК И ВРЕМЕННЫЙ ФАЙЛ (GAP-414). `MAX_COOKBOOK_BYTES` применяется здесь,
    а не остаётся декларацией: размер сверяется ДО скачивания (обещание `/meta`)
    и ВО ВРЕМЯ (`max_bytes` транспорта — сервер может обещать одно, а отдавать
    другое). Скачивание при этом перенесено ВНУТРЬ `try`: до фикса оно стояло
    выше блока уборки, и любой отказ транспорта оставлял `.part` в профиле
    пользователя навсегда — файл, который никто больше не удалит и не дочитает.
    """
    cb = state.cookbook
    result = check(client, state, ctx, config_dir=config_dir)
    if not result.get("available") and not force:
        cb["last_sync_at"] = utc_now_iso()
        state.save()
        return {"applied": False, "reason": result.get("reason"),
                "version": result.get("current"), "path": str(cookbook_path(config_dir))}

    if not result.get("signed"):
        raise ChannelError(
            "Подпись кукбука сервером не подтверждена — документ не скачивается "
            "(политика канала fail-closed)",
            kind="signature_not_available",
        )

    promised = result.get("size_bytes")
    try:
        promised_int = int(promised) if promised is not None else None
    except (TypeError, ValueError):
        promised_int = None
    if promised_int is not None and promised_int > MAX_COOKBOOK_BYTES:
        raise ChannelError(
            f"Кукбук обещан размером {promised_int} байт при потолке "
            f"{MAX_COOKBOOK_BYTES} — документ не скачивается, прежняя инструкция цела",
            kind="too_large",
        )

    target = cookbook_path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")

    try:
        downloaded = client.download(COOKBOOK_PREFIX, tmp,
                                     expected_size=promised,
                                     max_bytes=MAX_COOKBOOK_BYTES)
        sidecar = _fetch_sidecar(client)
        pubkey_raw = signature.decode_pubkey(getattr(ctx, "artifact_pubkey", ""))
        verified = signature.verify_artifact(
            tmp, sidecar, pubkey_raw,
            expected_name=COOKBOOK_FILENAME,
            expected_sha256=result.get("sha256"))

        # Версия ВНУТРИ скачанного обязана совпасть с обещанной в `/meta`:
        # иначе в профиль лёг бы документ, который канал считает одной
        # редакцией, а `self_check` и ярлык показывают другую.
        actual = read_version(tmp)
        if actual is None or (result.get("latest") and actual != result.get("latest")):
            raise ChannelError(
                f"В скачанном кукбуке версия '{actual or 'отсутствует'}' не совпадает с "
                f"обещанной каналом '{result.get('latest')}' — документ не применён, "
                f"прежняя инструкция цела",
                kind="artifact_type_mismatch",
            )

        tmp.replace(target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    cb["installed"] = {
        "version": result.get("latest"),
        "sha256": result.get("sha256"),
        "key_id": verified.get("key_id"),
        "signed_at": verified.get("signed_at"),
        "applied_at": utc_now_iso(),
        "path": str(target),
    }
    cb["last_sync_at"] = utc_now_iso()
    state.mark("cookbook", "ok", f"Инструкция обновлена до {result.get('latest')}")
    state.save()
    return {"applied": True, "reason": "updated", "version": result.get("latest"),
            "path": str(target), "bytes": downloaded.get("bytes") if isinstance(downloaded, dict) else None,
            "key_id": verified.get("key_id")}
