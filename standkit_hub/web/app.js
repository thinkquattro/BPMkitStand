/*
 * Vanilla JS дашборда standkit — без CDN, без сборки, без фреймворков.
 *
 * Аутентификация: сессионная HttpOnly-cookie ставится сервером при первом
 * переходе по "/?t=<token>" (см. standkit_hub/server.py::_handle_root).
 * Дальше все запросы к /api/* идут через fetch с credentials: "same-origin"
 * (cookie летит автоматически). Мутации (POST/DELETE) ДОПОЛНИТЕЛЬНО несут
 * заголовок X-Standkit-Token — сервер сверяет его с cookie/сессией
 * (double-submit) и с Origin/Referer запроса. Значение токена для заголовка
 * читается из той же cookie на клиенте (она HttpOnly, поэтому JS её прочитать
 * не может напрямую — вместо этого сервер один раз, в момент редиректа с "/?t=",
 * даёт странице возможность запомнить токен через query-параметр текущего
 * перехода; если параметр отсутствует (обычный повторный визит на "/"),
 * запросы на чтение всё равно проходят по cookie, а мутации в этом случае
 * требуют, чтобы пользователь заново открыл дашборд по ссылке с токеном).
 */

(() => {
  "use strict";

  // Токен для заголовка X-Standkit-Token берём из query-параметра "t" ТЕКУЩЕГО
  // запроса (если он есть — значит, страница только что открыта по ссылке
  // из standkit_hub.__main__ и сервер поставил cookie в этом же обмене).
  const urlParams = new URLSearchParams(window.location.search);
  // Токен сервер инжектит в <meta name="standkit-token"> для аутентифицированного
  // запроса (валидный ?t= ИЛИ session-cookie) — работает и после refresh/чистого "/".
  const metaEl = document.querySelector('meta[name="standkit-token"]');
  const metaToken =
    metaEl && metaEl.content && metaEl.content !== "__STANDKIT_TOKEN__" ? metaEl.content : "";
  let sessionToken =
    metaToken || urlParams.get("t") || sessionStorage.getItem("standkit_token") || "";
  if (sessionToken) {
    sessionStorage.setItem("standkit_token", sessionToken);
  }
  if (urlParams.get("t")) {
    // Токен в адресной строке больше не нужен — уберём из URL.
    window.history.replaceState({}, "", window.location.pathname);
  }

  function apiHeaders(mutation) {
    const headers = { "Content-Type": "application/json" };
    if (mutation && sessionToken) {
      headers["X-Standkit-Token"] = sessionToken;
    } else if (!mutation && sessionToken) {
      // Не обязателен для чтения (cookie достаточно), но не мешает.
      headers["X-Standkit-Token"] = sessionToken;
    }
    return headers;
  }

  async function apiGet(path) {
    const resp = await fetch(path, { credentials: "same-origin", headers: apiHeaders(false) });
    return handleResponse(resp);
  }

  async function apiSend(method, path, body) {
    const resp = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: apiHeaders(true),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    return handleResponse(resp);
  }

  async function handleResponse(resp) {
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      const message = (data && data.error) || `HTTP ${resp.status}`;
      throw new Error(message);
    }
    return data;
  }

  // --- вкладки ---

  function setupTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
      });
    });
  }

  // --- бэйджи статуса ---

  function badgeClass(state) {
    switch (state) {
      case "ok":
        return "badge badge-ok";
      case "down":
        return "badge badge-down";
      case "skipped":
        return "badge badge-skipped";
      default:
        return "badge badge-unknown";
    }
  }

  function badge(state) {
    return `<span class="${badgeClass(state)}">${state || "unknown"}</span>`;
  }

  function processBadge(state) {
    const label = state === "ok" ? "up" : state === "down" ? "down" : state || "unknown";
    return `<span class="${badgeClass(state)}">${label}</span>`;
  }

  function valueClass(state) {
    switch (state) {
      case "ok":
        return "value-ok";
      case "down":
        return "value-down";
      case "skipped":
        return "value-skipped";
      default:
        return "value-unknown";
    }
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function valueSpan(text, state) {
    return `<span class="value-cell ${valueClass(state)}">${escapeHtml(text)}</span>`;
  }

  function redisCell(redis) {
    const r = redis || {};
    const hasNumber = r.number !== null && r.number !== undefined;
    if (!hasNumber && (!r.state || r.state === "unknown")) {
      // Номер Redis не хранится в реестре стендов (он в конфиге самого
      // стенда) — это ожидаемое отсутствие данных, не ошибка. Показываем
      // нейтральный прочерк, а не жёлтый/красный статус.
      return '<span class="value-cell value-muted">—</span>';
    }
    return valueSpan(hasNumber ? String(r.number) : "—", r.state);
  }

  // --- иконки действий (инлайн-SVG, без внешних шрифтов/CDN) ---

  const ICON_PLAY =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M4 2.5v11l9-5.5-9-5.5z"/></svg>';
  const ICON_STOP =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><rect x="3.5" y="3.5" width="9" height="9" rx="1"/></svg>';
  const ICON_RESTART =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M13.2 8A5.2 5.2 0 1 1 10.9 3.6"/><path d="M13.4 2.6v3.4h-3.4"/></svg>';

  function actionButtons(name) {
    return `
      <button class="icon-btn icon-btn-play" data-action="start" data-name="${name}" title="Запустить">${ICON_PLAY}</button>
      <button class="icon-btn icon-btn-stop" data-action="stop" data-name="${name}" title="Остановить">${ICON_STOP}</button>
      <button class="icon-btn icon-btn-restart" data-action="restart" data-name="${name}" title="Перезапустить">${ICON_RESTART}</button>
    `;
  }

  // --- стенды ---

  let selectedStand = null;
  // Источник логов для панели "Текущее состояние": "stand" (логи стенда,
  // <stand_dir>/logs) или "bpmkit" (логи MCP, logs_path) — см.
  // standkit_hub/logs_browser.py::resolve_logs_dir. Дефолт — "stand".
  let logSource = "stand";

  async function refreshStands() {
    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";
    try {
      const data = await apiGet("/api/stands");
      renderStands(data.stands || []);
      setConnStatus(true);
    } catch (e) {
      errorEl.textContent = `Ошибка обновления: ${e.message}`;
      setConnStatus(false);
    }
  }

  function renderStands(stands) {
    const tbody = document.getElementById("stands-tbody");
    tbody.innerHTML = "";
    stands.forEach((s) => {
      const http = s.http || {};
      const db = s.db || {};
      const redis = s.redis || {};
      const process = s.process || {};
      const tr = document.createElement("tr");
      tr.dataset.name = s.name;
      if (s.name === selectedStand) tr.classList.add("selected");
      tr.innerHTML = `
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.transport)}</td>
        <td>${processBadge(process.state)}</td>
        <td>${valueSpan(http.url || "—", http.state)}</td>
        <td>${valueSpan(db.name || "—", db.state)}</td>
        <td>${redisCell(redis)}</td>
        <td class="row-actions">${actionButtons(s.name)}</td>
      `;
      tr.addEventListener("click", (evt) => {
        if (evt.target.closest("button")) return;
        selectStand(s.name);
      });
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll("button[data-action]").forEach((btn) => {
      btn.addEventListener("click", (evt) => {
        evt.stopPropagation();
        onStandAction(btn.dataset.name, btn.dataset.action);
      });
    });
  }

  function selectStand(name) {
    selectedStand = name;
    document.querySelectorAll(".stands-table tbody tr").forEach((tr) => {
      tr.classList.toggle("selected", tr.dataset.name === name);
    });
    refreshState();
    refreshLogFilesList();
  }

  async function onStandAction(name, action) {
    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";
    try {
      await apiSend("POST", `/api/stand/${encodeURIComponent(name)}/${action}`);
      await refreshStands();
      if (name === selectedStand) refreshState();
    } catch (e) {
      errorEl.textContent = `Ошибка (${name}/${action}): ${e.message}`;
    }
  }

  // --- текущее состояние выбранного стенда ---

  async function refreshState() {
    if (!selectedStand) return;
    document.getElementById("state-stand-name").textContent = `(${selectedStand})`;
    try {
      const data = await apiGet(
        `/api/stand/${encodeURIComponent(selectedStand)}/state?source=${encodeURIComponent(logSource)}`
      );
      document.getElementById("state-output").textContent = data.text || "";
    } catch (e) {
      document.getElementById("state-output").textContent = `Ошибка чтения состояния: ${e.message}`;
    }
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
  }

  async function refreshLogFilesList() {
    if (!selectedStand) return;
    const select = document.getElementById("log-file-select");
    const previous = select.value;
    select.innerHTML = '<option value="">(основной лог)</option>';
    try {
      const data = await apiGet(
        `/api/stand/${encodeURIComponent(selectedStand)}/logs/list?source=${encodeURIComponent(logSource)}`
      );
      (data.files || []).forEach((f) => {
        const opt = document.createElement("option");
        opt.value = f.name;
        opt.textContent = `${f.name} (${formatSize(f.size)})`;
        select.appendChild(opt);
      });
      if (previous && Array.from(select.options).some((o) => o.value === previous)) {
        select.value = previous;
      }
    } catch (e) {
      // Список файлов не критичен для показа "текущего состояния" — не шумим ошибкой.
    }
  }

  function setupStatePanel() {
    document.getElementById("log-source-select").addEventListener("change", (evt) => {
      logSource = evt.target.value;
      if (!selectedStand) return;
      // Список файлов и "текущее состояние" зависят от источника — перезапрашиваем оба.
      refreshState();
      refreshLogFilesList();
    });

    document.getElementById("log-file-open-btn").addEventListener("click", async () => {
      if (!selectedStand) return;
      const select = document.getElementById("log-file-select");
      const name = select.value;
      const out = document.getElementById("state-output");
      if (!name) {
        await refreshState();
        return;
      }
      try {
        const data = await apiGet(
          `/api/stand/${encodeURIComponent(selectedStand)}/logs/file?source=${encodeURIComponent(logSource)}&name=${encodeURIComponent(name)}&n=500`
        );
        out.textContent = (data.lines || []).join("\n") || "(лог пуст)";
      } catch (e) {
        out.textContent = `Ошибка чтения файла: ${e.message}`;
      }
    });

    document.getElementById("log-folder-open-btn").addEventListener("click", async () => {
      if (!selectedStand) return;
      const out = document.getElementById("state-output");
      try {
        const data = await apiSend(
          "POST",
          `/api/stand/${encodeURIComponent(selectedStand)}/logs/open-folder?source=${encodeURIComponent(logSource)}`
        );
        out.textContent = data.message || (data.ok ? "Папка логов открыта." : "Не удалось открыть папку логов.");
      } catch (e) {
        out.textContent = `Ошибка: ${e.message}`;
      }
    });
  }

  // --- карточка MCP/Companion ---

  async function refreshMcpVersion() {
    const el = document.getElementById("mcp-version");
    try {
      const data = await apiGet("/api/mcp/version");
      el.textContent = data.version || "не определена (manifest.json не найден)";
    } catch (e) {
      el.textContent = `ошибка: ${e.message}`;
    }
  }

  function setConnStatus(ok) {
    const el = document.getElementById("conn-status");
    el.textContent = ok ? "подключено" : "нет связи";
    el.style.color = ok ? "var(--bpmkit-ok)" : "var(--bpmkit-down)";
  }

  // --- локальный агент ---

  async function refreshAgentStatus() {
    const el = document.getElementById("agent-status");
    try {
      const data = await apiGet("/api/agent/status");
      el.textContent = data.running ? "запущен" : "остановлен";
    } catch (e) {
      el.textContent = `ошибка: ${e.message}`;
    }
  }

  function setupAgentTab() {
    document.getElementById("agent-start-btn").addEventListener("click", async () => {
      const errorEl = document.getElementById("agent-error");
      errorEl.textContent = "";
      try {
        await apiSend("POST", "/api/agent/start");
        await refreshAgentStatus();
      } catch (e) {
        errorEl.textContent = e.message;
      }
    });
    document.getElementById("agent-stop-btn").addEventListener("click", async () => {
      const errorEl = document.getElementById("agent-error");
      errorEl.textContent = "";
      try {
        await apiSend("POST", "/api/agent/stop");
        await refreshAgentStatus();
      } catch (e) {
        errorEl.textContent = e.message;
      }
    });
  }

  // --- настройки ---

  const SETTINGS_FIELDS = [
    "registry_path",
    "run_dir",
    "log_dir",
    "refresh_interval_sec",
    "agent_host",
    "agent_port",
    "token_ref",
    "readonly_token_ref",
    "tls_cert",
    "tls_key",
    "tls_client_ca",
    "audit_log",
    "lockout_max_failures",
    "lockout_window_sec",
  ];

  let currentAgents = [];

  function renderAgentsList() {
    const container = document.getElementById("agents-list");
    container.innerHTML = "";
    currentAgents.forEach((agent, idx) => {
      const row = document.createElement("div");
      row.className = "agent-row";
      row.innerHTML = `
        <input type="text" placeholder="имя" data-field="name" value="${agent.name || ""}" />
        <input type="text" placeholder="url (https://host:8765)" data-field="url" value="${agent.url || ""}" />
        <input type="text" placeholder="token_ref" data-field="token_ref" value="${agent.token_ref || ""}" />
        <button type="button" data-idx="${idx}">Удалить</button>
      `;
      row.querySelectorAll("input").forEach((input) => {
        input.addEventListener("input", () => {
          currentAgents[idx][input.dataset.field] = input.value;
        });
      });
      row.querySelector("button").addEventListener("click", () => {
        currentAgents.splice(idx, 1);
        renderAgentsList();
      });
      container.appendChild(row);
    });
  }

  async function loadSettings() {
    const data = await apiGet("/api/settings");
    const form = document.getElementById("settings-form");
    SETTINGS_FIELDS.forEach((field) => {
      const input = form.elements.namedItem(field);
      if (input) input.value = data[field] ?? "";
    });
    form.elements.namedItem("insecure").checked = !!data.insecure;
    currentAgents = (data.agents || []).map((a) => ({ ...a }));
    renderAgentsList();
    await refreshSecretStatuses();
  }

  async function refreshSecretStatuses() {
    for (const field of ["token_ref", "readonly_token_ref"]) {
      const statusEl = document.querySelector(`[data-ref-status="${field}"]`);
      const input = document.getElementById("settings-form").elements.namedItem(field);
      const ref = input ? input.value : "";
      if (!statusEl) continue;
      if (!ref) {
        statusEl.textContent = "";
        continue;
      }
      try {
        const data = await apiGet(`/api/secret/${encodeURIComponent(ref)}`);
        statusEl.textContent = data.has_secret ? "секрет задан" : "секрет НЕ задан";
        statusEl.style.color = data.has_secret ? "var(--bpmkit-ok)" : "var(--bpmkit-down)";
      } catch (e) {
        statusEl.textContent = `ошибка: ${e.message}`;
      }
    }
  }

  function setupSettingsForm() {
    const form = document.getElementById("settings-form");
    form.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const statusEl = document.getElementById("settings-status");
      statusEl.textContent = "";
      const payload = {};
      SETTINGS_FIELDS.forEach((field) => {
        const input = form.elements.namedItem(field);
        if (!input) return;
        payload[field] = input.type === "number" ? Number(input.value) : input.value;
      });
      payload.insecure = form.elements.namedItem("insecure").checked;
      payload.agents = currentAgents;
      try {
        await apiSend("POST", "/api/settings", payload);
        statusEl.textContent = "Настройки сохранены";
        await refreshSecretStatuses();
      } catch (e) {
        statusEl.textContent = `Ошибка сохранения: ${e.message}`;
      }
    });

    document.getElementById("add-agent-btn").addEventListener("click", () => {
      currentAgents.push({ name: "", url: "", token_ref: "" });
      renderAgentsList();
    });

    document.querySelectorAll(".set-secret-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const fieldName = btn.dataset.refField;
        const input = form.elements.namedItem(fieldName);
        const ref = input ? input.value : "";
        if (!ref) {
          window.alert("Сначала укажите ссылку на секрет (*_ref) в поле выше.");
          return;
        }
        const value = window.prompt(`Значение секрета для '${ref}' (не будет отображено повторно):`);
        if (value === null || value === "") return;
        try {
          await apiSend("POST", `/api/secret/${encodeURIComponent(ref)}`, { value });
          await refreshSecretStatuses();
        } catch (e) {
          window.alert(`Не удалось задать секрет: ${e.message}`);
        }
      });
    });

    document.getElementById("install-shortcut-btn").addEventListener("click", async () => {
      const statusEl = document.getElementById("settings-status");
      try {
        const data = await apiSend("POST", "/api/shortcut/install");
        statusEl.textContent = data.message;
      } catch (e) {
        statusEl.textContent = `Ошибка: ${e.message}`;
      }
    });
  }

  // --- инициализация ---

  function init() {
    setupTabs();
    setupAgentTab();
    setupSettingsForm();
    setupStatePanel();
    document.getElementById("refresh-stands-btn").addEventListener("click", refreshStands);

    refreshStands();
    refreshAgentStatus();
    refreshMcpVersion();
    loadSettings().catch((e) => {
      document.getElementById("settings-status").textContent = `Ошибка загрузки настроек: ${e.message}`;
    });

    const intervalMs = 10000;
    setInterval(() => {
      refreshStands();
      refreshAgentStatus();
      if (selectedStand) refreshState();
    }, intervalMs);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
