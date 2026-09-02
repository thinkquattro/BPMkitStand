# -*- coding: utf-8 -*-
"""Проверка подписей издателя: сайдкар артефакта и документ отзыва лицензий.

Почему модуль один, а ключа два. Оба формата подписаны Ed25519, но РАЗНЫМИ ключами и по
РАЗНЫМ правилам, и это не случайность:

* **артефакт** (`<файл>.exe` + `<файл>.exe.sig`) подписан ключом АРТЕФАКТОВ, который
  приезжает файлом поставки и может отсутствовать (издатель его ещё не выпустил — блокер Б1).
  Подписан не файл, а его 32-байтовый сырой sha256-дайджест: релиз весит десятки мегабайт,
  и гонять его через Ed25519 целиком незачем — sha256 уже посчитан для проверки целостности;
* **документ отзыва** (`revocations.json`) подписан ключом ЛИЦЕНЗИЙ, который вшит сюда
  константой и существует уже сегодня. Подписаны сырые байты канонического JSON, БЕЗ
  промежуточного хеша — так это делает оффлайн-подписыватель издателя.

Перепутать ключи местами нельзя: сайдкар, подписанный ключом лицензий, обязан быть
отвергнут. Поэтому ключ везде передаётся явным аргументом, а не берётся «по умолчанию из
модуля» — единственное исключение сделано для документа отзыва, где второго кандидата нет.

**Fail-closed без флага отключения.** Любой сомнительный исход — отказ: нет ключа, нет
сайдкара, сайдкар от другого файла, не сошёлся размер, не сошёлся sha256, чужой key_id,
не сошлась подпись. Обновление не применяется, старая версия не трогается. Ни один из этих
исходов не деградирует до предупреждения — цена ошибки здесь равна установке чужого кода.

**Почему у отказов разные `kind`.** `integrity_mismatch` («файл побился по дороге, повтори»)
и `artifact_signature_invalid` («файл подписан не тем, кем мы думали, повторять бесполезно»)
требуют от пользователя разных действий и по-разному отвечают на вопрос «ретраить ли на
следующем тике» (см. `errors.KIND_TITLES`). Один общий «ошибка проверки» превратил бы атаку
в мигающую сетевую ошибку.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Optional

from . import ed25519
from .errors import ChannelError
# PathLike переиспользуется из fsutil, а не объявляется заново: два одинаковых алиаса в
# одном пакете расходятся при первой же правке.
from .fsutil import PathLike, sha256_file

__all__ = [
    "SIG_FORMAT",
    "PUBLISHER_LICENSE_PUBKEY_B64",
    "RAW_KEY_LEN",
    "decode_pubkey",
    "load_pubkey_file",
    "verify_artifact",
    "verify_revocations_document",
]

#: Значение поля `format` сайдкара. Константа проверяется дословно: смена формата обязана
#: приводить к отказу «подпись не подтверждена», а не к попытке разобрать его по-старому.
SIG_FORMAT = "bpmkit-artifact-sig-v1"

#: Публичный ключ ЛИЦЕНЗИЙ издателя (стандартный base64 от 32 сырых байт). Вшит в код
#: намеренно: файл отзыва лицензий должен проверяться даже на машине, где поставка
#: повреждена или ключ артефактов отсутствует. Это НЕ ключ артефактов.
#: РОТАЦИЯ 02.09.2026 (плановая, перед первым коммерческим релизом): ключ 21.07 (h6xtHY...) выведён из
#: обращения. Эта константа -- ДВОЙНИК licensing.PUBLISHER_PUBLIC_KEY_B64 в BPMkit-dev
#: и обязана меняться ВМЕСТЕ с ней (процедура: docs/license_renewal_procedure.md §6 dev-репо).
PUBLISHER_LICENSE_PUBKEY_B64 = "kF8zBEeracMEY3AT7zN6z7mcCiJjnLuLnMZnn8ilorU="

#: Длина сырого Ed25519-ключа. В проекте везде Raw-кодирование, никаких PEM/DER.
RAW_KEY_LEN = 32

# Обязательные ключи сайдкара. Отсутствие любого — не «поле по умолчанию», а признак того,
# что перед нами не сайдкар издателя.
_SIDECAR_KEYS = ("format", "artifact", "size", "sha256", "signed_at", "key_id", "signature")

# Длина сырой подписи Ed25519.
_SIG_LEN = 64

# Сколько символов дайджеста ключа образует key_id (так его считает издатель).
_KEY_ID_LEN = 16


def _b64_raw(text: str) -> Optional[bytes]:
    """Терпимый декодер base64 → сырые байты, `None` при любой порче.

    Терпимость ровно в двух местах и обе вынужденные: издатель кодирует подпись артефакта
    СТАНДАРТНЫМ base64, а подпись документа отзыва — base64url БЕЗ padding. Поэтому
    `-`/`_` переводятся в `+`/`/`, а padding дописывается. Дальше — `validate=True`, то есть
    любой посторонний символ (комментарий, кириллица плейсхолдера) — отказ, и финальная
    защита в вызывающем: проверка ТОЧНОЙ длины результата.
    """
    if not isinstance(text, str):
        return None
    cleaned = "".join(text.split())
    if not cleaned:
        return None
    cleaned = cleaned.replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned.encode("ascii"), validate=True)
    except (ValueError, TypeError):
        # binascii.Error и UnicodeEncodeError — оба наследники ValueError.
        return None


def decode_pubkey(value: str) -> bytes:
    """Строка base64 → 32 сырых байта публичного ключа.

    Всё, что не декодируется ровно в 32 байта, трактуется как «ключа нет», а не как «ключ
    битый»: в поставке до выпуска настоящего ключа лежит плейсхолдер (пустая строка,
    комментарий, текст «ключ ещё не сгенерирован»), и пользователю надо сказать именно
    «канал релизов не настроен», а не «подпись неверна» — это разные действия с его стороны.
    """
    text = value if isinstance(value, str) else ""
    # Всё после '#' — комментарий: файл ключа человекочитаемый, издатель кладёт туда
    # пояснение, а пустой остаток означает ровно «ключ ещё не выпущен».
    text = text.split("#", 1)[0].strip()
    raw = _b64_raw(text) if text else None
    if raw is None or len(raw) != RAW_KEY_LEN:
        if not text:
            actual = "пусто"
        elif raw is None:
            actual = "строка не является base64"
        else:
            actual = f"{len(raw)} байт"
        raise ChannelError(
            f"Публичный ключ подписи артефактов не задан или повреждён: ожидалось {RAW_KEY_LEN} "
            f"сырых байт в base64, получено — {actual}",
            kind="pubkey_missing",
        )
    return raw


def load_pubkey_file(path: PathLike) -> bytes:
    """Публичный ключ из файла поставки (ровно одна строка base64).

    Отсутствие файла — такой же штатный исход, как плейсхолдер внутри него: ключ артефактов
    появляется в поставке только после того, как издатель его выпустит.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChannelError(
            f"Файл публичного ключа подписи артефактов недоступен: {target} ({exc})",
            kind="pubkey_missing",
        ) from exc
    try:
        return decode_pubkey(text)
    except ChannelError as exc:
        # Тот же kind, но с путём: без него пользователь не поймёт, какой из файлов чинить.
        raise ChannelError(f"{exc} (файл {target})", kind="pubkey_missing") from exc


