# ROADMAP — standkit / BPMkitStand

Дорожная карта **приложения** (свободное ядро `standkit` + дашборд
`standkit_hub` + агент `standkit_agent`). Полный список незакрытых «заделов» с
ссылками на код — [BACKLOG.md](BACKLOG.md). Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md).
Решения — [adr/](adr/). История изменений — [CHANGELOG.md](CHANGELOG.md).

Текущая версия на PyPI: **0.5.2**. Python ≥ 3.10, ядро и агент — stdlib-only.

## ✅ Сделано

- **Ядро `standkit`** — реестр стендов, модель `Stand` (`transport`
  `local`/`agent`, `host_kind` kestrel/iis/docker/k8s), быстрые health-пробы
  (процесс/HTTP/БД/Redis по TCP), жизненный цикл kestrel (headless start/stop/
  restart, pidfile), Secret-first доступ к секретам (env→keyring→фолбэк).
- **Hosting backends** (ADR-0001/0002) — kestrel / IIS (`appcmd`) / Docker
  (`docker`,`compose`) / Kubernetes (`kubectl`). Диспетчеризация по `host_kind`,
  прозрачна для хаба/агента/клиента.
- **Дашборд `standkit_hub`** — веб-UI (stdlib `http.server` + vanilla JS): список
  стендов + состояние, старт/стоп/рестарт (честный старт до HTTP-ok), очистка
  Redis, тёмная тема, модалка «Зарегистрировать стенд» (пишет в общий
  `projects.json`), панель «Текущее состояние» (tail лога текущей сессии),
  федерация с удалёнными агентами.
- **Агент `standkit_agent`** — stdlib-only HTTP/RPC с прод-харденингом: TLS/mTLS,
  fail-closed loopback, скоупы control/readonly, rate-limit/lockout, аудит.
- **Консольные точки входа** — `standkit-hub` / `standkit-gui` / `standkit-agent`.

### Правки текущего цикла (0.5.1 → 0.5.2) — см. [CHANGELOG.md](CHANGELOG.md)

- IIS: жизненный цикл **по Site** (App Pool не трогаем — может быть общим),
  корректный детект остановленного сайта (http.sys держит порт), читаемая ошибка
  `appcmd` (OEM-декод) + подсказка «запустить от администратора» — [ADR-0004](adr/0004-iis-site-scoped-lifecycle.md).
- Честный отказ при остановке стенда, запущенного вне диспетчера (нет pidfile).
- Хаб: колонка Redis с номером базы; HTTP-значение — ссылка; колонки не «прыгают»;
  модалки не закрываются при выделении текста; фидбэк кнопки «Обновить»; лёгкие
  логи IIS (только за сегодня + чтение хвоста файла); первый запуск создаёт папку
  реестра; Ctrl+C завершает дашборд без трейсбека.
- Упаковка: SPDX-лицензия (без deprecation-warning setuptools).

## 🔴 Крит до «боевого» релиса

- **Живая приёмка Docker / k8s / IIS** на реальном контуре — сейчас только моки
  (см. [ADR-0001](adr/0001-hosting-backends.md), [BACKLOG.md](BACKLOG.md)).

## ⏭ Ближайшее

- **Браузинг/скачивание логов удалённых стендов** — [ADR-0003](adr/0003-remote-log-browsing.md)
  (листинг папки логов агентом, выбор файла/даты, просмотр и скачивание).
- UI-индикатор `host_kind` в дашборде; `read_logs` бэкенда (iis/docker/k8s) в
  панели «Текущее состояние».
- Глубокие health-пробы БД/Redis (`SELECT 1`/`PING`) под опциональные зависимости
  — сейчас заглушки `SKIPPED` ([BACKLOG.md](BACKLOG.md)).

## 🧊 Бэклог (без срока)

- Транспорты `ssh`/`winrm` (сейчас зарезервированы в enum, не реализованы).
- Жизненный цикл kestrel: polling готовности, блокировка pidfile, graceful stop,
  Windows Job Object / Linux double-fork.
- Логи: ротация в `follow()`, постраничная навигация.
- Клиент: параллельный опрос агентов, кэш/дебаунс.
- Секреты: CLI-обёртка, файловый фолбэк `secrets.enc`.
- Остаточная безопасность агента — `SECURITY.md` §6.

Полный список с ссылками на код — [BACKLOG.md](BACKLOG.md).

## 🌐 Экосистема (вне этого репозитория)

- **Companion** — платная редакция на той же кодовой базе: автоапдейт MCP,
  лицензия/токен, подпись артефактов. Часть экосистемы BPMkit; здесь не живёт.
