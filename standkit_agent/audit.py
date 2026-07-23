"""
Структурный append-only аудит-лог агента (JSON-lines) — обязателен для
DevSecOps-эксплуатации headless-агента, управляющего процессами на хосте
стенда.

Формат одной строки (JSON-объект, без вложенных переводов строк):
    {
      "ts": "2026-07-23T12:34:56.789012+00:00",   # UTC ISO-8601
      "src_ip": "10.0.0.5",
      "identity": "control",                       # control|readonly|<CN>|"-"
      "method": "POST",
      "path": "/stand/demo/restart",
      "action": "restart",
      "result": "ok",                               # ok|denied|error
      "code": 200
    }

Секреты/токены НИКОГДА не попадают в аудит-запись — только идентичность
уровня "какой скоуп/CN использован", не сам токен.

STDLIB-ONLY: используется ``logging`` с отдельным именованным логгером и
файловым хендлером (не корневой логгер — чтобы не смешиваться с чужой
конфигурацией логирования встраивающего процесса).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_AUDIT_LOG_PATH = Path.home() / ".standkit" / "audit.log"

_LOGGER_NAME = "standkit_agent.audit"


def build_audit_logger(path: Optional[Path] = None) -> logging.Logger:
    """
    Возвращает (создавая при необходимости) файловый логгер аудита.

    Идемпотентно: повторный вызов с тем же путём не плодит дублирующиеся
    хендлеры (полезно в тестах, где ``run_server``/фабрики обработчика могут
    вызываться многократно в одном процессе).
    """
    p = Path(path) if path else DEFAULT_AUDIT_LOG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    logger_name = f"{_LOGGER_NAME}.{abs(hash(str(p.resolve())))}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # не утекает в root-логгер встраивающего процесса

    already_attached = any(
        isinstance(h, logging.FileHandler) and Path(h.baseFilename) == p.resolve()
        for h in logger.handlers
    )
    if not already_attached:
        handler = logging.FileHandler(p, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def audit_event(
    logger: logging.Logger,
    *,
    src_ip: str,
    identity: str,
    method: str,
    path: str,
    action: str,
    result: str,
    code: int,
) -> None:
    """Пишет одну JSON-строку аудита. Никогда не бросает исключений наружу."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "src_ip": src_ip,
        "identity": identity or "-",
        "method": method,
        "path": path,
        "action": action,
        "result": result,
        "code": code,
    }
    try:
        logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        # Аудит-лог не должен ронять обработку запроса ни при каких
        # обстоятельствах (диск полон, права на файл и т.п.).
        pass
