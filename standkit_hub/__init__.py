"""
standkit_hub — локальный веб-дашборд диспетчера стендов standkit (вариант A).

Отдаёт статический фронтенд (vanilla JS/CSS, без CDN и сборки) и JSON API
(``/api/*``) поверх stdlib ``http.server``. Опциональная десктопная оболочка —
через ``pywebview`` (extra ``standkit[desktop]``), см. ``standkit_hub.__main__``.

Заменяет собой прежний Qt-слой ``standkit_gui`` (удалён) — та же роль
("диспетчер стендов, который можно поставить на рабочий стол"), но без
PySide6: браузер универсален и не тянет тяжёлую GUI-зависимость.
"""

from __future__ import annotations
