# standkit

Свободный (MIT) диспетчер жизненного цикла стендов **BPMSoft** — старт/стоп/рестарт, health-пробы
(процесс/HTTP/БД/Redis), tail лога, единый реестр стендов. Без лицензионно-чувствительного
контента платформы, без dev-инструментов кастомизации BPMSoft.

## Модель: ядро + две оболочки

```
                 +-------------------------+
                 |     standkit_gui        |   PySide6 tray + окно (только у оператора)
                 |  федеративный клиент,    |   опрашивает N агентов по HTTP
                 |  N агентов + локальное   |
                 |  ядро                    |
                 +-----------+--------------+
                             | HTTP (Bearer-токен)
              +--------------+---------------+
              |                              |
    +---------v----------+        +----------v-----------+
    |  standkit_agent     |        |  standkit_agent      |
    |  (хост стенда A)    |  ...   |  (хост стенда N)     |
    |  stdlib http.server |        |  stdlib http.server  |
    |  + standkit (ядро)  |        |  + standkit (ядро)   |
    +---------------------+        +-----------------------+

    standkit (ядро) можно использовать и напрямую, без агента и без GUI —
    как библиотеку/CLI на локальной машине (transport="local").
```

- **`standkit`** (ядро, лицензия MIT) — движок жизненного цикла над реестром `projects.json`:
  headless start/stop/restart процесса стенда, health-пробы (процесс жив / HTTP / порт БД /
  порт Redis), tail per-stand лог-файла, Secret-first доступ к секретам. Без Qt, без сетевых
  зависимостей, без лицензионного гейта.
- **`standkit_agent`** — лёгкий headless-демон на каждом хосте стенда: ядро + крошечный HTTP/RPC
  сервер (только `stdlib`: `http.server`, `subprocess`, `socket`, `urllib`). Кроссплатформенный —
  Windows и Linux (стенды BPMSoft на .NET штатно живут и под Linux). Без Qt.
- **`standkit_gui`** — tray-приложение и окно диспетчера на PySide6/Qt, устанавливается только на
  машину оператора. Федеративный клиент: подключается к локальному ядру и/или к N удалённым
  агентам (`agent_url`) и показывает единый список стендов со статусами и кнопками управления.
  PySide6 — опциональная зависимость (`extra`), ядро и агент от неё не зависят.

