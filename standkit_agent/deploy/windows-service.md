# Установка standkit-agent как службы Windows

Агент — обычный долгоживущий Python-процесс (`python -m standkit_agent`), поэтому
его можно поставить службой Windows несколькими стандартными способами. Ниже —
краткие заметки для следующей итерации (полноценный установочный скрипт — TODO).

## Безопасность: least privilege (обязательно для прод-развёртывания)

Агент — RCE-поверхность по дизайну (start/stop/restart процессов на хосте
стенда). **Никогда не устанавливать службу под `LocalSystem`** (дефолт `sc.exe`
и NSSM без явного `ObjectName`/`-user`, если не указано иное) — это даёт
процессу агента полные привилегии системы, чего для его задач не требуется.

1. Создать выделенную service-учётную запись (не администратора):
   ```
   New-LocalUser -Name "standkit-agent" -NoPassword:$false -AccountNeverExpires
   # выдать право "Log on as a service" через secpol.msc → Local Policies →
   # User Rights Assignment → Log on as a service
   ```
2. Ограничить права этой учётной записи ACL на файловой системе ТОЛЬКО тем,
   что реально нужно агенту:
   - каталог(и) стенда(ов) из реестра (`stand_dir`) — чтение/запуск процесса;
   - каталог `run/`/`logs/` агента (pid-файлы, лог-файлы) — чтение/запись;
   - каталог с TLS-сертификатом/ключом агента — чтение (ключ — ACL только
     для сервисной учётной записи, не "Users"/"Everyone");
   - **не** давать права на системные каталоги, реестр Windows вне
     необходимого, другие профили пользователей.
3. При установке через NSSM/`sc.exe` — явно указать эту учётную запись
   (`nssm set standkit-agent ObjectName .\standkit-agent <пароль>` /
   `sc.exe config standkit-agent obj= .\standkit-agent password= ...`),
   не оставлять `LocalSystem` по умолчанию.
4. Firewall: если host не loopback — открыть входящий порт агента (по
   умолчанию 8765) ТОЛЬКО для конкретных источников (управляющий контур/VPN),
   не "любой адрес" (`New-NetFirewallRule ... -RemoteAddress <CIDR
   управляющего контура>`).
5. TLS/mTLS-сертификат и ключ — хранить в каталоге, доступном на чтение
   только сервисной учётной записи (Windows ACL `icacls` — убрать
   наследование, оставить только нужного пользователя).

## Вариант 1: NSSM (Non-Sucking Service Manager) — самый простой

1. Скачать [NSSM](https://nssm.cc/) и положить `nssm.exe` в `PATH`.
2. Установить службу:
   ```
   nssm install standkit-agent "C:\path\to\venv\Scripts\python.exe" ^
       -m standkit_agent --host 0.0.0.0 --port 8765 ^
       --registry C:\ProgramData\standkit\projects.json ^
       --token-ref standkit:CHANGE_ME:agent-token
   nssm set standkit-agent AppDirectory C:\path\to\standkit
   nssm start standkit-agent
   ```
3. Логи NSSM может писать в отдельные файлы (`nssm set standkit-agent AppStdout ...`)
   — полезно на время отладки, помимо собственного лога стенда standkit.

## Вариант 2: sc.exe (без сторонних утилит)

`sc.exe` умеет регистрировать только исполняемые файлы, реализующие Windows
Service Control API напрямую — обычный `python -m ...` так не работает без
обёртки. Для этого варианта нужен небольшой враппер на `pywin32`
(`win32serviceutil.ServiceFramework`) — **TODO следующей итерации**:
реализовать `standkit_agent/deploy/win_service.py` с классом-службой,
делегирующим в `standkit_agent.server.run_server`.

## Вариант 3: Task Scheduler с триггером "при загрузке системы"

Не настоящая служба (нет автоматического рестарта при падении, нет
управления через `services.msc`), но работает без дополнительных
зависимостей — годится как временное решение:
```
schtasks /Create /SC ONSTART /RL HIGHEST /TN "standkit-agent" ^
    /TR "C:\path\to\venv\Scripts\python.exe -m standkit_agent --host 0.0.0.0 --port 8765 --registry C:\ProgramData\standkit\projects.json --token-ref standkit:CHANGE_ME:agent-token"
```

## TODO (следующая итерация)

- Родной `pywin32`-враппер службы (Вариант 2) с корректной обработкой
  `SvcStop` → graceful shutdown HTTP-сервера агента.
- Инсталлятор/скрипт, автоматизирующий выбор варианта в зависимости от того,
  что доступно на целевой машине (`nssm.exe` в PATH → Вариант 1, иначе →
  подсказка поставить `pywin32` для Варианта 2).
