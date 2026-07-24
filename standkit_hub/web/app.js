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

  // --- тема (light/dark) ---

  const THEME_STORAGE_KEY = "standkit_theme";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
  }

  function setupTheme() {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    const prefersDark =
      !stored && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(stored || (prefersDark ? "dark" : "light"));

    document.getElementById("theme-toggle-btn").addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      const next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      localStorage.setItem(THEME_STORAGE_KEY, next);
    });
  }

  // --- модалка "О программе" ---

  let aboutVersionLoaded = false;

  function openAboutModal() {
    const overlay = document.getElementById("about-modal-overlay");
    overlay.hidden = false;
    if (!aboutVersionLoaded) {
      const versionEl = document.getElementById("about-modal-version");
      apiGet("/api/version")
        .then((data) => {
          versionEl.textContent = data.version || "н/д";
          aboutVersionLoaded = true;
        })
        .catch((e) => {
          versionEl.textContent = `ошибка: ${e.message}`;
        });
    }
  }

  function closeAboutModal() {
    document.getElementById("about-modal-overlay").hidden = true;
  }

  function setupAboutModal() {
    document.getElementById("about-btn").addEventListener("click", openAboutModal);
    document.getElementById("about-modal-close-btn").addEventListener("click", closeAboutModal);
    document.getElementById("about-modal-close-footer-btn").addEventListener("click", closeAboutModal);
    // Клик по затемнённому фону закрывает модалку — но только если нажатие
    // началось на фоне (см. bindOverlayDismiss: защита от выделения текста).
    bindOverlayDismiss(document.getElementById("about-modal-overlay"), closeAboutModal);
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && !document.getElementById("about-modal-overlay").hidden) {
        closeAboutModal();
      }
    });
  }

  // --- модалка "Зарегистрировать стенд" ---
  //
  // Регистрирует УЖЕ существующий стенд в общем реестре (POST
  // /api/stand/register, см. standkit_hub/server.py::_api_stand_register) —
  // НЕ провижининг. Та же разметка/классы, что у модалки "О программе" (см.
  // style.css .modal-overlay/.modal-box), плюс собственные условные блоки
  // полей (agent_*/iis_*/docker_*/k8s_*), которые показываются по значению
  // select'ов transport/host_kind без сторонних либ.

  function updateRegisterConditionalFields() {
    const form = document.getElementById("register-form");
    const transport = form.elements.namedItem("transport").value;
    const hostKind = form.elements.namedItem("host_kind").value;
    form.querySelectorAll(".register-conditional[data-when-transport]").forEach((el) => {
      el.hidden = el.dataset.whenTransport !== transport;
    });
    form.querySelectorAll(".register-conditional[data-when-host-kind]").forEach((el) => {
      el.hidden = el.dataset.whenHostKind !== hostKind;
    });
  }

  function showRegisterFormError(message) {
    const el = document.getElementById("register-form-error");
    el.textContent = message;
    el.classList.toggle("visible", !!message);
  }

  function openRegisterModal() {
    const overlay = document.getElementById("register-modal-overlay");
    const form = document.getElementById("register-form");
    form.reset();
    showRegisterFormError("");
    updateRegisterConditionalFields();
    overlay.hidden = false;
    form.elements.namedItem("name").focus();
  }

  function closeRegisterModal() {
    document.getElementById("register-modal-overlay").hidden = true;
  }

  // Поля формы, которые вообще уходят в JSON-тело запроса — только НЕПУСТЫЕ
  // (сервер и так игнорирует пустые строки, но так тело запроса компактнее
  // и понятнее в логах/отладке). Пароли в форме сознательно отсутствуют —
  // только secret_ref_* (agent_secret_ref).
  const _REGISTER_FIELD_NAMES = [
    "name",
    "transport",
    "host_kind",
    "stand_dir",
    "stand_host",
    "stand_port",
    "db_type",
    "db_host",
    "db_port",
    "db_name",
    "agent_url",
    "agent_secret_ref",
    "iis_site",
    "iis_app_pool",
    "docker_container",
    "docker_compose_file",
    "docker_compose_service",
    "k8s_namespace",
    "k8s_deployment",
  ];

  function collectRegisterPayload(form) {
    const payload = {};
    _REGISTER_FIELD_NAMES.forEach((field) => {
      const input = form.elements.namedItem(field);
      if (!input) return;
      const value = input.value.trim();
      if (!value) return;
      payload[field] = value;
    });
    return payload;
  }

  // Закрытие модалки кликом по подложке — но ТОЛЬКО если нажатие (mousedown)
  // началось на самой подложке. Иначе выделение текста мышью внутри окна,
  // отпущенное за его пределами (на подложке/за окном), ложно закрывает
  // модалку: браузер шлёт click на общего предка mousedown/mouseup — подложку.
  function bindOverlayDismiss(overlay, onClose) {
    let pressStartedOnOverlay = false;
    overlay.addEventListener("mousedown", (evt) => {
      pressStartedOnOverlay = evt.target === overlay;
    });
    overlay.addEventListener("click", (evt) => {
      if (evt.target === overlay && pressStartedOnOverlay) onClose();
      pressStartedOnOverlay = false;
    });
  }

  function setupRegisterModal() {
    const overlay = document.getElementById("register-modal-overlay");
    const form = document.getElementById("register-form");

    document.getElementById("register-stand-btn").addEventListener("click", openRegisterModal);
    document.getElementById("register-modal-close-btn").addEventListener("click", closeRegisterModal);
    document.getElementById("register-modal-cancel-btn").addEventListener("click", closeRegisterModal);
    bindOverlayDismiss(overlay, closeRegisterModal);
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && !overlay.hidden) closeRegisterModal();
    });

    form.elements.namedItem("transport").addEventListener("change", updateRegisterConditionalFields);
    form.elements.namedItem("host_kind").addEventListener("change", updateRegisterConditionalFields);

    form.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      showRegisterFormError("");
      const submitBtn = document.getElementById("register-modal-submit-btn");
      const payload = collectRegisterPayload(form);
      submitBtn.disabled = true;
      try {
        const data = await apiSend("POST", "/api/stand/register", payload);
        closeRegisterModal();
        showActionStatus(`Стенд ${data.name || payload.name} зарегистрирован`, false);
        await refreshStands();
      } catch (e) {
        // 400/409 остаются внутри модалки (не тост-прыжок) — пользователь
        // правит форму, не теряя введённые данные.
        showRegisterFormError(e.message);
      } finally {
        submitBtn.disabled = false;
      }
    });
  }

  // --- стилизованное подтверждение (замена window.confirm), Promise-обёртка ---
  //
  // Переиспользует разметку/классы модалки "О программе" (единый стиль сайта).
  // Используется для Стоп/Рестарт/Очистить Redis — код действий остаётся
  // линейным (await styledConfirm(...)) вместо колбэков.

  function styledConfirm(title, text) {
    return new Promise((resolve) => {
      const overlay = document.getElementById("confirm-modal-overlay");
      const okBtn = document.getElementById("confirm-modal-ok-btn");
      const cancelBtn = document.getElementById("confirm-modal-cancel-btn");
      const closeBtn = document.getElementById("confirm-modal-close-btn");

      document.getElementById("confirm-modal-title").textContent = title;
      document.getElementById("confirm-modal-text").textContent = text;
      overlay.hidden = false;
      okBtn.focus();

      function cleanup(result) {
        overlay.hidden = true;
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        closeBtn.removeEventListener("click", onCancel);
        overlay.removeEventListener("click", onOverlayClick);
        overlay.removeEventListener("mousedown", onOverlayMouseDown);
        document.removeEventListener("keydown", onKeydown);
        resolve(result);
      }
      function onOk() {
        cleanup(true);
      }
      function onCancel() {
        cleanup(false);
      }
      let pressStartedOnOverlay = false;
      function onOverlayMouseDown(evt) {
        pressStartedOnOverlay = evt.target === overlay;
      }
      function onOverlayClick(evt) {
        // Закрываем только если нажатие началось на подложке (не выделение
        // текста, отпущенное за пределами окна) — см. bindOverlayDismiss.
        if (evt.target === overlay && pressStartedOnOverlay) cleanup(false);
        pressStartedOnOverlay = false;
      }
      function onKeydown(evt) {
        if (evt.key === "Escape") cleanup(false);
      }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      closeBtn.addEventListener("click", onCancel);
      overlay.addEventListener("mousedown", onOverlayMouseDown);
      overlay.addEventListener("click", onOverlayClick);
      document.addEventListener("keydown", onKeydown);
    });
  }

  // --- область статуса действий (тост над таблицей стендов) ---

  let actionStatusTimer = null;

  function showActionStatus(message, isError) {
    const el = document.getElementById("action-status");
    el.textContent = message;
    el.classList.toggle("action-status-error", !!isError);
    el.classList.toggle("action-status-ok", !isError);
    el.classList.add("action-status-visible");
    if (actionStatusTimer) {
      clearTimeout(actionStatusTimer);
      actionStatusTimer = null;
    }
    // Успешные сообщения гаснут сами через паузу, ошибки остаются, пока их не сменит новое действие.
    if (!isError) {
      actionStatusTimer = setTimeout(() => {
        el.classList.remove("action-status-visible");
      }, 6000);
    }
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

  // escapeHtml (через textContent) не экранирует кавычки — для значения в
  // атрибуте href этого мало; добавляем экранирование " и '.
  function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Ячейка HTTP: если у стенда есть URL — отдаём кликабельную ссылку
  // (открывается в новой вкладке), иначе — прочерк. Цвет по состоянию пробы.
  function httpCell(http) {
    const url = http && http.url;
    const cls = valueClass(http && http.state);
    if (!url) {
      return `<span class="value-cell ${cls}">—</span>`;
    }
    return `<a class="value-cell value-link ${cls}" href="${escapeAttr(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a>`;
  }

  // --- иконки действий (инлайн-SVG, без внешних шрифтов/CDN) ---

  const ICON_PLAY =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M4 2.5v11l9-5.5-9-5.5z"/></svg>';
  const ICON_STOP =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><rect x="3.5" y="3.5" width="9" height="9" rx="1"/></svg>';
  const ICON_RESTART =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M13.2 8A5.2 5.2 0 1 1 10.9 3.6"/><path d="M13.4 2.6v3.4h-3.4"/></svg>';
  // Корзина ("очистить") — кнопка "Очистить Redis". Форма отличима от прочих
  // трёх (play/stop/restart), чтобы её не путали по силуэту.
  const ICON_REDIS_CLEAR =
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.2h10"/><path d="M6.4 4.2V2.9c0-.4.3-.7.7-.7h1.8c.4 0 .7.3.7.7v1.3"/><path d="M4.6 4.2l.6 8.2c.05.7.6 1.2 1.3 1.2h3c.7 0 1.25-.5 1.3-1.2l.6-8.2"/><path d="M6.7 6.6v4.3M9.3 6.6v4.3"/></svg>';

  // --- клиентское состояние "стенд запускается" (см. onStandAction/checkStartingTransitions) ---
  //
  // POST /start только ЗАПУСКАЕТ процесс — он ещё прогревается (dotnet/
  // компиляция схем занимают ~10-40с, иногда дольше), поэтому "Стенд запущен"
  // пишем только по факту готовности (http.state === "ok" на очередном
  // опросе), а не сразу после ответа POST.
  //
  // ВАЖНО: пока стенд в состоянии "starting", TCP-порт ещё может не
  // слушаться — это НОРМА прогрева, а не провал. Раньше клиент ошибочно
  // объявлял "не поднялся" по одному лишь process.state==="down" во время
  // прогрева. Теперь единственный вердикт "успех" — http.state==="ok";
  // единственный вердикт "мягкий провал" — истечение таймаута прогрева
  // (реально упавший процесс пользователь увидит в панели состояния/логах,
  // здесь мы больше не гадаем по process.state).

  const STARTING_SOFT_TIMEOUT_MS = 180000; // 3 минуты — мягкий таймаут прогрева

  const startingStands = new Map(); // name -> timestamp старта (Date.now())
  let lastStandsData = [];
  let fastPollTimer = null;

  function ensureFastPolling() {
    if (fastPollTimer) return;
    fastPollTimer = setInterval(() => {
      if (startingStands.size === 0) {
        clearInterval(fastPollTimer);
        fastPollTimer = null;
        return;
      }
      refreshStands();
    }, 2000);
  }

  function checkStartingTransitions(stands) {
    const now = Date.now();
    stands.forEach((s) => {
      if (!startingStands.has(s.name)) return;
      const httpState = s.http && s.http.state;
      if (httpState === "ok") {
        startingStands.delete(s.name);
        showActionStatus(`Стенд ${s.name} запущен`, false);
        return;
      }
      const startedAt = startingStands.get(s.name);
      if (now - startedAt >= STARTING_SOFT_TIMEOUT_MS) {
        // Мягкий таймаут: не факт провала — стенд может просто долго
        // прогреваться (или быть недоступен по другой причине). Снимаем
        // спиннер, но НЕ объявляем "не поднялся" — реальную картину
        // пользователь увидит в панели "Текущее состояние"/логах.
        startingStands.delete(s.name);
        showActionStatus(
          `Стенд ${s.name} всё ещё запускается — проверьте «Текущее состояние»/логи`,
          false
        );
      }
      // Иначе — прогрев продолжается (process.state==="down" на этом этапе
      // это норма, порт ещё не открыт): оставляем спиннер, вердикт не выносим.
    });
  }

  function processCell(s) {
    if (startingStands.has(s.name)) {
      return '<span class="process-starting"><span class="mini-spinner" aria-hidden="true"></span>Запускается…</span>';
    }
    return processBadge(s.process ? s.process.state : "unknown");
  }

  function actionButtons(s) {
    const processState = s.process ? s.process.state : "unknown";
    const isStarting = startingStands.has(s.name);
    const startDisabled = processState === "ok" || isStarting;
    const stopDisabled = processState === "down";
    const restartDisabled = processState === "down";
    const redisNumber = s.redis && s.redis.number;
    const redisKnown = redisNumber !== null && redisNumber !== undefined;
    const redisDisabled = !redisKnown;
    const redisTitle = redisKnown ? "Очистить Redis" : "redis не настроен у стенда";
    const name = escapeHtml(s.name);
    return `
      <button class="icon-btn icon-btn-play" data-action="start" data-name="${name}" title="Запустить"${startDisabled ? " disabled" : ""}>${ICON_PLAY}</button>
      <button class="icon-btn icon-btn-stop" data-action="stop" data-name="${name}" title="Остановить"${stopDisabled ? " disabled" : ""}>${ICON_STOP}</button>
      <button class="icon-btn icon-btn-restart" data-action="restart" data-name="${name}" title="Перезапустить"${restartDisabled ? " disabled" : ""}>${ICON_RESTART}</button>
      <button class="icon-btn icon-btn-redis" data-action="redis-clear" data-name="${name}" title="${escapeHtml(redisTitle)}"${redisDisabled ? " disabled" : ""}>${ICON_REDIS_CLEAR}</button>
    `;
  }

  // --- стенды ---

  let selectedStand = null;

  async function refreshStands() {
    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";
    try {
      const data = await apiGet("/api/stands");
      lastStandsData = data.stands || [];
      checkStartingTransitions(lastStandsData);
      renderStands(lastStandsData);
      updateLogsMenuState();
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
      const tr = document.createElement("tr");
      tr.dataset.name = s.name;
      if (s.name === selectedStand) tr.classList.add("selected");
      tr.innerHTML = `
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.transport)}</td>
        <td>${processCell(s)}</td>
        <td>${httpCell(http)}</td>
        <td>${valueSpan(db.name || "—", db.state)}</td>
        <td class="row-actions">${actionButtons(s)}</td>
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
        if (btn.disabled) return;
        onStandAction(btn.dataset.name, btn.dataset.action);
      });
    });
  }

  function selectStand(name) {
    selectedStand = name;
    document.querySelectorAll(".stands-table tbody tr").forEach((tr) => {
      tr.classList.toggle("selected", tr.dataset.name === name);
    });
    updateLogsMenuState();
    refreshState();
  }

  // --- подтверждения (стилизованная модалка) + обратная связь по действиям ---

  const _ACTION_LABELS = { start: "старт", stop: "остановка", restart: "рестарт", "redis-clear": "очистка Redis" };

  async function onStandAction(name, action) {
    if (action === "stop") {
      const confirmed = await styledConfirm("Остановка стенда", `Остановить стенд ${name}?`);
      if (!confirmed) return;
    } else if (action === "restart") {
      const confirmed = await styledConfirm("Перезапуск стенда", `Перезапустить стенд ${name}?`);
      if (!confirmed) return;
    } else if (action === "redis-clear") {
      const confirmed = await styledConfirm(
        "Очистка Redis",
        `Очистить Redis стенда ${name}? Это действие необратимо.`
      );
      if (!confirmed) return;
    }

    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";

    if (action === "start") {
      // ЧЕСТНЫЙ старт: только сообщаем, что запуск отправлен, "запущен" —
      // только по факту готовности (см. checkStartingTransitions).
      showActionStatus(`Запуск стенда ${name}…`, false);
    } else if (action === "restart") {
      showActionStatus(`Запущен рестарт стенда ${name}`, false);
    }

    try {
      const data = await apiSend("POST", `/api/stand/${encodeURIComponent(name)}/${action}`);
      const pidSuffix = data && typeof data.pid === "number" ? ` (pid ${data.pid})` : "";
      if (action === "start") {
        startingStands.set(name, Date.now());
        renderStands(lastStandsData);
        ensureFastPolling();
      } else if (action === "stop") {
        showActionStatus(`Стенд ${name} остановлен`, false);
      } else if (action === "restart") {
        showActionStatus(`Стенд ${name} перезапущен${pidSuffix}`, false);
      } else if (action === "redis-clear") {
        showActionStatus(data && data.message ? data.message : `Redis стенда ${name} очищен`, false);
      }
      await refreshStands();
      if (name === selectedStand) refreshState();
    } catch (e) {
      const label = _ACTION_LABELS[action] || action;
      showActionStatus(`Ошибка (${label} стенда ${name}): ${e.message}`, true);
      errorEl.textContent = `Ошибка (${name}/${action}): ${e.message}`;
    }
  }

  // --- текущее состояние выбранного стенда (только консоль стенда, source=stand) ---

  async function refreshState() {
    if (!selectedStand) return;
    document.getElementById("state-stand-name").textContent = `(${selectedStand})`;
    try {
      const data = await apiGet(`/api/stand/${encodeURIComponent(selectedStand)}/state?source=stand`);
      document.getElementById("state-output").textContent = data.text || "";
    } catch (e) {
      document.getElementById("state-output").textContent = `Ошибка чтения состояния: ${e.message}`;
    }
  }

  // --- "Открыть папку логов" — сплит-кнопка с выбором источника ---
  //
  // У стенда ДВА разных места логов (см. standkit_hub/logs_browser.py):
  // "stand" — логи самого стенда (<stand_dir>/logs, платформа/сборка) и
  // "bpmkit" — логи BPMkit-ПРОЕКТА (scaffold, <docs_folder>/logs, куда
  // пишутся логи разработки). Это разные каталоги — основная кнопка
  // выполняет последний выбранный источник (по умолчанию "stand"), стрелка
  // раскрывает меню с явным выбором. Пункт "Логи BPMkit-проекта"
  // дизейблится, если у выбранного стенда нет logs.bpmkit_available (см.
  // /api/stands — не задан extra["docs_folder"] либо нет папки
  // <docs_folder>/logs).

  let logsFolderSource = "stand";

  async function openLogsFolder(source) {
    if (!selectedStand) return;
    try {
      const data = await apiSend(
        "POST",
        `/api/stand/${encodeURIComponent(selectedStand)}/logs/open-folder?source=${encodeURIComponent(source)}`
      );
      showActionStatus(
        data.message || (data.ok ? "Папка логов открыта." : "Не удалось открыть папку логов."),
        !data.ok
      );
    } catch (e) {
      showActionStatus(`Ошибка открытия папки логов: ${e.message}`, true);
    }
  }

  function closeLogsMenu() {
    const menu = document.getElementById("log-folder-menu");
    const toggleBtn = document.getElementById("log-folder-menu-toggle-btn");
    menu.hidden = true;
    toggleBtn.setAttribute("aria-expanded", "false");
  }

  function toggleLogsMenu() {
    const menu = document.getElementById("log-folder-menu");
    const toggleBtn = document.getElementById("log-folder-menu-toggle-btn");
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    toggleBtn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  }

  // Дизейблит пункт меню "Логи BPMkit-проекта", если у ВЫБРАННОГО стенда
  // источник bpmkit недоступен (нет extra["docs_folder"] или нет папки
  // <docs_folder>/logs) — см. /api/stands::logs.bpmkit_available. Вызывается
  // после каждого refreshStands() и при смене выбранного стенда, т.к.
  // доступность может измениться между обновлениями (например, после
  // первого запуска разработки в scaffold-проекте).
  function updateLogsMenuState() {
    const bpmkitItem = document.getElementById("log-folder-menu-bpmkit");
    const stand = selectedStand ? lastStandsData.find((s) => s.name === selectedStand) : null;
    const available = Boolean(stand && stand.logs && stand.logs.bpmkit_available);
    bpmkitItem.disabled = !available;
    bpmkitItem.title = available
      ? "Логи BPMkit-проекта"
      : "у стенда не задан docs_folder / нет папки logs";
    // Если ранее выбранный источник стал недоступен — откатываемся на
    // всегда-доступный "stand" по умолчанию.
    if (logsFolderSource === "bpmkit" && !available) {
      logsFolderSource = "stand";
    }
  }

  function setupStatePanel() {
    document.getElementById("log-folder-open-btn").addEventListener("click", () => {
      openLogsFolder(logsFolderSource);
    });
    document.getElementById("log-folder-menu-toggle-btn").addEventListener("click", (evt) => {
      evt.stopPropagation();
      toggleLogsMenu();
    });
    document.querySelectorAll(".split-btn-menu-item").forEach((item) => {
      item.addEventListener("click", (evt) => {
        evt.stopPropagation();
        if (item.disabled) return;
        logsFolderSource = item.dataset.source;
        closeLogsMenu();
        openLogsFolder(logsFolderSource);
      });
    });
    document.addEventListener("click", (evt) => {
      const splitBtn = document.getElementById("log-folder-split-btn");
      if (!splitBtn.contains(evt.target)) closeLogsMenu();
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") closeLogsMenu();
    });
  }

  function setConnStatus(ok) {
    // Понятный индикатор связи с хабом: цветная точка + короткая подпись.
    // Раньше здесь было одинокое "…" — его путали с неактивным "меню из
    // трёх точек". До первого ответа (см. init/index.html) точка серая и
    // подпись "подключение…"; после первого запроса — зелёная/красная.
    const wrapEl = document.getElementById("conn-status");
    const dotEl = document.getElementById("conn-status-dot");
    const labelEl = document.getElementById("conn-status-label");
    dotEl.classList.remove("conn-status-dot-pending", "conn-status-dot-ok", "conn-status-dot-down");
    dotEl.classList.add(ok ? "conn-status-dot-ok" : "conn-status-dot-down");
    labelEl.textContent = ok ? "онлайн" : "нет связи";
    wrapEl.title = ok ? "Связь с хабом установлена" : "Нет связи с хабом";
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
    setupTheme();
    setupTabs();
    setupAboutModal();
    setupRegisterModal();
    setupAgentTab();
    setupSettingsForm();
    setupStatePanel();
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
      if (selectedStand) refreshState();
    }, intervalMs);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
