# ADR-0001 — Hosting backends: kestrel / iis / docker (k8s — следующим этапом)

- Статус: **Принято** (24.07.2026)
- Контекст-владелец: Владимир Терновский
- Связано: [REMOTE_STANDS.md](../REMOTE_STANDS.md), [ARCHITECTURE.md](../ARCHITECTURE.md)

## Контекст и проблема

Ядро `standkit` управляет стендом BPMSoft как **self-host Kestrel**: запускает
`dotnet BPMSoft.WebHost.dll` и следит за процессом по pid-файлу
(`standkit/lifecycle.py` + `standkit/platform.py`). Однако стенды в реальных
контурах живут не только так:

- **IIS** — стенд опубликован как IIS-приложение в Application Pool; жизненным
  циклом владеет IIS (`w3wp.exe`), а не наш `dotnet`-спавн.
- **Docker** — стенд крутится в контейнере (локально или на удалённом хосте);
  жизненный цикл — `docker start/stop/restart`, а не запуск процесса на хосте.
- **Kubernetes** — стенд как Deployment/Pod (следующий этап, не в этом ADR).

Что уже работает для IIS/Docker **без изменений**: HTTP/БД/Redis-пробы и
статус «up» (см. `health.process_running` — засчитывает стенд живым по открытому
TCP-порту, независимо от способа хостинга). **Не работает**: кнопки
старт/стоп/рестарт (шлют `dotnet`, что для IIS/контейнера бессмысленно) и проба
«процесс жив» по pid-файлу (у IIS/контейнера его нет).

## Решение

Ввести понятие **hosting backend** — как хостится стенд на своей машине,
**ортогонально** транспорту (`transport` = *где*: local / agent; `host_kind` =
*как*: kestrel / iis / docker). Управление жизненным циклом диспетчеризуется по
`host_kind`; для удалённых стендов ту же диспетчеризацию делает агент на хосте
стенда — новый код автоматически распространяется на remote.

### Модель реестра (`standkit/models.py`)

Новое поле `Stand.host_kind` (enum `HostKind`, дефолт **`kestrel`**; неизвестное
значение → `kestrel`, чтобы не ронять чтение реестра). Плюс опциональные поля
под конкретные бэкенды (используются только соответствующим бэкендом):

| Поле | Бэкенд | Назначение |
|------|--------|-----------|
| `iis_site` | iis | имя IIS-сайта |
| `iis_app_pool` | iis | имя Application Pool (приоритетно для recycle/state) |
| `iis_stdout_log_dir` | iis | папка stdout-логов ASP.NET Core (опц., иначе `<stand_dir>\logs`) |
| `docker_container` | docker | имя/ID контейнера (одиночный режим) |
| `docker_compose_file` | docker | путь к compose-файлу (compose-режим) |
| `docker_compose_service` | docker | имя сервиса в compose |

`host_kind` **ортогонален** `transport`: возможен `transport=agent` +
`host_kind=docker` (удалённый контейнер, управляемый агентом на его хосте).

### Абстракция (`standkit/hosting.py`)

Единый протокол бэкенда — тонкий слой поверх существующих `platform`/subprocess:

```
class HostingBackend(Protocol):
    def start(stand, *, run_dir, log_dir) -> Optional[int]      # pid | None
    def stop(stand, *, run_dir) -> bool
    def restart(stand, *, run_dir, log_dir) -> Optional[int]
    def is_running(stand, *, run_dir) -> bool
    def read_logs(stand, n, *, log_dir) -> Optional[list[str]]  # None → пусть читает файл-логи
```

`get_backend(stand) -> HostingBackend` по `stand.host_kind`.

- **KestrelBackend** — обёртка над ТЕКУЩЕЙ логикой (`platform.spawn_hidden` +
  pidfile). Поведение бит-в-бит прежнее; `read_logs` → `None` (хаб читает
  файл-лог как сейчас). Дефолт.
- **IisBackend** (Windows) — через `appcmd`
  (`%windir%\system32\inetsrv\appcmd.exe`):
  - start: `start apppool /apppool.name:<pool>` и/или `start site /site.name:<site>`;
  - stop: `stop apppool` / `stop site`;
  - restart: `recycle apppool /apppool.name:<pool>` (graceful) либо stop+start сайта;
  - is_running: `list apppool <pool> /text:state` → `Started`; иначе по сайту;
    fallback — TCP-порт.
  - read_logs: последние `n` строк из `iis_stdout_log_dir`/`<stand_dir>\logs`.
