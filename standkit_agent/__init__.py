"""
standkit_agent — лёгкий headless-демон на хосте стенда: ядро standkit +
крошечный HTTP/RPC сервер (stdlib-only: http.server, subprocess, socket,
urllib). Кроссплатформенный (Windows/Linux). Без Qt и без сторонних
зависимостей — специально, чтобы агент можно было развернуть на "голом"
хосте стенда без сборки колёс под конкретную ОС/архитектуру.
"""

__all__: list[str] = []
