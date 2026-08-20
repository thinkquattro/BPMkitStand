# -*- coding: utf-8 -*-
"""Список отозванных лицензий: скачать, проверить подпись, положить туда, где его читает MCP.

Зачем этот цикл вообще нужен и почему он устроен НЕ как остальные.

**1. Эндпоинт публичный, и запрос уходит БЕЗ конверта.** Это не упрощение и не забытая
авторизация. Отозванная лицензия по определению не проходит аутентификацию: основной канал
на ней отвечает `401 revoked`. Если бы файл отзыва запрашивался авторизованно, клиент с
отозванной лицензией — то есть единственный, кому этот файл адресован, — никогда бы его не
получил. Поэтому `refresh` не требует `ctx.envelope` и ходит с `authorized=False`, а
`401` в основном канале НЕ является причиной пропустить этот тик.

**2. Подпись проверяется всегда и без исключений.** Документ подписан оффлайн ключом
ЛИЦЕНЗИЙ издателя (он вшит в `signature.PUBLISHER_LICENSE_PUBKEY_B64` и существует уже
сегодня). Подделка этого файла означает не «показать лишнюю ошибку», а СНЯТИЕ отзыва с
отозванной лицензии — то есть ровно то, ради чего его и стали бы подделывать. Не сошлась
подпись → на диск не пишется ничего, наружу уходит `revocations_signature_invalid`.

**3. На диск кладутся БАЙТ В БАЙТ те данные, что прислал сервер.** Не «эквивалентный JSON»,
не `json.dumps(json.loads(...))`. Клиентский MCP проверяет подпись сам, по канонической
сериализации (`signature.canonical_revocations_payload`), и любая пере-сериализация —
другой порядок ключей, другие отступы, `ensure_ascii` — меняет байты, а значит ломает
подпись. Ошибка этого рода не воспроизводится на английских данных и вылезает только у
клиента с кириллицей в поле причины отзыва.

**4. Файл пишется в `ctx.revocations_target`** — пользовательский путь, который выдаёт сам
MCP. Не в `package_root`: поставка лежит в `Program Files`, запись туда требует прав
администратора, а Companion обязан работать без них.

**5. `404 revocations not configured` — штатное состояние, а не ошибка.** Сегодня файла у
издателя может не быть вовсе; тик тихо помечается `skipped`. Будить пользователя тут
нечем — отзыв никого не касается ровно до тех пор, пока издатель кого-нибудь не отзовёт.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import fsutil, signature
from .backend import CONTENT_PREFIX
from .errors import ChannelError, NotModified

__all__ = ["REVOCATIONS_PATH", "refresh", "is_license_revoked"]

#: Публичный эндпоинт списка отзыва. Без параметров и без конверта.
REVOCATIONS_PATH = f"{CONTENT_PREFIX}/revocations.json"

#: Разумный потолок размера документа. Список отзыва — это десятки идентификаторов; всё,
#: что на порядки больше, — либо не тот файл, либо страница-заглушка прокси, и разбирать
#: её как JSON незачем.
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


def refresh(client, state, ctx) -> dict:
    """Обновить локальную копию списка отзыва.

    Возвращает `{"changed", "revoked_count", "etag", "reason"}` (плюс `path` и
    `env_registered` — они нужны UI, чтобы объяснить, почему записанный файл может никем
    не читаться).

    Значения `reason`:

    * `updated` — файл скачан, подпись подтверждена, содержимое на диске обновлено;
    * `unchanged` — то же самое, но байты совпали с уже лежащими (сервер не поддержал ETag);
    * `not_modified` — сервер ответил `304`, файл не тронут вовсе;
    * `not_configured` — `404`, у издателя списка нет. Штатный пропуск, статус `skipped`.

    Отказы, кроме `404`, поднимаются наверх: записью `status=error` занимается тик
    планировщика, у которого объект ошибки со всеми полями уже в руках. Здесь помечаются
    только успех и штатный пропуск.
    """
    rev = state.revocations
    known_etag = rev.get("etag")
    previous_ids = [str(item) for item in (rev.get("revoked_ids") or [])]
    target_path = str(getattr(ctx, "revocations_target", "") or "").strip()
    env_registered = bool(getattr(ctx, "revocations_env_registered", False))

    try:
        # authorized=False — принципиально: см. п.1 докстринга модуля. Конверт здесь не
        # нужен и не запрашивается, поэтому цикл работает и с отозванной лицензией.
        response = client.request(REVOCATIONS_PATH, authorized=False, etag=known_etag)
    except NotModified:
        state.mark("revocations", "ok", "Список отзыва не изменился с прошлой проверки")
        state.save()
        return _result(False, previous_ids, known_etag, "not_modified",
                       target_path, env_registered)
    except ChannelError as exc:
        if exc.kind == "revocations_not_configured":
            state.mark("revocations", "skipped", exc.title())
            state.save()
            return _result(False, previous_ids, known_etag, "not_configured",
                           target_path, env_registered)
        raise

    body = bytes(getattr(response, "body", b"") or b"")
    headers = getattr(response, "headers", None) or {}
    if not body:
        raise ChannelError(
            "Бэкенд отдал пустое тело вместо списка отзыва лицензий — "
            "локальная копия не тронута",
            kind="bad_response",
        )
    if len(body) > _MAX_DOCUMENT_BYTES:
        raise ChannelError(
            f"Список отзыва подозрительно велик ({len(body)} байт) — документ отброшен",
            kind="bad_response",
        )

    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChannelError(
            "Список отзыва лицензий не разобран как JSON — локальная копия не тронута",
            kind="bad_response",
            detail=str(exc)[:300],
        ) from None
    if not isinstance(document, dict):
        raise ChannelError(
            "Список отзыва лицензий пришёл не объектом JSON — локальная копия не тронута",
            kind="bad_response",
            detail=f"тип тела: {type(document).__name__}",
        )

    # Проверка ДО записи на диск: подделанный документ не должен оказаться на диске даже
    # на мгновение — MCP может прочитать его между записью и откатом.
    revoked_ids = signature.verify_revocations_document(document)

    if not target_path:
        raise ChannelError(
            "MCP не сообщил, куда класть список отзыва лицензий (revocations_target пуст) — "
            "документ проверен, но не сохранён",
            kind="local_io",
        )

    path = Path(target_path)
    try:
        existing: Optional[bytes] = path.read_bytes()
    except OSError:
        existing = None

    if existing != body:
        try:
            # Именно `body`, а не пере-сериализованный `document`: подпись считается по
            # каноническим байтам, и любое переписывание её ломает (п.3 докстринга модуля).
            fsutil.atomic_write_bytes(path, body)
        except OSError as exc:
            raise ChannelError(
                f"Не удалось сохранить список отзыва лицензий в {path}: {exc}",
                kind="local_io",
            ) from None

    changed = (existing != body) or (revoked_ids != previous_ids)
    etag = headers.get("etag") if isinstance(headers, dict) else None

    rev["revoked_ids"] = list(revoked_ids)
    rev["etag"] = etag
    state.mark("revocations", "ok", _detail(len(revoked_ids), changed, env_registered))
    state.save()

    return _result(changed, revoked_ids, etag, "updated" if changed else "unchanged",
                   target_path, env_registered)


def is_license_revoked(state, license_id) -> bool:
    """Отозвана ли лицензия по локальной копии списка.

    Сравнение нечувствительно к регистру и обрамляющим пробелам НАМЕРЕННО: цена ложного
    «не отозвана» (работающая отозванная лицензия) выше цены ложного «отозвана», а
    идентификатор проходит через конфиги и копипасту, где регистр не сохраняется.

    Пустое состояние (список ещё ни разу не скачан) даёт `False`: канал не имеет права
    объявлять лицензию отозванной на основании отсутствия данных.
    """
    needle = str(license_id or "").strip().casefold()
    if not needle:
        return False
    for item in (state.revocations.get("revoked_ids") or []):
        if isinstance(item, str) and item.strip().casefold() == needle:
            return True
    return False


def _result(changed: bool, ids, etag, reason: str,
            path: str, env_registered: bool) -> dict:
    return {
        "changed": bool(changed),
        "revoked_count": len(list(ids or [])),
        "etag": etag,
        "reason": reason,
        "path": path or None,
        # Если MCP не зарегистрировал переменную окружения на этот файл, он его не читает —
        # цикл работает вхолостую. Молчать об этом нельзя: снаружи всё выглядит исправным.
        "env_registered": bool(env_registered),
    }


def _detail(count: int, changed: bool, env_registered: bool) -> str:
    head = (f"Список отзыва обновлён: отозванных лицензий — {count}" if changed
            else f"Список отзыва без изменений: отозванных лицензий — {count}")
    if not env_registered:
        head += ("; MCP не зарегистрировал путь к этому файлу — переустановите или "
                 "перезапустите MCP, иначе список не применяется")
    return head