def key_id_of(pubkey_raw: bytes) -> str:
    """`key_id` издателя: первые 16 символов sha256 от СЫРЫХ 32 байт ключа.

    Это не защита (ключ публичный), а различитель: он позволяет отличить «подписано другим
    ключом издателя» от «подпись не сошлась» и не гонять пользователя искать несуществующую
    порчу файла.
    """
    return hashlib.sha256(pubkey_raw).hexdigest()[:_KEY_ID_LEN]


def _norm_hex(value: object) -> str:
    """Нормализация hex-дайджеста для сравнения: регистр не значим, пробелы обрезаются."""
    return value.strip().lower() if isinstance(value, str) else ""


def _tail(digest: str, size: int = 8) -> str:
    """Хвост дайджеста для сообщения об ошибке. Полные 64 символа в тексте ошибки читать
    невозможно, а хвоста хватает, чтобы отличить два файла глазами."""
    return digest[-size:] if digest else "?"


def verify_artifact(path: PathLike, sidecar: dict, pubkey_raw: bytes, *,
                    expected_name: Optional[str] = None,
                    expected_sha256: Optional[str] = None) -> dict:
    """Полная fail-closed проверка скачанного артефакта по его сайдкару.

    Возвращает `{'key_id', 'signed_at', 'sha256', 'size'}` — то, что вызывающий кладёт в
    состояние канала и показывает в UI («установлено, подпись подтверждена, ключ …, дата …»).
    Любой отказ — `ChannelError` с точным `kind`.

    Порядок проверок не произволен: сначала то, что говорит «это вообще не про наш файл»
    (формат, имя), затем целостность (размер, sha256), и только потом криптография. Обратный
    порядок дал бы «подпись недействительна» на банально недокачанном файле — и пользователь
    пошёл бы искать взлом там, где оборвалась сеть.
    """
    target = Path(path)

    # --- 1. Это вообще сайдкар? -------------------------------------------------------
    if not isinstance(sidecar, dict):
        raise ChannelError(
            "Сайдкар подписи отсутствует или не является объектом JSON — "
            "обновление не применяется",
            kind="signature_not_available",
        )
    missing = [key for key in _SIDECAR_KEYS if key not in sidecar]
    if missing:
        raise ChannelError(
            "Сайдкар подписи неполон: нет обязательных полей " + ", ".join(missing),
            kind="signature_not_available",
        )

    # --- 2. Тот ли это формат ---------------------------------------------------------
    if sidecar["format"] != SIG_FORMAT:
        raise ChannelError(
            f"Неизвестный формат сайдкара подписи: ожидался {SIG_FORMAT!r}, "
            f"получен {sidecar['format']!r}",
            kind="signature_not_available",
        )

    # --- 3. Сайдкар точно от ЭТОГО файла ----------------------------------------------
    # Проверка важнее, чем кажется: sha256 ниже сойдётся и в том случае, если нам подсунули
    # старый (валидно подписанный!) артефакт вместе с его собственным сайдкаром — откат на
    # уязвимую версию. Имя из сайдкара — первый рубеж против такой подмены.
    if expected_name is not None and sidecar["artifact"] != expected_name:
        raise ChannelError(
            f"Сайдкар подписи выписан на другой файл: в сайдкаре {sidecar['artifact']!r}, "
            f"ожидался {expected_name!r}",
            kind="signature_not_available",
        )

    # --- 4. Размер --------------------------------------------------------------------
    try:
        actual_size = target.stat().st_size
    except OSError as exc:
        raise ChannelError(
            f"Скачанный артефакт недоступен для проверки: {target} ({exc})",
            kind="integrity_mismatch",
        ) from exc
    if actual_size != sidecar["size"]:
        raise ChannelError(
            f"Размер артефакта не сошёлся: ожидалось {sidecar['size']} байт, "
            f"фактически {actual_size} байт — данные отброшены",
            kind="integrity_mismatch",
        )

    # --- 5. sha256 файла --------------------------------------------------------------
    try:
        actual_sha = sha256_file(target)
    except OSError as exc:
        raise ChannelError(
            f"Не удалось посчитать контрольную сумму артефакта {target}: {exc}",
            kind="integrity_mismatch",
        ) from exc
    declared_sha = _norm_hex(sidecar["sha256"])
    # compare_digest, хотя оба значения публичные: так сравнение хешей выглядит одинаково
    # во всём коде проекта и не превращается в `==` при копировании в место, где это важно.
    if not hmac.compare_digest(actual_sha, declared_sha):
        raise ChannelError(
            f"Контрольная сумма артефакта не сошлась с сайдкаром: ожидался sha256 "
            f"…{_tail(declared_sha)}, фактический …{_tail(actual_sha)} — данные отброшены",
            kind="integrity_mismatch",
        )

    # --- 6. sha256, обещанный метаданными релиза --------------------------------------
    # Отдельная проверка, а не дубль предыдущей: сайдкар и метаданные релиза приезжают
    # РАЗНЫМИ ответами, и их расхождение — самостоятельный сигнал (подменён один из двух).
    if expected_sha256 is not None:
        expected_norm = _norm_hex(expected_sha256)
        if not hmac.compare_digest(actual_sha, expected_norm):
            raise ChannelError(
                f"Контрольная сумма артефакта не сошлась с метаданными релиза: ожидался "
                f"sha256 …{_tail(expected_norm)}, фактический …{_tail(actual_sha)} — "
                f"данные отброшены",
                kind="integrity_mismatch",
            )

    # --- 7. Тем ли ключом подписано ----------------------------------------------------
    raw_key = pubkey_raw if isinstance(pubkey_raw, (bytes, bytearray)) else b""
    if len(raw_key) != RAW_KEY_LEN:
        raise ChannelError(
            f"Публичный ключ подписи артефактов непригоден: ожидалось {RAW_KEY_LEN} сырых "
            f"байт, получено {len(raw_key)}",
            kind="pubkey_missing",
        )
    expected_key_id = key_id_of(bytes(raw_key))
    declared_key_id = _norm_hex(sidecar["key_id"])
    if declared_key_id != expected_key_id:
        # Именно «подписан чужим ключом», а не «подпись неверна»: подпись мы даже не
        # проверяли, и сказать про неё «недействительна» было бы неправдой. Пользователю
        # это подсказывает реальную причину — устаревший ключ в поставке или подмена.
        raise ChannelError(
            f"Артефакт подписан другим ключом: в сайдкаре key_id {declared_key_id or '?'}, "
            f"в поставке {expected_key_id} — обновление не применяется",
            kind="artifact_signature_invalid",
        )

    # --- 8. Собственно подпись ---------------------------------------------------------
    # Подписан РОВНО 32-байтовый сырой дайджест файла: ни сам файл, ни его hex-строка, ни
    # канонический JSON сайдкара. Любая «улучшенная» интерпретация здесь ломает совместимость
    # с подписывателем издателя молча — подпись просто перестанет сходиться.
    signature = _b64_raw(sidecar["signature"])
    if signature is None or len(signature) != _SIG_LEN:
        raise ChannelError(
            f"Подпись в сайдкаре не разобрана: ожидалось {_SIG_LEN} сырых байт в base64 — "
            f"обновление не применяется",
            kind="artifact_signature_invalid",
        )
    digest = bytes.fromhex(actual_sha)
    if not ed25519.verify(bytes(raw_key), signature, digest):
        raise ChannelError(
            f"Подпись артефакта {target.name} недействительна (ключ {expected_key_id}) — "
            f"обновление не применяется, установленная версия не тронута",
            kind="artifact_signature_invalid",
        )

    return {
        "key_id": expected_key_id,
        "signed_at": sidecar["signed_at"],
        "sha256": actual_sha,
        "size": actual_size,
    }


