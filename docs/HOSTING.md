# Виды хостинга стенда (`host_kind`)

Как стенд BPMSoft **хостится** на своей машине — отдельное измерение от того,
**где** им управлять (`transport`: `local`/`agent`). Полный дизайн — см.
[ADR-0001](adr/0001-hosting-backends.md).

| `transport` | *где* управлять стендом | local — процессом самого standkit; agent — через удалённый `standkit_agent` |
|---|---|---|
| `host_kind` | *как* стенд хостится | kestrel / iis / docker (k8s — задел, следующий этап) |

Оба поля независимы: например, `transport=agent` + `host_kind=docker` — это
удалённый контейнер, которым управляет агент на своём хосте.

## `host_kind` и связанные поля

По умолчанию `host_kind = "kestrel"` (текущее поведение standkit — headless
`dotnet <stand_dll>` + pidfile). Неизвестное/будущее значение `host_kind` в
реестре не роняет чтение — откатывается на `kestrel`.

| `host_kind` | Поле | Обязательность | Назначение |
|---|---|---|---|
| `iis` | `iis_site` | одно из двух (`iis_site` и/или `iis_app_pool`) | имя IIS-сайта |
| `iis` | `iis_app_pool` | одно из двух | имя Application Pool (приоритетно для recycle/state) |
| `iis` | `iis_stdout_log_dir` | опционально | папка stdout-логов ASP.NET Core (иначе `<stand_dir>\logs`) |
| `docker` | `docker_container` | контейнер ИЛИ compose-пара | имя/ID контейнера (одиночный режим) |
| `docker` | `docker_compose_file` | вместе с `docker_compose_service` | путь к compose-файлу |
| `docker` | `docker_compose_service` | вместе с `docker_compose_file` | имя сервиса в compose |

`Stand.validate()` проверяет обязательность этих полей для `iis`/`docker`.

## Примеры реестра

### IIS

```json
{
  "example-iis": {
    "transport": "local",
    "host_kind": "iis",
    "iis_site": "BPMSoft-example-iis",
    "iis_app_pool": "BPMSoft-example-iis-pool",
    "iis_stdout_log_dir": "C:\\inetpub\\wwwroot\\example-iis\\logs",
    "stand_dir": "C:\\inetpub\\wwwroot\\example-iis",
    "stand_host": "127.0.0.1",
    "stand_port": 5000
  }
}
```

### Docker (одиночный контейнер)

```json
{
  "example-docker": {
    "transport": "local",
    "host_kind": "docker",
    "docker_container": "bpmsoft-example-docker",
    "stand_dir": "/opt/bpmsoft/example-docker",
    "stand_host": "127.0.0.1",
    "stand_port": 5000
  }
}
```

### Docker (compose-сервис)

```json
{
  "example-docker-compose": {
    "transport": "local",
    "host_kind": "docker",
    "docker_compose_file": "/opt/bpmsoft/example/docker-compose.yml",
    "docker_compose_service": "webhost",
    "stand_dir": "/opt/bpmsoft/example",
    "stand_host": "127.0.0.1",
    "stand_port": 5000
  }
}
```

Полный образец схемы — [`projects.sample.json`](../projects.sample.json)
(записи `example-iis`, `example-docker`).

## Что делается «под капотом»

Диспетчеризация — `standkit.lifecycle` (start/stop/restart/is_running) и
`standkit.hosting.get_backend(stand)`:

- **kestrel** — без изменений: `dotnet <stand_dll>` + pidfile
  (`standkit.lifecycle`/`standkit.platform`).
- **iis** — через `appcmd.exe`
  (`%windir%\system32\inetsrv\appcmd.exe`):
  - старт: `start apppool /apppool.name:<pool>` и/или `start site /site.name:<site>`;
  - стоп: `stop apppool` / `stop site`;
  - рестарт: `recycle apppool /apppool.name:<pool>` (graceful) либо stop+start сайта;
  - проверка «жив»: `list apppool <pool> /text:state` → `Started`; иначе по сайту;
    фолбэк — открытый TCP-порт стенда.
- **docker** — через CLI `docker` / `docker compose`:
  - одиночный контейнер: `docker start|stop|restart <container>`,
    `docker inspect -f "{{.State.Running}}" <container>`,
    `docker logs --tail N <container>`;
  - compose-сервис: `docker compose -f <file> up -d|stop|restart <service>`,
    состояние — парсинг `docker compose -f <file> ps`,
    логи — `docker compose -f <file> logs --tail N <service>`.

Health-проба «процесс» (`standkit.health.check_stand`) для `iis`/`docker`
консультируется с соответствующим бэкендом, с сохранением фолбэка на TCP-порт
стенда (если бэкенд не подтвердил «жив», но порт открыт — стенд считается
живым: он может быть поднят и вручную).

## Требования

- **IIS**: платформа — только Windows; агент/служба standkit должна иметь
  права на управление IIS (обычно — членство в группе `IIS_IUSRS` недостаточно,
  нужны права на `appcmd.exe`/WAS, см. документацию IIS по правам на удалённое
  администрирование). Живая приёмка — на стенде с реальным IIS.
- **Docker**: установленный Docker Engine (`docker` в PATH процесса
  standkit); для compose-режима — плагин `docker compose` (Compose V2).

## Ограничения

- `host_kind=k8s` — допускается схемой реестра, логика не реализована
  (следующий этап, отдельный ADR).
- UI-индикатор `host_kind` в дашборде `standkit_hub` — вне зоны ADR-0001
  (бэклог).

См. также: [ADR-0001](adr/0001-hosting-backends.md),
[REMOTE_STANDS.md](REMOTE_STANDS.md) (транспорт `agent`, ортогонален
`host_kind`).
