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
        showActionStatus(`Тема применена, но не сохранена: ${describeApiError(e)}`, true);
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
          versionEl.textContent = `ошибка: ${describeApiError(e)}`;
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
    const scheme = form.elements.namedItem("stand_scheme").value;
    form.querySelectorAll(".register-conditional[data-when-transport]").forEach((el) => {
      el.hidden = el.dataset.whenTransport !== transport;
    });
    form.querySelectorAll(".register-conditional[data-when-host-kind]").forEach((el) => {
      el.hidden = el.dataset.whenHostKind !== hostKind;
    });
    // verify_tls показываем только при stand_scheme=https: на http флаг ничего
    // не меняет и лишь путает (GAP-001). Механика — та же самая, третий
    // атрибут data-when-*, а не отдельная ветка «на новый лад».
    form.querySelectorAll(".register-conditional[data-when-scheme]").forEach((el) => {
      el.hidden = el.dataset.whenScheme !== scheme;
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
    // reset() возвращает КАЖДОЕ поле к его разметочному дефолту — в том числе
    // select схемы (http) и чекбокс verify_tls (checked). Пересчёт условных
    // блоков строго ПОСЛЕ reset: иначе видимость осталась бы от прошлого
    // открытия и не совпала бы со значениями в полях.
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

  // Поля формы, которые вообще уходят в JSON-тело запроса. Текстовые — только
  // НЕПУСТЫЕ (сервер и так игнорирует пустые строки, но так тело запроса
  // компактнее и понятнее в логах/отладке); чекбоксы — всегда, настоящим
  // boolean (см. collectRegisterPayload). Список обязан быть подмножеством
  // server.py::_REGISTER_ALLOWED_FIELDS плюс "name" (его сервер обрабатывает
  // отдельно, как ключ записи реестра) — согласованность держит тест
  // tests/test_hub_register.py. Пароли в форме сознательно отсутствуют —
  // только secret_ref_* (agent_secret_ref).
  const _REGISTER_FIELD_NAMES = [
    "name",
    "transport",
    "host_kind",
    "stand_dir",
    "logs_dir",
    "stand_scheme",
    "verify_tls",
    "stand_host",
    "stand_port",
    "db_type",
    "db_host",
    "db_port",
    "db_name",
    "redis_host",
    "redis_port",
    "agent_url",
    "agent_secret_ref",
    // Доверие к сертификату АГЕНТА (GAP-008). Живут в условном блоке
    // транспорта agent (data-when-transport), поэтому при transport=local не
    // уезжают на сервер вовсе — включая чекбокс (см. ветку block.hidden ниже).
    "agent_ca",
    "agent_verify_tls",
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

      // Поле из СКРЫТОГО условного блока не отправляем вовсе. Для текстовых
      // полей (agent_*/iis_*/docker_*/k8s_*) это выходило само собой: скрытый
      // блок обычно пуст, а пустая строка отсекается ниже. Для чекбокса так не
      // выйдет — значение у него есть всегда, и при stand_scheme=http в реестр
      // уезжал бы бессмысленный verify_tls. Одно правило закрывает оба случая
      // и заодно чинит старую мелочь: заполнил IIS-поля, передумал и выбрал
      // docker — их значения больше не едут на сервер.
      const block = input.closest ? input.closest(".register-conditional") : null;
      if (block && block.hidden) return;

      if (input.type === "checkbox") {
        // ГРАБЛЯ (GAP-001, п.2): у чекбокса input.value — это "on" независимо
        // от того, снят флажок или нет. Если собирать его как строку, ветка
        // "пустое не отправляем" ниже никогда не сработает, а СНЯТЫЙ флажок
        // приедет как "on" — то есть «выключено» превратится во «включено».
        // Убрать же чекбокс из тела при снятом флажке тоже нельзя: сервер
        // применит дефолт модели (verify_tls=true) и молча вернёт проверку
        // сертификата. Единственный правильный вариант — настоящий boolean из
        // .checked, и БЕЗ проверки на пустоту (false — валидное значение).
        payload[field] = input.checked;
        return;
      }

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
      statusEl.textContent = describeApiError(e);
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
    form.elements.namedItem("stand_scheme").addEventListener("change", updateRegisterConditionalFields);

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
        showRegisterFormError(describeApiError(e));
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

  // Причина отказа пробы, пришедшая с сервера (http.reason / redis.reason,
  // см. GAP-002/GAP-003), показывается ровно тем же приёмом, что и
  // process.reason в processCell: подсказка в title. Плюс класс
  // .value-has-reason — тонкий пунктир под значением, чтобы оператор ВООБЩЕ
  // понял, что сюда есть смысл навести мышь (голый title невидим).
  function reasonAttrs(reason) {
    if (!reason) return { cls: "", attr: "" };
    return { cls: " value-has-reason", attr: ` title="${escapeAttr(reason)}"` };
  }

  // Ячейка HTTP: если у стенда есть URL — отдаём кликабельную ссылку
  // (открывается в новой вкладке), иначе — прочерк. Цвет по состоянию пробы.
  //
  // http.reason объясняет `down` (таймаут, отказ соединения, TLS-ошибка с
  // подсказкой «задайте stand_scheme=https») и содержит фактический URL, по
  // которому стучались, — раньше наружу уходило одно неинформативное слово.
  function httpCell(http) {
    const url = http && http.url;
    const cls = valueClass(http && http.state);
    // title вешаем на саму ссылку: href/target/rel не трогаем, клик и открытие
    // в новой вкладке работают как прежде. Прочерк (url пуст) — тоже с title.
    const reason = reasonAttrs(http && http.reason);
    if (!url) {
      return `<span class="value-cell ${cls}${reason.cls}"${reason.attr}>—</span>`;
    }
    return `<a class="value-cell value-link ${cls}${reason.cls}" href="${escapeAttr(url)}"${reason.attr} target="_blank" rel="noopener">${escapeHtml(url)}</a>`;
  }

  // Ячейка Redis: показывает НОМЕР базы Redis стенда (тот же, что фигурирует
  // при очистке Redis), цвет — по состоянию пробы. Прочерк, если у стенда
  // Redis не настроен.
  //
  // redis.reason различает «адрес Redis не задан в реестре» и «задан, но
  // недоступен» (GAP-003, п.4) — раньше обе ситуации выглядели одинаково, с
  // жёстко зашитым «Redis не настроен у стенда». Этот текст остался фолбэком
  // на случай ответа без reason (старый агент или снапшот без проб).
  function redisCell(redis) {
    const num = redis && redis.number;
    const reason = reasonAttrs(redis && redis.reason);
    if (num === null || num === undefined) {
      const title = (redis && redis.reason) || "Redis не настроен у стенда";
      return `<span class="value-cell value-muted${reason.cls}" title="${escapeAttr(title)}">—</span>`;
    }
    const title = (redis && redis.reason) || "Номер базы Redis";
    return `<span class="value-cell ${valueClass(redis.state)}${reason.cls}" title="${escapeAttr(title)}">${escapeHtml(String(num))}</span>`;
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
      // Сетевой сбой уже подробно описан в баннере — не дублируем длинный
      // текст ещё и в строке тулбара.
      errorEl.textContent = isNetworkError(e) ? "" : `Ошибка обновления: ${describeApiError(e)}`;
      setConnStatus(false, e);
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
      document.getElementById("stands-error").textContent = isNetworkError(e)
        ? ""
        : `Ошибка обновления: ${describeApiError(e)}`;
      setConnStatus(false, e);
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
      showActionStatus(`Ошибка (${label} стенда ${name}): ${describeApiError(e)}`, true);
      errorEl.textContent = `Ошибка (${name}/${action}): ${describeApiError(e)}`;
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
      document.getElementById("state-output").textContent = `Ошибка чтения состояния: ${describeApiError(e)}`;
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
      showActionStatus(`Ошибка открытия папки логов: ${describeApiError(e)}`, true);
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

  // Человеческий текст вместо браузерного «Failed to fetch».
  //
  // fetch отвергается TypeError'ом одинаково и когда процесс диспетчера убит,
  // и когда он ещё не поднялся — различить нельзя, поэтому формулировка
  // покрывает оба случая и говорит, что делать. Отдельно разбираем 401: он
  // означает не «нет связи», а «сессия не подтверждена» — типично после
  // полного закрытия браузера (сессионная cookie не пережила) или когда
  // диспетчер перезапустили и он выдал новый токен.
  const OFFLINE_MESSAGE =
    "Нет связи с диспетчером — похоже, он остановлен. Данные в таблице устарели. " +
    "Запустите диспетчер ярлыком на рабочем столе и обновите страницу.";
  const UNAUTHORIZED_MESSAGE =
    "Сессия дашборда не подтверждена — эта вкладка открыта от прежнего запуска диспетчера. " +
    "Запустите диспетчер ярлыком на рабочем столе и откройте дашборд заново.";

  function isNetworkError(e) {
    // TypeError — то, чем fetch отвергается при недоступном сервере. Статус на
    // объекте ошибки проставляет только handleResponse, то есть его наличие
    // означает, что сервер ответил и это не сетевой сбой.
    return e instanceof TypeError && e.status === undefined;
  }

  function describeApiError(e) {
    if (isNetworkError(e)) return OFFLINE_MESSAGE;
    if (e && e.status === 401) return UNAUTHORIZED_MESSAGE;
    return e && e.message ? e.message : String(e);
  }

  function setConnStatus(ok, error) {
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

    // Точки в углу мало: пока связи нет, таблица продолжает показывать
    // последний успешный снапшот, и «Работает» из кэша неотличимо от свежего.
    // Поэтому — заметный баннер плюс гашение таблицы через класс на <body>.
    const banner = document.getElementById("offline-banner");
    if (banner) {
      if (ok) {
        banner.hidden = true;
      } else {
        document.getElementById("offline-banner-text").textContent = describeApiError(error);
        banner.hidden = false;
      }
    }
    document.body.classList.toggle("hub-offline", !ok);
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
    // Канал обновлений опрашивается, только когда его вкладка открыта: тики у
    // него редкие (минуты и сутки), и дёргать статус в фоне ради страницы, на
    // которую никто не смотрит, — расход впустую. Значок «нужен перезапуск»
    // при этом не теряется: он зажигается первым запросом при загрузке и после
    // каждого действия.
    if (companionAvailable && companionTabIsActive()) {
      refreshCompanionStatus({ quiet: true });
    }
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
      el.textContent = `ошибка: ${describeApiError(e)}`;
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
        errorEl.textContent = describeApiError(e);
      }
    });
    document.getElementById("agent-stop-btn").addEventListener("click", async () => {
      const errorEl = document.getElementById("agent-error");
      errorEl.textContent = "";
      try {
        await apiSend("POST", "/api/agent/stop");
        await refreshAgentStatus();
      } catch (e) {
        errorEl.textContent = describeApiError(e);
      }
    });
  }

  // --- канал обновлений издателя (вкладка «Обновления») ---
  //
  // Ядро дашборда ничего не знает о платной редакции: оно спрашивает
  // /api/companion/status и рисует то, что пришло. Свободная редакция отвечает
  // 503 с человеческим текстом — это не ошибка связи, а честный ответ «такой
  // возможности здесь нет», и показывается он как объяснение, а не как сбой.
  //
  // Значения проставляются ТОЛЬКО через textContent по статичной разметке (см.
  // index.html): в карточки едут строки, пришедшие от издателя — описания
  // отказов, номера версий, пути на диске. Сборка их через innerHTML означала бы
  // исполнение чужой разметки в окне управления процессами.

  const COMPANION_ACTION_PATHS = {
    sync_patterns: "/api/companion/sync",
    check_update: "/api/companion/check-update",
    stage_update: "/api/companion/stage-update",
    apply_update: "/api/companion/apply-update",
    rollback: "/api/companion/rollback",
    refresh_revocations: "/api/companion/revocations",
  };

  // Почему действие сейчас недоступно. Кнопка не прячется — она выключается и
  // объясняется: спрятанная кнопка читается как «функции нет вовсе», и
  // пользователь идёт искать её в настройках и в поддержке.
  const COMPANION_ACTION_REASONS = {
    apply_update: "Устанавливать нечего: сначала подготовьте обновление кнопкой «Подготовить»",
    rollback: "Откатываться не на что: канал ещё не устанавливал обновлений на этой машине",
  };
  const COMPANION_DISABLED_REASON =
    "Канал обновлений выключен в настройках (вкладка «Настройки» → «Канал обновлений»)";

  // Человеческие подписи статусов цикла плюс класс цвета из общей палитры.
  const COMPANION_STATUS_LABELS = {
    ok: ["выполнен успешно", "value-ok"],
    skipped: ["пропущен — делать нечего", "value-muted"],
    error: ["завершился ошибкой", "value-down"],
    never: ["ещё не выполнялся", "value-muted"],
    disabled: ["выключен в настройках", "value-muted"],
    halted: ["остановлен до вмешательства", "value-unknown"],
  };

  // Отдельная подпись действия для строки результата — чтобы сообщение об
  // успехе звучало как ответ на нажатую кнопку, а не как отчёт системы.
  const COMPANION_ACTION_DONE = {
    sync_patterns: "Паттерны синхронизированы",
    check_update: "Проверка обновления выполнена",
    stage_update: "Обновление подготовлено",
    apply_update: "Обновление установлено",
    rollback: "Откат выполнен",
    refresh_revocations: "Список отзывов обновлён",
  };

  let companionAvailable = true;
  let companionBusy = false;

  function companionEl(id) {
    return document.getElementById(id);
  }

  function companionCard(cycle) {
    return document.querySelector(`.companion-card[data-cycle="${cycle}"]`);
  }

  function setCompanionField(cycle, role, text, colorClass) {
    const card = companionCard(cycle);
    if (!card) return;
    const el = card.querySelector(`[data-role="${role}"]`);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("value-ok", "value-down", "value-unknown", "value-muted");
    if (colorClass) el.classList.add(colorClass);
  }

  function formatCompanionTime(value) {
    // Метки состояния — UTC секундной точности с Z. Показываем локальное время:
    // «когда это было» пользователь сверяет со своими часами, а не с UTC.
    if (!value) return "ещё не было";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleString("ru-RU");
  }

  function formatCompanionEta(seconds) {
    if (seconds === null || seconds === undefined) return "по расписанию не запускается";
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "—";
    if (value <= 1) return "вот-вот";
    return `через ${formatAge(value)}`;
  }

  function renderCompanionCycle(cycle, status) {
    const cycles = (status && status.cycles) || {};
    const state = (status && status.state) || {};
    const info = cycles[cycle] || {};
    const block = state[cycle] || {};

    const card = companionCard(cycle);
    const stateEl = card ? card.querySelector('[data-role="cycle-enabled"]') : null;
    if (stateEl) {
      const enabled = info.enabled !== false;
      stateEl.textContent = enabled ? "по расписанию" : "расписание выключено";
      stateEl.classList.toggle("companion-cycle-off", !enabled);
    }

    // Блокировка цикла (не-retriable отказ) важнее последнего статуса: она
    // объясняет, почему повторов больше не будет, пока человек не вмешается.
    let key = String(info.last_status || block.status || "never");
    if (info.halted) key = "halted";
    else if (info.enabled === false) key = "disabled";
    const [label, colorClass] = COMPANION_STATUS_LABELS[key] || [key, "value-muted"];
    setCompanionField(cycle, "last-status", label, colorClass);
    setCompanionField(cycle, "last-time",
      formatCompanionTime(block.last_run_at || block.last_check_at));
    setCompanionField(cycle, "next-run",
      info.enabled === false ? "—" : formatCompanionEta(info.next_run_in_sec));

    const detailEl = card ? card.querySelector('[data-role="detail"]') : null;
    if (detailEl) {
      const detail = info.halted
        ? `${info.halt_reason || "повтор бессмысленен"} — нажмите кнопку действия, чтобы попробовать снова`
        : String(info.last_detail || block.detail || "");
      detailEl.textContent = detail;
      detailEl.hidden = !detail;
    }
  }

  function renderCompanionStatus(status) {
    const patterns = (status.state && status.state.patterns) || {};
    const releases = (status.state && status.state.releases) || {};
    const revocations = (status.state && status.state.revocations) || {};
    const settings = status.settings || {};

    companionEl("companion-cards").hidden = false;
    companionEl("companion-unavailable").hidden = true;

    const enabled = status.enabled !== false && settings.enabled !== false;
    const editionEl = companionEl("companion-edition");
    editionEl.textContent = enabled
      ? "Канал обновлений: включён"
      : "Канал обновлений: выключен в настройках";
    editionEl.classList.toggle("companion-edition-off", !enabled);

    ["patterns", "releases", "revocations"].forEach((cycle) =>
      renderCompanionCycle(cycle, status));

    setCompanionField("patterns", "applied-count", String(patterns.applied_count ?? "—"));
    setCompanionField("patterns", "patterns-root", patterns.root || "по умолчанию, из поставки MCP");
    setCompanionField("releases", "current-version", releases.current_version || "как в поставке");
    setCompanionField("releases", "known-latest", releases.known_latest || "неизвестна — проверок ещё не было");
    setCompanionField(
      "releases",
      "staged-version",
      releases.staged_version
        ? `${releases.staged_version}${releases.staged_signed === false ? " (подпись не подтверждена)" : ""}`
        : "нет",
      releases.staged_version ? "value-ok" : null
    );
    setCompanionField("revocations", "revoked-count", String(revocations.revoked_count ?? "—"));

    // Цикл релизов выключен — говорим об этом честно и объясняем, от чего
    // зависит его включение (ключ подписи артефактов и HTTPS у издателя).
    const releasesOff = companionEl("companion-releases-off");
    if (releasesOff) {
      const releasesEnabled = ((status.cycles || {}).releases || {}).enabled;
      releasesOff.hidden = releasesEnabled === true;
    }

    updateCompanionActions(status);
    updateCompanionRestartBanner(releases);

    const contextEl = companionEl("companion-context");
    const context = status.context || {};
    const parts = [];
    if (context.detail) parts.push(`Лицензионный контекст: ${context.detail}`);
    if (status.last_error) parts.push(`Последний сбой канала: ${status.last_error}`);
    contextEl.textContent = parts.join(" · ");
  }

  function updateCompanionActions(status) {
    const allowed = (status && status.actions) || {};
    const enabled = !status || status.enabled !== false;
    document.querySelectorAll("[data-companion-action]").forEach((btn) => {
      const action = btn.dataset.companionAction;
      const ok = allowed[action] === true && !companionBusy;
      btn.disabled = !ok;
      if (companionBusy) {
        btn.title = "Дождитесь завершения текущего действия";
      } else if (!enabled) {
        btn.title = COMPANION_DISABLED_REASON;
      } else if (allowed[action] !== true) {
        btn.title = COMPANION_ACTION_REASONS[action] || "Сейчас действие недоступно";
      } else {
        btn.title = "";
      }
    });
  }

  function updateCompanionRestartBanner(releases) {
    const banner = companionEl("companion-restart-banner");
    const attention = companionEl("companion-tab-attention");
    const required = !!(releases && releases.restart_required);
    if (banner) banner.hidden = !required;
    // Значок на кнопке вкладки: баннер лежит внутри панели, а знать о
    // необходимости перезапуска нужно с любой вкладки.
    if (attention) attention.hidden = !required;
    const detail = companionEl("companion-restart-detail");
    if (detail && required && releases && releases.current_version) {
      detail.textContent =
        `Установлена версия ${releases.current_version}. Она начнёт работать только после ` +
        "полного перезапуска Claude Desktop: перезагрузка плагина MCP-сервер заново не поднимает.";
    }
  }

  function showCompanionUnavailable(message) {
    companionAvailable = false;
    companionEl("companion-cards").hidden = true;
    const editionEl = companionEl("companion-edition");
    editionEl.textContent = "Свободная редакция";
    editionEl.classList.add("companion-edition-off");
    const note = companionEl("companion-unavailable");
    note.hidden = false;
    // Точку в конце ставим сами: серверный текст — это заголовок причины, он
    // приходит без завершающей точки, и без неё две фразы слипаются в одну.
    const reason = String(message || "").trim().replace(/[.\s]+$/, "");
    note.textContent =
      `${reason}. Канал доставки обновлений издателя (паттерны, обновления MCP, отзыв ` +
      "лицензий) входит в платную редакцию BPMkit; управление стендами работает без него.";
    companionEl("companion-tab-attention").hidden = true;
  }

  async function refreshCompanionStatus(options) {
    const quiet = !!(options && options.quiet);
    const errorEl = companionEl("companion-error");
    if (!errorEl) return;
    if (!quiet) errorEl.textContent = "";
    try {
      const data = await apiGet("/api/companion/status");
      companionAvailable = true;
      renderCompanionStatus(data);
    } catch (e) {
      if (e && e.status === 503 && e.data && e.data.edition === "free") {
        // Не ошибка: так свободная редакция сообщает, что канала здесь нет.
        showCompanionUnavailable(e.data.error || "Канал обновлений недоступен.");
        errorEl.textContent = "";
        return;
      }
      if (!quiet) errorEl.textContent = describeApiError(e);
    }
  }

  async function runCompanionAction(action) {
    const path = COMPANION_ACTION_PATHS[action];
    if (!path) return;
    const errorEl = companionEl("companion-error");
    const statusEl = companionEl("companion-status-text");
    errorEl.textContent = "";
    statusEl.textContent = "Выполняется…";
    companionBusy = true;
    updateCompanionActions(null);
    try {
      // Версию не запрашиваем: «Подготовить» и «Откатить» без неё берут
      // последнюю доступную и последний бэкап соответственно — ровно то, чего
      // ждёт человек, нажавший кнопку. Выбор конкретной версии — работа CLI
      // (python -m standkit_companion stage-update --version …), а не окна с
      // вводом номера, в котором легко ошибиться.
      const data = await apiSend("POST", path, {});
      statusEl.textContent = COMPANION_ACTION_DONE[action] || "Готово";
      if (data && data.status) {
        renderCompanionStatus(data.status);
      }
    } catch (e) {
      statusEl.textContent = "";
      errorEl.textContent = describeApiError(e);
    } finally {
      companionBusy = false;
      // Свежий статус после ЛЮБОГО исхода: отказ мог изменить состояние
      // (например, снять подготовленное обновление), и кнопки обязаны это
      // отразить, а не остаться в картине «до».
      await refreshCompanionStatus({ quiet: true });
    }
  }

  function setupCompanionTab() {
    document.querySelectorAll("[data-companion-action]").forEach((btn) => {
      btn.addEventListener("click", () => runCompanionAction(btn.dataset.companionAction));
    });
    const refreshBtn = companionEl("companion-refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        companionEl("companion-status-text").textContent = "";
        refreshCompanionStatus();
      });
    }
    // Отдельный обработчик на кнопке вкладки, а не правка setupTabs: сама
    // механика вкладок работает по data-tab и знать про канал не должна.
    const tabBtn = companionEl("companion-tab-btn");
    if (tabBtn) {
      tabBtn.addEventListener("click", () => refreshCompanionStatus({ quiet: true }));
    }
  }

  function companionTabIsActive() {
    const panel = document.getElementById("tab-companion");
    return !!(panel && panel.classList.contains("active"));
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

    // Интервалы канала обновлений. Проверяются ЯВНЫМ списком полей секции
    // (COMPANION_SETTINGS_MAP), а не через SETTINGS_FIELDS: секция вложенная и
    // в плоский список не входит. Без этой проверки мусор в поле просто не
    // уехал бы на сервер (см. collectCompanionSettings) — то есть пользователь
    // увидел бы «Настройки сохранены» и прежнее значение в поле.
    COMPANION_SETTINGS_MAP.forEach(([field, _path, unit]) => {
      if (!unit) return;
      const input = form.elements.namedItem(field);
      if (!input) return;
      setFieldError(input, "");
      const raw = (input.value || "").trim();
      if (!raw) return; // пусто = «оставить прежнее значение»
      const value = Number(raw);
      if (!Number.isFinite(value) || value < 1) {
        const msg = "интервал должен быть положительным числом";
        setFieldError(input, msg);
        problems.push(`${fieldLabel(input)}: ${msg}`);
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

  // --- настройки канала обновлений: вложенная секция в плоской форме ---
  //
  // SETTINGS_FIELDS/loadSettings/setupSettingsForm работают с ПЛОСКИМИ именами
  // полей: имя поля = ключ конфига. У канала конфиг вложенный
  // (companion.patterns.interval_sec), и подобрать его плоскими именами можно
  // было бы только соглашением вида «подчёркивание = уровень вложенности» —
  // молчаливым правилом, о которое спотыкается первый же ключ с подчёркиванием
  // в имени (их тут два: interval_sec, auto_stage_release).
  //
  // Поэтому секция собирается и разбирается ЯВНОЙ парой функций ниже. Цена —
  // одна таблица соответствия; выигрыш — форма остаётся честной: имя поля не
  // притворяется ключом конфига, а единицы измерения (минуты/часы против
  // секунд) конвертируются в одном месте, а не в трёх.

  // Поле формы → [путь в конфиге, множитель к секундам]. Множитель 1 означает
  // «не число времени» (строка/флаг).
  const COMPANION_SETTINGS_MAP = [
    ["companion_enabled", ["enabled"], 0],
    ["companion_backend_url", ["backend_url"], 0],
    ["companion_mcp_cli", ["mcp_cli"], 0],
    ["companion_patterns_enabled", ["patterns", "enabled"], 0],
    ["companion_patterns_interval_min", ["patterns", "interval_sec"], 60],
    ["companion_releases_enabled", ["releases", "enabled"], 0],
    ["companion_releases_interval_hours", ["releases", "interval_sec"], 3600],
    ["companion_revocations_enabled", ["revocations", "enabled"], 0],
    ["companion_revocations_interval_min", ["revocations", "interval_sec"], 60],
    ["companion_auto_stage_release", ["auto_stage_release"], 0],
    ["companion_require_pattern_signature", ["require_pattern_signature"], 0],
  ];

  function companionSettingValue(companion, path) {
    let node = companion;
    for (const key of path) {
      if (node === null || node === undefined) return undefined;
      node = node[key];
    }
    return node;
  }

  /** Разложить вложенную секцию `companion` из ответа /api/settings по полям формы. */
  function fillCompanionSettings(form, companion) {
    const data = companion || {};
    COMPANION_SETTINGS_MAP.forEach(([field, path, unit]) => {
      const input = form.elements.namedItem(field);
      if (!input) return;
      const value = companionSettingValue(data, path);
      if (input.type === "checkbox") {
        input.checked = !!value;
        return;
      }
      if (unit) {
        // Секунды → минуты/часы. Округляем вверх: округление вниз способно
        // опустить значение ниже серверного минимума, и сервер поджал бы его
        // обратно — поле «прыгало» бы при каждом сохранении.
        const seconds = Number(value);
        input.value = Number.isFinite(seconds) && seconds > 0
          ? String(Math.ceil(seconds / unit))
          : "";
        return;
      }
      input.value = value === undefined || value === null ? "" : String(value);
    });
    const note = document.getElementById("companion-settings-note");
    if (note) {
      note.textContent = data.enabled === false ? " — выключен" : " — включён";
    }
  }

  /** Собрать вложенную секцию `companion` из полей формы для POST /api/settings. */
  function collectCompanionSettings(form) {
    const companion = {};
    COMPANION_SETTINGS_MAP.forEach(([field, path, unit]) => {
      const input = form.elements.namedItem(field);
      if (!input) return;
      let value;
      if (input.type === "checkbox") {
        value = input.checked;
      } else if (unit) {
        const amount = Number(input.value);
        // Пустое/битое поле не отправляем вовсе: сервер сохранит прежнее
        // значение (секция мержится), а не подставит ноль, который он же
        // потом поджал бы до минимума.
        if (!Number.isFinite(amount) || amount <= 0) return;
        value = Math.round(amount * unit);
      } else {
        value = String(input.value || "").trim();
      }
      const target = path.length === 1 ? companion : (companion[path[0]] = companion[path[0]] || {});
      target[path[path.length - 1]] = value;
    });
    return companion;
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
    // Вложенная секция канала — отдельной функцией (см. комментарий выше).
    fillCompanionSettings(form, data.companion);
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
        statusEl.textContent = `ошибка: ${describeApiError(e)}`;
      }
    }
  }

  function setupSettingsForm() {
    const form = document.getElementById("settings-form");

    // Нативная проверка number-полей (min/max) блокирует отправку формы МОЛЧА,
    // если проблемное поле лежит в СВЁРНУТОЙ группе <details>: браузеру некуда
    // показать подсказку у невидимого элемента, и кнопка «Сохранить» просто
    // перестаёт отвечать — ни ошибки, ни сохранения. Поймано на секции «Канал
    // обновлений» (интервалы с min), но касается любой свёрнутой группы.
    // Событие invalid НЕ всплывает — слушаем на фазе захвата, раскрываем группу
    // и передаём фокус, чтобы человек увидел и подсказку, и само поле.
    form.addEventListener(
      "invalid",
      (evt) => {
        const field = evt.target;
        if (!field || !field.closest) return;
        const group = field.closest("details");
        if (group && !group.open) group.open = true;
      },
      true
    );

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
      // Вложенная секция канала: собирается отдельно и уходит одним объектом,
      // сервер мержит её поверх текущей и сам поджимает интервалы к минимуму.
      payload.companion = collectCompanionSettings(form);
      try {
        // Тему в payload сознательно НЕ кладём: её меняет только переключатель
        // в шапке, а сервер мержит тело поверх текущего конфига — значение
        // сохранится само.
        const saved = await apiSend("POST", "/api/settings", payload);
        statusEl.textContent = "Настройки сохранены";
        // Новый refresh_interval_sec применяем немедленно, без перезагрузки
        // страницы (старый таймер снимается внутри applyRefreshInterval).
        applyRefreshInterval(saved && saved.refresh_interval_sec);
        // Сервер мог поджать интервал канала к минимуму — показываем то, что
        // реально сохранено, иначе форма врала бы про собственное значение.
        fillCompanionSettings(form, saved && saved.companion);
        // Настройки канала могли измениться — обновляем вкладку «Обновления»
        // сразу, а не при следующем заходе на неё.
        if (companionAvailable) refreshCompanionStatus({ quiet: true });
        await refreshSecretStatuses();
      } catch (e) {
        statusEl.textContent = `Ошибка сохранения: ${describeApiError(e)}`;
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
          window.alert(`Не удалось задать секрет: ${describeApiError(e)}`);
        }
      });
    });

    document.getElementById("install-shortcut-btn").addEventListener("click", async () => {
      const statusEl = document.getElementById("settings-status");
      try {
        const data = await apiSend("POST", "/api/shortcut/install");
        statusEl.textContent = data.message;
      } catch (e) {
        statusEl.textContent = `Ошибка: ${describeApiError(e)}`;
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
    setupCompanionTab();
    setupSettingsForm();
    setupStatePanel();
    setupActionStatus();
    document.getElementById("refresh-stands-btn").addEventListener("click", refreshStandsWithFeedback);

    // Быстрая первая отрисовка (?probe=0) + полный статус вторым запросом.
    firstPaint();
    // Push-обновления; при их отсутствии работает резервный таймер ниже.
    setupEventStream();
    refreshAgentStatus();
    // Статус канала спрашивается сразу, ещё до открытия вкладки: только так
    // значок «нужен перезапуск Claude Desktop» может зажечься на кнопке вкладки
    // у человека, который сидит на «Стендах» и во вкладку не заглядывает.
    refreshCompanionStatus({ quiet: true });
    loadSettings().catch((e) => {
      document.getElementById("settings-status").textContent = `Ошибка загрузки настроек: ${describeApiError(e)}`;
    });

    // Стартовый период — дефолтный; реальный refresh_interval_sec приедет с
    // первым же ответом /api/stands и перезаведёт таймер (applyRefreshInterval).
    restartBackgroundTimer();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