def canonical_revocations_payload(doc: dict) -> bytes:
    """Канонические байты документа отзыва — ровно то, что подписывает издатель.

    Три параметра сериализации зафиксированы дословно и НИ ОДИН не косметический:

    * `sort_keys=True` — порядок ключей в JSON не определён, без сортировки подпись
      сходилась бы через раз в зависимости от парсера;
    * `separators=(",", ":")` — любой пробел меняет байты, а значит и подпись;
    * `ensure_ascii=False` — кириллица подписывается КАК UTF-8, а не как `\\uXXXX`.
      С `ensure_ascii=True` документ без кириллицы проверялся бы, а с кириллицей — нет;
      такой баг ловится только в проде и выглядит как «у некоторых не работает отзыв».

    Поле `signature` исключается: подписать документ, содержащий собственную подпись,
    невозможно по построению.
    """
    payload = {key: value for key, value in doc.items() if key != "signature"}
    return json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")


def verify_revocations_document(doc: dict, pubkey_raw: Optional[bytes] = None) -> list[str]:
    """Проверить подпись документа отзыва лицензий и вернуть список отозванных id.

    Ключ по умолчанию — вшитый `PUBLISHER_LICENSE_PUBKEY_B64` (ключ ЛИЦЕНЗИЙ, не артефактов).

    Все отказы схлопнуты в один `kind`: с точки зрения последствий «подписи нет», «подпись
    не разобрана» и «подпись не сошлась» одинаковы — документу отзыва верить нельзя, а
    подделка документа означает СНЯТИЕ отзыва с отозванной лицензии. Молча вернуть пустой
    список здесь — худший из возможных исходов, поэтому пустого списка на ошибке не бывает.
    """
    if not isinstance(doc, dict):
        raise ChannelError(
            "Документ отзыва лицензий не является объектом JSON",
            kind="revocations_signature_invalid",
        )

    signature = _b64_raw(doc.get("signature"))
    if signature is None or len(signature) != _SIG_LEN:
        raise ChannelError(
            f"Документ отзыва лицензий не подписан или подпись не разобрана: ожидалось "
            f"{_SIG_LEN} сырых байт в base64",
            kind="revocations_signature_invalid",
        )

    key = bytes(pubkey_raw) if pubkey_raw is not None else decode_pubkey(
        PUBLISHER_LICENSE_PUBKEY_B64
    )

    try:
        payload = canonical_revocations_payload(doc)
    except (TypeError, ValueError) as exc:
        raise ChannelError(
            f"Документ отзыва лицензий не сериализуется канонически: {exc}",
            kind="revocations_signature_invalid",
        ) from exc

    # Подписаны СЫРЫЕ байты канонического JSON, без промежуточного sha256 — в отличие от
    # артефакта. Документ маленький, лишний хеш только добавил бы шаг, где можно разойтись
    # с подписывателем издателя.
    if not ed25519.verify(key, signature, payload):
        raise ChannelError(
            "Подпись документа отзыва лицензий не подтверждена ключом издателя — "
            "документ отброшен",
            kind="revocations_signature_invalid",
        )

    revoked = doc.get("revoked")
    if not isinstance(revoked, list):
        return []
    # Нестроковые элементы отбрасываются молча: подпись уже подтвердила авторство документа,
    # а сравнивать id лицензии придётся со строкой.
    return [item.strip() for item in revoked if isinstance(item, str) and item.strip()]
