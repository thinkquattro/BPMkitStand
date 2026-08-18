# Архитектура standkit

Этот документ — краткое зеркало архитектурных решений для контекста разработки
в самом репозитории `standkit`: только то, что нужно держать перед глазами при
правке кода. Обоснования и разбор альтернатив по отдельным подсистемам —
в [adr/](adr/); дорожная карта — в [ROADMAP.md](ROADMAP.md), незакрытые заделы —
в [BACKLOG.md](BACKLOG.md).

## Модель: ядро + две оболочки

```
standkit        — MIT, движок жизненного цикла, stdlib-only, без сети "наружу",
                   без лицензионно-чувствительного контента BPMSoft.
standkit_agent   — MIT, stdlib-only HTTP-обёртка вокруг ядра, кроссплатформенная
                   (Windows/Linux), разворачивается на КАЖДОМ хосте стенда.
standkit_hub     — MIT, stdlib-only локальный веб-дашборд (http.server) +
                   vanilla JS/CSS фронтенд, федеративный клиент N агентов +
                   локального ядра, ставится ТОЛЬКО на машину оператора.
                   Опциональная нативная оболочка — pywebview (extra [desktop]).
```

Веб-дашборд (вариант A) заменил прежнюю PySide6/Qt-оболочку `standkit_gui`
(удалена целиком): та же роль ("диспетчер на машине оператора"), но без
тяжёлой GUI-зависимости — браузер универсален, фронтенд отдаёт сам хаб.

Границы зависимостей — принципиальны и проверяются на уровне импортов:
- `standkit/*`, `standkit_agent/*` и `standkit_hub/*` (серверная часть) —
  STDLIB-ONLY, никаких сторонних веб-фреймворков/GUI-тулкитов.
- `standkit_hub/__main__.py` — единственное место, где опционально
  импортируется `pywebview` (только под `--desktop`, в try/except; при
  отсутствии extra `[desktop]` хаб печатает понятное сообщение и падает
  обратно в системный браузер, а не роняется исключением импорта).
- `standkit` не тянет сторонних пакетов вообще (`dependencies = []` в
  `pyproject.toml`) — секреты (`standkit/secrets.py`) используют `keyring`
  только опционально, через `try/except ImportError`.
- Фронтенд `standkit_hub/web/*` — vanilla JS/CSS, без CDN и без шага сборки
  (работает офлайн, отдаётся тем же `http.server`, что и API).

## Транспорт: `local` | `agent` (задел `ssh` / `winrm`)

Поле `transport` в записи реестра (`standkit/models.py::Transport`) определяет,
как ядро/GUI достаёт до конкретного стенда:

- `local` — стенд поднимается тем же процессом, что вызывает `standkit`
  (`standkit/lifecycle.py` поверх `standkit/platform.py`, headless-процесс
  через `subprocess`).
- `agent` — управление идёт по HTTP к `standkit_agent`, слушающему на
  `agent_url`, с Bearer-токеном, разрешаемым через `agent_secret_ref`
  (Secret-first, см. `standkit/secrets.py`).
- `ssh` / `winrm` — **схема допускает** эти значения (см.
  `projects.sample.json`, запись `example-future-ssh`), но логика НЕ
  реализована: `Stand.from_dict` толерантен к неизвестным будущим значениям
  (не роняет чтение реестра), а код, требующий конкретный транспорт
  (`lifecycle._require_local`, `client.FederatedClient._dispatch_action`),
  явно бросает `NotImplementedError`/`LifecycleError` для нереализованных
  транспортов, а не тихо делает не то.

## Кроссплатформенность

- Все пути — `pathlib.Path`, без хардкода `C:\...`.
- Запуск процесса стенда (`standkit/platform.py::spawn_hidden`) — раздельные
  ветки для `sys.platform == "win32"` (скрытое консольное окно,
  `CREATE_NO_WINDOW` + отдельная группа процессов) и POSIX
  (`start_new_session=True`, эквивалент `setsid`).
