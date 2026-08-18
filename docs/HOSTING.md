# Виды хостинга стенда (`host_kind`)

Как стенд BPMSoft **хостится** на своей машине — отдельное измерение от того,
**где** им управлять (`transport`: `local`/`agent`). Полный дизайн — см.
[ADR-0001](adr/0001-hosting-backends.md).

| `transport` | *где* управлять стендом | local — процессом самого standkit; agent — через удалённый `standkit_agent` |
|---|---|---|
| `host_kind` | *как* стенд хостится | kestrel / iis / docker / k8s |

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
| `iis` | `iis_stdout_log_dir` | опционально | папка stdout-логов ASP.NET Core (иначе — подкаталог `logs` внутри `stand_dir`, имя ищется **без учёта регистра**: BPMSoft на Linux пишет в `Logs`) |
| любой | `logs_dir` | опционально | явный каталог логов, если он лежит не подкаталогом `stand_dir`. Приоритет ниже `iis_stdout_log_dir` для `host_kind=iis` |
| `docker` | `docker_container` | контейнер ИЛИ compose-пара | имя/ID контейнера (одиночный режим) |
| `docker` | `docker_compose_file` | вместе с `docker_compose_service` | путь к compose-файлу |
| `docker` | `docker_compose_service` | вместе с `docker_compose_file` | имя сервиса в compose |
| `k8s` | `k8s_deployment` | обязательно | имя Kubernetes Deployment |
| `k8s` | `k8s_namespace` | опционально (пусто → `default`) | namespace кластера |
| `k8s` | `k8s_context` | опционально (пусто → текущий контекст kubeconfig) | контекст kubectl |
| `k8s` | `k8s_container` | опционально | имя контейнера в поде (для `read_logs` при нескольких контейнерах) |
| `k8s` | `k8s_replicas` | опционально, по умолчанию `1` | число реплик при `start` |

`Stand.validate()` проверяет обязательность этих полей для `iis`/`docker`/`k8s`.

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

### Kubernetes (k8s)

```json
{
  "example-k8s": {
    "transport": "local",
    "host_kind": "k8s",
    "k8s_namespace": "bpmsoft",
    "k8s_deployment": "bpmsoft-example-k8s",
    "k8s_context": "example-cluster",
    "k8s_container": "webhost",
    "k8s_replicas": 1,
    "stand_dir": "/opt/bpmsoft/example-k8s",
    "stand_host": "127.0.0.1",
    "stand_port": 5000
  }
}
```

Полный образец схемы — [`projects.sample.json`](../projects.sample.json)
(записи `example-iis`, `example-docker`, `example-k8s`).

## Что делается «под капотом»

Диспетчеризация — `standkit.lifecycle` (start/stop/restart/is_running) и
`standkit.hosting.get_backend(stand)`:

- **kestrel** — без изменений: `dotnet <stand_dll>` + pidfile
  (`standkit.lifecycle`/`standkit.platform`).
- **iis** — через `appcmd.exe`
  (`%windir%\system32\inetsrv\appcmd.exe`). ⚠️ **Требует запуска диспетчера
  «от имени администратора»** — см. раздел «Требования».
  «Стенд в IIS» = его **Site**: диспетчер управляет сайтом и намеренно **не
  трогает App Pool** (пул может быть общим с другими приложениями, его остановка
  положила бы и их). App Pool задействуется только когда `iis_site` не задан
  вовсе (единственный хэндл стенда).
  - старт: `start apppool /apppool.name:<pool>` (если задан, чтобы сайт мог
    обслуживаться) + `start site /site.name:<site>`;
  - стоп: `stop site /site.name:<site>` (**только сайт**; при отсутствии
    `iis_site` — `stop apppool`);
  - рестарт: `stop site` + `start site` (**App Pool не рециклится**; recycle
    пула — только когда сайт не задан);
  - проверка «жив»: состояние **сайта** `list site <site> /text:state` →
    `Started` (при отсутствии `iis_site` — состояние пула); фолбэк на открытый
    TCP-порт — только если `appcmd` не дал определённого состояния (порт держит
    http.sys даже у остановленного сайта, поэтому открытый порт сам по себе
    «живым» не считается).
- **docker** — через CLI `docker` / `docker compose`:
  - одиночный контейнер: `docker start|stop|restart <container>`,
    `docker inspect -f "{{.State.Running}}" <container>`,
    `docker logs --tail N <container>`;
  - compose-сервис: `docker compose -f <file> up -d|stop|restart <service>`,
    состояние — парсинг `docker compose -f <file> ps`,
    логи — `docker compose -f <file> logs --tail N <service>`.
- **k8s** — через CLI `kubectl` (базовые аргументы — `kubectl [--context
  <k8s_context>] -n <k8s_namespace|default>`):
  - старт: `... scale deployment/<deployment> --replicas=<k8s_replicas|1>`;
  - стоп: `... scale deployment/<deployment> --replicas=0` (в Kubernetes нет
    отдельной команды "остановить" — общепринятый эквивалент "стопа");
  - рестарт: `... rollout restart deployment/<deployment>`;
  - проверка «жив»: `... get deployment <deployment> -o
    jsonpath={.status.readyReplicas}` → число > 0; фолбэк — открытый TCP-порт
    стенда;
  - логи: `... logs deployment/<deployment> --tail N [-c <k8s_container>]`.

Health-проба «процесс» (`standkit.health.check_stand`) для `iis`/`docker`/`k8s`
консультируется с соответствующим бэкендом, с сохранением фолбэка на TCP-порт
стенда (если бэкенд не подтвердил «жив», но порт открыт — стенд считается
живым: он может быть поднят и вручную).

## Требования

- **IIS**: платформа — только Windows. ⚠️ **Диспетчер (`standkit-hub`) должен
  быть запущен «от имени администратора» (elevated).** `appcmd.exe` управляет
  конфигурацией IIS и читает `%windir%\system32\inetsrv\config\redirection.config`,
  что требует прав администратора; без elevation любая IIS-операция падает с
  ошибкой прав (например, `код 1168` / «не удалось открыть файл конфигурации
  из-за отсутствия необходимых разрешений»). Членства в группе `IIS_IUSRS`
  **недостаточно**. Как запускать elevated: ярлык/консоль → правый клик →
  «Запуск от имени администратора», либо из уже поднятой административной
  консоли `standkit-hub`. Диспетчер в такой ситуации отдаёт понятную подсказку
  «запустите от имени администратора» в тексте ошибки. Живая приёмка — на
  стенде с реальным IIS.
- **Docker**: установленный Docker Engine (`docker` в PATH процесса
  standkit); для compose-режима — плагин `docker compose` (Compose V2).
- **Kubernetes**: установленный `kubectl` в PATH процесса standkit и
  доступный kubeconfig/контекст (переменная `KUBECONFIG` либо
  `~/.kube/config`) с правами на `get`/`scale`/`rollout`/`logs` над
  Deployment в указанном namespace.

## Ограничения

- UI-индикатор `host_kind` в дашборде `standkit_hub` — вне зоны ADR-0001
  (бэклог).

См. также: [ADR-0001](adr/0001-hosting-backends.md),
[REMOTE_STANDS.md](REMOTE_STANDS.md) (транспорт `agent`, ортогонален
`host_kind`).
