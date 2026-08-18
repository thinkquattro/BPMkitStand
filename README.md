<div align="center">

<img src="https://raw.githubusercontent.com/thinkquattro/BPMkitStand/main/standkit_hub/web/bpmkit-logo.svg" alt="BPMkit" width="300"/>

# BPMkitStand

**Свободный диспетчер стендов BPMSoft.**
Локальный веб-дашборд для запуска, остановки и мониторинга ваших стендов — в один клик, без консоли.

[![License: MIT](https://img.shields.io/badge/License-MIT-f9763d.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/OS-Windows%20%7C%20Linux-lightgrey.svg)]()

[Сайт](https://bpmkit.pro) · [Telegram](https://t.me/quattrolife) · [Companion-версия](https://bpmkit.pro)

</div>

---

## Что это

**BPMkitStand** — бесплатный инструмент экосистемы [BPMkit](https://bpmkit.pro) для управления
локальными и удалёнными стендами BPMSoft. Открывает лёгкий локальный веб-дашборд в браузере:
видно состояние каждого стенда, его можно запустить/остановить/перезапустить, посмотреть логи
текущей сессии и очистить Redis — всё без ручных команд в PowerShell.

Ядро (`standkit`) написано на чистой стандартной библиотеке Python — без тяжёлых зависимостей,
работает на Windows и Linux.

## Возможности

- **Дашборд стендов** — список из общего реестра BPMkit (`projects.json`), состояние в реальном времени.
- **Жизненный цикл** — старт / стоп / рестарт стенда с честной обратной связью (спиннер до готовности, без ложных «не поднялся» на прогреве).
- **Логи** — просмотр логов текущей сессии стенда, открытие папки логов стенда и папки BPMkit-проекта.
- **Redis** — очистка кэша стенда (номер БД берётся из конфигурации стенда).
- **Тёмная и светлая тема** с переключателем.
- **Встроенная справка** — кнопка «?» в шапке открывает кукбук: установка, работа с дашбордом, удалённые агенты, траблшутинг. Один самодостаточный файл, работает офлайн.
- **Безопасность по умолчанию** — дашборд слушает только `127.0.0.1`, сессионный токен, защита мутаций (CSRF + проверка Origin).
- **Удалённые стенды** *(в развитии)* — через лёгкие кроссплатформенные headless-агенты с TLS/mTLS.

## Скриншоты

| Дашборд (светлая тема) | Дашборд (тёмная тема) |
|---|---|
| ![Дашборд, светлая тема](https://raw.githubusercontent.com/thinkquattro/BPMkitStand/main/docs/img/dashboard-light.png) | ![Дашборд, тёмная тема](https://raw.githubusercontent.com/thinkquattro/BPMkitStand/main/docs/img/dashboard-dark.png) |

<div align="center">
  <img src="https://raw.githubusercontent.com/thinkquattro/BPMkitStand/main/docs/img/about.png" alt="Модальное окно «О программе»" width="360"/>
</div>

## Как это работает

BPMkitStand состоит из **ядра и двух оболочек**:

- **`standkit`** — ядро (MIT) на чистой стандартной библиотеке Python: движок жизненного цикла
  над реестром `projects.json` (старт/стоп/рестарт процесса стенда), health-пробы (процесс / HTTP
  / порт БД / порт Redis), tail лога, Secret-first доступ к секретам. Без веб-слоя и сетевых
  зависимостей — можно использовать и как библиотеку/CLI.
- **`standkit_hub`** — локальный веб-дашборд (то, что видно на скриншотах). Сам себя отдаёт по
  HTTP через `stdlib http.server` (vanilla JS/CSS, без CDN и сборки) и открывается в системном
  браузере; опционально — в нативном окне (`standkit[desktop]`, `--desktop`). Устанавливается
  только на машину оператора.
- **`standkit_agent`** — лёгкий headless-демон на хосте удалённого стенда (см. ниже).

Дашборд — **федеративный клиент**: он собирает в один список и локальные стенды (управляет ими
напрямую через ядро), и удалённые (ходит к их агентам по HTTP). В таблице колонка «Транспорт»
показывает, как дашборд дотягивается до стенда: `local` или `agent`.

Реестр стендов — **единый с MCP BPMkit**: один `projects.json` (стенды под ключом `projects`),
путь резолвится через `BPMSOFT_PROJECTS_FILE` → `%APPDATA%\BPMkit\projects.json`
(`~/.config/BPMkit/...` на Linux) → `./projects.json`. Секреты (пароли БД, токены агентов) в
реестре не хранятся — только ссылки на них (Secret-first).

Запуск стенда честный: дашборд поднимает `dotnet <stand_dll>` и держит спиннер до реального
ответа web-хоста по HTTP, а не рапортует «запущено» по факту создания процесса.

Виды хостинга (kestrel/iis/docker) — см. [docs/HOSTING.md](docs/HOSTING.md).

## Удалённые стенды

Стенды на других хостах (виртуалки, серверы, контуры заказчика) управляются через **федерацию
лёгких кроссплатформенных агентов** (`standkit_agent`, Windows/Linux, только stdlib). На хосте
стенда поднимается агент, дашборд оператора ходит к нему по HTTPS с Bearer-токеном; стенд
объявляется удалённым одним полем `transport: "agent"` в реестре. Агент — RCE-поверхность по
дизайну, поэтому защищён secure-defaults: loopback по умолчанию, fail-closed на non-loopback без
TLS, TLS 1.2+/mTLS, скоупы control/readonly, lockout по IP, аудит.

**Полное описание, установка агента, TLS/mTLS, служба и траблшутинг — в
[docs/REMOTE_STANDS.md](docs/REMOTE_STANDS.md).**

## Установка

Нужен Python 3.10+.

```bash
pip install standkit
```

Обновление — `pip install -U standkit`. Конкретная версия (в том числе откат) —
`pip install "standkit==0.6.1"`; полный список выпусков — на
[PyPI](https://pypi.org/project/standkit/).

Запуск дашборда:

```bash
standkit-hub
```

Откроется браузер с локальным дашбордом. Реестр стендов берётся из
`%APPDATA%\BPMkit\projects.json` (или из переменной окружения `BPMSOFT_PROJECTS_FILE`).

## Реестр стендов

BPMkitStand использует тот же реестр, что и MCP BPMkit — единый `projects.json`.
Образец формата — [`projects.sample.json`](projects.sample.json). Стенд можно
зарегистрировать прямо из дашборда (кнопка «Зарегистрировать стенд»).

## Безопасность

Дашборд и агент проектировались с расчётом на прод-контур. Модель угроз, харденинг и чек-лист —
в [SECURITY.md](SECURITY.md). Кратко: fail-closed bind на loopback, TLS 1.2+/mTLS для агента,
скоупы токена, per-IP lockout, аудит без утечки секретов.

## Документация проекта

**Карта всех документов — [docs/README.md](docs/README.md)**: кому, о чём и когда открывать.
Коротко:

| Документ | Для кого |
|---|---|
| [**Кукбук**](standkit_hub/web/cookbook.html) — обзор, установка, дашборд, реестр, хостинг, агент, безопасность, траблшутинг. Один standalone-файл: в работающем диспетчере открывается кнопкой «?» в шапке, без него — прямо с диска в браузере | оператор |
| [docs/COOKBOOK_LINUX.md](docs/COOKBOOK_LINUX.md) — пошаговое развёртывание агента на Linux: pipx/venv, реестр, секреты, systemd, TLS/mTLS, разбор типичных ошибок с реальными сообщениями | администратор |
| [docs/REMOTE_STANDS.md](docs/REMOTE_STANDS.md) · [docs/HOSTING.md](docs/HOSTING.md) · [SECURITY.md](SECURITY.md) | оператор / администратор |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/adr/](docs/adr/) · [docs/CHANGELOG.md](docs/CHANGELOG.md) | разработчик |
| [docs/ROADMAP.md](docs/ROADMAP.md) — крупные работы · [docs/BACKLOG.md](docs/BACKLOG.md) — заглушки и заделы в коде · [docs/GAPs/](docs/GAPs/README.md) — код прав, а сценарий оператора ломается | планирование |

> Статус: `standkit` — молодой проект (0.8.x). Ядро/агент/хаб работоспособны, но
> ряд возможностей — это **каркас/заделы** (например, глубокие пробы БД/Redis,
> транспорты ssh/winrm, живая приёмка Docker/k8s/IIS). Что именно ещё не
> дописано — прозрачно перечислено в [docs/BACKLOG.md](docs/BACKLOG.md).

## BPMkitStand и Companion

Бесплатная версия — полноценный диспетчер стендов. **Companion-версия** дополнительно даёт
автообновление MCP BPMkit и контроль лицензии; поставляется в составе установщика MCP-клиента.
Подробнее — на [bpmkit.pro](https://bpmkit.pro).

## Лицензия

[MIT](LICENSE) © Владимир Терновский

---

<div align="center">
<sub>Часть экосистемы <a href="https://bpmkit.pro">BPMkit</a> — AI-ассистента для разработки на BPMSoft.</sub>
</div>
