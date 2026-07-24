"""
Работа с per-stand лог-файлом: tail последних N строк и генератор "follow"
(аналог ``tail -f``) для стрим-панели GUI/агента.

Читает файл КАК БАЙТЫ и декодирует "умным" перебором кодировок (см.
``_decode_bytes``), а не строго как UTF-8: дочерний .NET-процесс стенда на
Windows нередко пишет консольный вывод в системной однобайтовой кодировке
(cp1251/cp866), а не в UTF-8 — строгий "utf-8"-декод в этом случае бил
кириллицу в "◇" вместо того, чтобы показать её как есть.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator, Optional

# Однобайтовые кандидаты для скоринга (см. ``_decode_bytes``). UTF-8/UTF-8-BOM
# сюда не входят — они проверяются отдельно, СТРОГО, до скоринга (см. ниже).
_SINGLE_BYTE_CANDIDATES = ("cp1251", "cp866")

# --- скоринг "читаемости" декодированного текста ---
#
# cp1251 и cp866 — обе однобайтовые кодировки: почти ЛЮБАЯ байтовая
# последовательность декодируется ими БЕЗ ОШИБОК (за редкими исключениями
# вроде байта 0x98 в cp1251), поэтому раньше "первая, что декодировалась без
# ошибок" почти всегда отдавала предпочтение cp1251 (она была в списке
# кандидатов раньше cp866) — даже когда файл был на самом деле в cp866, что
# давало мойибаке на кириллице. Вместо "первая успешная" — декодируем ОБЕИМИ
# кандидатными кодировками (``errors="replace"``, чтобы не падать) и выбираем
# ту, чей результат "читаемее" по эвристической оценке:
#   + печатные ASCII, \r\n\t, кириллица (U+0400-04FF), типичная пунктуация
#     (кавычки-ёлочки, тире, обычные знаки препинания) — увеличивают оценку;
#   - символ-заменитель (U+FFFD, недекодируемый байт), control-символы C1
#     (U+0080-009F), псевдографика box-drawing (U+2500-257F) и прочие редкие
#     latin-1-символы (U+00A0-00FF вне кириллицы) — уменьшают оценку (это
#     типичный "мусор", который получается при декоде байт НЕ той однобайтовой
#     кодировкой).
_GOOD_PUNCTUATION = set("«»—-.,!?;:()[]{}'\"/\\%$#@&*+=_~`")


def _readability_score(text: str) -> int:
    """Эвристическая оценка "читаемости" текста — см. пояснение выше модуля."""
    score = 0
    for ch in text:
        cp = ord(ch)
        if ch in "\r\n\t":
            score += 1
        elif 0x20 <= cp <= 0x7E:
            score += 1
        elif 0x0400 <= cp <= 0x04FF:
            score += 2
        elif ch in _GOOD_PUNCTUATION:
            score += 1
        elif ch == "�":
            score -= 6
        elif 0x80 <= cp <= 0x9F:
            score -= 6
        elif 0x2500 <= cp <= 0x257F:
            score -= 5
        elif 0xA0 <= cp <= 0xFF:
            score -= 2
        else:
            score -= 1
    return score


def _decode_single_byte_best(data: bytes) -> str:
    """
    Декодирует байты каждой однобайтовой кандидатной кодировкой (см.
    ``_SINGLE_BYTE_CANDIDATES``, ``errors="replace"`` — эти кодировки
    практически никогда не бросают исключение сами по себе) и возвращает
    результат кодировки с НАИБОЛЬШЕЙ оценкой читаемости (``_readability_score``).
    При равной оценке побеждает первый кандидат по порядку списка (детерминизм).
    """
    best_text: Optional[str] = None
    best_score: Optional[int] = None
    for encoding in _SINGLE_BYTE_CANDIDATES:
        decoded = data.decode(encoding, errors="replace")
        sc = _readability_score(decoded)
        if best_score is None or sc > best_score:
            best_text, best_score = decoded, sc
    assert best_text is not None  # список кандидатов непуст
    return best_text


def _decode_bytes(data: bytes) -> str:
    """
    Декодирует байты лога:

    (a) если байты — валидный UTF-8/UTF-8 с BOM (``errors="strict"``) —
        возвращает его как есть (UTF-8 в приоритете, если он строго валиден:
        однобайтовые кодировки почти никогда не бракуют произвольные байты,
        поэтому им нельзя доверять раньше строгой UTF-8-проверки);
    (b) иначе — скоринг читаемости между cp1251 и cp866 (см.
        ``_decode_single_byte_best``), кодировка с большей оценкой побеждает;
    (c) если это тоже не помогло (не должно происходить — (b) всегда
        возвращает строку) — фолбэк на UTF-8 с ``errors="replace"``, чтобы
        никогда не падать на бинарном мусоре.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    try:
        return _decode_single_byte_best(data)
    except Exception:
        return data.decode("utf-8", errors="replace")