- Запуск ВНЕШНИХ КОНСОЛЬНЫХ УТИЛИТ (`appcmd`, `sc`, `docker`, `kubectl`,
  `taskkill`, `tasklist`, `powershell`) — ТОЛЬКО через
  `standkit/platform.py::run_console`, который на win32 добавляет тот же
  `CREATE_NO_WINDOW`. Прямой `subprocess.run` в остальных модулях пакета
  запрещён и стережётся тестом `tests/test_no_window.py`: у родителя без
  своей консоли (`pythonw`, служба, фоновый поллер хаба) каждый такой вызов
  рождает на экране мигающее чёрное окно, а из терминала дефект не виден
  (GAP-138: поллер опрашивал IIS-стенд парой `appcmd` раз в ~12 с).
- `standkit_agent` — только `stdlib` (`http.server`, `subprocess`, `socket`,
  `urllib`), потому что стенды BPMSoft на .NET штатно живут и под Linux, а
  агент должен разворачиваться на голом хосте без сборки колёс под
  конкретную ОС/архитектуру.
- Деплой агента как службы ОС — раздельные шаблоны:
  `standkit_agent/deploy/standkit-agent.service` (systemd, Linux) и
  `standkit_agent/deploy/windows-service.md` (заметки NSSM/Task
  Scheduler/pywin32, Windows).

## Границы free (MIT) / paid

`standkit` — воронка на платный продукт **BPMkit**. Разделительная линия:

| В standkit (MIT, бесплатно)                        | В BPMkit (платно)                                  |
|-----------------------------------------------------|-----------------------------------------------------|
| start/stop/restart headless-процесса стенда          | Провижининг нового стенда "с нуля" (`provision_stand`) |
| Health-пробы: процесс/HTTP/TCP-порт БД и Redis        | Глубокие БД-операции (`db_create/restore/backup`)     |
| Tail/follow лог-файла                                | Деплой пакетов (WSC/UBS), кастомизация JS/C#          |
| Реестр стендов (чтение/запись `projects.json`)        | Генерация документов, git-онбординг пакетов стенда    |
| Secret-first доступ к секретам (обёртка над keyring)  | Административные операции над живым стендом (ESQ, роли, права) |

`standkit.registry.Registry.add_existing()` — это **привязка уже
существующего стенда** к реестру standkit, не провижининг: каталог/БД/
дистрибутив должны существовать заранее. Полноценный провижининг —
исключительно зона BPMkit.

## Что уже реализовано в каркасе vs TODO

> Полный, поддерживаемый список «упомянуто в коде, но не реализовано» со ссылками
> на конкретные символы/файлы — [BACKLOG.md](BACKLOG.md). Дорожная карта —
> [ROADMAP.md](ROADMAP.md).

Рабочая минимальная логика, покрытая тестами: `standkit/models.py`,
`standkit/registry.py`, `standkit/health.py` (быстрые пробы: `tcp_open`,
`http_ok`, `process_alive`). Скелетные модули с явными `TODO` в докстрингах:
`standkit/lifecycle.py` (нет graceful stop с эскалацией SIGKILL/таймаутом),
`standkit/platform.py` (нет Job Object на Windows, нет double-fork на Linux),
`standkit/health.py::db_deep_check/redis_deep_check` (заглушки — требуют
опциональных зависимостей `psycopg2`/`pyodbc`/`redis-py`), `standkit_hub`
(опрос стендов — по кнопке "Обновить"/интервалу polling из фронтенда, а не
push/SSE; live follow лога — TODO, сейчас только периодический tail).

`standkit_agent` прошёл прод-харденинг (см. `standkit_agent/security.py`,
`standkit_agent/audit.py`, README.md → раздел «Безопасность»): TLS/mTLS,
fail-closed bind-defaults (loopback-only без явного `--insecure`), скоупы
control/readonly с `hmac.compare_digest`, rate limiting/lockout по IP,
структурный аудит-лог, input-hardening. Осознанно не реализовано (TODO):
полноценная PKI/ротация сертификатов и токенов "на лету", per-stand ACL
(сейчас скоуп бинарный на уровне всего реестра агента), CN→scope маппинг для
mTLS. Деплой-шаблоны (`standkit_agent/deploy/`) обновлены под least privilege
(systemd sandboxing, выделенный не-root сервисный аккаунт на Windows).