- **DockerBackend** (кроссплатформенно) — через CLI `docker` / `docker compose`:
  - одиночный: `docker start|stop|restart <container>`,
    `docker inspect -f '{{.State.Running}}' <container>`,
    `docker logs --tail N <container>`;
  - compose: `docker compose -f <file> up -d|stop|restart <service>`,
    состояние — `docker compose -f <file> ps`.

### Диспетчеризация (`standkit/lifecycle.py`)

`start/stop/restart/is_running` — тонкие диспетчеры по `host_kind`. **Ветка
kestrel = существующий код без изменений** (минимум риска для 243 тестов и
существующих моков `test_lifecycle.py`); iis/docker → соответствующий бэкенд.
Публичные сигнатуры функций не меняются — хаб/агент/клиент правок не требуют.

### Пробы (`standkit/health.py`)

Проба «процесс» для `host_kind in {iis, docker}` консультируется с
`backend.is_running` (состояние App Pool / контейнера), сохраняя fallback на
TCP-порт. Ветка kestrel — как сейчас (pidfile ∨ порт). HTTP/БД/Redis — без
изменений.

## Границы и риски

- **Не трогаем** UI хаба (`standkit_hub/web/*`) и его сервер — бэкенд-слой
  прозрачен, дашборд выглядит идентично. Колонку «хостинг» в UI можно добавить
  позже, отдельным шагом (сейчас — вне scope, чтобы «не сломать красоту»).
- IIS-операции требуют прав (агент/служба под учёткой с правами на IIS);
  фиксируем в доке, живую приёмку IIS делает коллега на своём стенде.
- Docker: приёмка на реальном стенде, развёрнутом совместно.
- Безопасность агента (RCE-поверхность) не меняется — добавляются лишь новые
  внешние вызовы (`appcmd`/`docker`), покрытые той же аутентификацией/аудитом.

## План (этапность)

1. **Этот ADR** + модель `host_kind` + `hosting.py` (kestrel/iis/docker) +
   диспетчеризация lifecycle + проба health + образцы реестра + тесты (моки
   subprocess). Версия → **0.4.0**.
2. Живая приёмка: Docker — совместно; IIS — коллега.
3. Хаб: опциональная колонка/бейдж «хостинг» (UI), read_logs через бэкенд в
   панели «Текущее состояние» для iis/docker.
4. **Kubernetes** (`host_kind=k8s`): `kubectl` (scale/rollout/get/logs),
   Deployment. Реализовано в **v0.5.0** — см. [ADR-0002](0002-k8s-backend.md).
5. **Регистрация стенда из UI** — модалка «Зарегистрировать стенд» в дашборде
   (пишет в общий `projects.json` через `Registry.add_existing`). Реализовано в
   **v0.5.0**.

## Бэклог (зафиксировано)

- [x] IIS backend — реализация + документация (v0.4.0; приёмка: коллега).
- [x] Docker backend — реализация (v0.4.0).
- [x] k8s backend — реализация (v0.5.0, [ADR-0002](0002-k8s-backend.md)).
- [x] Регистрация стенда из UI — модалка в дашборде (v0.5.0).
- [ ] **🔴 КРИТ ДО РЕЛИЗА — живая приёмка Docker и k8s бэкендов.** Юнит-тесты на моках
  зелёные, но на реальном контуре ещё не гоняли. Docker — развернуть стенд совместно;
  k8s — на реальном кластере (kubectl scale/rollout/get/logs). Оба — блокеры релиза.
  (IIS — приёмку делает коллега на своём стенде.)
- [ ] **Браузинг логов удалённых стендов (приоритетный бэклог, ADR-0003).** Сейчас
  «Открыть папку логов» = `os.startfile` — только для локального стенда; у удалённого
  (agent) открывать нечего. Нужно: агент отдаёт **листинг папки логов** (файлы/подпапки с
  датой и размером — у BPMSoft папка логов именуется по дате, `папка логов = дата`),
  оператор **выбирает** файл/дату, **смотрит** и **скачивает** (файл целиком либо папку
  архивом; ленивое скачивание без предзагрузки либо монтирование в сессию). Новые
  эндпоинты агента: `GET /stand/{name}/logs/list`, `.../logs/file?path=…` (traversal-safe),
  `.../logs/download?path=…` (файл/zip). UI: выбор файла/даты + просмотр + «Скачать».
  Затрагивает `standkit_agent/server.py`, `standkit_hub/*`, `standkit/logs.py`.
- [ ] UI: индикатор `host_kind` в дашборде + read_logs бэкенда (iis/docker/k8s) в «Текущем
  состоянии».
