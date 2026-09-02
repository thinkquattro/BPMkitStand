# -*- coding: utf-8 -*-
"""Классификация исходов канала — единственный словарь причин на весь модуль.

Зачем отдельный модуль. Худший исход этой задачи — «что-то не качается» без причины.
Поэтому КАЖДЫЙ отказ канала обязан приехать сюда типизированным: с машинным `kind`,
человеческим русским текстом и явным ответом на два вопроса — «повторять ли на следующем
тике» и «показывать ли это пользователю». Свободных строк-ошибок в модуле нет.

Таблица различения (ТЗ §1.4). Три пары исходов внешне похожи и их СИСТЕМАТИЧЕСКИ путают —
здесь они разведены явно:

* `403 feature_disabled` — у издателя выключен ПРИЁМ (это про отправку кандидатов, не про
  наш канал). Называть это проблемой лицензии нельзя: ровно эта ошибка уже была допущена в
  `feedback.py` клиентского MCP;
* `404 release not configured` — владелец ещё не выложил релиз. Штатный молчаливый пропуск
  тика, НЕ ошибка;
* `404 signature not available` — подписи нет ЛИБО она не от этого файла. Это **fail-closed**:
  обновление не применяется, причина пишется в лог. Внешне от предыдущего отличается только
  строкой `detail`, поэтому она и разбирается дословно.

Форма тела 401 у бэкенда — ВЛОЖЕННАЯ: `{"detail": {"error_code": ..., "detail": ...}}`,
а не `{"detail": "revoked"}`. Разбор — `kind_from_payload`.
"""
from __future__ import annotations

from typing import Optional

__all__ = [
    "CompanionError",
    "ChannelError",
    "ContextUnavailable",
    "NotModified",
    "KIND_TITLES",
    "kind_from_payload",
    "kind_from_detail",
]


# --------------------------------------------------------------------------------------
# Человеческие названия исходов. Ключ — машинный `kind`, значение — (текст, повторять ли,
# показывать ли пользователю). «Показывать» = поднимать в UI хаба как проблему; остальное
# видно только в подробном состоянии.
# --------------------------------------------------------------------------------------
KIND_TITLES = {
    # --- лицензия -------------------------------------------------------------------
    "invalid_envelope": ("Конверт лицензии не распознан", False, True),
    "signature_invalid": ("Подпись конверта лицензии не подтверждена", False, True),
    "not_yet_valid": ("Лицензия ещё не вступила в силу", False, True),
    "expired": ("Срок действия лицензии истёк", False, True),
    "revoked": ("Лицензия отозвана", False, True),
    "no_license": ("Лицензионный ключ не найден на этой машине", False, True),
    # --- сервер ---------------------------------------------------------------------
    "server_misconfigured": ("Бэкенд издателя не настроен (нет публичного ключа)", True, True),
    "feature_disabled": ("У издателя выключен приём (к получению обновлений не относится)",
                         True, False),
    # 403 роутер контента не отдаёт вовсе — код оставлен, потому что ровно эту ошибку уже
    # принимали за проблему лицензии в клиентском `feedback.py`, и различать её обязаны обе
    # стороны, даже если сегодня её присылает только эндпоинт приёма кандидатов.
    # --- контент --------------------------------------------------------------------
    "release_not_configured": ("Релиз ещё не выложен издателем", True, False),
    "signature_not_available": ("Подпись релиза не подтверждена — обновление не применяется",
                                True, True),
    "revocations_not_configured": ("Файл отзыва лицензий не выложен", True, False),
    "revocations_signature_invalid": ("Подпись файла отзыва не подтверждена", False, True),
    "integrity_mismatch": ("Контрольная сумма не сошлась — данные отброшены", True, True),
    "artifact_signature_invalid": ("Подпись бинаря недействительна — обновление не применяется",
                                   False, True),
    "pubkey_missing": ("Публичный ключ подписи артефактов отсутствует в поставке",
                       False, True),
    # GAP-212: подпись доказывает ПОДЛИННОСТЬ файла, но не его ПРИМЕНИМОСТЬ. Издательский
    # конвейер до 02.09.2026 публиковал `.mcpb` (бандл), тогда как apply_staged кладёт
    # скачанное на место бинаря MCP: подпись бандла была бы совершенно честной, и все
    # проверки канала прошли бы, а на месте `bpmkit.exe` оказался бы ZIP-архив. Отдельный
    # kind (не `local_io` и не `artifact_signature_invalid`) -- чтобы UI не путал «издатель
    # выложил не тот артефакт» с файловой ошибкой или подделкой. Не retriable: следующий тик
    # скачает ровно тот же файл; чинится только на стороне издателя.
    "artifact_type_mismatch": ("Издатель выложил артефакт не того типа — обновление не "
                               "применяется", False, True),
    # --- транспорт ------------------------------------------------------------------
    # Адрес издателя не по `https` (и это не локальный бэкенд) — запрос НЕ уходил.
    # Не retriable: следующий тик пойдёт по тому же адресу и упрётся в то же самое,
    # чинится только человеком в настройках. Видимо пользователю: молча выключенный
    # канал обновлений — ровно то состояние, из-за которого их и не замечают.
    "insecure_transport": ("Адрес издателя не защищён TLS — канал остановлен",
                           False, True),
    "offline": ("Бэкенд издателя недоступен", True, False),
    "range_invalid": ("Сервер отверг докачку — состояние сброшено", True, False),
    "bad_response": ("Ответ бэкенда не разобран", True, False),
    # 304 приходит из urllib исключением, а не значением, и поднимается наружу как
    # NotModified. В UI как ошибка НЕ показывается никогда — строка нужна лишь чтобы
    # `to_dict()` не подписал штатный исход «Неизвестной ошибкой канала».
    "not_modified": ("У вас уже актуальная версия", False, False),
    "http_error": ("Бэкенд ответил ошибкой", True, False),
    # --- локальные -------------------------------------------------------------------
    "context_unavailable": ("Лицензионный контекст недоступен: рядом нет CLI BPMkit",
                            True, True),
    "disabled": ("Цикл выключен в настройках", False, False),
    "blocked_by_policy": ("Заблокировано политикой безопасности", False, True),
    "local_io": ("Локальная ошибка файловой системы", True, True),
    "mcp_running": ("Обнаружен запущенный MCP-сервер BPMkit — закройте Claude Desktop",
                    True, True),
    "nothing_staged": ("Подготовленного обновления нет", False, False),
    "nothing_to_rollback": ("Откатываться не на что: предыдущая версия не сохранена",
                            False, True),
    "unknown": ("Неизвестная ошибка канала", True, True),
}