def tail(log_path: Path, n: int = 100, *, max_bytes: Optional[int] = None) -> list[str]:
    """
    Возвращает последние ``n`` строк файла лога (без завершающих переводов строк).

    Если файла ещё нет (стенд ни разу не запускался) — возвращает пустой список,
    а не бросает исключение: отсутствие лога — обычное состояние свежей записи
    реестра.

    ``max_bytes`` (опц.) — для ОЧЕНЬ больших логов (IIS/.NET-хосты могут писать
    сотни МБ в день) читать не весь файл, а только последние ``max_bytes`` байт
    (с конца, через seek). Первая строка такого хвоста почти наверняка обрезана
    посередине — она отбрасывается. Без ``max_bytes`` — прежнее поведение (файл
    целиком).
    """
    p = Path(log_path)
    if not p.exists():
        return []
    if max_bytes is not None and max_bytes > 0:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if size > max_bytes:
            with p.open("rb") as f:
                f.seek(size - max_bytes)
                data = f.read()
            text = _decode_bytes(data)
            lines = text.splitlines()
            if lines:
                lines = lines[1:]  # обрезанная «половинка» первой строки
            return lines[-n:] if n > 0 else lines
    data = p.read_bytes()
    text = _decode_bytes(data)
    lines = text.splitlines()
    return lines[-n:] if n > 0 else []


_SESSION_START_MARKER = "=== START pid="
_APP_STARTING_MARKER = "Application starting"


def extract_current_session(text: str) -> str:
    """
    Обрезает текст лога до ТЕКУЩЕЙ (последней) сессии стенда — панель
    "Текущее состояние" в хабе не должна показывать хвост со ВСЕМИ прошлыми
    запусками (десятки блоков "=== START pid=…"/"Application starting…"), а
    только вывод с начала последнего запуска.

    Границы сессии ищутся с КОНЦА текста (побеждает последнее вхождение), в
    порядке приоритета:
      1. строка, содержащая ``"=== START pid="`` — маркер, который standkit
         пишет в лог при старте процесса стенда (формат
         ``=== START pid=NNNN ts=<ISO> ===``);
      2. если такой строки нет — строка, содержащая ``"Application starting"``
         (типичная первая строка вывода .NET-хоста стенда при холодном
         старте — используется как fallback для логов, которые ведёт не сам
         standkit, а сам стенд/сторонний раннер, без маркера ``START pid=``).

    Если ни одна граница не найдена — возвращает текст без изменений (лог
    короткий/не содержит распознаваемых маркеров, обрезать нечего).

    Найденная граничная строка ВКЛЮЧАЕТСЯ в результат (сессия показывается
    с "=== START pid=…" или "Application starting…" включительно).
    """
    if not text:
        return text

    lines = text.split("\n")

    start_idx: Optional[int] = None
    for i in range(len(lines) - 1, -1, -1):
        if _SESSION_START_MARKER in lines[i]:
            start_idx = i
            break

    if start_idx is None:
        for i in range(len(lines) - 1, -1, -1):
            if _APP_STARTING_MARKER in lines[i]:
                start_idx = i
                break

    if start_idx is None:
        return text

    return "\n".join(lines[start_idx:])


def follow(log_path: Path, *, poll_interval: float = 0.5) -> Iterator[str]:
    """
    Генератор, отдающий новые строки лога по мере их появления (аналог ``tail -f``).

    Блокирующий: между появлениями новых строк "спит" ``poll_interval`` секунд.
    Предназначен для использования в отдельном потоке/соединении (например,
    long-poll или SSE-эндпоинт агента) — вызывающая сторона сама решает, когда
    остановить итерацию (просто перестать тянуть значения из генератора).

    Читает файл в байтовом режиме и декодирует каждую завершённую строку через
    ``_decode_bytes`` — тот же "умный" перебор кодировок, что и в ``tail``,
    построчно (буферизуя неполный "хвост" до следующего перевода строки),
    чтобы не резать многобайтовые последовательности пополам между двумя
    опросами.

    Обработка ротации лог-файла (при пересоздании файла с тем же именем позиция
    чтения может "уехать" за пределы нового файла) — бэклог, см. docs/ARCHITECTURE.md.
    """
    p = Path(log_path)
    # Ждём появления файла, если стенд ещё не успел его создать.
    while not p.exists():
        time.sleep(poll_interval)

    with p.open("rb") as f:
        f.seek(0, 2)  # сразу к концу файла — follow не показывает историю
        buffer = b""
        while True:
            chunk = f.read()
            if chunk:
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    yield _decode_bytes(raw_line).rstrip("\r")
            else:
                time.sleep(poll_interval)
