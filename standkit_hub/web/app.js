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
      const error = new Error(message);
      // Тело ошибки прокидываем на объекте Error: 409 на Стоп/Рестарт несёт
      // описание найденного процесса (adopt_required/candidate), без которого
      // нельзя показать осмысленное подтверждение усыновления.
      error.status = resp.status;
      error.data = data;
      throw error;
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

  // --- тема (light/dark/auto) ---
  //
  // ИСТОЧНИК ПРАВДЫ — HubConfig.theme на сервере, а не localStorage браузера.
  // localStorage привязан к origin (включая ПОРТ), и пока хаб стартовал на
  // эфемерном порту, каждый запуск давал новый origin и пустое хранилище —
  // отсюда жалоба «тема не запоминается». Теперь выбор уходит в конфиг через
  // POST /api/settings, а localStorage остался лишь кэшем на случай, если
  // сервер почему-то не подставил атрибут в <html data-theme>.
  //
  // В data-theme лежит РОВНО то, что в конфиге (light|dark|auto). Разрешать
  // "auto" в конкретную тему здесь НЕЛЬЗЯ: это превратило бы выбор «как в
  // системе» в зафиксированный light/dark при первом же сохранении. Разрешение
  // делает CSS через @media (prefers-color-scheme) — см. style.css.

  const THEME_STORAGE_KEY = "standkit_theme";
  // Порядок обхода по клику на переключателе.
  const THEMES = ["auto", "light", "dark"];
  const THEME_LABELS = { auto: "как в системе", light: "светлая", dark: "тёмная" };

  function normalizeTheme(value) {
    return THEMES.indexOf(value) >= 0 ? value : "auto";
  }

  function readCachedTheme() {
    try {
      return localStorage.getItem(THEME_STORAGE_KEY);
    } catch (e) {
      // Приватный режим / отключённое хранилище — не повод ломать дашборд.
      return null;
    }
  }

  function currentTheme() {
    return normalizeTheme(document.documentElement.getAttribute("data-theme"));
  }

  function applyTheme(theme) {
    const normalized = normalizeTheme(theme);
    document.documentElement.setAttribute("data-theme", normalized);
    const btn = document.getElementById("theme-toggle-btn");
    if (btn) {
      btn.title = `Тема: ${THEME_LABELS[normalized]} (клик — следующая)`;
      btn.setAttribute("aria-label", `Тема: ${THEME_LABELS[normalized]}`);
    }
    try {
      localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch (e) {
      /* см. readCachedTheme */
    }
    return normalized;
  }

  function setupTheme() {
    // Сервер уже подставил тему в <html data-theme> при отдаче index.html —
    // ничего перерисовывать не нужно, только зафиксировать состояние кнопки.
    // Плейсхолдер остался незаменённым (страница открыта не через хаб) —
    // падаем на кэш, затем на "auto".
    const fromServer = document.documentElement.getAttribute("data-theme");
    const known = THEMES.indexOf(fromServer) >= 0;
    applyTheme(known ? fromServer : readCachedTheme() || "auto");

    document.getElementById("theme-toggle-btn").addEventListener("click", async () => {
      const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
      applyTheme(next);
      try {
        await apiSend("POST", "/api/settings", { theme: next });
      } catch (e) {
        // Тема применена визуально, но не сохранена — честно говорим об этом,
        // иначе после перезагрузки пользователь молча получит прежнюю.
        showActionStatus(`Тема применена, но не сохранена: ${e.message}`, true);
      }
    });
  }

  // --- режим отображения: полный дашборд / компактное окно-виджет ---

  const VIEWS = ["full", "compact"];

  function currentView() {
    const value = document.documentElement.getAttribute("data-view");
    return VIEWS.indexOf(value) >= 0 ? value : "full";
  }

  /**
   * Переключает режим перезагрузкой с другим ``?view=``, а не переставляя
   * атрибут на лету.
   *
   * Так режим переживает перезагрузку страницы, попадает в закладку и в
   * ярлык PWA (shortcut «Компактный режим» в manifest.webmanifest), а сервер
   * успевает проставить data-view ДО выполнения JS — компактное окно не
   * мигает полноразмерным дашбордом. Сессионный токен при этом не теряется:
   * он лежит в HttpOnly-cookie, выставленной при первом заходе.
   */
  function setupViewToggle() {
    const btn = document.getElementById("view-toggle-btn");
    if (!btn) return;

    const isCompact = currentView() === "compact";
    btn.textContent = isCompact ? "▣" : "▭";
    btn.title = isCompact ? "Обычный режим" : "Компактный режим";
    btn.setAttribute("aria-label", btn.title);

    btn.addEventListener("click", () => {
      const url = new URL(window.location.href);
      if (isCompact) {
        url.searchParams.delete("view");
      } else {
        url.searchParams.set("view", "compact");
      }
      // Токен из адресной строки не тащим: он уже в cookie, а в истории
      // браузера ему делать нечего.
      url.searchParams.delete("t");
      window.location.assign(url.toString());
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
    document.getElementById("iis-detect-status").textContent = "";
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

  // Кнопка «Определить автоматически» для host_kind=iis: спрашивает сервер,
  // какой IIS-сайт соответствует введённым каталогу/порту (POST /api/iis/detect),
  // и заполняет iis_site/iis_app_pool. Пользователь всегда может исправить
  // подставленные значения — это подсказка, а не автоматика вместо него.
  async function detectIisSite(form) {
    const statusEl = document.getElementById("iis-detect-status");
    const btn = document.getElementById("iis-detect-btn");
    const standDir = form.elements.namedItem("stand_dir").value.trim();
    const standPort = form.elements.namedItem("stand_port").value.trim();
    if (!standDir) {
      statusEl.textContent = "Сначала укажите каталог стенда (stand_dir).";
      return;
    }
    btn.disabled = true;
    statusEl.textContent = "Ищем сайт в IIS…";
    try {
      const data = await apiSend("POST", "/api/iis/detect", {
        stand_dir: standDir,
        stand_port: standPort ? Number(standPort) : 0,
      });
      const match = (data && data.match) || {};
      if (match.site) form.elements.namedItem("iis_site").value = match.site;
      if (match.app_pool) form.elements.namedItem("iis_app_pool").value = match.app_pool;
      const how = match.matched_by === "binding" ? "по биндингу порта" : "по каталогу сайта";
      statusEl.textContent = `Найден сайт «${match.site || "?"}» (${how}).`;
    } catch (e) {
      statusEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function setupRegisterModal() {
    const overlay = document.getElementById("register-modal-overlay");
    const form = document.getElementById("register-form");

    document.getElementById("iis-detect-btn").addEventListener("click", () => detectIisSite(form));

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
  //
  // Тост ВСЕГДА гаснет сам: раньше ошибки висели «до следующего действия»
  // и, будучи position:fixed поверх панели инструментов, перехватывали клики
  // по кнопке «Обновить» — из-за чего казалось, что кнопка не реагирует.
  // Теперь: ошибка живёт дольше успеха, но конечное время; есть крестик и
  // закрытие по Escape; сам блок не ловит мышь (pointer-events см. в CSS),
  // клики проходят сквозь него к кнопкам под ним.

  let actionStatusTimer = null;
  const ACTION_STATUS_TTL_OK = 6000;
  const ACTION_STATUS_TTL_ERROR = 20000;

  function hideActionStatus() {
    const el = document.getElementById("action-status");
    if (!el) return;
    el.classList.remove("action-status-visible");
    if (actionStatusTimer) {
      clearTimeout(actionStatusTimer);
      actionStatusTimer = null;
    }
  }

  function showActionStatus(message, isError) {
    const el = document.getElementById("action-status");
    const textEl = document.getElementById("action-status-text") || el;
    textEl.textContent = message;
    el.classList.toggle("action-status-error", !!isError);
    el.classList.toggle("action-status-ok", !isError);
    el.classList.add("action-status-visible");
    if (actionStatusTimer) {
      clearTimeout(actionStatusTimer);
      actionStatusTimer = null;
    }
    actionStatusTimer = setTimeout(
      () => el.classList.remove("action-status-visible"),
      isError ? ACTION_STATUS_TTL_ERROR : ACTION_STATUS_TTL_OK
    );
  }

  function setupActionStatus() {
    const closeBtn = document.getElementById("action-status-close");
    if (closeBtn) closeBtn.addEventListener("click", hideActionStatus);
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") hideActionStatus();
    });
  }

  // --- бэйджи статуса ---
  //
  // "pending" — пробы ЕЩЁ НЕ выполнялись (ответ на /api/stands?probe=0 либо
  // первый круг фонового опроса на сервере, см. server.py::PENDING_PROBE_STATE).
  // Это принципиально НЕ "unknown": "unknown" — честный результат выполненной
  // проверки («проверять нечем»), а здесь проверки просто ещё не было.

  const PENDING_STATE = "pending";
  const PENDING_LABEL = "проверяется…";

  function badgeClass(state) {
    switch (state) {
      case "ok":
        return "badge badge-ok";
      case "down":
        return "badge badge-down";
      case "skipped":
        return "badge badge-skipped";
      case PENDING_STATE:
        return "badge badge-pending";
      default:
        return "badge badge-unknown";
    }
  }

  function badge(state) {
    const label = state === PENDING_STATE ? PENDING_LABEL : state || "unknown";
    return `<span class="${badgeClass(state)}">${label}</span>`;
  }

  function processBadge(state) {
    const label =
      state === "ok"
        ? "up"
        : state === "down"
        ? "down"
        : state === PENDING_STATE
        ? PENDING_LABEL
        : state || "unknown";
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
      case PENDING_STATE:
        return "value-pending";
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

  // Ячейка Redis: показывает НОМЕР базы Redis стенда (тот же, что фигурирует
  // при очистке Redis), цвет — по состоянию пробы. Прочерк, если у стенда
  // Redis не настроен.
  function redisCell(redis) {
    const num = redis && redis.number;
    if (num === null || num === undefined) {
      return `<span class="value-cell value-muted" title="Redis не настроен у стенда">—</span>`;
    }
    return `<span class="value-cell ${valueClass(redis.state)}" title="Номер базы Redis">${escapeHtml(String(num))}</span>`;
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

  // Бейдж «вне диспетчера»: стенд жив, но поднят мимо диспетчера (нет живого
  // pidfile), поэтому Стоп/Рестарт по нему потребуют усыновления — см.
  // /api/stands::process.external и onStandAction. Показываем ДО нажатия
  // кнопок, чтобы состояние не выяснялось методом получения отказа.
  const ICON_EXTERNAL_TITLE =
    "Стенд запущен вне диспетчера: pid неизвестен. Стоп/Рестарт спросят подтверждение, " +
    "чтобы взять процесс под управление.";

  function processCell(s) {
    if (startingStands.has(s.name)) {
      return '<span class="process-starting"><span class="mini-spinner" aria-hidden="true"></span>Запускается…</span>';
    }
    const process = s.process || {};
    let html = processBadge(process.state);
    if (process.reason) {
      // Причина от бэкенда хостинга (IIS: сайт/пул остановлен, порт держит
      // http.sys) — иначе наружу уходил бы один неинформативный "down".
      html = `<span title="${escapeAttr(process.reason)}">${html}</span>`;
    }
    if (process.external) {
      html += ` <span class="badge badge-external" title="${escapeAttr(ICON_EXTERNAL_TITLE)}">вне диспетчера</span>`;
    }
    return html;
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

  // Единая точка применения ответа /api/stands (и SSE-события "stands"):
  // отрисовка + подхват интервала автообновления + отметка возраста данных.
  function applyStandsPayload(data) {
    lastStandsData = (data && data.stands) || [];
    applyRefreshInterval(data && data.refresh_interval_sec);
    checkStartingTransitions(lastStandsData);
    renderStands(lastStandsData);
    updateLogsMenuState();
    // Появился (или исчез) стенд с transport=agent — блок параметров демона
    // в настройках должен появиться или спрятаться без перезагрузки страницы.
    // Здесь, а не в refreshStands: применение данных приходит и из SSE.
    updateAgentBlockVisibility(lastStandsData);
    updateSnapshotAge(data);
  }

  // Возвращает true, если данные реально обновились (нужно вызывающему,
  // чтобы отличить успех от ошибки — см. refreshStandsWithFeedback).
  async function refreshStands() {
    const errorEl = document.getElementById("stands-error");
    errorEl.textContent = "";
    try {
      const data = await apiGet("/api/stands");
      applyStandsPayload(data);
      setConnStatus(true);
      return true;
    } catch (e) {
      errorEl.textContent = `Ошибка обновления: ${e.message}`;
      setConnStatus(false);
      return false;
    }
  }

  // ПЕРВАЯ ОТРИСОВКА: ?probe=0 — слепок реестра БЕЗ единой сетевой пробы.
  // Ответ приходит за десятки миллисекунд, даже если половина стендов сидит
  // за firewall'ом с DROP, поэтому таблица и индикатор связи появляются сразу
  // (раньше страница висела серой всё время полного опроса). Все пробы в этом
  // ответе имеют состояние "pending" — "проверяется…" в таблице. Полные
  // статусы приезжают вторым запросом либо push'ем через SSE.
  async function firstPaint() {
    try {
      const data = await apiGet("/api/stands?probe=0");
      applyStandsPayload(data);
      setConnStatus(true);
    } catch (e) {
      document.getElementById("stands-error").textContent = `Ошибка обновления: ${e.message}`;
      setConnStatus(false);
    }
    await refreshStands();
  }

  // Ручное обновление по кнопке — с видимой обратной связью: кнопка
  // блокируется и меняет подпись на «Обновление…», по завершении показывается
  // подтверждение со временем (у автообновления по таймеру этого нет, чтобы не
  // мигать постоянно).
  //
  // Локальный ответ приходит за десятки миллисекунд, поэтому подпись держим
  // минимум MIN_BUSY_MS — иначе смена текста незаметна и кнопка выглядит
  // «мёртвой». Итог показываем ВСЕГДА: успех — подтверждение, ошибку — тост,
  // а не только запись в мелкий #stands-error, которую легко не заметить.
  const MIN_BUSY_MS = 350;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function refreshStandsWithFeedback() {
    const btn = document.getElementById("refresh-stands-btn");
    if (btn.disabled) return;
    const orig = btn.dataset.label || btn.textContent;
    btn.dataset.label = orig;
    const startedAt = Date.now();
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.textContent = "Обновление…";
    let ok = false;
    try {
      ok = await refreshStands();
    } finally {
      const elapsed = Date.now() - startedAt;
      if (elapsed < MIN_BUSY_MS) await sleep(MIN_BUSY_MS - elapsed);
      btn.textContent = orig;
      btn.classList.remove("is-busy");
      btn.disabled = false;
    }
    if (ok) {
      showActionStatus(`Список обновлён • ${new Date().toLocaleTimeString()}`, false);
    } else {
      const detail = document.getElementById("stands-error").textContent || "нет связи с хабом";
      showActionStatus(detail, true);
    }
  }

  function renderStands(stands) {
    const tbody = document.getElementById("stands-tbody");
    tbody.innerHTML = "";
    stands.forEach((s) => {
      const http = s.http || {};
      const db = s.db || {};
      const redis = s.redis || {};
      const tr = document.createElement("tr");
      tr.dataset.name = s.name;
      if (s.name === selectedStand) tr.classList.add("selected");
      tr.innerHTML = `
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.transport)}</td>
        <td>${processCell(s)}</td>
        <td>${httpCell(http)}</td>
        <td>${valueSpan(db.name || "—", db.state)}</td>
        <td>${redisCell(redis)}</td>
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

  // --- усыновление стенда, поднятого вне диспетчера ---
  //
  // Сервер на Стоп/Рестарт такого стенда отвечает 409 с описанием найденного
  // процесса (adopt_required + candidate) и НИЧЕГО не убивает. Пользователь
  // видит, что именно предлагается остановить (pid, образ, каталог), и только
  // после явного согласия запрос повторяется с ?force=1. Молчаливого kill нет
  // ни на одной ветке — это требование безопасности, а не UX-украшение.

  function describeCandidate(candidate) {
    if (!candidate) return "";
    const parts = [`PID ${candidate.pid}`];
    if (candidate.image) parts.push(candidate.image);
    const where = candidate.cwd || candidate.exe_path || candidate.cmdline;
    if (where) parts.push(`каталог ${where}`);
    return parts.join(", ");
  }

  async function confirmAdoption(name, action, candidate) {
    const what = action === "restart" ? "перезапустить" : "остановить";
    return styledConfirm(
      "Стенд запущен вне диспетчера",
      `Стенд ${name} поднят не диспетчером. Найден процесс ${describeCandidate(candidate)}. ` +
        `Взять его под управление и ${what}?`
    );
  }

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
      let data;
      try {
        data = await apiSend("POST", `/api/stand/${encodeURIComponent(name)}/${action}`);
      } catch (e) {
        // 409 «нужно усыновление»: спрашиваем и, если согласились, повторяем
        // ровно тот же запрос с ?force=1. Отказ — тихий выход без ошибки.
        const payload = e.data;
        if (!payload || !payload.adopt_required) throw e;
        const confirmed = await confirmAdoption(name, action, payload.candidate);
        if (!confirmed) return;
        data = await apiSend(
          "POST",
          `/api/stand/${encodeURIComponent(name)}/${action}?force=1`
        );
      }
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

  // --- интервал автообновления ---
  //
  // Значение приезжает С СЕРВЕРА в каждом ответе /api/stands и в SSE-событии
  // (HubConfig.refresh_interval_sec). Раньше здесь стояла константа 10000, а
  // одноимённое поле формы настроек ни на что не влияло. Теперь изменение
  // применяется без перезагрузки страницы: старый таймер снимается через
  // clearInterval и заводится новый.

  const DEFAULT_REFRESH_INTERVAL_SEC = 10;
  // Нижняя граница — зеркало poller.MIN_POLL_INTERVAL_SEC на сервере: чаще
  // опрашивать всё равно нечего, снапшот обновляется не быстрее.
  const MIN_REFRESH_INTERVAL_SEC = 2;

  let refreshIntervalMs = DEFAULT_REFRESH_INTERVAL_SEC * 1000;
  let backgroundTimer = null;

  function backgroundTick() {
    // Пока жив SSE, список стендов приходит push'ем — дёргать /api/stands
    // таймером незачем. Остальное (локальный агент, панель состояния) через
    // SSE не ходит и обновляется по таймеру всегда.
    if (!sseHealthy) refreshStands();
    refreshAgentStatus();
    if (selectedStand) refreshState();
  }

  function restartBackgroundTimer() {
    if (backgroundTimer !== null) {
      clearInterval(backgroundTimer);
      backgroundTimer = null;
    }
    backgroundTimer = setInterval(backgroundTick, refreshIntervalMs);
  }

  function applyRefreshInterval(seconds) {
    const parsed = Number(seconds);
    if (!Number.isFinite(parsed) || parsed <= 0) return;
    const ms = Math.max(MIN_REFRESH_INTERVAL_SEC, parsed) * 1000;
    if (ms === refreshIntervalMs && backgroundTimer !== null) return;
    refreshIntervalMs = ms;
    restartBackgroundTimer();
  }

  // --- возраст снапшота ---
  //
  // Хаб отдаёт не «живой» опрос, а снапшот фонового поллера с отметкой
  // времени (generated_at/age_sec). Честно показываем возраст — но ТОЛЬКО
  // когда он о чём-то говорит: пробы ещё не выполнялись либо снапшот старше
  // двух периодов обновления (значит, фоновый опрос буксует). В норме
  // элемент пуст и не отвлекает.

  function formatAge(sec) {
    const whole = Math.round(sec);
    if (whole < 60) return `${whole} с`;
    const minutes = Math.round(whole / 60);
    if (minutes < 60) return `${minutes} мин`;
    return `${Math.round(minutes / 60)} ч`;
  }

  function updateSnapshotAge(data) {
    const el = document.getElementById("stands-age");
    if (!el) return;
    if (data && data.probed === false) {
      el.textContent = "статусы проверяются…";
      el.title = "Показан слепок реестра, пробы ещё выполняются";
      return;
    }
    const age = data ? Number(data.age_sec) : NaN;
    if (!Number.isFinite(age) || age <= (refreshIntervalMs / 1000) * 2) {
      el.textContent = "";
      el.title = "";
      return;
    }
    el.textContent = `данные от ${formatAge(age)} назад`;
    el.title = "Фоновый опрос давно не обновлял снапшот состояния стендов";
  }

  // --- поток обновлений (SSE) ---
  //
  // GET /api/events (text/event-stream) — сервер сам присылает новый снапшот,
  // как только фоновый поллер его собрал. EventSource не умеет слать
  // кастомные заголовки, поэтому авторизация идёт по той же сессионной
  // HttpOnly-cookie, что и у обычных GET /api/* — второго механизма токенов
  // не заводим (см. шапку файла). credentials для same-origin EventSource
  // отправляются по умолчанию.
  //
  // Обрыв — штатная ситуация: браузер переподключается сам (сервер шлёт
  // "retry: 5000"), а до восстановления список обновляет резервный таймер.
  // Если поток не поднимается подряд SSE_MAX_FAILURES раз (сервер без
  // фонового опроса отвечает 503, прокси режет event-stream) — закрываем его
  // совсем и честно живём на опросе.

  const SSE_MAX_FAILURES = 3;

  let eventSource = null;
  let sseHealthy = false;
  let sseFailures = 0;

  function setupEventStream() {
    if (typeof EventSource === "undefined") return;
    let source;
    try {
      source = new EventSource("/api/events");
    } catch (e) {
      return;
    }
    eventSource = source;

    source.addEventListener("open", () => {
      sseFailures = 0;
      sseHealthy = true;
    });

    source.addEventListener("stands", (evt) => {
      let data = null;
      try {
        data = JSON.parse(evt.data);
      } catch (e) {
        return;
      }
      sseFailures = 0;
      sseHealthy = true;
      document.getElementById("stands-error").textContent = "";
      applyStandsPayload(data);
      setConnStatus(true);
    });

    source.addEventListener("error", () => {
      sseHealthy = false;
      sseFailures += 1;
      if (sseFailures >= SSE_MAX_FAILURES) {
        source.close();
        if (eventSource === source) eventSource = null;
      }
    });

    // Уход со страницы освобождает поток ThreadingHTTPServer на сервере, а не
    // ждёт, пока heartbeat упрётся в закрытый сокет.
    window.addEventListener("pagehide", () => {
      source.close();
      sseHealthy = false;
    });
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

  // Поля блока «Агент (расширенное)» — зеркалят флаги CLI standkit-agent и
  // нужны только администратору хоста удалённого стенда. Список отдельно от
  // SETTINGS_FIELDS, потому что по нему решается, показывать ли блок вообще.
  const AGENT_FIELDS = [
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

  // Поля, которые имеет смысл валидировать на клиенте до отправки: пользователь
  // получает ответ мгновенно и рядом с полем, а не общей строкой ошибки снизу.
  const PORT_FIELDS = ["agent_port"];
  const POSITIVE_INT_FIELDS = [
    "refresh_interval_sec",
    "lockout_max_failures",
    "lockout_window_sec",
  ];
  const PATH_FIELDS = ["registry_path", "run_dir", "log_dir", "tls_cert", "tls_key", "tls_client_ca", "audit_log"];

  // Символы, недопустимые в пути и на Windows, и на Linux. Полноценную проверку
  // существования делает сервер — здесь отсекаем только заведомый мусор
  // (например, вставленную из терминала команду вместо пути).
  const BAD_PATH_CHARS = /[<>"|?*\r\n\t]/;

  function fieldLabel(input) {
    const label = input.closest("label");
    if (!label) return input.name;
    const text = label.childNodes[0] && label.childNodes[0].textContent;
    return (text || input.name).trim();
  }

  function setFieldError(input, message) {
    const label = input.closest("label") || input.parentElement;
    if (!label) return;
    let errorEl = label.querySelector(".field-error");
    if (!message) {
      if (errorEl) errorEl.remove();
      input.removeAttribute("aria-invalid");
      return;
    }
    if (!errorEl) {
      errorEl = document.createElement("small");
      errorEl.className = "field-error";
      label.appendChild(errorEl);
    }
    errorEl.textContent = message;
    input.setAttribute("aria-invalid", "true");
  }

  /**
   * Проверяет форму настроек на клиенте. Возвращает список сообщений об
   * ошибках (пустой — всё в порядке) и подсвечивает проблемные поля.
   *
   * Это удобство, а не защита: сервер валидирует то же самое независимо —
   * форма не единственный способ записать конфиг.
   */
  function validateSettingsForm(form) {
    const problems = [];
    SETTINGS_FIELDS.forEach((field) => {
      const input = form.elements.namedItem(field);
      if (!input) return;
      setFieldError(input, "");
      const raw = (input.value || "").trim();
      if (!raw) return; // пустое поле = «взять дефолт», это законно

      if (PORT_FIELDS.includes(field)) {
        const port = Number(raw);
        if (!Number.isInteger(port) || port < 1 || port > 65535) {
          const msg = "порт должен быть целым числом от 1 до 65535";
          setFieldError(input, msg);
          problems.push(`${fieldLabel(input)}: ${msg}`);
        }
        return;
      }

      if (POSITIVE_INT_FIELDS.includes(field)) {
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 1) {
          const msg = "значение должно быть положительным числом";
          setFieldError(input, msg);
          problems.push(`${fieldLabel(input)}: ${msg}`);
        }
        return;
      }

      if (PATH_FIELDS.includes(field) && BAD_PATH_CHARS.test(raw)) {
        const msg = "путь содержит недопустимые символы";
        setFieldError(input, msg);
        problems.push(`${fieldLabel(input)}: ${msg}`);
      }
    });

    currentAgents.forEach((agent, idx) => {
      const hasAny = (agent.name || agent.url || agent.token_ref || "").length > 0;
      if (!hasAny) return;
      if (!agent.name) problems.push(`Удалённый агент №${idx + 1}: не указано имя`);
      if (!agent.url) {
        problems.push(`Удалённый агент №${idx + 1}: не указан url`);
      } else if (!/^https?:\/\/.+/i.test(agent.url.trim())) {
        problems.push(`Удалённый агент №${idx + 1}: url должен начинаться с http:// или https://`);
      }
    });

    return problems;
  }

  /**
   * Показывает блок «Агент (расширенное)» только тогда, когда он может
   * понадобиться: в реестре есть хотя бы один стенд с transport=agent, либо
   * уже настроен удалённый агент, либо какое-то из полей блока непусто
   * (иначе пользователь не смог бы увидеть то, что сам когда-то ввёл).
   */
  function updateAgentBlockVisibility(stands) {
    const block = document.getElementById("settings-agent-advanced");
    if (!block) return;
    const form = document.getElementById("settings-form");
    const hasAgentStand = (stands || []).some(
      (s) => s && s.process && s.process.transport === "agent"
    );
    const hasRemoteAgents = currentAgents.length > 0;
    const hasFilledField = AGENT_FIELDS.some((field) => {
      const input = form && form.elements.namedItem(field);
      return input && String(input.value || "").trim() !== "";
    });
    block.hidden = !(hasAgentStand || hasRemoteAgents || hasFilledField);
  }

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

    const counter = document.getElementById("remote-agents-count");
    if (counter) {
      counter.textContent = currentAgents.length ? ` — настроено: ${currentAgents.length}` : "";
    }
  }

  async function loadSettings() {
    const data = await apiGet("/api/settings");
    const form = document.getElementById("settings-form");
    const defaults = data.defaults || {};
    SETTINGS_FIELDS.forEach((field) => {
      const input = form.elements.namedItem(field);
      if (!input) return;
      input.value = data[field] ?? "";
      setFieldError(input, "");
      // Пустое поле означает «взять дефолт» — показываем, какой именно, чтобы
      // пользователю не приходилось гадать или лезть в --help.
      const fallback = defaults[field];
      input.placeholder =
        fallback === undefined || fallback === null || fallback === ""
          ? "не задано"
          : String(fallback);
    });
    form.elements.namedItem("insecure").checked = !!data.insecure;
    // Тема — из конфига (источник правды). Обычно совпадает с тем, что уже
    // подставил сервер в <html data-theme>; расхождение возможно, если конфиг
    // правили снаружи (руками, вторым экземпляром хаба).
    applyTheme(data.theme);
    // Интервал автообновления применяем сразу после сохранения настроек —
    // не дожидаясь следующего ответа /api/stands.
    applyRefreshInterval(data.refresh_interval_sec);
    currentAgents = (data.agents || []).map((a) => ({ ...a }));
    renderAgentsList();
    updateAgentBlockVisibility(lastStandsData);
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
      statusEl.classList.remove("status-error");

      const problems = validateSettingsForm(form);
      if (problems.length) {
        statusEl.textContent =
          problems.length === 1
            ? `Не сохранено — ${problems[0]}`
            : `Не сохранено — ошибок: ${problems.length}. ${problems.join("; ")}`;
        statusEl.classList.add("status-error");
        const firstBad = form.querySelector('[aria-invalid="true"]');
        if (firstBad) firstBad.focus();
        return;
      }

      const payload = {};
      SETTINGS_FIELDS.forEach((field) => {
        const input = form.elements.namedItem(field);
        if (!input) return;
        payload[field] = input.type === "number" ? Number(input.value) : input.value;
      });
      payload.insecure = form.elements.namedItem("insecure").checked;
      payload.agents = currentAgents;
      try {
        // Тему в payload сознательно НЕ кладём: её меняет только переключатель
        // в шапке, а сервер мержит тело поверх текущего конфига — значение
        // сохранится само.
        const saved = await apiSend("POST", "/api/settings", payload);
        statusEl.textContent = "Настройки сохранены";
        // Новый refresh_interval_sec применяем немедленно, без перезагрузки
        // страницы (старый таймер снимается внутри applyRefreshInterval).
        applyRefreshInterval(saved && saved.refresh_interval_sec);
        await refreshSecretStatuses();
      } catch (e) {
        statusEl.textContent = `Ошибка сохранения: ${e.message}`;
      }
    });

    document.getElementById("add-agent-btn").addEventListener("click", () => {
      currentAgents.push({ name: "", url: "", token_ref: "" });
      renderAgentsList();
      // Появился удалённый агент — параметры демона стали релевантны.
      updateAgentBlockVisibility(lastStandsData);
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
    setupViewToggle();
    setupTabs();
    setupAboutModal();
    setupRegisterModal();
    setupAgentTab();
    setupSettingsForm();
    setupStatePanel();
    setupActionStatus();
    document.getElementById("refresh-stands-btn").addEventListener("click", refreshStandsWithFeedback);

    // Быстрая первая отрисовка (?probe=0) + полный статус вторым запросом.
    firstPaint();
    // Push-обновления; при их отсутствии работает резервный таймер ниже.
    setupEventStream();
    refreshAgentStatus();
    loadSettings().catch((e) => {
      document.getElementById("settings-status").textContent = `Ошибка загрузки настроек: ${e.message}`;
    });

    // Стартовый период — дефолтный; реальный refresh_interval_sec приедет с
    // первым же ответом /api/stands и перезаведёт таймер (applyRefreshInterval).
    restartBackgroundTimer();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