`standkit` работает воронкой на платный продукт **BPMkit** (провижининг, деплой, БД-операции,
кастомизация JS/C#, документирование) — см. экосистему в
[BPMkit-dev](https://github.com/thinkquattro/BPMkit-dev). Детальный дизайн и обоснования решения
«ядро+оболочки» — там же: ADR-0019 и
`docs/планы/companion_dispatcher_f_l2a_2026-07-23.md`.

## Установка

```bash
# только ядро (движок жизненного цикла + библиотека для агента)
pip install standkit

# + GUI-оболочка (диспетчер на машине оператора)
pip install standkit[gui]

# + инструменты разработки/тестирования
pip install standkit[dev]

# агент ставится отдельно на каждый хост стенда (тот же пакет, extra не нужен)
pip install standkit

# dev/loopback (secure default — без TLS, но только на 127.0.0.1)
python -m standkit_agent --registry ./projects.json --token-ref standkit:my-stand:agent-token

# прод/удалённый доступ — TLS+mTLS обязателен (см. раздел «Безопасность» ниже)
python -m standkit_agent --host 0.0.0.0 --port 8765 \
    --registry /opt/standkit/projects.json \
    --token-ref standkit:my-stand:agent-token \
    --tls-cert agent.crt --tls-key agent.key --tls-client-ca clients-ca.crt
```

## Быстрый старт

1. Скопируйте `projects.sample.json` в `projects.json` и заполните под свой(и) стенд(ы)
   (или используйте `standkit.registry.add_existing(...)`, чтобы привязать уже существующий
   стенд программно — **это не провижининг**, каталог стенда должен уже существовать).
2. Локальное управление стендом:

   ```python
   from standkit.registry import Registry
   from standkit import lifecycle, health

   reg = Registry.load("projects.json")
   stand = reg.get("example-local")

   lifecycle.start(stand)
   print(health.process_alive(lifecycle.pidfile_path(stand)))
   print(health.tcp_open(stand.db_host, stand.db_port))
   lifecycle.stop(stand)
   ```

3. Удалённое управление через агента — стенд с `transport: "agent"` в реестре, GUI/клиент сам
   ходит по HTTP к `agent_url` с токеном из `agent_secret_ref`.

## Безопасность

`standkit_agent` — headless-демон, который по HTTP принимает команды
start/stop/restart процессов на хосте стенда. Это **RCE-поверхность по
дизайну**: любой, кто может успешно аутентифицироваться против агента,
управляет жизненным циклом процесса на этом хосте. К агенту нужно относиться
как к критической инфраструктуре, а не к вспомогательной утилите.

**Никогда не выставлять агента в недоверенную сеть без TLS/mTLS.** Удалённый
доступ к агенту — только через управляющий контур/VPN/SSH-туннель, либо через
TLS с обязательной клиентской аутентификацией (mTLS).

Secure-defaults, действующие "из коробки" (см. `standkit_agent/security.py`):

- **`--host` по умолчанию `127.0.0.1`** (не `0.0.0.0`) — агент по умолчанию не
  слушает ничего, кроме loopback.
- **Fail-closed bind-проверка**: если `--host` не loopback (`127.0.0.1`/`::1`/
  `localhost`) И не настроен TLS (`--tls-cert`/`--tls-key`) — агент
  **отказывается стартовать** с понятной ошибкой. Обойти можно только явным
  `--insecure` (для dev/тестовых сценариев за изолированным периметром —
  агент выведет громкое предупреждение в stderr; никогда не использовать так
  в проде).
- **TLS**: `ssl.SSLContext(PROTOCOL_TLS_SERVER)`, минимум TLS 1.2, современный
  набор AEAD-шифров (ECDHE + AES-GCM/ChaCha20-Poly1305).
- **mTLS**: `--tls-client-ca <CA.pem>` включает проверку клиентского
  сертификата (`CERT_REQUIRED`) — соединения без валидного сертификата
  отклоняются на уровне TLS-хендшейка, до обработчика запроса. CN клиентского
  сертификата попадает в аудит-лог.
- **Bearer-токен** сравнивается ТОЛЬКО через `hmac.compare_digest` (защита от
  timing-атак). Два скоупа: **control** (`--token-ref`, start/stop/restart +
  весь read) и **readonly** (`--readonly-token-ref`, опционально — только
  `/stands`, `/status`, `/logs`).
- **Rate limiting/lockout**: после серии неудачных аутентификаций с одного
  IP в скользящем окне — 429 до истечения окна (пороги настраиваются:
  `--lockout-max-failures`, `--lockout-window`).
- **Аудит**: append-only JSON-lines лог всех запросов (`--audit-log`, по
  умолчанию `~/.standkit/audit.log`) — `ts`/`src_ip`/`identity`/`method`/
  `path`/`action`/`result`/`code`. Токены и секреты в аудит-лог никогда не
  попадают.
- **Input-hardening**: лимит тела запроса (64 КБ), таймаут соединения, кап на
  `n` в `/logs` (10000), валидация имени стенда, `400` на некорректный ввод
  (не `500`/креш процесса).

Полный список параметров и их назначение — `python -m standkit_agent --help`.
Least-privilege рекомендации для запуска как службы ОС — см.
`standkit_agent/deploy/standkit-agent.service` (systemd, Linux) и
`standkit_agent/deploy/windows-service.md` (Windows).

**Осознанно не реализовано (TODO следующей итерации)** — см. докстринги
соответствующих модулей: полноценная PKI/выдача и ротация сертификатов
(сейчас сертификаты — забота оператора, агент только потребляет готовые PEM);
per-stand ACL (сейчас скоуп бинарный — control видит и управляет ВСЕМИ
стендами реестра агента, нет разбивки "этому токену — только стенд X");
ротация токенов "на лету" без рестарта агента; CN клиентского сертификата →
скоуп напрямую (сейчас скоуп определяется только Bearer-токеном, CN — только
для аудита).

## Статус

Каркас (skeleton) для итеративной разработки. Ключевые модули ядра (`registry`, `health`,
`models`) содержат рабочую минимальную логику и покрыты тестами; `lifecycle`/`platform`/агент/GUI —
скелетные, с явными `TODO` на платформенные тонкости (см. код).

## Лицензия

MIT, © standkit contributors. См. [LICENSE](LICENSE).
