# Кукбук: агент standkit на Linux

Пошаговое развёртывание `standkit_agent` на Linux-хосте стенда — от чистой
системы до службы `systemd` с TLS. Каждая команда и каждое сообщение об ошибке
в этом документе воспроизведены на живом контуре, а не составлены по памяти.

**Проверено:** Ubuntu 24.04.4 LTS, Python 3.12.3, standkit 0.7.0, 18.08.2026.
Стенды: контейнер Docker за HTTPS с самоподписанным сертификатом и процесс
Kestrel (`dotnet <dll>`) — оба под управлением одного агента.

Дальше по тексту стенды называются `stand-a` (Docker) и `stand-b` (Kestrel),
хост — `example-host`.

> 💡 Тексты отказов и поля реестра ниже приведены к поведению **0.8.0**:
> preflight рабочих каталогов агента
> ([7.3](#73-отказы-старта-службы-что-вы-увидите-и-что-задать)), `logs_dir`,
> `redis_host`/`redis_port` ([3.2](#32-обязательные-поля)),
> `agent_ca`/`agent_verify_tls`
> ([10.2](#102-самоподписанный-сертификат-агента-и-дашборд)). На 0.7.0 те же
> ситуации выглядят иначе — отличия отмечены по месту.

Если вы ищете общее описание удалённых стендов и модель безопасности —
[REMOTE_STANDS.md](REMOTE_STANDS.md). Здесь только Linux и только практика.

---

## Что вы получите в итоге

- агент, установленный в изолированное окружение, без порчи системного Python;
- реестр стендов по явному пути, не зависящий от `$HOME` служебного аккаунта;
- токены агента в файле окружения с правами `640`, а не в командной строке;
- служба `systemd`, которая переживает перезагрузку;
- TLS (и по желанию mTLS) на канале дашборд → агент;
- проверяемый чек-лист: что должно ответить `/stands` и `/stand/<имя>/status`.

Время: 20–30 минут на первый хост.

---

## Шаг 0. Что должно быть на хосте

```bash
# версия ОС и Python
cat /etc/os-release | head -2
python3 -V

# стенд, которым будет управлять агент, уже работает
docker ps --format '{{.Names}}\t{{.Ports}}'   # для стендов в Docker
ss -ltnp | grep -E ':(5000|5010|5020)'        # чем занят порт стенда
```

Требования: Python 3.9+ (в 24.04 — 3.12), сетевой доступ до PyPI (или
подготовленный wheel), права `sudo` для установки службы.

Отдельно проверьте, что вы понимаете **адрес обращения к стенду с хоста** —
именно он пойдёт в реестр (см. [Шаг 3](#шаг-3-регистрация-стенда)).

---

## Шаг 1. Установка пакета

### 1.1. Почему `pip install` не сработает

На чистой Ubuntu 24.04 `pip` не установлен вовсе:

```console
$ pip install standkit
bash: pip: command not found
```

Ставим и пробуем снова:

```console
$ sudo apt-get install -y python3-pip
$ pip install standkit
error: externally-managed-environment

× This environment is externally managed
╰─> To install Python packages system-wide, try apt install
    python3-xyz, where xyz is the package you are trying to
    install.
    ...
    it may be easiest to use pipx install xyz, which will manage a
    virtual environment for you. Make sure you have pipx installed.
...
hint: See PEP 668 for the detailed specification.
```

Это не поломка, а PEP 668: системный Python на Debian/Ubuntu защищён от
установки пакетов мимо `apt`. `pip install --user` упирается в то же самое.

**`--break-system-packages` на сервере не использовать.** На Debian/Ubuntu на
системном Python написан сам `apt` — сломав его, вы теряете пакетный менеджер.

### 1.2. Интерактивная работа: pipx

```console
$ sudo apt-get install -y pipx
$ pipx install standkit
  installed package standkit 0.7.0, installed using Python 3.12.3
  These apps are now globally available
    - standkit-agent
    - standkit-gui
    - standkit-hub
done! ✨ 🌟 ✨
```

Если pipx предупредил, что `~/.local/bin` не в `PATH`:

```console
⚠️  Note: '/home/ubuntu/.local/bin' is not on your PATH environment variable.
    These apps will not be globally accessible until your PATH is updated. Run
    `pipx ensurepath` to automatically add it...
```

выполните `pipx ensurepath` и перелогиньтесь.

Нюанс, из-за которого команду «то видно, то нет»: `~/.profile` в Ubuntu
добавляет `~/.local/bin` в `PATH` **только для login-оболочки**. В обычном
`bash -c`, в скриптах cron и в юнитах systemd этого пути нет:

```console
$ bash -c 'standkit-agent --help'
bash: standkit-agent: command not found
```

Поэтому в службах и скриптах всегда пишите абсолютный путь.

Python установленного окружения (нужен для скриптов регистрации стендов):

```
~/.local/share/pipx/venvs/standkit/bin/python
```

Системный `python3` пакет не видит — это отдельная типовая ошибка:

```console
$ python3 -c 'import standkit'
ModuleNotFoundError: No module named 'standkit'
```

### 1.3. Для службы: отдельный venv в /opt

Служба работает под сервисным аккаунтом, у которого нет вашего `~/.local`.
Ставим агента в общесистемный venv:

```console
$ sudo python3 -m venv /opt/standkit/venv
$ sudo /opt/standkit/venv/bin/pip install --upgrade pip
$ sudo /opt/standkit/venv/bin/pip install standkit
Successfully installed standkit-0.7.0
$ /opt/standkit/venv/bin/python -c "import standkit; print(standkit.__version__)"
0.7.0
```

Пакет — stdlib-only, зависимостей нет, установка занимает секунды.
Опциональный extra: `pipx inject standkit keyring` (см.
[Секреты](#шаг-4-секреты-агента)).

Обновление и откат — тоже из PyPI, других источников установки не требуется:

| Задача | Команда |
|---|---|
| посмотреть доступные версии | `pip index versions standkit` |
| обновить до последней | `sudo /opt/standkit/venv/bin/pip install -U standkit` |
| зафиксировать конкретную версию | `sudo /opt/standkit/venv/bin/pip install "standkit==<версия>"` |
| откатиться на предыдущую | `sudo /opt/standkit/venv/bin/pip install "standkit==0.7.0"` |
| то же для pipx-окружения | `pipx upgrade standkit` или `pipx install --force "standkit==0.7.0"` |

После обновления агента — `sudo systemctl restart standkit-agent`: реестр и
конфигурация читаются один раз при старте (см.
[9.3](#93-реестр-читается-при-старте)).

---

## Шаг 2. Реестр стендов

Агент управляет теми стендами, которые перечислены в его реестре
`projects.json`. На чистой машине файла нет — это нормально: пустой реестр
читается без ошибки, файл создаётся при первой записи.

Порядок автопоиска на Linux:

1. `$BPMSOFT_PROJECTS_FILE` (если файл существует);
2. `$XDG_CONFIG_HOME/BPMkit/projects.json` → `~/.config/BPMkit/projects.json`;
3. `./projects.json` в текущем каталоге.

**На сервере на автопоиск не полагайтесь.** У сервисного пользователя другой
`$HOME`, и агент молча уедет искать реестр не туда. Вот как это выглядит:

```console
$ sudo -u standkit /opt/standkit/venv/bin/python -m standkit_agent \
      --token-ref standkit:stand-a:agent-token
[standkit-agent] слушаю 127.0.0.1:8765 (tls=off),
    реестр=/home/standkit/.config/BPMkit/projects.json, стендов=0, readonly-токен=нет
```

`стендов=0` — единственный признак, что реестр не тот. Всегда указывайте путь
явно:

```bash
sudo install -d -m 750 /opt/standkit
# ... --registry /opt/standkit/projects.json
```

---

## Шаг 3. Регистрация стенда

CLI-команды регистрации у агента нет. Есть три способа: Python API (ниже),
ручная правка `projects.json` по образцу `projects.sample.json` и форма
«Зарегистрировать стенд» в дашборде (на headless-сервере неудобна — хаб
слушает loopback).

### 3.1. Развилка, на которой ошибаются: две записи в двух реестрах

Один и тот же стенд описан дважды, по-разному:

| Где | `transport` | Что означает |
|---|---|---|
| Реестр **на хосте стенда** (у агента) | `local` | агент управляет стендом на своей машине напрямую |
| Реестр **оператора** (рядом с дашбордом) | `agent` | дашборд ходит к стенду через агента: нужны `agent_url` и `agent_secret_ref` |

Если в реестре агента написать `transport=agent`, агент попытается сходить сам
к себе. Если в реестре оператора оставить `local` — дашборд будет искать
стенд на своей машине.

### 3.2. Обязательные поля

| Поле | Кому | Что писать |
|---|---|---|
| `name`, `stand_dir` | всем | имя записи и каталог стенда |
| `stand_host`, `stand_port` | всем | **адрес обращения к стенду с хоста** |
| `stand_scheme` | стендам за TLS | `https` (по умолчанию `http`) |
| `verify_tls` | self-signed | `false` (по умолчанию `true`) |
| `host_kind` | всем | `kestrel` \| `docker` \| `iis` \| `k8s` |
| `docker_container` | `host_kind=docker` | имя контейнера (или compose-пара) |
| `stand_dll`, `dotnet` | `host_kind=kestrel` | имя dll и путь к `dotnet` |
| `logs_dir` | по желанию | каталог логов, если он **вне** `stand_dir`; пусто — резолвится сам |
| `db_host`, `db_port` | по желанию | поверхностная проба БД |
| `redis_host`, `redis_port` | по желанию | поверхностная проба Redis; **только парой** |
| `secret_ref_db` | по желанию | ссылка на секрет, **не** `db_password` |

> 💡 С 0.8.0 из формы «Зарегистрировать стенд» задаются в том числе
> `stand_scheme`, `verify_tls`, `logs_dir` и `redis_host`/`redis_port`. Чего в
> форме нет — `stand_dll`, `dotnet`, `secret_ref_db`: их пишут в `projects.json`
> или через Python API, как в рецептах ниже. На headless-хосте стенда формы нет
> вовсе — там только реестр.

**`logs_dir` почти всегда оставляют пустым.** Пустое значение означает «найти
подкаталог `logs` внутри `stand_dir`», и имя сравнивается **без учёта
регистра** — это ровно тот пункт, на котором Linux отличается от Windows:

| Раскладка на диске | Найдётся при пустом `logs_dir` |
|---|---|
| `/opt/stands/stand-a/Logs/` | да — так пишет BPMSoft |
| `/opt/stands/stand-a/logs/` | да |
| `/var/log/bpmsoft/stand-a/` | нет — только через явный `logs_dir` |

> ⚠️ До 0.8.0 каталог искался строго как `logs`. На Windows регистронезависимая
> ФС это прощала, а на Linux дашборд сообщал «каталог не найден» при живых
> логах в `Logs`. Если вы видите такое сообщение — сначала обновите пакет.

Явный `logs_dir` используется **как есть**, без досбора имени подкаталога: если
указанного каталога нет, логи считаются недоступными, и никакого «поищем рядом»
не происходит — иначе тихий фолбэк маскировал бы опечатку в реестре.

### 3.3. Рецепт: стенд в Docker за HTTPS

```bash
sudo install -d -o standkit -g standkit -m 750 /opt/standkit

/opt/standkit/venv/bin/python - <<'PY'
from pathlib import Path
from standkit.models import Stand, Transport, HostKind
from standkit.registry import Registry

reg = Registry.load(Path("/opt/standkit/projects.json"))
reg.add_existing(Stand(
    name="stand-a",
    transport=Transport.LOCAL,
    host_kind=HostKind.DOCKER,
    docker_container="stand-a",
    stand_dir="/opt/stands/stand-a",
    stand_host="127.0.0.1",
    stand_port=5010,          # ЛЕВЫЙ порт из docker ps, см. ниже
    stand_scheme="https",     # стенд за TLS
    verify_tls=False,         # сертификат самоподписанный
    # logs_dir не задан: подкаталог logs/Logs найдётся внутри stand_dir сам
    db_type="postgres", db_host="127.0.0.1", db_port=5432,
    db_name="stand_a", db_user="postgres",
    secret_ref_db="standkit:stand-a:db",
    redis_host="127.0.0.1",   # адрес С ТОЧКИ ЗРЕНИЯ ХОСТА АГЕНТА
    redis_port=6379,          # задавать только парой, см. 3.5
), make_default=True)
reg.save()
print("зарегистрирован:", reg.names())
PY
```

Запускать этим python, не системным (см. [1.2](#12-интерактивная-работа-pipx)).

`stand_scheme="https"` и `verify_tls=False` — то, без чего проба стенда за TLS
уходит на `http://` и честно отдаёт `down` при живом стенде. С 0.8.0 обе
настройки есть и в форме регистрации дашборда (селектор «Схема» и чекбокс
«Проверять сертификат», который показывается только при `https`), так что
скрипт выше нужен именно для headless-хоста.

> ⚠️ `verify_tls: false` — послабление для дев-контуров с самоподписанным
> сертификатом, а не режим по умолчанию для прод-стендов.

### 3.4. Рецепт: стенд Kestrel

```python
Stand(
    name="stand-b",
    transport=Transport.LOCAL,
    host_kind=HostKind.KESTREL,
    stand_dir="/opt/stands/stand-b",
    stand_dll="BPMSoft.WebHost.dll",
    dotnet="dotnet",
    stand_host="127.0.0.1",
    stand_port=5020,
    stand_scheme="https",
    verify_tls=False,
    logs_dir="/var/log/bpmsoft/stand-b",   # только если логи ВНЕ stand_dir
    redis_host="127.0.0.1",
    redis_port=6379,
)
```

Каталог стенда и его файлы должны быть доступны сервисному пользователю
агента: именно он будет запускать `dotnet <dll>` и писать pid-файл. Это же
касается каталога логов: `logs_dir` (или подкаталог `Logs` внутри `stand_dir`)
читается тем же пользователем.

### 3.5. Docker: какой адрес писать в реестр

Агент работает **на хосте, вне контейнера**, поэтому в `stand_host` /
`stand_port` идёт адрес обращения с хоста — не адрес bind и не внутренний IP
контейнера.

```console
$ docker ps --format '{{.Names}}\t{{.Ports}}'
stand-a	8080/tcp, 127.0.0.1:5011->80/tcp, 127.0.0.1:5010->443/tcp
```

- `127.0.0.1:5010->443/tcp` — нужен **левый** порт: `5010`;
- `8080/tcp` без стрелки — порт объявлен, но наружу не опубликован; проба по
  нему честно даст `down`;
- внутренний IP контейнера (`172.17.x.x`) работает, но меняется при
  пересоздании — в реестр не писать;
- `stand_host="0.0.0.0"` — писать нельзя. На Linux такая проба может случайно
  сработать (ядро трактует `0.0.0.0` как локальный адрес), но на Windows-агенте
  и при любой удалённой проверке это сломается.

Те же правила — для `db_host`/`db_port` и `redis_host`/`redis_port`: адрес
берётся **с точки зрения хоста, где выполняется проба**, то есть хоста агента,
а не машины оператора.

| Что в записи стенда | Колонка «Redis» в дашборде |
|---|---|
| пусто | `unknown`, подсказка «адрес Redis не задан в реестре (redis_host/redis_port)» |
| адрес задан, порт слушает | `ok` |
| Redis в compose без публикации порта наружу | `down` — с хоста агента его не видно |
| задан только `redis_host` | ошибка записи: «redis_host задан без корректного redis_port (1–65535)» |
| задан только `redis_port` | ошибка записи: «redis_port задан без redis_host» |

> 💡 `redis_host`/`redis_port` — нормальные поля записи с 0.8.0 (раньше это были
> свободные ключи, которых не было ни в форме, ни в модели). Старые реестры
> продолжают работать: чтение из свободных ключей сохранено как фолбэк.

---

## Шаг 4. Секреты агента

### 4.1. Формула имени переменной

```
STANDKIT_SECRET__ + ref, где все символы кроме [A-Za-z0-9] → "_", всё в UPPER
```

| `ref` в команде | Переменная окружения |
|---|---|
| `standkit:stand-a:agent-token` | `STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN` |
| `standkit:stand-a:agent-ro-token` | `STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_RO_TOKEN` |
| `standkit:stand-a:db` | `STANDKIT_SECRET__STANDKIT_STAND_A_DB` |

Имя можно не считать вручную: агент печатает его сам, если секрет не найден.

```console
$ standkit-agent --registry /opt/standkit/projects.json \
      --token-ref standkit:stand-a:agent-token
[standkit-agent] ОШИБКА: не удалось получить секрет control-токена агента
('standkit:stand-a:agent-token'): Секрет 'standkit:stand-a:agent-token' не найден
ни в переменной окружения STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN,
ни в keyring, ни в переданном fallback
```

Порядок резолва: переменная окружения → keyring → фолбэк из реестра.

### 4.2. keyring на headless-сервере не работает

Это ожидаемо: без сессии рабочего стола бэкенда нет.

```console
$ python -c "import keyring; print(keyring.get_keyring())"
keyring.backends.fail.Keyring (priority: 0)
$ python -c "import keyring; keyring.set_password('standkit','probe','x')"
NoKeyringError: No recommended backend was available...
```

**На сервере основной путь — переменные окружения.** keyring — вариант для
десктопа оператора.

### 4.3. Файл с токенами

```bash
sudo install -d -m 755 /etc/standkit
sudo tee /etc/standkit/agent.env >/dev/null <<ENV
STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_RO_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ENV
sudo chown root:standkit /etc/standkit/agent.env
sudo chmod 640 /etc/standkit/agent.env
```

Почему `root:standkit` и `640`, а не `600` под `root`: systemd читает
`EnvironmentFile=` от root и с `600` тоже стартует, но любой ручной запуск от
сервисного аккаунта — например при отладке — молча провалится:

```console
$ sudo -u standkit bash -c '. /etc/standkit/agent.env'
cat: /etc/standkit/agent.env: Permission denied
```

и агент упадёт на «секрет не найден», хотя файл заполнен верно.

---

## Шаг 5. Первый запуск вручную

Проверочный запуск — на loopback, без TLS:

```bash
set -a; . /etc/standkit/agent.env; set +a
standkit-agent \
  --registry /opt/standkit/projects.json \
  --token-ref standkit:stand-a:agent-token \
  --readonly-token-ref standkit:stand-a:agent-ro-token \
  --run-dir /opt/standkit/run \
  --log-dir /opt/standkit/logs \
  --audit-log /opt/standkit/logs/audit.log
```

```console
[standkit-agent] слушаю 127.0.0.1:8765 (tls=off), реестр=/opt/standkit/projects.json,
    стендов=2, readonly-токен=да
```

`--token-ref` обязателен всегда:

```console
$ standkit-agent --registry /opt/standkit/projects.json
standkit-agent: error: the following arguments are required: --token-ref
```

Слушать не-loopback адрес без TLS агент откажется — это защита, а не ошибка:

```console
$ standkit-agent --host 0.0.0.0 --port 8443 ... --token-ref ...
[standkit-agent] Отказ старта: host='0.0.0.0' не loopback, TLS не настроен
(--tls-cert/--tls-key) — headless-агент управляет процессами на хосте стенда
(RCE-поверхность), открытый HTTP наружу по умолчанию запрещён (fail-closed).
Варианты: (1) слушать loopback (127.0.0.1) за управляющим контуром/VPN/SSH-туннелем;
(2) настроить TLS/mTLS (--tls-cert/--tls-key[/--tls-client-ca]);
(3) если это осознанный dev-сценарий — передать --insecure...
```

С `--insecure` он стартует, но предупреждает в полный голос — для постоянной
эксплуатации это не вариант:

```console
[standkit-agent] !!! ВНИМАНИЕ: --insecure — агент слушает 0.0.0.0:8443 ОТКРЫТЫМ HTTP
без TLS на non-loopback адресе. Это RCE-поверхность (start/stop/restart процессов
стенда) без шифрования и без аутентификации транспорта. НЕ использовать в
проде/недоверенной сети.
```

---

## Шаг 6. Проверка API

Эндпоинта `/health` у агента **нет** — не ищите. Все маршруты требуют
`Authorization: Bearer <токен>`.

| Метод | Путь | Скоуп |
|---|---|---|
| GET | `/stands` | любой |
| GET | `/stand/<имя>/status` | любой |
| GET | `/stand/<имя>/logs?n=100` | любой |
| POST | `/stand/<имя>/start` | control |
| POST | `/stand/<имя>/stop[?force=1]` | control |
| POST | `/stand/<имя>/restart[?force=1]` | control |
| POST | `/stand/<имя>/adopt?force=1` | control |

```console
$ TOKEN=$(sudo grep AGENT_TOKEN= /etc/standkit/agent.env | cut -d= -f2)
$ curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/stands
{"stands": ["stand-a", "stand-b"]}

$ curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/stand/stand-a/status
{"name": "stand-a", "process": "ok", "http": "ok", "db": "ok", "redis": "down",
 "last_deploy": "unknown",
 "details": {"redis_reason": "Redis не отвечает на 127.0.0.1:6379 (адрес проверяется
              с хоста, где выполняется проба)"}}
```

`details` — не декоративное поле и далеко не всегда пустое: туда по ключу
`<проба>_reason` попадает причина отказа каждой пробы, которой есть что
сказать (`http_reason`, `redis_reason`). Пусто он бывает, только когда
объяснять нечего — все пробы `ok`. Лежачий стенд выглядит так, и причина
отказа HTTP приходит вместе с URL, по которому стучались (чаще всего ошибка
именно в нём — не та схема или не тот порт):

```console
$ curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/stand/stand-b/status
{"name": "stand-b", "process": "down", "http": "down", "db": "unknown", "redis": "down",
 "last_deploy": "unknown",
 "details": {"http_reason": "соединение отклонено — на 127.0.0.1:5020 никто не слушает
              (URL: https://127.0.0.1:5020/)",
             "redis_reason": "Redis не отвечает на 127.0.0.1:6379 (адрес проверяется
              с хоста, где выполняется проба)"}}
```

> Переносы строк внутри значений добавлены здесь для ширины страницы — `curl`
> отдаёт JSON одной строкой. Стенды в примерах — те же, что зарегистрированы
> в [3.3](#33-рецепт-стенд-в-docker-за-https)/[3.4](#34-рецепт-стенд-kestrel)
> (`redis_host`/`redis_port` заданы, поэтому вместо «адрес Redis не задан в
> реестре» приходит «не отвечает»).

Ожидаемые отказы — проверьте, что они именно такие:

```console
$ curl -s http://127.0.0.1:8765/stands                       # без заголовка
HTTP/1.0 401 Unauthorized
$ curl -s -H "Authorization: Bearer wrong" .../stands
{"error": "unauthorized"}
$ curl -s -X POST -H "Authorization: Bearer $RO_TOKEN" .../stand/stand-a/stop
{"error": "forbidden: insufficient scope"}
$ curl -s -H "Authorization: Bearer $TOKEN" .../stand/no-such-stand/status
HTTP/1.0 404 Not Found
{"error": "Стенд 'no-such-stand' не найден в реестре /opt/standkit/projects.json"}
$ curl -s -H "Authorization: Bearer $TOKEN" .../health
HTTP/1.0 404 Not Found
{"error": "not found"}
```

### 6.1. Управление стендом

```console
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" .../stand/stand-b/start
{"ok": true, "pid": 145}
$ curl -s -H "Authorization: Bearer $TOKEN" ".../stand/stand-b/logs?n=6"
{"lines": ["info: Microsoft.Hosting.Lifetime[0]", "      Application started...",
           "      Content root path: /opt/stands/stand-b"]}
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" .../stand/stand-b/stop
{"ok": true}
```

Для стендов в Docker `pid` равен `null` — объект управления это контейнер, а не
процесс; `logs` возвращает то, что контейнер пишет в stdout (у стенда,
логирующего в файл, список будет пустым).

### 6.2. 409 `adopt_required` — это фича

Стенд, поднятый мимо диспетчера (руками, чужим скриптом, юнитом systemd),
агент не убивает молча:

```console
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" .../stand/stand-b/stop
HTTP/1.0 409 Conflict
{
  "error": "стенд 'stand-b' запущен вне диспетчера. Найден процесс PID 4857, dotnet,
            /opt/stands/stand-b. Требуется подтверждение, чтобы взять его под
            управление и остановить.",
  "adopt_required": true,
  "candidate": {"pid": 4857, "port": 5020, "image": "dotnet",
                "exe_path": "/usr/lib/dotnet/dotnet", "cwd": "/opt/stands/stand-b",
                "cmdline": "dotnet BPMSoft.WebHost.dll",
                "matched_by": "/opt/stands/stand-b"}
}
```

Проверьте кандидата (это точно ваш стенд?) и подтвердите:

```console
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" ".../stand/stand-b/adopt?force=1"
{"ok": true, "candidate": {"pid": 4857, ...}}
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" .../stand/stand-b/stop
{"ok": true}
```

После усыновления агент пишет pid-файл, и стенд для него ничем не отличается от
запущенного им самим. Для Docker-стендов усыновление не требуется: объект
управления — именованный контейнер.

---

## Шаг 7. Служба systemd

### 7.1. Сервисный аккаунт и каталоги

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin standkit
sudo install -d -o standkit -g standkit -m 750 /opt/standkit /opt/standkit/run /opt/standkit/logs
sudo chown standkit:standkit /opt/standkit/projects.json
sudo chown -R standkit:standkit /opt/stands          # каталоги стендов
sudo usermod -aG docker standkit                     # только для host_kind=docker
```

Про `usermod -aG docker`: членство в группе `docker` эквивалентно root на
хосте. Если это неприемлемо — используйте отдельного пользователя только под
Docker-стенды или откажитесь от управления контейнерами через агента.

### 7.2. Юнит

```ini
[Unit]
Description=standkit agent
After=network.target

[Service]
Type=simple
User=standkit
Group=standkit
WorkingDirectory=/opt/standkit
EnvironmentFile=/etc/standkit/agent.env
ExecStart=/opt/standkit/venv/bin/python -m standkit_agent \
    --host 127.0.0.1 --port 8765 \
    --registry /opt/standkit/projects.json \
    --token-ref standkit:stand-a:agent-token \
    --readonly-token-ref standkit:stand-a:agent-ro-token \
    --run-dir /opt/standkit/run --log-dir /opt/standkit/logs \
    --audit-log /opt/standkit/logs/audit.log
Restart=on-failure
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/opt/standkit/run /opt/standkit/logs /opt/standkit/projects.json

[Install]
WantedBy=multi-user.target
```

Полный вариант с максимальным набором ограничений systemd —
`standkit_agent/deploy/standkit-agent.service` в репозитории.

```bash
sudo cp standkit-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now standkit-agent
systemctl status standkit-agent --no-pager
```

### 7.3. Отказы старта службы: что вы увидите и что задать

Рабочие каталоги агента (`--run-dir`, `--log-dir` и каталог файла
`--audit-log`) проверяются **до** открытия сокета и **до** строки «слушаю …».
Непригодный путь — это одна строка в `stderr`, код возврата `1` и никакого
traceback.

Так же устроен и занятый порт: строка «слушаю …» печатается ПОСЛЕ фактической
привязки сокета, а не перед попыткой. Практический вывод для чтения журнала:
есть «слушаю …» — агент встал на порт и принимает запросы; нет её — старт не
состоялся, диагноз в строке выше (это верно для всех отказов старта: небезопасный
bind, непригодные каталоги, занятый порт, нет прав на порт).

**Каталог логов недоступен на запись.** Типовая причина — каталоги остались от
ручных запусков под вашим пользователем.

```console
$ sudo journalctl -u standkit-agent -n 3 --no-pager
python[5097]: [standkit-agent] Отказ старта: каталог логов /opt/standkit/logs
    недоступен на запись пользователю standkit — задайте --log-dir или выдайте права
systemd[1]: standkit-agent.service: Main process exited, code=exited, status=1/FAILURE
```

Лечение: `sudo chown -R standkit:standkit /opt/standkit/run /opt/standkit/logs`.

Остальные пути формулируются так же, меняется только название: «каталог
pid-файлов …», «каталог аудит-лога …». Отдельная ветка — сам файл аудита,
созданный другим пользователем при писучем каталоге:

```console
[standkit-agent] Отказ старта: аудит-лог /opt/standkit/logs/audit.log недоступен
на запись пользователю standkit — задайте --audit-log или выдайте права
```

Если каталога ещё нет и создать его нельзя — в тексте назван тот предок, в
который не пускают:

```console
[standkit-agent] Отказ старта: каталог pid-файлов /opt/standkit/run не существует
и не может быть создан пользователем standkit: нет прав на запись в /opt/standkit
(Permission denied) — задайте --run-dir или выдайте права
```

**У сервисного аккаунта нет `$HOME`.** Аккаунт создан с `--no-create-home`, а
хотя бы один из трёх путей не задан флагом:

```console
[standkit-agent] Отказ старта: домашний каталог /home/standkit недоступен
(не существует или не каталог), поэтому дефолты ~/.standkit/run, ~/.standkit/logs
и ~/.standkit/audit.log нерабочие — при запуске под сервисным аккаунтом
(useradd --no-create-home, systemd) задайте все три пути явно: --run-dir,
--log-dir и --audit-log
```

> ⚠️ **Все три пути задаются явно — это условие служебного запуска, а не
> стилистика.** По умолчанию агент берёт `~/.standkit/run`, `~/.standkit/logs`
> и `~/.standkit/audit.log`. У аккаунта из
> [7.1](#71-сервисный-аккаунт-и-каталоги) домашнего каталога нет вовсе, а
> `ProtectHome=yes` в юните закрывает `/home` даже там, где он есть. Пропустили
> один флаг — `$HOME` снова участвует в резолве, и старт снова заведомо ломается.
>
> Значения `--run-dir`, `--log-dir` и `--audit-log` обязаны совпадать с тем, что
> перечислено в `ReadWritePaths=`: при `ProtectSystem=strict` путь вне этого
> списка для процесса read-only, и preflight честно откажет в старте.

**Порт занят** — чаще всего ранее запущенным вручную агентом, который забыли
остановить:

```console
[standkit-agent] Отказ старта: не удалось привязаться к 127.0.0.1:8765 — порт уже
занят другим процессом (возможно, агент уже запущен); освободите порт или задайте
другой через --port
```

```bash
ss -ltnp | grep 8765
pkill -f 'standkit_agent'      # если это ваш ручной запуск
```

**`status=200/CHDIR`** — отказ уровня systemd: агент даже не запускался,
сервисный пользователь не может войти в `WorkingDirectory`.

```console
Active: activating (auto-restart) (Result: exit-code)
Main PID: 5056 (code=exited, status=200/CHDIR)
...
(python)[5056]: standkit-agent.service: Changing to the requested working directory
                failed: Permission denied
```

Лечение: `sudo chown standkit:standkit /opt/standkit`.

Сводно:

| Что в журнале | Что задать / поправить |
|---|---|
| `каталог pid-файлов … недоступен на запись пользователю …` | `--run-dir`, права, `ReadWritePaths` |
| `каталог логов … недоступен на запись пользователю …` | `--log-dir`, права, `ReadWritePaths` |
| `каталог аудит-лога …` / `аудит-лог … недоступен на запись` | `--audit-log`, права на сам файл |
| `… не может быть создан … нет прав на запись в <предок>` | права на названный предок либо другой путь во флаге |
| `домашний каталог … нерабочие` | все три флага сразу: `--run-dir`, `--log-dir`, `--audit-log` |
| `порт уже занят другим процессом` | освободить порт или взять другой `--port` |
| `нет прав на привязку к этому порту` | `--port` из непривилегированного диапазона (≥ 1024) |
| `status=200/CHDIR` | права на `WorkingDirectory` — это ещё не агент |

> 💡 До 0.8.0 те же ситуации выглядели иначе: строка «слушаю …» печаталась
> раньше проверки путей, а падение приезжало сырым traceback
> (`PermissionError`, `OSError: [Errno 98] Address already in use`). Поэтому
> старые записи в журналах читаются как «стартовал и сразу упал», хотя сокет не
> открывался вовсе.

### 7.4. Проверка после перезагрузки

```console
$ systemctl is-enabled standkit-agent && systemctl is-active standkit-agent
enabled
active
```

Важно: агент при старте **не поднимает стенды сам**. После перезагрузки хоста
Docker-стенд с `restart: unless-stopped` вернётся сам, а Kestrel-стенд останется
`down`, пока его не запустят — через `POST /stand/<имя>/start` или собственным
юнитом стенда.

---

## Шаг 8. TLS и mTLS

### 8.1. Сертификаты

```bash
sudo install -d -o standkit -g standkit -m 750 /etc/standkit/tls
cd /tmp && mkdir tls && cd tls

# серверный сертификат агента: CN и SAN = имя/адрес, по которому к нему ходит дашборд
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout agent.key -out agent.crt \
  -subj "/CN=example-host" \
  -addext "subjectAltName=DNS:example-host,IP:10.0.0.10"

# CA для клиентских сертификатов (mTLS)
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout clients-ca.key -out clients-ca.crt -subj "/CN=standkit-clients-ca"

# клиентский сертификат дашборда
openssl req -nodes -newkey rsa:2048 -keyout dashboard.key -out dashboard.csr \
  -subj "/CN=dashboard-01"
openssl x509 -req -in dashboard.csr -CA clients-ca.crt -CAkey clients-ca.key \
  -CAcreateserial -out dashboard.crt -days 365

sudo cp agent.crt agent.key clients-ca.crt /etc/standkit/tls/
sudo chown -R standkit:standkit /etc/standkit/tls
sudo chmod 600 /etc/standkit/tls/agent.key
```

Мелочь, на которой легко споткнуться: `sudo chown standkit:standkit
/etc/standkit/tls/*` не сработает — звёздочку раскрывает ваша оболочка, у
которой нет прав на чтение каталога `750`. Используйте `chown -R` на каталог.

### 8.2. Запуск с TLS

Добавьте в `ExecStart`:

```
    --host 0.0.0.0 --port 8443 \
    --tls-cert /etc/standkit/tls/agent.crt \
    --tls-key  /etc/standkit/tls/agent.key \
    --tls-client-ca /etc/standkit/tls/clients-ca.crt
```

```console
[standkit-agent] слушаю 0.0.0.0:8443 (tls=on+mtls), реестр=..., стендов=2
```

Без `--tls-client-ca` в строке будет `tls=on` — шифрование есть, клиент не
проверяется.

### 8.3. Как это выглядит со стороны клиента

```console
# plain HTTP на TLS-порт — соединение рвётся
$ curl -sS http://10.0.0.10:8443/stands
curl: (56) Recv failure

# https без доверия к CA
$ curl -sS https://10.0.0.10:8443/stands
curl: (60) SSL certificate problem: self-signed certificate

# обращение по имени, которого нет в SAN
$ curl -sS --cacert agent.crt https://localhost:8443/stands
curl: (60) SSL: no alternative certificate subject name matches target host name 'localhost'

# mTLS: без клиентского сертификата
$ curl -sS --cacert agent.crt -H "Authorization: Bearer $TOKEN" https://10.0.0.10:8443/stands
curl: (56) OpenSSL SSL_read: ... tlsv13 alert certificate required, errno 0

# как надо
$ curl -sS --cacert agent.crt --cert dashboard.crt --key dashboard.key \
       -H "Authorization: Bearer $TOKEN" https://10.0.0.10:8443/stands
{"stands": ["stand-a", "stand-b"]}
```

Клиентский сертификат не заменяет токен: без `Authorization` вернётся `401`.
CN клиента при этом попадает в аудит-лог — по нему видно, кто стучался.

### 8.4. Сетевой периметр

```bash
sudo ufw allow from 10.0.0.0/24 to any port 8443 proto tcp
sudo ufw enable
sudo ufw status verbose
```

```console
Status: active
Default: deny (incoming), allow (outgoing), deny (routed)

To                         Action      From
--                         ------      ----
8443/tcp                   ALLOW IN    10.0.0.0/24
```

Loopback ufw не режет — локальные проверки продолжат работать.

---

## Шаг 9. Эксплуатация

### 9.1. Ротация токена

```bash
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
sudo sed -i "s|^STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN=.*|STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN=$NEW|" \
     /etc/standkit/agent.env
sudo systemctl restart standkit-agent
```

Старый токен перестаёт работать сразу (`401`), новый начинает (`200`). Не
забудьте обновить запись в реестре оператора.

### 9.2. Блокировка после подбора

Пять неудачных аутентификаций с одного IP → `429` на всё окно (по умолчанию
300 с), включая **верный** токен:

```console
попытка 1..5 -> HTTP 401
попытка 6    -> HTTP 429
верный токен -> HTTP 429
{"error": "too many failed attempts, try later"}
```

Счётчик живёт в памяти процесса: `sudo systemctl restart standkit-agent`
снимает блокировку немедленно. Пороги — `--lockout-max-failures`,
`--lockout-window`.

### 9.3. Реестр читается при старте

После любой правки `projects.json` нужен рестарт агента — иначе вы будете
чинить конфиг и не видеть эффекта.

Живой пример: контейнер пересоздали с другим портом.

```console
$ docker ps --format '{{.Names}}\t{{.Ports}}'
stand-a	127.0.0.1:5013->80/tcp, 127.0.0.1:5012->443/tcp

$ curl ... /stand/stand-a/status
{"process": "ok", "http": "down", ...}          # порт в реестре старый

# правим stand_port на 5012
$ curl ... /stand/stand-a/status
{"process": "ok", "http": "down", ...}          # без рестарта ничего не изменилось

$ sudo systemctl restart standkit-agent
$ curl ... /stand/stand-a/status
{"process": "ok", "http": "ok", ...}
```

### 9.4. Аудит

```console
$ sudo tail -2 /opt/standkit/logs/audit.log
{"ts": "2026-08-18T12:12:55+00:00", "src_ip": "127.0.0.1", "identity": "-",
 "method": "GET", "path": "/stands", "action": "stands", "result": "denied", "code": 401}
{"ts": "2026-08-18T12:13:05+00:00", "src_ip": "127.0.0.1", "identity": "control",
 "method": "GET", "path": "/stands", "action": "stands", "result": "ok", "code": 200}
```

Формат — JSON lines, дописывается append-only, токенов и секретов не содержит.
`identity` — скоуп (`control`/`readonly`), а при mTLS для отказов — CN клиента.
Ротацию настраивайте своим `logrotate`.

### 9.5. Диагностика

```bash
systemctl status standkit-agent --no-pager -l
sudo journalctl -u standkit-agent -n 50 --no-pager
sudo journalctl -u standkit-agent -f
ss -ltnp | grep 8765            # слушает ли агент
sudo tail -f /opt/standkit/logs/<имя-стенда>.log   # лог стенда, поднятого агентом
```

---

## Шаг 10. Подключение из дашборда оператора

До этого места агент проверялся с самого хоста. Теперь — со стороны оператора,
где работает дашборд.

### 10.1. Запись в реестре оператора

Тот же стенд описывается второй раз, уже с `transport=agent` (см.
[3.1](#31-развилка-на-которой-ошибаются-две-записи-в-двух-реестрах)):

```json
{
  "default": "stand-a",
  "projects": {
    "stand-a": {
      "transport": "agent",
      "agent_url": "https://example-host:8765",
      "agent_secret_ref": "standkit:stand-a:agent-token",
      "host_kind": "kestrel",
      "stand_dir": "/opt/stands/stand-a",
      "stand_host": "127.0.0.1",
      "stand_port": 5020,
      "stand_scheme": "https",
      "verify_tls": false
    }
  }
}
```

Токен агента на машине оператора задаётся так же, как на хосте — переменной
окружения по формуле из [4.1](#41-формула-имени-переменной):

```powershell
$env:STANDKIT_SECRET__STANDKIT_STAND_A_AGENT_TOKEN = "<токен>"
```

`stand_host` / `stand_port` в записи оператора не используются для похода к
стенду — пробы выполняет агент на своей стороне и возвращает готовый статус.

Если сертификат агента самоподписанный, к этой же записи добавляются `agent_ca`
и `agent_verify_tls` — см. [10.2](#102-самоподписанный-сертификат-агента-и-дашборд).

### 10.2. Самоподписанный сертификат агента и дашборд

Дашборд ходит к агенту со штатной проверкой цепочки сертификатов. Самоподписанный
сертификат агента системному хранилищу машины оператора неизвестен, поэтому
канал «хаб → агент» рвётся ещё до токена — стенд при этом жив, агент отвечает,
`curl --cacert agent.crt` с самого хоста работает.

Штатное решение — два поля записи реестра **оператора**:

| Поле | Значение | Смысл |
|---|---|---|
| `agent_ca` | путь к `agent.crt` (или CA-бандлу) **на машине дашборда** | чем доверять сертификату агента |
| `agent_verify_tls` | `true` по умолчанию | проверять ли цепочку вообще; `false` — только дев-контур |

Рецепт целиком:

1. скопировать `agent.crt` с хоста агента на машину оператора — публичную часть,
   ключ `agent.key` не копируется никогда;
2. прописать путь в `agent_ca` записи стенда: руками в `projects.json` либо в
   форме «Зарегистрировать стенд» (поле «Сертификат агента (agent_ca)»);
3. обновить страницу дашборда — статус приходит.

```json
{
  "transport": "agent",
  "agent_url": "https://example-host:8765",
  "agent_secret_ref": "standkit:stand-a:agent-token",
  "agent_ca": "C:\\standkit\\agent.crt",
  "agent_verify_tls": true
}
```

> ⚠️ **Главная путаница: `verify_tls` — это не про агента.**
> `stand_scheme`/`verify_tls` описывают health-пробу **самого стенда** (стук в
> web-хост BPMSoft), а `agent_ca`/`agent_verify_tls` — управляющее
> HTTPS-соединение **хаба с демоном агента**. Пути разные, сертификаты разные.
> Оператор, нашедший `verify_tls` и снявший его, для канала до агента не
> поменял ничего — и обоснованно недоумевает, почему всё равно
> `CERTIFICATE_VERIFY_FAILED`. В форме регистрации эти поля разведены по разным
> блокам именно поэтому.

Диагнозы клиента дашборда (текст виден в карточке стенда, целиком — с префиксом
`Ошибка обращения к агенту https://example-host:8765: …`):

| Текст ошибки | Что произошло | Что делать |
|---|---|---|
| `сертификат агента не доверенный (self-signed certificate) — укажите agent_ca (путь к сертификату агента) в записи стенда` | цепочка не проверяется системным хранилищем | указать `agent_ca` |
| `имя в сертификате агента не совпадает с адресом (Hostname mismatch, certificate is not valid for '10.0.0.10') — имя в agent_url должно совпадать с CN/SAN сертификата агента` | в `agent_url` адрес, которого нет в CN/SAN (типовое: сертификат на имя, а в реестре IP) | привести `agent_url` к CN/SAN либо перевыпустить сертификат с нужным SAN |
| `файл сертификата агента не найден: /etc/standkit/agent.crt (поле agent_ca записи стенда)` | путь указывает в пустоту — обычно скопирован с хоста агента | указать путь, существующий **на машине дашборда** |
| `не удалось прочитать сертификат агента … (поле agent_ca записи стенда): …` | файл есть, но это не PEM | скопировать `agent.crt` заново, целиком |

Валидация записи ловит и рассогласование: `agent_ca` при `agent_url` на `http://`
— это ошибка («agent_ca имеет смысл только при agent_url на https:// — по http
канал до агента не шифруется и сертификат не проверяется»), а не молча
проигнорированная настройка.

`agent_verify_tls: false` **приоритетнее** `agent_ca`: если проверка снята,
соединение состоится с любым сертификатом, и старая ссылка в `agent_ca` уже
ничего не значит. Для управляющего канала, по которому уезжает bearer-токен,
это плохой постоянный режим — только дев-контур.

Для постоянной эксплуатации правильный путь прежний: выпустить сертификат агента
доверенным в вашем контуре CA и импортировать этот CA на машины операторов —
тогда `agent_ca` не нужен вовсе.

После этого статус приходит нормально:

```console
{"name": "stand-a", "process": "ok", "http": "ok", "db": "ok", "redis": "down",
 "last_deploy": "unknown",
 "details": {"redis_reason": "Redis не отвечает на 127.0.0.1:6379 (адрес проверяется
              с хоста, где выполняется проба)"}}
```

> 💡 Исторический обходной путь для версий до 0.8.0, где полей агента ещё не
> было: переменная окружения `SSL_CERT_FILE` с путём к сертификату/CA,
> выставленная **до** запуска хаба (она читается при инициализации `ssl`).
> Рекомендацией это больше не является.

### 10.3. Версии хаба и агента могут различаться

Проверено на живом контуре: дашборд 0.6.1 на Windows корректно работает с
агентом 0.7.0 на Linux — контракт HTTP обратно совместим. Помните только, какая
сторона читает какое поле:

| Поля | Кто их обрабатывает | Что обновлять |
|---|---|---|
| `stand_scheme`, `verify_tls`, `logs_dir`, `redis_host`/`redis_port` | сторона, которая делает пробу, — агент | агент на хосте стенда |
| `agent_ca`, `agent_verify_tls` | клиент дашборда | хаб на машине оператора |

Запись реестра оператора и запись реестра агента живут отдельно: поле,
добавленное не в тот файл, просто не сработает.

## Типичные проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `pip: command not found` | на чистой Ubuntu pip не установлен | `sudo apt-get install -y python3-pip`, но ставить агента через pipx/venv |
| `error: externally-managed-environment` | PEP 668 | `pipx install standkit` или venv в `/opt`; не `--break-system-packages` |
| `standkit-agent: command not found` | `~/.local/bin` не в `PATH` в non-login оболочке | `pipx ensurepath` + перелогин; в юнитах — абсолютный путь |
| `ModuleNotFoundError: No module named 'standkit'` | скрипт запущен системным `python3` | `~/.local/share/pipx/venvs/standkit/bin/python` или `/opt/standkit/venv/bin/python` |
| `error: the following arguments are required: --token-ref` | `--token-ref` обязателен всегда | добавить флаг |
| `Секрет ... не найден ни в переменной окружения ...` | переменная не задана/забыт префикс `STANDKIT_SECRET__` | взять имя из самого сообщения |
| `NoKeyringError` | headless-хост без бэкенда keyring | секреты через переменные окружения |
| `cat: /etc/standkit/agent.env: Permission denied` | файл `600 root:root`, читают от сервисного аккаунта | `chown root:standkit` + `chmod 640` |
| `status=200/CHDIR` в systemd | нет прав на `WorkingDirectory` | `chown standkit:standkit /opt/standkit` |
| `Отказ старта: каталог логов ... недоступен на запись пользователю ...` | каталоги остались от ручных запусков | `chown -R standkit:standkit /opt/standkit/run /opt/standkit/logs`, см. [7.3](#73-отказы-старта-службы-что-вы-увидите-и-что-задать) |
| `Отказ старта: аудит-лог ... недоступен на запись пользователю ...` | файл аудита создан другим пользователем | `chown standkit:standkit /opt/standkit/logs/audit.log` |
| `Отказ старта: домашний каталог ... нерабочие` | сервисный аккаунт без `$HOME`, пути не заданы флагами | задать все три: `--run-dir`, `--log-dir`, `--audit-log` |
| `стендов=0` при старте | реестр найден не тот (автопоиск ушёл в `$HOME`) | всегда `--registry <абсолютный путь>` |
| `RegistryError: Некорректный JSON реестра ...` | битый `projects.json` | восстановить из копии; `python3 -m json.tool` перед рестартом |
| `{"error": "Стенд 'x' не найден в реестре ..."}` (404) | опечатка в имени или правка не в том файле | сверить с `GET /stands` |
| `{"error": "invalid stand name"}` (400) | недопустимые символы в имени | использовать имя из `/stands` |
| `http: down` при живом стенде | стенд за TLS, а `stand_scheme=http` / `verify_tls=true` при self-signed | `stand_scheme=https`, `verify_tls=false`, **рестарт агента** |
| `http: down`, `process: ok` после пересоздания контейнера | сменился опубликованный порт | обновить `stand_port`, рестарт агента |
| `лог недоступен (источник «Стенд»: каталог не найден — ...)` при живых логах | до 0.8.0 подкаталог искался строго как `logs`, BPMSoft пишет в `Logs` | обновить пакет; нестандартную раскладку задать полем `logs_dir` |
| колонка «Redis» пустая, подсказка «адрес Redis не задан в реестре» | не заполнена пара `redis_host`/`redis_port` | заполнить оба поля, рестарт агента |
| `redis: down` при работающем Redis | Redis в compose без публикации порта наружу | опубликовать порт либо указать адрес, видимый с хоста агента |
| `process_reason: ... permission denied ... docker.sock` | сервисный пользователь не в группе `docker` | `usermod -aG docker standkit` (осознавая, что это ≈ root) |
| `409 adopt_required` | стенд поднят вне диспетчера | проверить кандидата, затем `adopt?force=1` |
| `Отказ старта: не удалось привязаться к ... — порт уже занят другим процессом` | порт занят другим экземпляром агента | `ss -ltnp \| grep 8765`, остановить лишний процесс или взять другой `--port` |
| `429 too many failed attempts` | сработал lockout | подождать окно или `systemctl restart standkit-agent` |
| `сертификат агента не доверенный (self-signed certificate) — укажите agent_ca ...` | дашборд не доверяет сертификату агента | скопировать `agent.crt` на машину оператора и указать его в `agent_ca`, см. [10.2](#102-самоподписанный-сертификат-агента-и-дашборд) |
| `имя в сертификате агента не совпадает с адресом (Hostname mismatch ...)` | в `agent_url` адрес, которого нет в CN/SAN | привести `agent_url` к CN/SAN или перевыпустить сертификат |
| `файл сертификата агента не найден: ... (поле agent_ca записи стенда)` | в `agent_ca` путь с хоста агента, а не с машины хаба | указать путь, существующий на машине дашборда |
| дашборд не видит стенд, агент отвечает с самого хоста | запись в реестре оператора осталась `transport=local` или не задан `agent_secret_ref` | привести запись к виду из [10.1](#101-запись-в-реестре-оператора) |
| `curl: (60) SSL certificate problem` | клиент не доверяет сертификату агента | `--cacert agent.crt` или добавить CA в доверенные |
| `curl: (60) SSL: no alternative certificate subject name` | обращаетесь по имени, которого нет в SAN | выпустить сертификат с нужным SAN |
| `tlsv13 alert certificate required` | включён mTLS, клиент без сертификата | `--cert/--key` клиента |
| `Отказ старта: host=... не loopback, TLS не настроен` | fail-closed | настроить TLS либо слушать loopback |

---

## Приложение А. Клиент на Windows

В PowerShell `curl` — это алиас `Invoke-WebRequest`, флаги `-s -H` он не
понимает. Варианты:

```powershell
# настоящий curl
curl.exe -s -H "Authorization: Bearer $env:TOKEN" https://example-host:8443/stands

# нативный PowerShell
Invoke-RestMethod -Uri https://example-host:8443/stands `
  -Headers @{ Authorization = "Bearer $env:TOKEN" }
```

Для self-signed сертификата агента добавьте его CA в доверенные на машине
оператора — отключать проверку в скриптах не нужно.

---

## Приложение Б. Чек-лист приёмки

- [ ] `standkit-agent --help` доступен под тем пользователем, который его запускает
- [ ] реестр указан явно, в строке старта `стендов=<N>`, N совпадает с ожиданием
- [ ] `GET /stands` возвращает все стенды
- [ ] `GET /stand/<имя>/status` даёт `process: ok`, `http: ok` для каждого живого стенда
- [ ] `POST .../stop` + `POST .../start` отрабатывают и стенд возвращается в `ok`
- [ ] стенд, поднятый вне диспетчера, даёт `409 adopt_required`, а не молчаливое убийство
- [ ] неверный токен → `401`, readonly-токен на `stop` → `403`
- [ ] служба `enabled`, переживает `reboot`
- [ ] TLS включён, клиент ходит с `--cacert`; для прод-контура включён mTLS
- [ ] порт агента закрыт файрволом для всех, кроме управляющего контура
- [ ] `agent.env` имеет права `640 root:<сервисная группа>`
- [ ] в `projects.json` нет `db_password` — только `secret_ref_db`
- [ ] аудит-лог пишется и попал в ротацию

---

## Известные ограничения

- `host_kind=iis` — только Windows (нужен `appcmd.exe`);
- глубокие пробы БД и Redis не выполняются: `db`/`redis` показывают
  поверхностную доступность порта (TCP-коннект), `PING`/SQL-запроса нет;
- каталог логов стенда с `transport=agent` дашборд не читает: путь проверяется
  на машине хаба, и для такого стенда он честно пишет «каталог логов живёт на
  хосте стенда; хаб проверяет его локально и потому не видит — `<путь>`». Логи
  смотрите через `GET /stand/<имя>/logs` у агента или на самом хосте;
- причина отказа пробы приезжает подсказкой (`title`) у значения в таблице
  дашборда — отдельной колонки «почему» нет;
- keyring на headless-хостах недоступен (см. [4.2](#42-keyring-на-headless-сервере-не-работает));
- реестр читается один раз при старте: после любой правки `projects.json` нужен
  рестарт агента (см. [9.3](#93-реестр-читается-при-старте)).
