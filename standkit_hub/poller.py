"""
Фоновый опрос стендов и кэш снапшота состояния для веб-дашборда.

ЗАЧЕМ. Раньше хаб был тонким прокси: КАЖДЫЙ ``GET /api/stands`` синхронно
ходил по всем стендам (health-пробы, HTTP к агентам, appcmd/docker/kubectl) и
только потом отвечал браузеру. Открытие страницы стоило «сумма таймаутов всех
недоступных стендов» — на контуре с тремя стендами за firewall это десятки
секунд серого экрана.

Теперь хаб — маленький демон: отдельный поток опрашивает стенды с периодом
``refresh_interval_sec`` и кладёт результат в память, а HTTP-запрос отдаёт
готовый снапшот с отметкой времени. Открытие страницы мгновенно ВСЕГДА,
данные устарели максимум на один тик — и это честно видно в ответе
(``generated_at``/``age_sec``).

Поток — daemon (не держит процесс при выходе) и корректно останавливается из
``server_close()`` хаба (см. standkit_hub.server.HubHTTPServer).

STDLIB-ONLY: ``threading``, ``time``, ``dataclasses``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# Нижняя граница периода опроса. Пользователь может выставить
# ``refresh_interval_sec = 1`` (или 0 руками в конфиге) — без этого пола
# фоновый поток превратился бы в busy-loop, дёргающий appcmd/docker в цикле.
MIN_POLL_INTERVAL_SEC = 2.0

# Верхняя граница ожидания в одном шаге цикла. Нужна только для того, чтобы
# поток регулярно перечитывал интервал из конфига (его могли изменить в
# настройках) и замечал флаг остановки, даже если период задан огромным.
MAX_POLL_STEP_SEC = 5.0


@dataclass
class StatusSnapshot:
    """
    Готовый к отдаче снапшот состояния всех стендов.

    ``probed`` — были ли реально выполнены пробы. ``False`` означает «это
    моментальный слепок реестра без единой сетевой пробы» (первый заход, пока
    фоновый опрос ещё не завершил первый круг, либо явный ``?probe=0``) —
    честный признак того, что состояния в нём заглушечные, а не измеренные.
    """

    stands: list[dict] = field(default_factory=list)
    default: str = ""
    probed: bool = False
    generated_at: float = 0.0
    error: Optional[str] = None
    # Отпечаток источников (конфиг + реестр) на момент сборки. Нужен, чтобы
    # отличить «снапшот просто немного устарел» от «состав стендов изменился,
    # и отдавать этот снапшот уже нельзя»: реестр правят и мимо хаба —
    # руками, из MCP BPMkit, при регистрации стенда. Без этой проверки после
    # добавления стенда пользователь до следующего тика видел старый список.
    sources: tuple = ()

    def to_payload(self, *, now: Optional[float] = None) -> dict:
        """JSON-тело ответа ``GET /api/stands`` из снапшота."""
        moment = time.time() if now is None else now
        payload = {
            "stands": self.stands,
            "default": self.default,
            "probed": self.probed,
            "generated_at": self.generated_at,
            "age_sec": max(0.0, round(moment - self.generated_at, 3)) if self.generated_at else None,
        }
        if self.error:
            payload["error"] = self.error
        return payload


class StatusPoller:
    """
    Фоновый поток опроса + кэш последнего снапшота.

    Зависимости передаются callable'ами, а не импортом веб-слоя: модуль не
    знает ни про ``http.server``, ни про реестр — его можно тестировать в
    изоляции, подсунув функции-заглушки.

    :param build: собирает свежий снапшот (может быть медленным — вызывается
        только из фонового потока либо из явного ``refresh_now``).
    :param interval: возвращает желаемый период опроса в секундах (читается
        ПЕРЕД каждым ожиданием — изменение ``refresh_interval_sec`` в
        настройках подхватывается без перезапуска хаба).
    """

    def __init__(self, build: Callable[[], StatusSnapshot], interval: Callable[[], float]):
        self._build = build
        self._interval = interval
        self._snapshot: Optional[StatusSnapshot] = None
        self._version = 0
        self._cond = threading.Condition()
        self._stopping = False
        self._poke = False
        self._thread: Optional[threading.Thread] = None

    # --- состояние ---

    @property
    def version(self) -> int:
        """Счётчик обновлений снапшота — по нему SSE понимает, что есть что слать."""
        with self._cond:
            return self._version

    def snapshot(self) -> Optional[StatusSnapshot]:
        """Последний снапшот или ``None``, если первый круг опроса ещё не завершён."""
        with self._cond:
            return self._snapshot

    def is_stopping(self) -> bool:
        """True, если поллер попросили остановиться (SSE-цикл обязан это заметить)."""
        with self._cond:
            return self._stopping

    def wait_for_change(self, since_version: int, timeout: float) -> tuple[int, Optional[StatusSnapshot]]:
        """
        Блокируется до появления снапшота новее ``since_version`` (или до
        таймаута/остановки). Возвращает ``(version, snapshot)`` — вызывающий
        сам решает, слать ли что-то клиенту (версия не изменилась → это был
        таймаут, время послать heartbeat).
        """
        with self._cond:
            self._cond.wait_for(
                lambda: self._version != since_version or self._stopping,
                timeout=timeout,
            )
            return self._version, self._snapshot

    def _publish(self, snapshot: StatusSnapshot) -> None:
        with self._cond:
            self._snapshot = snapshot
            self._version += 1
            self._cond.notify_all()

    # --- опрос ---

    def refresh_now(self) -> StatusSnapshot:
        """
        Синхронно собирает и публикует свежий снапшот. Используется после
        мутаций (start/stop/restart) — чтобы UI не ждал следующего тика.
        """
        snapshot = self._safe_build()
        self._publish(snapshot)
        return snapshot

    def _safe_build(self) -> StatusSnapshot:
        try:
            return self._build()
        except Exception as exc:  # noqa: BLE001 - фоновый поток не имеет права умереть
            # Честный отказ: снапшот с текстом ошибки, а не молчаливо
            # «всё хорошо» и не падение потока опроса.
            return StatusSnapshot(
                stands=[], default="", probed=False, generated_at=time.time(), error=str(exc)
            )

    def _current_interval(self) -> float:
        try:
            value = float(self._interval())
        except (TypeError, ValueError):
            value = MIN_POLL_INTERVAL_SEC
        return max(MIN_POLL_INTERVAL_SEC, value)

    def poke(self) -> None:
        """
        Просит фоновый поток не досыпать текущий интервал, а опросить стенды
        прямо сейчас. Вызывается после мутаций (start/stop/restart/очистка
        Redis) — сам HTTP-запрос при этом НЕ блокируется опросом.
        """
        with self._cond:
            self._poke = True
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
            self._publish(self._safe_build())

            deadline = time.monotonic() + self._current_interval()
            with self._cond:
                while True:
                    if self._stopping:
                        return
                    if self._poke:
                        self._poke = False
                        break
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    # Шаг ожидания ограничен сверху, чтобы поток регулярно
                    # перечитывал интервал/флаги даже при большом периоде.
                    self._cond.wait(min(MAX_POLL_STEP_SEC, left))

    # --- жизненный цикл ---

    def start(self) -> None:
        """Запускает фоновый поток (daemon). Повторный вызов — no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        with self._cond:
            self._stopping = False
            self._poke = False
        self._thread = threading.Thread(
            target=self._loop, name="standkit-hub-poller", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """
        Просит поток остановиться и ждёт его не дольше ``timeout``.

        Вызывается из ``server_close()`` хаба. Поток daemon, поэтому даже
        застрявшая на длинном таймауте проба не удержит процесс.
        """
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
