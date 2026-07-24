"""
Тесты standkit.logs: tail/follow и "умный" декод байт лога (см. ``_decode_bytes``)
— строгая проверка utf-8-sig/utf-8, а если байты не валидный UTF-8 —
СКОРИНГ читаемости между cp1251 и cp866 (не "первая, что декодировалась без
ошибок" — обе однобайтовые кодировки почти всегда декодируют произвольные
байты без исключений, поэтому побеждать должна не первая по списку, а та,
чей результат реально похож на текст), чтобы кириллица в консольном логе
стенда не билась в мойибаке при не-UTF-8 кодировке дочернего .NET-процесса
(типичный случай на русской Windows).
"""

from __future__ import annotations

import threading
import time as _time

from standkit.logs import _decode_bytes, extract_current_session, follow, tail


# --- tail(): базовое поведение ---


def test_tail_missing_file_returns_empty_list(tmp_path):
    assert tail(tmp_path / "does-not-exist.log") == []


def test_tail_utf8_file(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert tail(p, 2) == ["line2", "line3"]


def test_tail_respects_n(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("\n".join(f"line{i}" for i in range(10)) + "\n", encoding="utf-8")
    assert tail(p, 3) == ["line7", "line8", "line9"]


def test_tail_n_zero_returns_empty_list(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("line1\n", encoding="utf-8")
    assert tail(p, 0) == []


# --- tail(): "умный" декод кодировок ---


def test_tail_utf8_bom_file(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes("привет\nмир\n".encode("utf-8-sig"))
    assert tail(p) == ["привет", "мир"]


def test_tail_cp1251_file_decodes_cyrillic_correctly(tmp_path):
    # Симулируем консольный вывод .NET-хоста стенда в системной cp1251
    # (типичный случай на русской Windows) — скоринг читаемости должен
    # уверенно выбрать cp1251, а не cp866 (декод теми же байтами в cp866 дал
    # бы мойибаке/псевдографику вместо связного текста).
    p = tmp_path / "stand.log"
    text = "Запуск сервера BPMSoft\nОшибка подключения к БД\n"
    p.write_bytes(text.encode("cp1251"))
    assert tail(p) == ["Запуск сервера BPMSoft", "Ошибка подключения к БД"]


def test_tail_cp866_file_decodes_cyrillic_correctly(tmp_path):
    # Симулируем консольный вывод .NET-хоста стенда в системной cp866
    # (типичный случай на русской консоли Windows/cmd.exe) — раньше
    # алгоритм "первая кодировка, что декодируется без ошибок" почти всегда
    # ошибочно "выигрывал" в пользу cp1251 (обе однобайтовые кодировки
    # декодируют произвольные байты практически без исключений), давая
    # мойибаке вместо кириллицы. Скоринг читаемости должен уверенно выбрать
    # cp866 по фактическому виду результата, а не по порядку в списке.
    p = tmp_path / "stand.log"
    text = "Штатный режим работы\nПорт занят\n"
    p.write_bytes(text.encode("cp866"))
    assert tail(p) == ["Штатный режим работы", "Порт занят"]


def test_tail_cp866_file_without_forcing_letters_still_decodes_correctly(tmp_path):
    # Дополнительная проверка скоринга без "форсирующих" cp1251-неопределённые
    # байты (вроде "Ш"/0x98) — обычный русский текст в cp866, где старый
    # алгоритм "первая без ошибок" почти гарантированно склонился бы к cp1251.
    p = tmp_path / "stand.log"
    text = "Инициализация конфигурации завершена\nСоединение с базой данных установлено\n"
    p.write_bytes(text.encode("cp866"))
    assert tail(p) == [
        "Инициализация конфигурации завершена",
        "Соединение с базой данных установлено",
    ]


def test_decode_bytes_prefers_utf8_over_single_byte_encodings():
    # Валидный UTF-8 с кириллицей должен декодироваться как UTF-8 (строгая
    # проверка идёт ДО скоринга однобайтовых кодировок), а не "случайно"
    # продекодироваться как cp1251/cp866.
    data = "привет мир".encode("utf-8")
    assert _decode_bytes(data) == "привет мир"


def test_decode_bytes_never_raises_on_arbitrary_bytes():
    # cp1251/cp866 — однобайтовые кодировки, они декодируют почти любой байт
    # 0x00-0xFF без исключения (используем errors="replace" в скоринге), так
    # что до финального errors="replace"-фолбэка UTF-8 доходит редко — но
    # контракт функции: НИКОГДА не бросать исключение и всегда вернуть str,
    # каким бы ни был байтовый мусор.
    result = _decode_bytes(b"\xff\xfe\x00\x01garbage")
    assert isinstance(result, str)


# --- follow(): построчная выдача новых строк, тот же "умный" декод ---


def test_follow_yields_new_lines_with_mixed_encodings(tmp_path):
    p = tmp_path / "stand.log"
    p.write_text("старая строка (уже была на момент старта follow)\n", encoding="utf-8")

    gen = follow(p, poll_interval=0.05)
    collected: list[str] = []

    def _consume():
        for line in gen:
            collected.append(line)
            if len(collected) >= 2:
                return

    thread = threading.Thread(target=_consume, daemon=True)
    thread.start()
    _time.sleep(0.2)  # дать генератору дойти до конца файла (follow не отдаёт историю)

    with p.open("ab") as f:
        f.write("новая строка (utf-8)\n".encode("utf-8"))
        f.write("Ошибка в cp1251\n".encode("cp1251"))

    thread.join(timeout=3.0)
    assert collected == ["новая строка (utf-8)", "Ошибка в cp1251"]


# --- extract_current_session(): обрезка лога до последней сессии стенда ---


def test_extract_current_session_multisession_returns_only_last_section():
    # Несколько блоков "=== START pid=…" — должна остаться ТОЛЬКО последняя
    # секция (от последнего маркера до конца), прошлые запуски отрезаются.
    text = (
        "=== START pid=100 ts=2026-01-01T00:00:00 ===\n"
        "Application starting\n"
        "старый запуск 1, строка A\n"
        "старый запуск 1, строка B\n"
        "=== START pid=200 ts=2026-01-01T01:00:00 ===\n"
        "Application starting\n"
        "старый запуск 2, строка A\n"
        "=== START pid=300 ts=2026-01-01T02:00:00 ===\n"
        "Application starting\n"
        "Application started\n"
        "текущая сессия, строка A\n"
        "текущая сессия, строка B"
    )
    result = extract_current_session(text)
    assert result.startswith("=== START pid=300")
    assert "старый запуск 1" not in result
    assert "старый запуск 2" not in result
    assert "текущая сессия, строка A" in result
    assert "текущая сессия, строка B" in result


def test_extract_current_session_no_start_marker_falls_back_to_last_application_starting():
    # Нет "=== START pid=" вообще (лог ведёт не сам standkit) — но есть
    # несколько "Application starting": берём от ПОСЛЕДНЕГО вхождения.
    text = (
        "Application starting\n"
        "старый запуск, строка A\n"
        "Application starting\n"
        "Application started\n"
        "текущая сессия, строка A\n"
        "текущая сессия, строка B"
    )
    result = extract_current_session(text)
    assert result.count("Application starting") == 1
    assert "старый запуск" not in result
    assert "текущая сессия, строка A" in result
    assert "текущая сессия, строка B" in result


def test_extract_current_session_no_boundaries_returns_text_as_is():
    text = "просто строка без маркеров\nещё одна строка"
    assert extract_current_session(text) == text


def test_extract_current_session_empty_text_returns_as_is():
    assert extract_current_session("") == ""


def test_extract_current_session_start_marker_wins_over_application_starting():
    # Если есть ОБА типа маркеров — приоритет за "=== START pid=" (последнее
    # вхождение), даже если "Application starting" встречается позже него.
    text = (
        "Application starting\n"
        "старый запуск без маркера START, строка A\n"
        "=== START pid=555 ts=2026-01-01T03:00:00 ===\n"
        "Application starting\n"
        "текущая сессия, строка A"
    )
    result = extract_current_session(text)
    assert result.startswith("=== START pid=555")
    assert "старый запуск без маркера START" not in result


def test_tail_max_bytes_reads_only_end_and_drops_partial_first_line(tmp_path):
    # Огромный лог (IIS/.NET) не читаем целиком: с max_bytes берём только хвост,
    # обрезанную «половинку» первой строки выбрасываем.
    from standkit.logs import tail

    p = tmp_path / "big.log"
    lines = [f"line{i:03d}" for i in range(100)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = p.stat().st_size

    assert tail(p, n=5, max_bytes=size // 2) == lines[-5:]
    assert tail(p, n=3) == lines[-3:]              # без max_bytes — как раньше
    assert tail(p, n=2, max_bytes=size * 2) == lines[-2:]  # лимит больше файла
