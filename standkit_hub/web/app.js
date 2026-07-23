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
  let sessionToken = urlParams.get("t") || sessionStorage.getItem("standkit_token") || "";
  if (urlParams.get("t")) {
    sessionStorage.setItem("standkit_token", sessionToken);
    // Токен в адресной строке не нужен — cookie уже выставлена сервером.
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

  // --- стенды ---

  let selectedStand = null;

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
      const st = s.status || {};
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.name}</td>
        <td>${s.transport}</td>
        <td>${badge(st.process)}</td>
        <td>${badge(st.http)}</td>
        <td>${badge(st.db)}</td>
        <td>${badge(st.redis)}</td>
        <td class="row-actions">
          <button data-action="start" data-name="${s.name}">Старт</button>
          <button data-action="stop" data-name="${s.name}">Стоп</button>
          <button data-action="restart" data-name="${s.name}">Рестарт</button>
          <button data-action="logs" data-name="${s.name}">Логи</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll("button[data-action]").forEach((btn) => {
      btn.addEventListener("click", () => onStandAction(btn.dataset.name, btn.dataset.action));
    });
  }

  async function onStandAction(name, action) {
    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";
    try {
      if (action === "logs") {
        selectedStand = name;
        await refreshLogs();
        return;
      }
      await apiSend("POST", `/api/stand/${encodeURIComponent(name)}/${action}`);
      await refreshStands();
    } catch (e) {
      errorEl.textContent = `Ошибка (${name}/${action}): ${e.message}`;
    }
  }

  async function refreshLogs() {
    if (!selectedStand) return;
    document.getElementById("log-stand-name").textContent = `(${selectedStand})`;
    try {
      const data = await apiGet(`/api/stand/${encodeURIComponent(selectedStand)}/logs?n=200`);
      document.getElementById("log-output").textContent = (data.lines || []).join("\n") || "(лог пуст)";
    } catch (e) {
      document.getElementById("log-output").textContent = `Ошибка чтения логов: ${e.message}`;
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
    document.getElementById("refresh-stands-btn").addEventListener("click", refreshStands);

    refreshStands();
    refreshAgentStatus();
    loadSettings().catch((e) => {
      document.getElementById("settings-status").textContent = `Ошибка загрузки настроек: ${e.message}`;
    });

    const intervalMs = 10000;
    setInterval(() => {
      refreshStands();
      refreshAgentStatus();
      if (selectedStand) refreshLogs();
    }, intervalMs);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
