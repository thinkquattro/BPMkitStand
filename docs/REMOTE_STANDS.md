# Удалённые стенды — описание и инструкция

Как управлять стендами BPMSoft, которые живут не на машине оператора, а на других
хостах (виртуалки, серверы, dev-контуры заказчика), из того же локального дашборда
BPMkitStand.

> Быстрая навигация: [Как это устроено](#как-это-устроено) · [Модель безопасности](#модель-безопасности) · [Установка агента](#установка-агента-пошагово) · [Подключение из дашборда](#подключение-из-дашборда) · [Проверка и траблшутинг](#проверка-и-траблшутинг) · [Ограничения](#ограничения-и-планы)

---

## Как это устроено

BPMkitStand управляет удалёнными стендами через **федерацию лёгких headless-агентов**.
На каждом хосте, где живут стенды, запускается маленький демон `standkit_agent`; дашборд
оператора (`standkit_hub`) ходит к этим агентам по HTTP(S) и показывает все стенды —
локальные и удалённые — единым списком.

```
   Машина оператора                         Хосты стендов (Windows / Linux)
 ┌───────────────────────┐                ┌──────────────────────────────┐
 │   standkit_hub        │   HTTPS+токен  │   standkit_agent (хост A)     │
 │   (веб-дашборд)       │───────────────▶│   stdlib http.server         │
 │                       │                │   + standkit (ядро)          │
 │   федеративный клиент │                │   start/stop/restart/logs    │
 │   локальное ядро +    │                └──────────────────────────────┘
 │   N агентов           │   HTTPS+токен  ┌──────────────────────────────┐
 │                       │───────────────▶│   standkit_agent (хост N)     │
 └───────────────────────┘                └──────────────────────────────┘
```

Ключевые свойства агента:

- **Лёгкий и кроссплатформенный.** Только стандартная библиотека Python (`http.server`,
  `ssl`, `subprocess`, `socket`) — никаких веб-фреймворков и pip-зависимостей, кроме самого
  `standkit`. Работает под Windows и под Linux (стенды BPMSoft на .NET штатно живут под обе ОС).
- **То же ядро, что локально.** Агент — это то же ядро `standkit`, обёрнутое в крошечный
  HTTP/RPC-слой. Поведение старта/стопа/логов идентично локальному.
- **Единый реестр.** Стенд объявляется удалённым одним полем `transport: "agent"` в том же
  `projects.json`. Ничего в коде менять не нужно.

### Что агент умеет (эндпоинты)

| Метод | Путь | Скоуп | Действие |
|-------|------|-------|----------|
| `GET`  | `/stands` | read | список стендов реестра агента |
| `GET`  | `/stand/{name}/status` | read | health-статус стенда (процесс/HTTP/БД/Redis) |
| `GET`  | `/stand/{name}/logs?n=100` | read | последние `n` строк лога |
| `POST` | `/stand/{name}/start` | control | запустить стенд |
| `POST` | `/stand/{name}/stop` | control | остановить стенд |
| `POST` | `/stand/{name}/restart` | control | перезапустить стенд |

Эндпоинта `/health` у агента **нет** — живость проверяется `GET /stands` с токеном.

---

## Модель безопасности

Агент по HTTP принимает команды start/stop/restart процессов на хосте стенда. Это
**RCE-поверхность по дизайну**: любой, кто может успешно аутентифицироваться против агента,
управляет жизненным циклом процессов на этом хосте. Относитесь к агенту как к критической
инфраструктуре.

**Правило №1 — никогда не выставлять агента в недоверенную сеть открытым HTTP.** Удалённый
доступ только через TLS с обязательной клиентской аутентификацией (mTLS), либо внутри
управляющего контура / VPN / SSH-туннеля.

Что защищает агента «из коробки» (secure-defaults):

- **Loopback по умолчанию.** `--host` = `127.0.0.1` — без явной настройки агент не слушает
  ничего, кроме localhost.
- **Fail-closed.** Если `--host` не loopback И не настроен TLS — агент **отказывается
  стартовать**. Обойти можно только явным `--insecure` (только dev/тест за изолированным
  периметром, с громким предупреждением в stderr).
- **TLS 1.2+** с современным набором AEAD-шифров; **mTLS** (`--tls-client-ca`) отклоняет
  соединения без валидного клиентского сертификата ещё на TLS-хендшейке.
- **Bearer-токен** сравнивается только через `hmac.compare_digest` (защита от timing-атак).
  Два скоупа: **control** (управление + чтение) и **readonly** (только чтение).
- **Lockout по IP** после серии неудачных аутентификаций; **append-only JSON-аудит** всех
  запросов без токенов и секретов; лимиты тела/таймауты/валидация ввода.

Полная модель угроз и чек-лист развёртывания — в [SECURITY.md](../SECURITY.md).

---

## Установка агента (пошагово)

Выполняется **на каждом хосте стенда**.

> **Linux:** подробный разбор с реальными выводами команд, systemd-юнитом, TLS/mTLS
> и таблицей типичных ошибок — [COOKBOOK_LINUX.md](COOKBOOK_LINUX.md). Ниже —
> краткая версия для обеих платформ.

### 1. Поставить пакет

Нужен Python 3.10+ и установленный `dotnet` (для запуска стендов BPMSoft).

Агент оформляется службой, поэтому ему нужен предсказуемый путь к интерпретатору —
ставим в выделенный venv, а не в системный Python (в свежих дистрибутивах Linux он
защищён PEP 668 и `pip install` в него откажет с `error: externally-managed-environment`):

```bash
# Linux
sudo useradd --system --no-create-home --shell /usr/sbin/nologin standkit
sudo mkdir -p /opt/standkit/{tls,logs,run}
sudo python3 -m venv /opt/standkit/venv
sudo /opt/standkit/venv/bin/pip install standkit
sudo chown -R standkit:standkit /opt/standkit
```

```powershell
# Windows (от администратора)
python -m venv C:\ProgramData\standkit\venv
C:\ProgramData\standkit\venv\Scripts\pip install standkit
```

Свежий `main` до релиза — `pip install "git+https://github.com/thinkquattro/BPMkitStand.git"`.

### 2. Подготовить реестр на хосте агента

Агент читает тот же формат реестра, что и дашборд. На хосте агента нужен `projects.json`,
где перечислены стенды **этого** хоста (можно только они). Пример записи локального для
агента стенда:

> **Отдельной CLI-команды регистрации нет.** Файл пишется руками по образцу
> [`projects.sample.json`](../projects.sample.json) либо скриптом через
> `Registry.add_existing()` — питоном ТОГО окружения, куда установлен пакет
> (`/opt/standkit/venv/bin/python`; системный `python3` модуля не увидит).
> Каталог и файл создаются при первом `save()`. Пошаговый рецепт — в кукбуке,
> раздел «Реестр стендов».

```json
{
  "default": "",
  "projects": {
    "client-uat": {
      "transport": "local",
      "stand_dir": "/opt/bpmsoft/client-uat",
      "stand_dll": "BPMSoft.WebHost.dll",
      "dotnet": "dotnet",
      "stand_host": "127.0.0.1",
      "stand_port": 5000,
      "db_type": "postgres",
      "db_host": "127.0.0.1",
      "db_port": 5432,
      "db_name": "client_uat",
      "secret_ref_db": "standkit:client-uat:db"
    }
  }
}
```

> На стороне агента стенд остаётся `transport: "local"` — агент поднимает его локально
> у себя. Удалённым (`transport: "agent"`) он становится только в реестре **оператора** —
> см. [Подключение из дашборда](#подключение-из-дашборда).

**Какой адрес писать в `stand_host`/`stand_port`.** Агент работает на хосте, вне
контейнера, поэтому это адрес **обращения к стенду с хоста**, а не адрес слушания:
`0.0.0.0` даёт ложный `http: down`. Для Docker порт берётся из
`docker ps --format '{{.Names}}\t{{.Ports}}'` — в `127.0.0.1:5010->5002/tcp` нужен **левый**
(5010); строка `5000/tcp` без стрелки означает, что порт наружу не опубликован. Внутренний
IP контейнера (`172.17.x.x`) работать будет, но меняется при пересоздании — в реестр не
писать. То же правило для `db_host`/`db_port` и `redis_host`/`redis_port`.

**Агент читает реестр при старте** — после правки `projects.json` нужен рестарт службы.

**Стенд за TLS.** Схема health-пробы задаётся полем `stand_scheme` (`http` по умолчанию,
`https` для стенда за TLS); `verify_tls: false` отключает проверку цепочки сертификатов —
это нужно для типового дев-контура с self-signed. Дефолты повторяют прежнее поведение,
старые реестры править не требуется. По той же схеме дашборд строит ссылку «Открыть стенд».

### 3. Задать токен(ы) агента как секрет

Токены не передаются в командной строке открытым текстом — только ссылкой на секрет
(Secret-first). Задайте control-токен (и, при желании, отдельный readonly):

```bash
# сгенерировать надёжный токен:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Дальше значение нужно положить туда, откуда его возьмёт `standkit.secrets`. Порядок
разрешения — переменная окружения → keyring → явный фолбэк.

**Служба на headless-хосте — переменная окружения** (keyring под сервисной учётной записью
без сессии обычно недоступен). Имя переменной = `STANDKIT_SECRET__` + ссылка на секрет
в верхнем регистре, где всё, кроме букв и цифр, заменено на `_`:

```bash
# /etc/standkit/agent.env — chmod 600, владелец standkit; подключается в unit через EnvironmentFile
STANDKIT_SECRET__STANDKIT_CLIENT_UAT_AGENT_TOKEN=<control-токен>
STANDKIT_SECRET__STANDKIT_CLIENT_UAT_AGENT_READONLY_TOKEN=<readonly-токен>
```

**Машина с рабочим keyring** (нужно дополнение `standkit[secrets]`) — значение вводится
из stdin, чтобы не попасть в историю команд:

```bash
python -c "import getpass; from standkit.secrets import set_secret; \
    set_secret('standkit:client-uat:agent-token', getpass.getpass('Токен: '))"
```

> ⚠️ Отдельного CLI (`python -m standkit.secrets set …`) в пакете **нет** — модуль без
> `__main__`, такая команда молча завершается с кодом 0 и ничего не задаёт. До 18.08.2026
> этот раздел ошибочно предлагал именно её. Статус CLI-обёртки — [BACKLOG.md](BACKLOG.md),
> раздел «Секреты».

### 4. Выпустить сертификаты (для прод/удалённого доступа)

Для доступа по сети нужен серверный TLS-сертификат агента и, крайне желательно, mTLS —
CA, которым подписаны клиентские сертификаты дашборда. Выпуск сертификатов — задача вашей
PKI/оператора; агент только потребляет готовые PEM. Минимальный самоподписанный вариант
для закрытого контура:

```bash
# серверный сертификат агента (CN = имя хоста, по которому к нему ходит дашборд)
openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
    -keyout agent.key -out agent.crt -subj "/CN=client-host"
# CA для клиентских сертификатов (mTLS) — по нему агент проверяет дашборд
openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
    -keyout clients-ca.key -out clients-ca.crt -subj "/CN=BPMkitStand clients CA"
```

### 5. Запустить агента

`--token-ref` обязателен: без него argparse откажет ещё до старта. Каталоги по
умолчанию — `~/.standkit/run`, `~/.standkit/logs`, `~/.standkit/audit.log`; у службы свой
`$HOME`, поэтому `--run-dir`/`--log-dir`/`--audit-log` задавайте явно.

**Dev / закрытый loopback (без сети):**

```bash
python -m standkit_agent \
    --registry ./projects.json \
    --token-ref standkit:client-uat:agent-token
```

**Прод / удалённый доступ (TLS + mTLS обязательны):**

```bash
python -m standkit_agent --host 0.0.0.0 --port 8765 \
    --registry /opt/standkit/projects.json \
    --token-ref standkit:client-uat:agent-token \
    --readonly-token-ref standkit:client-uat:agent-readonly-token \
    --tls-cert /etc/standkit/agent.crt \
    --tls-key /etc/standkit/agent.key \
    --tls-client-ca /etc/standkit/clients-ca.crt \
    --audit-log /var/log/standkit/audit.log
```

Все параметры — `python -m standkit_agent --help`.

### 6. Оформить как службу (чтобы жил после перезагрузки)

- **Linux (systemd):** готовый least-privilege юнит —
  [`standkit_agent/deploy/standkit-agent.service`](../standkit_agent/deploy/standkit-agent.service).
- **Windows (служба):** инструкция —
  [`standkit_agent/deploy/windows-service.md`](../standkit_agent/deploy/windows-service.md).

---

## Подключение из дашборда

На **машине оператора** в реестре `projects.json` объявите стенд удалённым — `transport: "agent"`
плюс адрес агента и ссылка на секрет с его токеном:

```json
{
  "projects": {
    "client-uat": {
      "transport": "agent",
      "agent_url": "https://client-host:8765",
      "agent_secret_ref": "standkit:client-uat:agent-token",

      "stand_dir": "/opt/bpmsoft/client-uat",
      "stand_port": 5000,
      "db_type": "postgres",
      "db_host": "db-host",
      "db_port": 5432,
      "db_name": "client_uat"
    }
  }
}
```

Затем на стороне оператора задайте секрет с тем же токеном, что и на агенте, — кнопкой
«Задать секрет…» в «Настройках» дашборда либо тем же однострочником:

```bash
python -c "import getpass; from standkit.secrets import set_secret; \
    set_secret('standkit:client-uat:agent-token', getpass.getpass('Токен: '))"
```

Запустите дашборд — удалённый стенд появится в общем списке рядом с локальными, в колонке
**«Транспорт»** будет `agent`. Кнопки старт/стоп/рестарт, статус и логи работают так же, как
для локальных стендов — запросы прозрачно уходят к агенту.

```bash
python -m standkit_hub
```

---

## Проверка и траблшутинг

Проверить агента напрямую (readonly-токеном, чтобы ничего не запустить):

```bash
curl --cacert agent.crt \
     --cert client.crt --key client.key \
     -H "Authorization: Bearer <readonly-token>" \
     https://client-host:8765/stands
```

| Симптом | Вероятная причина | Что сделать |
|---------|-------------------|-------------|
| Агент не стартует, пишет про fail-closed | non-loopback host без TLS | добавить `--tls-cert/--tls-key` (или `--insecure` только для dev) |
| `401 unauthorized` | токен на операторе ≠ токену на агенте | сверить секрет по одному `*-ref` с обеих сторон |
| `403 forbidden: insufficient scope` | использован readonly-токен для управления | использовать control-токен (`--token-ref`) |
| `429 too many failed attempts` | сработал lockout по IP | подождать окно `--lockout-window`, проверить токен |
| В дашборде стенд с ошибкой связи | агент недоступен/таймаут/сертификат | проверить сеть/файрвол, срок и CN сертификата |
| `the following arguments are required: --token-ref` | ключ обязателен всегда | добавить `--token-ref standkit:<стенд>:agent-token` |
| `error: externally-managed-environment` | PEP 668 на свежих Ubuntu/Debian | ставить через pipx или venv, `--break-system-packages` на рабочем хосте не использовать |
| `No module named 'standkit_agent'` в скрипте | запущен системный `python3`, а пакет в venv/pipx | звать питон окружения (`/opt/standkit/venv/bin/python`) |
| `/etc/standkit/agent.env: Permission denied` | файл 600 под root, служба под другим пользователем | `chown` на пользователя службы |
| `SecretError` при верном `ref` | забыт префикс `STANDKIT_SECRET__` (или лишний `_` от `echo … | tr`) | сверить имя по формуле, использовать `printf '%s'` |
| `RegistryError: Стенд не найден` | регистрация не выполнялась или ушла в другой файл | задавать `--registry` явным абсолютным путём |
| `409 adopt_required` на stop/restart | стенд поднят вне диспетчера, согласия не было | повторить с `?force=1` либо `POST /stand/<имя>/adopt` — это штатный отказ, не ошибка |
| `http: down` при живом стенде | `stand_host=0.0.0.0`, неопубликованный порт — или стенд за TLS без `stand_scheme` | взять адрес обращения с хоста; для TLS — `"stand_scheme": "https"` и, при self-signed сертификате, `"verify_tls": false` |
| Статус не меняется после правки реестра | агент читает реестр при старте | перезапустить службу агента |
| PowerShell: `ParameterBindingException` на `curl` | `curl` там алиас `Invoke-WebRequest` | `curl.exe -s -H "Authorization: Bearer …"` или `Invoke-RestMethod -Headers @{Authorization="Bearer $tok"}` |
| TLS-хендшейк отклонён | нет/невалиден клиентский сертификат (mTLS) | выпустить клиентский сертификат из того же CA (`--tls-client-ca`) |

Аудит-лог агента (`--audit-log`) — append-only JSON-lines: `ts`, `src_ip`, `identity`,
`method`, `path`, `action`, `result`, `code`. Токены и секреты в него не попадают.

---

## Ограничения и планы

Осознанно **пока не реализовано** (на карте развития):

- Полноценная PKI: выпуск и ротация сертификатов — сейчас забота оператора, агент лишь
  потребляет готовые PEM.
- Per-stand ACL: скоуп бинарный (control видит и управляет **всеми** стендами реестра агента);
  разбивки «этому токену — только стенд X» пока нет.
- Ротация токенов «на лету» без рестарта агента.
- CN клиентского сертификата → скоуп напрямую (сейчас скоуп определяется только Bearer-токеном;
  CN используется лишь для идентичности в аудите).
- Параллельный опрос агентов дашбордом (сейчас — последовательный; при большом числе агентов
  и таймаутах масштабируется хуже).
- Транспорты `ssh` / `winrm` — допускаются схемой реестра, но логика не реализована.
- `host_kind` (kestrel/iis/docker — как стенд ХОСТИТСЯ, ортогонально транспорту выше) —
  см. [docs/HOSTING.md](HOSTING.md); `host_kind=k8s` — задел, логика не реализована.

Автообновление MCP BPMkit и контроль лицензии на удалённых контурах — в **Companion-версии**;
см. [bpmkit.pro](https://bpmkit.pro).
