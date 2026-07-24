# ADR-0002 — Kubernetes hosting backend (host_kind=k8s)

- Статус: **Принято / реализовано** (v0.5.0, 24.07.2026)
- Связано: [ADR-0001 hosting backends](0001-hosting-backends.md), [HOSTING.md](../HOSTING.md)

## Контекст

ADR-0001 ввёл абстракцию hosting backends (kestrel/iis/docker) с заделом под
Kubernetes. Настоящий ADR фиксирует реализацию k8s-бэкенда как ещё одного члена
того же семейства — без изменения абстракции.

## Решение

`host_kind=k8s` управляет стендом как **Deployment** в кластере через CLI
`kubectl` (stdlib-only, без `kubernetes`-клиента как зависимости). Поля реестра:

| Поле | Назначение |
|------|-----------|
| `k8s_deployment` | имя Deployment (обязательно) |
| `k8s_namespace` | namespace (пусто → `default`) |
| `k8s_context` | kube-context (опц.) |
| `k8s_container` | контейнер для логов (опц.) |
| `k8s_replicas` | сколько реплик поднимать при start (дефолт 1) |

Семантика жизненного цикла (у Deployment нет «процесса»/pid):

| Действие | Команда |
|----------|---------|
| start | `kubectl [--context C] -n NS scale deployment/DEP --replicas=<k8s_replicas>` |
| stop | `... scale deployment/DEP --replicas=0` |
| restart | `... rollout restart deployment/DEP` |
| is_running | `... get deployment DEP -o jsonpath={.status.readyReplicas}` → >0 (иначе TCP-фолбэк) |
| логи | `... logs deployment/DEP --tail N [-c CONTAINER]` |

`kubectl` резолвится через `shutil.which`; отсутствие → `HostingError`. Пробы при
ошибке возвращают False, не бросают. `start`/`restart` возвращают `None` (pid'а нет).

## Диспетчеризация

`lifecycle.*` теперь направляет в hosting-бэкенд всё, кроме `kestrel`
(IIS/DOCKER/**K8S**); `health.check_stand` для k8s берёт состояние через
`backend.is_running` (readyReplicas) с TCP-фолбэком. Kestrel-путь не изменён.

## Границы (в бэклог ADR-0001)

- **🔴 Живая приёмка на реальном кластере — крит до релиза** (тесты пока на моках
  `subprocess`).
- Только `kubectl` + активный kubeconfig/context; RBAC-права на scale/rollout/logs —
  зона оператора.
- StatefulSet/Pod напрямую, `kubectl port-forward`, in-cluster API (ServiceAccount) —
  возможные расширения следующих итераций.