# Дословные `error_code` бэкенда (app/auth.py) — единственные, что он присылает в 401/500.
_BACKEND_ERROR_CODES = frozenset({
    "invalid_envelope", "signature_invalid", "not_yet_valid",
    "expired", "revoked", "server_misconfigured", "feature_disabled",
})

# Дословные строки `detail`, по которым РАЗЛИЧАЮТСЯ два 404 (см. докстринг модуля).
_DETAIL_KINDS = {
    "release not configured": "release_not_configured",
    "signature not available": "signature_not_available",
    "revocations not configured": "revocations_not_configured",
}


class CompanionError(Exception):
    """База всех отказов канала. Ловится одним `except` на границе тика."""

    kind = "unknown"

    def __init__(self, message: str = "", *, kind: Optional[str] = None,
                 http_status: Optional[int] = None, detail: str = "") -> None:
        if kind:
            self.kind = kind
        self.http_status = http_status
        self.detail = detail or ""
        super().__init__(message or self.title())

    # -- представление -----------------------------------------------------------------
    def title(self) -> str:
        return KIND_TITLES.get(self.kind, KIND_TITLES["unknown"])[0]

    @property
    def retriable(self) -> bool:
        """Повторять ли на следующем тике. False — повтор бессмысленен без действий
        человека (битая/отозванная лицензия, недействительная подпись)."""
        return KIND_TITLES.get(self.kind, KIND_TITLES["unknown"])[1]

    @property
    def user_visible(self) -> bool:
        """Поднимать ли в UI как проблему. False — штатное состояние (релиз не выложен,
        офлайн), будить пользователя нечем."""
        return KIND_TITLES.get(self.kind, KIND_TITLES["unknown"])[2]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title(),
            "message": str(self),
            "detail": self.detail,
            "http_status": self.http_status,
            "retriable": self.retriable,
            "user_visible": self.user_visible,
        }


class ChannelError(CompanionError):
    """Отказ на стороне бэкенда или транспорта."""


class ContextUnavailable(CompanionError):
    """Не удалось получить лицензионный контекст у CLI MCP.

    Это НЕ «нет лицензии»: лицензия может быть в порядке, а рядом просто нет установленного
    BPMkit (или путь к нему не задан в настройках). Разводится отдельным kind, чтобы
    пользователь чинил то, что сломано на самом деле.
    """

    kind = "context_unavailable"


class NotModified(CompanionError):
    """`304 Not Modified` — у нас уже актуальная версия.

    Исключение, а не возврат, потому что `urllib` и сам бросает `HTTPError` на 304: ловить
    его как успех в каждом вызывающем — источник ошибок. Вызывающий обязан трактовать это
    как «ничего делать не надо», НЕ как проблему.
    """

    kind = "not_modified"


def kind_from_payload(payload, fallback: str = "http_error") -> str:
    """Машинный `kind` из тела ответа бэкенда.

    Бэкенд отдаёт ВЛОЖЕННУЮ форму `{"detail": {"error_code": ..., "detail": ...}}` для
    401/500 и плоскую `{"detail": "release not configured"}` для 404. Разбираются обе;
    неизвестный `error_code` не подменяется молча — возвращается `fallback`, чтобы
    расхождение контрактов было видно, а не выглядело как знакомая ошибка.
    """
    if not isinstance(payload, dict):
        return fallback
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = detail.get("error_code")
        if isinstance(code, str) and code in _BACKEND_ERROR_CODES:
            return code
        return fallback
    if isinstance(detail, str):
        return kind_from_detail(detail, fallback)
    return fallback


def kind_from_detail(detail: str, fallback: str = "http_error") -> str:
    """Разбор ПЛОСКОЙ строки `detail` (404-семейство). Сравнение — по нормализованной
    строке целиком, не по вхождению подстроки: `signature not available` и `release not
    configured` обязаны разойтись однозначно."""
    key = (detail or "").strip().lower()
    return _DETAIL_KINDS.get(key, fallback)
