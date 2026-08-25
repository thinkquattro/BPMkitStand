# -*- coding: utf-8 -*-
"""Файловые примитивы канала: атомарная запись, бэкап, подмена файла с retry.

Почему отдельный модуль. В репозитории атомарной записи не было нигде: и `HubConfig.save`,
и `Registry.save`, и pid-файл агента пишут обычным `write_text`. Для конфига это терпимо
(его правит человек, и он на глазах), для СОСТОЯНИЯ канала — нет: обрыв в середине записи
даёт битый JSON, канал сбрасывает курсор и перекачивает базу паттернов с нуля. Здесь —
минимальный набор, которого хватает каналу, без правки MIT-ядра.

Windows-специфика, ради которой это не однострочник:

* `os.replace` на Windows падает `PermissionError`, если целевой файл открыт другим
  процессом (типичный случай — MCP-сервер, запущенный хостом, держит свой `.exe`). Поэтому
  `replace_with_retry` не «пробует один раз и сдаётся», а повторяет с паузой и в конце
  честно говорит, что файл занят, — вместо того чтобы оставить установку в полусостоянии;
* временный файл создаётся **в том же каталоге**, что и целевой: `os.replace` атомарен
  только в пределах одного тома, а `%TEMP%` может лежать на другом диске.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "replace_with_retry",
    "backup_copy",
    "sha256_file",
    "probe_writable",
]

PathLike = Union[str, "os.PathLike[str]"]

# Пауза и число попыток подмены занятого файла. 10 × 0.3 с ≈ 3 с — этого хватает, чтобы
# пережить короткий скачок (антивирус читает свежескачанный .exe), и мало, чтобы не
# подвесить тик планировщика на минуты.
_REPLACE_ATTEMPTS = 10
_REPLACE_PAUSE_S = 0.3

_CHUNK = 1024 * 1024


def _tmp_sibling(target: Path, suffix: str = ".tmp") -> Path:
    return target.with_name(target.name + suffix)


def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    """Запись «всё или ничего»: временный файл рядом → `flush`+`fsync` → `os.replace`.

    `fsync` обязателен: без него `os.replace` может завершиться раньше, чем содержимое
    доедет до диска, и после аварийного выключения на месте окажется файл нулевой длины —
    то есть ровно та потеря, от которой мы защищаемся.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_sibling(target)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Текстовый вариант `atomic_write_bytes`.

    Перевод строки НЕ нормализуется: файлы канала (markdown паттернов, JSON состояния)
    пишутся с `\\n` на всех платформах — так их дифф не «взрывается» при переносе профиля
    между Windows и Linux.
    """
    atomic_write_bytes(path, text.encode(encoding))


def replace_with_retry(src: PathLike, dst: PathLike,
                       attempts: int = _REPLACE_ATTEMPTS,
                       pause: float = _REPLACE_PAUSE_S) -> None:
    """`os.replace(src, dst)` с повторами на `PermissionError`/`OSError`.

    Бросает последнюю ошибку, если файл так и остался занят — вызывающий обязан
    трактовать это как «обновление не применено, старая версия цела», а не как успех.
    """
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(pause)
    assert last is not None
    raise last


def probe_writable(path: PathLike) -> None:
    """Неразрушающая проба «можно ли сейчас подменить этот файл» (GAP-161).

    Открывает файл в режиме `r+b` (чтение+запись, БЕЗ усечения — `truncate` здесь не
    вызывается вовсе) и сразу закрывает, не изменив ни байта. Смысл именно в этом режиме:
    на Windows работающий `.exe` обычно открыт загрузчиком с долей `FILE_SHARE_READ`, без
    `FILE_SHARE_WRITE`/`FILE_SHARE_DELETE` — и тогда `open(path, "r+b")` падает тем же
    `PermissionError`, каким закончилась бы настоящая подмена (`replace_with_retry`).
    Вызывающий получает этот сигнал РАНЬШЕ, чем сделан бэкап и затронута установленная
    версия — можно честно отказать, не начиная мутацию вовсе.

    Файла нет на диске (`path` не существует) — тихий выход: пробовать нечего, и это НЕ
    признак занятости, а отдельный (штатный) случай, который решает вызывающий код.

    Это не абсолютная гарантия: между пробой и настоящей подменой файл теоретически может
    и освободиться, и занятся заново (TOCTOU) — поэтому проба ДОПОЛНЯЕТ обработку
    `OSError` из `replace_with_retry`, а не отменяет её. Бросает исходный `OSError` как
    есть — классификацию (`kind`, текст для пользователя) делает вызывающий, как и для
    ошибки самой подмены.
    """
    target = Path(path)
    if not target.is_file():
        return
    with open(target, "r+b"):
        pass


def backup_copy(path: PathLike, backup_dir: PathLike, name: str) -> Optional[Path]:
    """Копия файла в каталог бэкапов под явным именем. `None`, если исходника нет.

    Именно копия, а не переименование: до успешной подмены исходный файл обязан остаться
    на месте — иначе неудачное обновление оставит систему вообще без бинаря.
    """
    src = Path(path)
    if not src.is_file():
        return None
    dst_dir = Path(backup_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    shutil.copy2(src, dst)
    return dst


def sha256_file(path: PathLike) -> str:
    """Потоковый sha256 файла (hex, нижний регистр). Файл целиком в память не грузится —
    релизы весят десятки мегабайт."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
