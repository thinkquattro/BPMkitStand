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

  // Версия сборки ЭТОЙ статики (GAP-276 п.2) — из <meta name="standkit-build">,
  // куда её подставляет сервер, читая файл пакета С ДИСКА (server.py::
  // on_disk_standkit_version). Именно с диска: после `pip install` поверх
  // работающего хаба эта страница уже новая, а код сервера в памяти — ещё
  // старый, и расхождение двух чисел — единственный надёжный признак того,
  // что диспетчер пора перезапустить.
  //
  // Плейсхолдер (файл открыли с диска мимо сервера) или пустая строка —
  // сверка не делается вовсе: показать плашку «версии разошлись» там, где
  // сравнивать не с чем, хуже, чем не показать её никогда.
  const buildMetaEl = document.querySelector('meta[name="standkit-build"]');
  const HUB_BUILD_VERSION_RAW = (buildMetaEl && buildMetaEl.content) || "";
  const HUB_BUILD_VERSION =
    HUB_BUILD_VERSION_RAW && HUB_BUILD_VERSION_RAW.indexOf("__") !== 0
      ? HUB_BUILD_VERSION_RAW
      : "";

  function apiHeaders(mutation) {
    const headers = { "Content-Type": "application/json" };
    if (HUB_BUILD_VERSION) {
      // Сервер использует его, чтобы 404 на неизвестный маршрут отличал
      // «такой функции нет» от «сервер старее страницы» (server.py::
      // _send_unknown_api_route).
      headers["X-Standkit-Build"] = HUB_BUILD_VERSION;
    }
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

  // --- сцены и разделы настроек ---
  //
  // Вкладок больше нет: экранов ровно два — «Стенды» (главный) и «Настройки»
  // (шестерёнка в шапке). Раньше пять равноправных вкладок ставили «Локальный
  // агент», нужный единицам, вровень со списком стендов, ради которого
  // диспетчер и открывают. Разделы настроек — вертикальная рейка внутри
  // второго экрана, а не свёрнутые группы: список разделов виден целиком.

  const SCENES = ["stands", "settings"];

  function currentScene() {
    const el = document.querySelector(".scene.active");
    if (!el) return "stands";
    return el.id === "scene-settings" ? "settings" : "stands";
  }

  function showScene(name) {
    const target = SCENES.indexOf(name) >= 0 ? name : "stands";
    document.querySelectorAll(".scene").forEach((el) => {
      el.classList.toggle("active", el.id === `scene-${target}`);
    });
    const btn = document.getElementById("btn-settings");
    if (btn) btn.classList.toggle("active", target === "settings");
  }

  function selectSettingsPane(name) {
    document.querySelectorAll("#settings-rail button").forEach((b) => {
      b.classList.toggle("active", b.dataset.pane === name);
    });
    document.querySelectorAll(".settings-pane").forEach((pane) => {
      pane.classList.toggle("active", pane.dataset.pane === name);
    });
  }

  function openSettings(pane) {
    showScene("settings");
    if (pane) selectSettingsPane(pane);
  }

  function setupScenes() {
    document.getElementById("settings-rail").addEventListener("click", (evt) => {
      const btn = evt.target.closest("button[data-pane]");
      if (btn) selectSettingsPane(btn.dataset.pane);
    });
    document.getElementById("btn-settings").addEventListener("click", () => {
      showScene(currentScene() === "settings" ? "stands" : "settings");
    });
    document.getElementById("brand-btn").addEventListener("click", () => showScene("stands"));
    // «О программе» больше не модалка: та же информация лежит разделом
    // настроек, рядом с версией MCP и путём к CLI.
    document.getElementById("about-btn").addEventListener("click", () => openSettings("about"));
    // Любая кнопка «Открыть лицензию» — из баннера, из модалки, откуда угодно.
    document.querySelectorAll("[data-open-license]").forEach((btn) => {
      btn.addEventListener("click", () => {
        closeUpdatesDialog();
        closeLicenseCritModal();
        openSettings("license");
      });
    });
  }

  // --- тост (обратная связь по действиям окна обновлений и лицензии) ---

  let toastTimer = null;

  function toast(message) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("toast-visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("toast-visible"), 4000);
  }

  // Спиннер прямо на нажатой кнопке: действия канала ходят в сеть и занимают
  // секунды — без видимой занятости кнопку жмут повторно.
  function setButtonBusy(btn, label) {
    if (!btn) return;
    if (btn.dataset.idleLabel === undefined) btn.dataset.idleLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "";
    const spin = document.createElement("span");
    spin.className = "btn-spinner";
    btn.appendChild(spin);
    btn.appendChild(document.createTextNode(label));
  }

  function clearButtonBusy(btn) {
    if (!btn || btn.dataset.idleLabel === undefined) return;
    btn.textContent = btn.dataset.idleLabel;
    delete btn.dataset.idleLabel;
    btn.disabled = false;
  }

  // --- даты ---
  //
  // Везде, где показывается срок лицензии, формат один: dd.mm.yyyy. Локаль
  // браузера здесь не спрашивается намеренно — дата в баннере, в карточке и в
  // строке состояния обязана выглядеть одинаково, иначе их не сопоставить.

  function formatDate(value) {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    const dd = String(parsed.getDate()).padStart(2, "0");
    const mm = String(parsed.getMonth() + 1).padStart(2, "0");
    return `${dd}.${mm}.${parsed.getFullYear()}`;
  }

  /** «1 день / 2 дня / 5 дней» — иначе баннер говорит «через 3 дней». */
  function pluralDays(count) {
    const n = Math.abs(Math.trunc(count));
    const tens = n % 100;
    if (tens >= 11 && tens <= 14) return `${n} дней`;
    const ones = n % 10;
    if (ones === 1) return `${n} день`;
    if (ones >= 2 && ones <= 4) return `${n} дня`;
    return `${n} дней`;
  }

  function pluralPatterns(count) {
    const n = Math.abs(Math.trunc(count));
    const tens = n % 100;
    if (tens >= 11 && tens <= 14) return `${n} паттернов`;
    const ones = n % 10;
    if (ones === 1) return `${n} паттерн`;
    if (ones >= 2 && ones <= 4) return `${n} паттерна`;
    return `${n} паттернов`;
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
    // Подпись кнопки — только title/aria-label: сама иконка инлайновый SVG в
    // разметке, и подменять её текстовым глифом («▣»/«▭») значило бы вернуть
    // зависимость от шрифта системы, из-за которой шапка выглядела разной на
    // разных машинах.
    btn.classList.toggle("active", isCompact);
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

  // --- раздел «О программе» ---
  //
  // Раньше это была модалка с кнопкой ⓘ. Теперь — раздел настроек: версия
  // диспетчера, версия MCP, адрес и редакция стоят рядом с полем «CLI BPMkit»
  // и карточкой лицензии, то есть ровно там, где их и ищут, когда что-то не
  // сходится. Значения проставляются textContent (см. шапку index.html).

  let hubVersion = "";

  async function loadVersionInfo() {
    const el = document.getElementById("about-version");
    const originEl = document.getElementById("about-origin");
    if (originEl) originEl.textContent = window.location.origin;
    try {
      const data = await apiGet("/api/version");
      hubVersion = data.version || "";
      el.textContent = hubVersion ? `BPMkitStand ${hubVersion}` : "н/д";
      // GAP-278 п.3: «Редакция: с каналом обновлений» — перевод внутреннего
      // edition=companion. Пользователь спрашивает не про редакцию, а про то,
      // будут ли приходить обновления и что делать, если нет; ссылка ведёт
      // ровно туда, где это чинится. Настоящий статус канала (лицензия
      // истекла/отозвана) приходит отдельным снимком — см. renderAboutUpdates.
      renderAboutUpdates(data.edition);
      checkVersionSkew(hubVersion);
      reportInstallOutcome();
    } catch (e) {
      el.textContent = `ошибка: ${describeApiError(e)}`;
    }
  }

  // --- рассинхрон версий страницы и сервера (GAP-276 п.2) ---
  //
  // Статика читается с диска на каждый запрос, код сервера — из памяти
  // процесса. `pip install` поверх работающего хаба даёт новую страницу на
  // старом сервере: «О программе» показывает версию из ПАМЯТИ (то есть врёт
  // относительно того, что лежит на диске), а новые разделы зовут маршруты,
  // которых у старого сервера нет. Плашка ставит перед пользователем ровно
  // тот вопрос, который он может решить — перезапустить диспетчер.

  function showVersionSkewBanner(pageVersion, serverVersion) {
    const banner = document.getElementById("version-skew-banner");
    const text = document.getElementById("version-skew-text");
    if (!banner || !text) return;
    text.textContent =
      `Диспетчер обновлён до ${pageVersion}, а сервер работает на ${serverVersion}. ` +
      "Перезапустите диспетчер, чтобы страница и сервер снова совпали.";
    banner.hidden = false;
  }

  function checkVersionSkew(serverVersion) {
    // Плейсхолдер (запуск из исходников) — сверять нечего, см.
    // HUB_BUILD_VERSION. Пустая версия сервера — тоже: это ошибка чтения
    // /api/version, о ней уже сказано в «О программе».
    if (!HUB_BUILD_VERSION || !serverVersion) return;
    if (HUB_BUILD_VERSION === serverVersion) return;
    showVersionSkewBanner(HUB_BUILD_VERSION, serverVersion);
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
    // Та же форма, что и по «+ Стенд»: приглашение на пустом реестре —
    // второй вход в то же действие, а не отдельный сценарий (GAP-278 п.2).
    const firstStandBtn = document.getElementById("register-first-stand-btn");
    if (firstStandBtn) firstStandBtn.addEventListener("click", openRegisterModal);
    const aboutLicenseLink = document.getElementById("about-license-link");
    if (aboutLicenseLink) {
      aboutLicenseLink.addEventListener("click", (evt) => {
        evt.preventDefault();
        selectSettingsPane("license");
      });
    }
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
  // Используется для Стоп/Рестарт/Очистить Redis, а также для перезапуска
  // диспетчера с правами администратора (GAP-311) — код действий остаётся
  // линейным (await styledConfirm(...)) вместо колбэков. okLabel по умолчанию
  // "Подтвердить" (как было раньше у Стоп/Рестарт/Redis) — GAP-311 передаёт
  // своё ("Перезапустить"), не меняя остальные вызовы.

  function styledConfirm(title, text, okLabel) {
    return new Promise((resolve) => {
      const overlay = document.getElementById("confirm-modal-overlay");
      const okBtn = document.getElementById("confirm-modal-ok-btn");
      const cancelBtn = document.getElementById("confirm-modal-cancel-btn");
      const closeBtn = document.getElementById("confirm-modal-close-btn");

      document.getElementById("confirm-modal-title").textContent = title;
      document.getElementById("confirm-modal-text").textContent = text;
      okBtn.textContent = okLabel || "Подтвердить";
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
    // Показываем host:port, а не полный URL: схема и завершающий слэш в колонке
    // одинаковы у всех строк и съедают ширину, из-за которой длинные имена
    // хостов резались многоточием. Полный адрес и причина отказа — в title.
    const title = [url, http && http.reason].filter(Boolean).join(" — ");
    return `<a class="value-cell value-link ${cls}${reason.cls}" href="${escapeAttr(url)}" title="${escapeAttr(title)}" target="_blank" rel="noopener">${escapeHtml(shortHttpLabel(url))}</a>`;
  }

  /** "http://host:5001/" → "host:5001" (полный адрес остаётся в title). */
  function shortHttpLabel(url) {
    try {
      const parsed = new URL(url);
      return parsed.host || url;
    } catch (e) {
      return url;
    }
  }

  // --- чип хостинга и иконка движка БД ---
  //
  // И то и другое — мелкая подпись рядом с уже показанным значением, а не новая
  // колонка: в таблице их семь, восьмая не помещается ни в компактное окно, ни
  // в ноутбучный экран. Поля host_kind/db_type приходят с сервера в /api/stands;
  // когда их нет (старый агент, ответ без этих ключей), подпись просто не
  // рисуется — молча и без «unknown».

  const HOST_KIND_CHIPS = {
    kestrel: [".NET", "Kestrel (.NET)"],
    iis: ["IIS", "IIS"],
    docker: ["Docker", "Docker"],
    k8s: ["K8s", "Kubernetes"],
  };

  const DB_ICONS = {
    postgres:
      '<svg class="db-ico" viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4" rx="5.5" ry="2.2"/><path d="M2.5 4v8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4"/><path d="M2.5 8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2"/></svg>',
    mssql:
      '<svg class="db-ico" viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="2"/><path d="M5.5 10.5c.5.7 1.3 1 2.3 1 1.3 0 2.2-.6 2.2-1.6 0-2.2-4.3-1-4.3-3.2 0-1 .9-1.7 2.1-1.7.9 0 1.7.4 2.1 1"/></svg>',
  };

  const DB_ENGINE_LABELS = { postgres: "PostgreSQL", mssql: "MS SQL Server" };

  function hostChip(s) {
    const kind = s && s.host_kind;
    const chip = HOST_KIND_CHIPS[kind];
    if (!chip) return "";
    return ` <span class="host-chip" title="Хостинг: ${escapeAttr(chip[1])}">${escapeHtml(chip[0])}</span>`;
  }

  function standNameCell(s) {
    return `<b class="stand-name">${escapeHtml(s.name)}</b>${hostChip(s)}`;
  }

  const REMOTE_HTTP_TITLE =
    "Удалённый стенд без агента: известен только веб-адрес. Состояние — по HTTP-пробе; " +
    "управление процессом, БД и Redis недоступны.";

  // Из "https://host:443" (или голого host) делает host для колонки транспорта:
  // в неё не помещается полный URL, а нужен ответ на вопрос «где стенд живёт».
  function hostOf(value) {
    const text = String(value || "");
    try {
      if (text.includes("://")) return new URL(text).hostname;
    } catch (e) {
      /* мусор в адресе не должен ронять отрисовку строки */
    }
    return text;
  }

  // Транспорт agent значит «стендом управляет агент на другой машине» — без
  // имени этой машины строка не отвечает на единственный вопрос, ради которого
  // на неё смотрят: где стенд физически живёт.
  function transportCell(s) {
    // Транспорт http (GAP-277) — удалённый стенд БЕЗ агента. Слово «агент» в
    // этой строке было прямой дезинформацией: агента нет и не будет, поэтому
    // пользователь не мог отличить «агент упал» от «агента здесь нет».
    // Legacy-запись agent без agent_url сервер отдаёт с transport_warning и
    // remote_host — показываем её как http, но с явным предупреждением, а не
    // молча (реестр мы не переписывали, и оператор должен знать почему).
    const warn = s.transport_warning || "";
    if (s.transport === "http" || warn) {
      const rhost = s.remote_host || (s.http && s.http.url) || "";
      const title = warn ? `${REMOTE_HTTP_TITLE} ${warn}` : REMOTE_HTTP_TITLE;
      const label = "удалённый · http";
      const body = rhost ? `${label} · ${escapeHtml(hostOf(rhost))}` : label;
      const badge = warn
        ? ` <span class="badge badge-warn" title="${escapeAttr(warn)}">запись agent без адреса</span>`
        : "";
      return `<span title="${escapeAttr(title)}">${body}</span>${badge}`;
    }
    if (s.transport !== "agent") return escapeHtml(s.transport || "—");
    const host = s.agent || (s.process && s.process.agent) || "";
    return host ? `агент · ${escapeHtml(host)}` : "агент";
  }

  function dbCell(s) {
    // Имя базы из реестра у стенда без агента (GAP-277) — ничем не
    // подтверждённая строка: её состояние никто не проверял. Показывать её
    // цветом пробы значит выдавать запись реестра за факт.
    if (s.transport === "http" || s.transport_warning) {
      return `<span class="value-cell value-skipped" title="${escapeAttr(
        "не проверяется без агента"
      )}">—</span>`;
    }
    const db = s.db || {};
    const engine = db.type || s.db_type || "";
    const icon = DB_ICONS[engine] || "";
    const label = DB_ENGINE_LABELS[engine] || "";
    const title = label ? ` title="${escapeAttr(label)}"` : "";
    return `<span class="value-cell db-cell ${valueClass(db.state)}"${title}>${icon}${escapeHtml(db.name || "—")}</span>`;
  }

  // Ячейка Redis: показывает НОМЕР базы Redis стенда (тот же, что фигурирует
  // при очистке Redis), цвет — по состоянию пробы. Прочерк, если у стенда
  // Redis не настроен.
  //
  // redis.reason различает «адрес Redis не задан в реестре» и «задан, но
  // недоступен» (GAP-003, п.4) — раньше обе ситуации выглядели одинаково, с
  // жёстко зашитым «Redis не настроен у стенда». Этот текст остался фолбэком
  // на случай ответа без reason (старый агент или снапшот без проб).
  function redisCell(redis, stand) {
    if (stand && (stand.transport === "http" || stand.transport_warning)) {
      return `<span class="value-cell value-skipped" title="${escapeAttr(
        "не проверяется без агента"
      )}">—</span>`;
    }
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

  const NO_AGENT_TITLE = "без агента управление процессом недоступно";

  function actionButtons(s) {
    const processState = s.process ? s.process.state : "unknown";
    const isStarting = startingStands.has(s.name);
    // Удалённый стенд без агента (GAP-277): кнопки процесса не «пока серые»,
    // а неприменимы в принципе — активная кнопка, которая ничего не может
    // сделать, и была жалобой владельца.
    const noAgent = s.transport === "http" || !!s.transport_warning;
    const startDisabled = noAgent || processState === "ok" || isStarting;
    const stopDisabled = noAgent || processState === "down";
    const restartDisabled = noAgent || processState === "down";
    const redisNumber = s.redis && s.redis.number;
    const redisKnown = redisNumber !== null && redisNumber !== undefined;
    const redisDisabled = noAgent || !redisKnown;
    const redisTitle = noAgent
      ? NO_AGENT_TITLE
      : redisKnown
      ? "Очистить Redis"
      : "redis не настроен у стенда";
    const name = escapeHtml(s.name);
    return `
      <button class="icon-btn icon-btn-play" data-action="start" data-name="${name}" title="${escapeAttr(noAgent ? NO_AGENT_TITLE : "Запустить")}"${startDisabled ? " disabled" : ""}>${ICON_PLAY}</button>
      <button class="icon-btn icon-btn-stop" data-action="stop" data-name="${name}" title="${escapeAttr(noAgent ? NO_AGENT_TITLE : "Остановить")}"${stopDisabled ? " disabled" : ""}>${ICON_STOP}</button>
      <button class="icon-btn icon-btn-restart" data-action="restart" data-name="${name}" title="${escapeAttr(noAgent ? NO_AGENT_TITLE : "Перезапустить")}"${restartDisabled ? " disabled" : ""}>${ICON_RESTART}</button>
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
    updateStatuslineStands(lastStandsData);
    // Список стендов для однократной операции с правами администратора
    // (GAP-311) должен обновляться тем же путём — стенды могут появиться,
    // пропасть или сменить host_kind без переоткрытия «О программе».
    populateElevatedOnceStandSelect();
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
    // Кнопка стала иконочной (GAP-278 п.2), поэтому занятость показывается
    // классом-вращением, а не подменой текста: писать «Обновление…» внутри
    // кнопки 28×28 некуда.
    const startedAt = Date.now();
    btn.disabled = true;
    btn.classList.add("is-busy");
    let ok = false;
    try {
      ok = await refreshStands();
    } finally {
      const elapsed = Date.now() - startedAt;
      if (elapsed < MIN_BUSY_MS) await sleep(MIN_BUSY_MS - elapsed);
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

    // GAP-278 п.2: счётчик в заголовке таблицы и крупное приглашение вместо
    // пустой таблицы с шапкой колонок.
    const count = document.getElementById("stands-count");
    if (count) count.textContent = stands.length ? ` · ${stands.length}` : "";
    const empty = document.getElementById("stands-empty");
    const table = document.querySelector(".stands-table");
    if (empty) empty.hidden = stands.length > 0;
    if (table) table.hidden = stands.length === 0;
    stands.forEach((s) => {
      const http = s.http || {};
      const redis = s.redis || {};
      const tr = document.createElement("tr");
      tr.dataset.name = s.name;
      if (s.name === selectedStand) tr.classList.add("selected");
      tr.innerHTML = `
        <td>${standNameCell(s)}</td>
        <td>${transportCell(s)}</td>
        <td>${processCell(s)}</td>
        <td>${httpCell(http)}</td>
        <td>${dbCell(s)}</td>
        <td>${redisCell(redis, s)}</td>
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
      // Нехватка прав администратора (GAP-311) — не рядовая ошибка: обычный
      // тост/errorEl тут ни при чём, нужен отдельный незакрывающийся блок с
      // выбором «перезапустить диспетчер» / «поднять права на одну операцию».
      if (e && e.data && e.data.elevation_required) {
        showStandElevationError(name, action, describeApiError(e));
        return;
      }
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

  // Меню «Справка» в шапке (GAP-522): кукбук BPMkit и кукбук BPMkitStand.
  // Пункты — обычные <a target="_blank">, здесь только раскрытие/закрытие.
  function setHelpMenuOpen(open) {
    const menu = document.getElementById("help-menu");
    const btn = document.getElementById("help-btn");
    if (!menu || !btn) return;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.classList.toggle("active", open);
  }

  function setupHelpMenu() {
    const wrap = document.getElementById("help-menu-wrap");
    const btn = document.getElementById("help-btn");
    const menu = document.getElementById("help-menu");
    if (!wrap || !btn || !menu) return;
    btn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      setHelpMenuOpen(menu.hidden);
      if (!menu.hidden) {
        const first = menu.querySelector(".help-menu-item");
        if (first) first.focus();
      }
    });
    menu.querySelectorAll(".help-menu-item").forEach((item) => {
      // Ссылка открывается браузером сама; меню просто закрываем.
      item.addEventListener("click", () => setHelpMenuOpen(false));
    });
    document.addEventListener("click", (evt) => {
      if (!wrap.contains(evt.target)) setHelpMenuOpen(false);
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && !menu.hidden) {
        setHelpMenuOpen(false);
        btn.focus();
      }
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
    // Канал обновлений опрашивается, только когда его окно открыто: тики у него
    // редкие (часы и сутки), и дёргать статус в фоне ради закрытого диалога —
    // расход впустую. Бейдж на кнопке шапки при этом не теряется: он зажигается
    // первым запросом при загрузке и после каждого действия.
    if (companionAvailable && updatesDialogIsOpen()) {
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
    if (!Number.isFinite(age)) {
      el.textContent = "";
      el.title = "";
      el.classList.remove("stands-age-stale");
      return;
    }
    // GAP-278 п.2: раньше строка молчала, пока данные не устареют вдвое, и
    // пользователь не видел НИКАКОГО признака, что список обновляется сам, —
    // отсюда и впечатление, что без кнопки «Обновить» таблица мёртвая.
    // Теперь возраст показывается всегда; «устарело» лишь подсвечивается.
    const stale = age > (refreshIntervalMs / 1000) * 2;
    el.textContent = `обновлено ${formatAge(age)} назад`;
    el.title = stale
      ? "Фоновый опрос давно не обновлял снапшот состояния стендов"
      : "Список обновляется автоматически";
    el.classList.toggle("stands-age-stale", stale);
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

  // --- права администратора ---
  //
  // Управление стендами IIS идёт через appcmd.exe, а он без прав
  // администратора не работает В ПРИНЦИПЕ (не читает даже собственную
  // конфигурацию). Раньше пользователь узнавал об этом из ошибки после клика
  // «Старт», а совет «запустите диспетчер от имени администратора» было не
  // так просто выполнить: повторный запуск ярлыка видел уже работающий
  // экземпляр на том же порту и просто открывал браузер на нём.

  // Клиентский дедлайн ожидания ≥ серверного TTL заявки на elevation (180с,
  // см. контракт GAP-311) — иначе клиент сдаётся раньше, чем сервер сам
  // признал заявку истёкшей, и показывает "не дождались" при живом ожидании.
  const RESTART_WAIT_MS = 200000;
  const RESTART_POLL_MS = 1000;
  const ELEVATED_OP_WAIT_MS = 200000;
  const ELEVATED_OP_POLL_MS = 1000;

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  // Общий помост «кнопка занята запросом»: блокирует её на время запроса и
  // возвращает управление в любом терминальном исходе (успех, отказ, ошибка,
  // дедлайн) — без этого повторный клик во время ожидания UAC/операции мог
  // бы отправить второй такой же запрос.
  function setElevationButtonBusy(btn, busy) {
    if (btn) btn.disabled = !!busy;
  }

  // Последний ответ GET /api/hub/elevation — используется и шапкой (щит), и
  // разделом настроек «О программе», и выбором стендов для однократной
  // операции, чтобы не дублировать запрос на каждый из трёх потребителей.
  let lastElevationData = null;

  // Состояние → (data-elev-state, title/aria-label кнопки-щита). Решение
  // владельца 16.09.2026: на Windows щит виден ВСЕГДА, а не только без прав
  // (пересматривает решение 15.09.2026) — цвет и подпись несут сам статус.
  function elevationButtonPresentation(data) {
    if (data.elevated === true) {
      return {
        state: "ok",
        title: "Диспетчер работает с правами администратора",
        label: "Есть права администратора",
      };
    }
    if (data.elevated === false) {
      return {
        state: "bad",
        title: "Нет прав администратора — управление стендами IIS недоступно. "
          + "Нажмите, чтобы перезапустить диспетчер с правами администратора.",
        label: "Нет прав администратора — перезапустить с правами",
      };
    }
    return {
      state: "unknown",
      title: "Не удалось определить права администратора",
      label: "Права администратора не определены",
    };
  }

  async function refreshElevation() {
    const btn = document.getElementById("elevation-btn");
    try {
      const data = await apiGet("/api/hub/elevation");
      lastElevationData = data;
      // supported=false (не Windows) — щита нет вовсе: повышать нечего.
      // На Windows щит виден при любом elevated (true/false/null).
      if (btn) {
        btn.hidden = !(data && data.supported);
        if (data && data.supported) {
          const presentation = elevationButtonPresentation(data);
          btn.dataset.elevState = presentation.state;
          btn.title = presentation.title;
          btn.setAttribute("aria-label", presentation.label);
        }
      }
      updateAboutElevation(data);
    } catch (e) {
      if (btn) btn.hidden = true;
      updateAboutElevation(null);
    }
  }

  // --- раздел настроек «О программе»: строка «Права диспетчера» + однократная операция ---

  function updateAboutElevation(data) {
    const stateEl = document.getElementById("about-elevation-state");
    const userEl = document.getElementById("about-elevation-user");
    const restartBtn = document.getElementById("about-elevation-restart-btn");
    const onceBlock = document.getElementById("about-elevated-once");
    if (!stateEl) return;
    if (!data) {
      stateEl.textContent = "неизвестно";
      if (userEl) userEl.textContent = "";
      if (restartBtn) restartBtn.hidden = true;
      if (onceBlock) onceBlock.hidden = true;
      return;
    }
    // elevated: true|false|null — null ("неизвестно") приходит, когда ОС не
    // распознана надёжно или запрос к системе сам не выяснил ответ; это НЕ
    // то же самое, что "нет", поэтому не сворачиваем в false.
    stateEl.textContent = data.elevated === true ? "есть" : data.elevated === false ? "нет" : "неизвестно";
    if (userEl) userEl.textContent = data.user ? `(${data.user})` : "";
    if (restartBtn) restartBtn.hidden = !data.can_restart;
    // Однократная операция доступна только там, где вообще есть UAC — на
    // POSIX элевировать пока нечем (elevated-помощник — Windows-механизм).
    if (onceBlock) onceBlock.hidden = !data.supported;
  }

  // Список стендов IIS для селекта однократной операции. Вызывается и из
  // refreshElevation (появление/исчезание блока), и из applyStandsPayload
  // (список стендов может поменяться без переоткрытия «О программе»).
  function populateElevatedOnceStandSelect() {
    const sel = document.getElementById("about-elevated-once-stand");
    const runBtn = document.getElementById("about-elevated-once-run-btn");
    if (!sel) return;
    const iisStands = lastStandsData.filter((s) => s && s.host_kind === "iis");
    sel.innerHTML = iisStands
      .map((s) => `<option value="${escapeAttr(s.name)}">${escapeHtml(s.name)}</option>`)
      .join("");
    sel.disabled = iisStands.length === 0;
    if (runBtn) runBtn.disabled = iisStands.length === 0;
  }

  function showRestartOverlay(text, hint, closable) {
    const overlay = document.getElementById("restart-overlay");
    document.getElementById("restart-overlay-text").textContent = text;
    const hintEl = document.getElementById("restart-overlay-hint");
    hintEl.textContent = hint || "";
    hintEl.hidden = !hint;
    const footer = document.getElementById("restart-overlay-footer");
    if (footer) footer.hidden = !closable;
    overlay.hidden = false;
  }

  function hideRestartOverlay() {
    document.getElementById("restart-overlay").hidden = true;
  }

  function showElevatedOpOverlay(text, closable) {
    const overlay = document.getElementById("elevated-op-overlay");
    document.getElementById("elevated-op-overlay-text").textContent = text;
    const footer = document.getElementById("elevated-op-overlay-footer");
    if (footer) footer.hidden = !closable;
    overlay.hidden = false;
  }

  function hideElevatedOpOverlay() {
    document.getElementById("elevated-op-overlay").hidden = true;
  }

  // Общее сообщение «хаб уже отвечает, но не под нашей сессией» — терминальный
  // исход, который на первой фазе (старый хаб отвечает 401) и на второй (уже
  // отвечает НОВЫЙ хаб, но 401) означает одно и то же: файл передачи сессии
  // протух, либо elevated-процесс поднят под другой учётной записью.
  function showSessionNotMovedOverlay() {
    showRestartOverlay(
      "Диспетчер перезапущен с правами администратора, но сессия не перенеслась.",
      "Откройте дашборд заново — по ярлыку «BPMkit Диспетчер» на рабочем столе.",
      true
    );
  }

  async function waitForHubBack(triggerBtn) {
    const deadline = Date.now() + RESTART_WAIT_MS;
    // Пока СТАРЫЙ хаб отвечает, он же знает исход запроса UAC (отказ/провал
    // относительно ТОГО restart-elevated, который мы только что отправили,
    // см. GET /api/hub/elevation::restart) — спрашиваем его напрямую вместо
    // того, чтобы гадать по одной лишь потере связи. Как только связь с ним
    // обрывается (старый процесс завершается сам при захвате порта новым,
    // elevated, экземпляром — контракт GAP-311), переключаемся на ожидание
    // нового по /api/version — это и есть исходное поведение перезапуска.
    let oldHubGone = false;
    let sawPending = false;
    while (Date.now() < deadline) {
      await sleep(RESTART_POLL_MS);
      if (!oldHubGone) {
        try {
          const data = await apiGet("/api/hub/elevation");
          const restart = data && data.restart;
          // Новый процесс может занять порт МЕЖДУ двумя опросами (старый
          // остановился, новый поднялся быстрее интервала опроса), и обрыва
          // связи вкладка так и не увидит — отвечать ей будет уже НОВЫЙ хаб
          // с той же сессией. Живьём 16.09.2026: оверлей не закрывался, щит
          // оставался красным, хотя диспетчер уже работал с правами. Признаки
          // нового процесса: права уже есть (перезапуск предлагается только
          // без них) либо исчезло состояние перезапуска, которое старый хаб
          // держал «в ожидании».
          if (data && data.elevated === true) {
            window.location.reload();
            return;
          }
          if (restart && (restart.status === "pending" || restart.status === "requesting")) {
            sawPending = true;
          } else if (sawPending && !restart) {
            window.location.reload();
            return;
          }
          if (restart && restart.status === "refused") {
            setElevationButtonBusy(triggerBtn, false);
            showRestartOverlay(
              restart.message || "Повышение прав не подтверждено — диспетчер продолжает работать без них.",
              "",
              true
            );
            return;
          }
          if (restart && restart.status === "failed") {
            setElevationButtonBusy(triggerBtn, false);
            showRestartOverlay(
              restart.message || "Перезапуск диспетчера с правами администратора не удался.",
              "",
              true
            );
            return;
          }
          // restart.status === "requesting" | "pending" (или сам ответ ещё
          // не в курсе) — старый хаб всё ещё жив и ждёт вместе с нами.
          continue;
        } catch (e) {
          if (e && e.status === 401) {
            // Хаб уже отвечает (значит, это уже НОВЫЙ процесс), но сессия
            // не переехала — молчаливый reload дал бы пустую страницу с
            // ошибками, честнее сказать прямо.
            setElevationButtonBusy(triggerBtn, false);
            showSessionNotMovedOverlay();
            return;
          }
          if (isNetworkError(e)) {
            // Старый процесс погас — это ожидаемая фаза, переходим к ожиданию
            // нового (по /api/version, без прежнего сессионного токена — на
            // одном порту он либо тот же самый, либо новый ответит 401 выше).
            oldHubGone = true;
          }
          // Иной статус (например 500) — хаб ещё жив, но не разобрался; не
          // считаем это концом ожидания, пробуем на следующем витке.
        }
      } else {
        try {
          await apiGet("/api/version");
          window.location.reload();
          return;
        } catch (e) {
          if (e && e.status === 401) {
            // Тот же тупик, что и выше, только увиденный уже НА второй фазе
            // (отвечает новый процесс, сессия всё равно не перенеслась).
            setElevationButtonBusy(triggerBtn, false);
            showSessionNotMovedOverlay();
            return;
          }
          // Обрыв связи продолжается — новый процесс ещё не поднялся, ждём дальше.
        }
      }
    }
    setElevationButtonBusy(triggerBtn, false);
    showRestartOverlay(
      "Не дождались перезапуска диспетчера.",
      "Возможно, запрос UAC остался без ответа. Откройте дашборд заново по ярлыку.",
      true
    );
  }

  // Общий сценарий «перезапустить диспетчер с правами администратора»:
  // используется и щитом в шапке, и кнопкой в «О программе», и кнопкой
  // «Перезапустить с правами администратора» в ошибке elevation_required —
  // везде одно и то же подтверждение, один и то же POST, один и тот же оверлей.
  async function restartElevatedFlow(triggerBtn) {
    // Кнопка уже занята предыдущим кликом (двойной клик до того, как она
    // успела дизейблиться, или повторный вызов из другого места разметки,
    // ссылающегося на ту же кнопку) — второй запрос не отправляем.
    if (triggerBtn && triggerBtn.disabled) return;
    setElevationButtonBusy(triggerBtn, true);
    const confirmed = await styledConfirm(
      "Перезапуск с правами администратора",
      "Перезапустить диспетчер с правами администратора? Windows покажет окно с запросом прав — " +
        "подтвердите его. Диспетчер поднимется на том же адресе, страница переподключится сама. " +
        "Запущенные стенды не останавливаются.",
      "Перезапустить"
    );
    if (!confirmed) {
      setElevationButtonBusy(triggerBtn, false);
      return;
    }
    try {
      await apiSend("POST", "/api/hub/restart-elevated");
    } catch (e) {
      setElevationButtonBusy(triggerBtn, false);
      if (e && e.status === 409 && e.data && e.data.cancelled) {
        // Отказ в самом окне UAC — это не ошибка сервера, а явное решение
        // пользователя в системном диалоге; оверлей ожидания тут не нужен,
        // диспетчер и не пытался переехать.
        showActionStatus(
          "Повышение прав не подтверждено — диспетчер продолжает работать без них.",
          true
        );
        return;
      }
      // 409 БЕЗ cancelled (перезапуск уже запрошен кем-то другим, ещё
      // requesting/pending) — это настоящая ошибка, текст отдаёт сервер.
      showActionStatus(
        `Не удалось перезапустить диспетчер с правами администратора: ${describeApiError(e)}`,
        true
      );
      return;
    }
    showRestartOverlay(
      "Подтвердите запрос Windows (UAC), если он ещё открыт. Ждём, пока диспетчер поднимется заново…"
    );
    // Кнопка остаётся занятой на всё ожидание: если исход терминальный
    // (refused/failed/таймаут/сессия не перенеслась), waitForHubBack сама
    // её разблокирует; при успехе страница перезагрузится, и это неважно.
    waitForHubBack(triggerBtn);
  }

  // --- Выход из диспетчера (Д-3) ---
  //
  // Одна и та же функция за кнопкой в шапке и за пунктом в «О программе».
  // Подтверждение обязательно и обязано СНИМАТЬ главный страх: человек видит
  // в таблице живые стенды и справедливо боится погасить их вместе с
  // диспетчером. Они переживут выход (сервер детей не трогает, см.
  // HubHTTPServer.request_self_shutdown) — об этом прямо сказано в тексте.

  async function exitHubFlow(triggerBtn) {
    if (triggerBtn && triggerBtn.disabled) return;
    const confirmed = await styledConfirm(
      "Выход из диспетчера",
      "Закрыть диспетчер стендов? Запущенные стенды продолжат работать — выход останавливает " +
        "только сам диспетчер. Чтобы открыть его снова, запустите ярлык «Диспетчер стендов BPMkit».",
      "Выйти"
    );
    if (!confirmed) return;
    if (triggerBtn) triggerBtn.disabled = true;
    try {
      await apiSend("POST", "/api/hub/shutdown");
    } catch (e) {
      // Обрыв связи ТУТ — штатный исход, а не ошибка: сервер мог закрыть
      // сокет раньше, чем ответ дошёл до вкладки. Раз просили выйти — считаем,
      // что вышли, и показываем прощальный экран.
      if (!isNetworkError(e)) {
        if (triggerBtn) triggerBtn.disabled = false;
        showActionStatus(`Не удалось закрыть диспетчер: ${describeApiError(e)}`, true);
        return;
      }
    }
    showRestartOverlay(
      "Диспетчер закрыт. Запущенные стенды продолжают работать.",
      "Чтобы открыть диспетчер снова, запустите ярлык «Диспетчер стендов BPMkit». Эту вкладку можно закрыть.",
      false
    );
  }

  // --- Перезапуск БЕЗ повышения прав (GAP-276 п.3) ---
  //
  // Отличается от restartElevatedFlow ровно одним: эндпоинтом (и, как
  // следствие, отсутствием окна UAC). Ожидание возврата — та же waitForHubBack:
  // она не знает и не обязана знать, каким путём поднялся новый процесс.

  async function restartHubFlow(triggerBtn) {
    if (triggerBtn && triggerBtn.disabled) return;
    setElevationButtonBusy(triggerBtn, true);
    const confirmed = await styledConfirm(
      "Перезапуск диспетчера",
      "Перезапустить диспетчер? Он поднимется на том же адресе, страница переподключится сама. " +
        "Запущенные стенды не останавливаются. Права администратора при этом не запрашиваются.",
      "Перезапустить"
    );
    if (!confirmed) {
      setElevationButtonBusy(triggerBtn, false);
      return;
    }
    try {
      await apiSend("POST", "/api/hub/restart");
    } catch (e) {
      setElevationButtonBusy(triggerBtn, false);
      showActionStatus(`Не удалось перезапустить диспетчер: ${describeApiError(e)}`, true);
      return;
    }
    showRestartOverlay("Перезапускаем диспетчер. Ждём, пока он поднимется заново…");
    waitForHubBack(triggerBtn);
  }

  // Сценарий «только эта операция» (GAP-311, П2): без перезапуска диспетчера,
  // одна операция стенда выполняется через elevated-помощника. Используется
  // и из кнопки «Только эту операцию» в ошибке стенда, и из блока «Однократная
  // операция с правами администратора» в «О программе».
  async function runElevatedOp(stand, action, triggerBtn) {
    if (triggerBtn && triggerBtn.disabled) return;
    setElevationButtonBusy(triggerBtn, true);
    let opId;
    try {
      const data = await apiSend("POST", "/api/hub/elevated-op", { stand, action });
      opId = data && data.op_id;
    } catch (e) {
      setElevationButtonBusy(triggerBtn, false);
      if (e && e.status === 409 && e.data && e.data.cancelled) {
        showActionStatus(
          "Повышение прав не подтверждено — диспетчер продолжает работать без них.",
          true
        );
        return;
      }
      // 409 БЕЗ cancelled (та же операция уже запрошена и ждёт исхода) —
      // настоящая ошибка, текст отдаёт сервер.
      showActionStatus(
        `Не удалось выполнить операцию с правами администратора: ${describeApiError(e)}`,
        true
      );
      return;
    }
    showElevatedOpOverlay(
      "Подтвердите запрос Windows (UAC), если он ещё открыт. Выполняем операцию с правами администратора…"
    );
    await pollElevatedOp(opId, stand, triggerBtn);
  }

  async function pollElevatedOp(opId, stand, triggerBtn) {
    if (!opId) {
      hideElevatedOpOverlay();
      setElevationButtonBusy(triggerBtn, false);
      showActionStatus("Диспетчер не вернул идентификатор операции.", true);
      return;
    }
    // Клиентский дедлайн — тот же приём, что у waitForHubBack: без него сбой
    // GET .../elevated-op/<id> (или бесконечный "pending") держал бы кнопку
    // занятой и оверлей открытым вечно.
    const deadline = Date.now() + ELEVATED_OP_WAIT_MS;
    while (Date.now() < deadline) {
      await sleep(ELEVATED_OP_POLL_MS);
      let data;
      try {
        data = await apiGet(`/api/hub/elevated-op/${encodeURIComponent(opId)}`);
      } catch (e) {
        if (e && e.status === 404) {
          // op_id неизвестен диспетчеру — например, диспетчер перезапускался
          // (тот же процесс или другой) и потерял состояние операции. Ждать
          // дальше бессмысленно: результат не восстановить, это терминальный исход.
          setElevationButtonBusy(triggerBtn, false);
          showElevatedOpOverlay(
            "Диспетчер перезапустился, пока выполнялась операция — результат неизвестен.",
            true
          );
          return;
        }
        if (e && e.status === 401) {
          // Сессия не подтверждена — тоже верный признак, что диспетчер
          // успел перезапуститься; дальше опрашивать нечего.
          setElevationButtonBusy(triggerBtn, false);
          showElevatedOpOverlay(
            "Сессия дашборда не подтверждена — похоже, диспетчер перезапустился. Откройте дашборд заново.",
            true
          );
          return;
        }
        // Сетевой сбой (или иной статус) — не сдаёмся до дедлайна, пробуем
        // ещё раз на следующем витке, оверлей не трогаем.
        continue;
      }
      if (!data || data.status === "pending") continue;
      hideElevatedOpOverlay();
      setElevationButtonBusy(triggerBtn, false);
      if (data.status === "ok") {
        showActionStatus(data.message || "Операция выполнена с правами администратора", false);
        await refreshStands();
        if (stand === selectedStand) refreshState();
      } else {
        // refused (отказ в UAC) / error / expired — во всех трёх случаях
        // операция не выполнена, различие только в тексте от сервера.
        showActionStatus(
          data.message || "Операция с правами администратора не выполнена.",
          true
        );
      }
      return;
    }
    setElevationButtonBusy(triggerBtn, false);
    showElevatedOpOverlay("Не дождались результата операции.", true);
  }

  // --- блок «нет прав» в ошибке действия со стендом (см. onStandAction) ---

  // Контекст незакрытой ошибки elevation_required: какой стенд/действие
  // предложить повторить кнопками «Перезапустить»/«Только эту операцию».
  let pendingElevationOp = null;

  function showStandElevationError(name, action, message) {
    pendingElevationOp = { name, action };
    const el = document.getElementById("stand-elevation-error");
    if (!el) return;
    document.getElementById("stand-elevation-error-text").textContent = message;
    el.hidden = false;
  }

  function hideStandElevationError() {
    const el = document.getElementById("stand-elevation-error");
    if (el) el.hidden = true;
    pendingElevationOp = null;
  }

  function setupStandElevationError() {
    const el = document.getElementById("stand-elevation-error");
    if (!el) return;
    document.getElementById("stand-elevation-error-close").addEventListener("click", hideStandElevationError);
    const restartBtn = document.getElementById("stand-elevation-error-restart-btn");
    restartBtn.addEventListener("click", () => {
      hideStandElevationError();
      restartElevatedFlow(restartBtn);
    });
    const onceBtn = document.getElementById("stand-elevation-error-once-btn");
    onceBtn.addEventListener("click", () => {
      const op = pendingElevationOp;
      hideStandElevationError();
      if (op) runElevatedOp(op.name, op.action, onceBtn);
    });
  }

  function setupElevation() {
    const btn = document.getElementById("elevation-btn");
    // Клик — только когда прав правда нет (перезапуск с правами через UAC).
    // При elevated=true щит просто показывает «всё в порядке» и ведёт в
    // «О программе», ничего не перезапуская; при elevated=null (не
    // определили) кликать нечем — оставляем клик тем же переходом.
    if (btn) {
      btn.addEventListener("click", () => {
        if (lastElevationData && lastElevationData.elevated === false) {
          restartElevatedFlow(btn);
          return;
        }
        // elevated === true (всё в порядке) или null (не определили) —
        // перезапускать нечего, ведём в «О программе».
        openSettings("about");
      });
    }
    const exitBtn = document.getElementById("exit-btn");
    if (exitBtn) exitBtn.addEventListener("click", () => exitHubFlow(exitBtn));
    const skewRestartBtn = document.getElementById("version-skew-restart-btn");
    if (skewRestartBtn) skewRestartBtn.addEventListener("click", () => restartHubFlow(skewRestartBtn));
    const restartOverlayCloseBtn = document.getElementById("restart-overlay-close-btn");
    if (restartOverlayCloseBtn) restartOverlayCloseBtn.addEventListener("click", hideRestartOverlay);
    const elevatedOpOverlayCloseBtn = document.getElementById("elevated-op-overlay-close-btn");
    if (elevatedOpOverlayCloseBtn) elevatedOpOverlayCloseBtn.addEventListener("click", hideElevatedOpOverlay);
    setupStandElevationError();
    setupAboutElevation();
  }

  // --- «О программе»: кнопка перезапуска и однократная операция ---

  function setupAboutElevation() {
    const restartBtn = document.getElementById("about-elevation-restart-btn");
    if (restartBtn) restartBtn.addEventListener("click", () => restartElevatedFlow(restartBtn));
    // Д-3 / GAP-276: перезапуск без прав и выход — те же сценарии, что у
    // кнопок в шапке и у плашки рассинхрона версий.
    const plainRestartBtn = document.getElementById("about-restart-btn");
    if (plainRestartBtn) plainRestartBtn.addEventListener("click", () => restartHubFlow(plainRestartBtn));
    const aboutExitBtn = document.getElementById("about-exit-btn");
    if (aboutExitBtn) aboutExitBtn.addEventListener("click", () => exitHubFlow(aboutExitBtn));
    const runBtn = document.getElementById("about-elevated-once-run-btn");
    if (runBtn) {
      runBtn.addEventListener("click", () => {
        const standSel = document.getElementById("about-elevated-once-stand");
        const actionSel = document.getElementById("about-elevated-once-action");
        const stand = standSel && standSel.value;
        const action = actionSel && actionSel.value;
        if (!stand) return;
        runElevatedOp(stand, action, runBtn);
      });
    }
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

  // --- канал обновлений издателя (окно «Обновления») ---
  //
  // Ядро дашборда ничего не знает о платной редакции: оно спрашивает
  // /api/companion/status и рисует то, что пришло. Свободная редакция отвечает
  // 503 с человеческим текстом — это не ошибка связи, а честный ответ «такой
  // возможности здесь нет».
  //
  // Экран канала свёрнут из вкладки с тремя карточками-«циклами» в одно окно с
  // двумя строками: паттерны и MCP-сервер. Цикл отзыва лицензий из интерфейса
  // убран целиком (GAP-241) — он всегда включён и тикает сам; показывать
  // пользователю карточку, у которой нет ни одного его решения, незачем.
  //
  // Значения проставляются ТОЛЬКО через textContent по статичной разметке (см.
  // index.html): в окно едут строки, пришедшие от издателя — описания отказов,
  // номера версий, пути на диске.

  const COMPANION_ACTION_PATHS = {
    sync_patterns: "/api/companion/sync",
    check_update: "/api/companion/check-update",
    stage_update: "/api/companion/stage-update",
    apply_update: "/api/companion/apply-update",
    rollback: "/api/companion/rollback",
    refresh_revocations: "/api/companion/revocations",
    // GAP-279 (ADR-0048): установщик как артефакт обновления.
    stage_installer: "/api/companion/stage-installer",
    apply_installer: "/api/companion/apply-installer",
  };

  // Подпись занятой кнопки: «Обновляем…» честнее универсального «Подождите» —
  // человек видит, ЧТО именно сейчас делается его нажатием.
  const COMPANION_ACTION_BUSY = {
    sync_patterns: "Обновляем…",
    check_update: "Проверяем…",
    apply_update: "Устанавливаем…",
    rollback: "Откатываем…",
    stage_installer: "Скачиваем установщик…",
    apply_installer: "Запускаем установщик…",
  };

  const COMPANION_ACTION_DONE = {
    sync_patterns: "Паттерны обновлены",
    check_update: "Проверка обновлений выполнена",
    apply_update: "MCP обновлён",
    rollback: "Откат выполнен",
    stage_installer: "Установщик скачан и проверен",
    apply_installer: "Установщик запущен",
  };

  // Почему действие сейчас недоступно. Кнопка не прячется — она выключается и
  // объясняется: спрятанная кнопка читается как «функции нет вовсе».
  const COMPANION_ACTION_REASONS = {
    apply_update: "Устанавливать нечего: новая версия ещё не скачана",
    rollback: "Откатываться не на что: канал ещё не устанавливал обновлений на этой машине",
    apply_installer: "Устанавливать нечего: установщик новой версии ещё не скачан — нажмите «Проверить обновления»",
    stage_installer: "Скачать установщик сейчас нельзя",
  };
  const COMPANION_DISABLED_REASON =
    "Канал обновлений выключен в настройках (Настройки → Обновления)";

  // Фоновый опрос статуса канала (GAP-279): статус дешёвый (без запуска CLI), тики
  // канала — часы, поэтому раз в 5 минут достаточно для уведомления.
  const COMPANION_BACKGROUND_POLL_MS = 300000;

  let companionAvailable = true;
  let companionBusy = false;
  let lastCompanionStatus = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function updatesDialogIsOpen() {
    const overlay = byId("updates-overlay");
    return !!(overlay && !overlay.hidden);
  }

  function openUpdatesDialog() {
    const overlay = byId("updates-overlay");
    if (!overlay) return;
    overlay.hidden = false;
    refreshCompanionStatus({ quiet: true });
  }

  function closeUpdatesDialog() {
    const overlay = byId("updates-overlay");
    if (overlay) overlay.hidden = true;
  }

  /** «обновлено 2 ч назад» / «обновлено 08.09.2026 13:40» — по возрасту метки. */
  function describeMoment(value) {
    if (!value) return "ещё не было";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    const ageSec = (Date.now() - parsed.getTime()) / 1000;
    if (ageSec < 90) return "только что";
    if (ageSec < 86400) return `${formatAge(ageSec)} назад`;
    return parsed.toLocaleString("ru-RU");
  }

  function releasesBlock(status) {
    return ((status && status.state) || {}).releases || {};
  }

  function patternsBlock(status) {
    return ((status && status.state) || {}).patterns || {};
  }

  /** Есть ли что показать бейджем на кнопке шапки: новая версия или перезапуск. */
  function companionHasNews(status) {
    if (!status) return false;
    const rel = releasesBlock(status);
    if (rel.restart_required) return true;
    if (rel.staged_version) return true;
    if (pendingInstaller(status)) return true;
    const latest = rel.known_latest;
    const current = rel.current_version;
    return !!(latest && current && String(latest) !== String(current));
  }

  function renderUpdatesBadge(status) {
    const news = companionHasNews(status);
    const badge = byId("updates-badge");
    if (badge) badge.hidden = !news;
    // GAP-279: фолбэк без разрешения на уведомления — бейдж плюс заголовок вкладки
    // «(1) Диспетчер…»: вкладку видно в панели браузера, даже когда она не активна.
    renderDocumentTitle(news);
  }

  let baseDocumentTitle = null;

  function renderDocumentTitle(news) {
    if (baseDocumentTitle === null) {
      baseDocumentTitle = String(document.title || "").replace(/^\(\d+\)\s*/, "");
    }
    const wanted = news ? `(1) ${baseDocumentTitle}` : baseDocumentTitle;
    if (document.title !== wanted) document.title = wanted;
  }

  // --- установщик как артефакт обновления (GAP-279, ADR-0048) ---
  //
  // «Установить обновление» не подменяет bpmkit.exe, а ЗАПУСКАЕТ подписанный
  // установщик целиком (MCP + диспетчер + скиллы + документация) тихо. Установщик
  // сам останавливает диспетчер и поднимает его заново — эта страница в этот момент
  // теряет связь, ждёт новый процесс и перезагружается, показывая новую версию.

  const INSTALL_WAIT_MS = 600000; // 10 минут: установка с обновлением скиллов не мгновенна
  const INSTALL_POLL_MS = 2000;
  // Сколько ждать, прежде чем считать «установщик уже завершился, а диспетчер жив»
  // провалом: процесс установщика стартует не мгновенно, и первые секунды его
  // `running` может ещё не отражать.
  const INSTALL_EXIT_GRACE_MS = 15000;
  // Новый диспетчер отвечает, но эта вкладка — от прежней сессии (401). Новый процесс
  // сам открывает дашборд в новой вкладке и ставит cookie сессии; ждём её столько.
  const INSTALL_SESSION_WAIT_MS = 30000;
  const INSTALL_PENDING_KEY = "standkit-install-pending";
  const INSTALL_CLOSE_APPS_NOTE =
    "Закройте Claude Desktop (и другие приложения, где подключён BPMkit) — иначе " +
    "установщик не сможет заменить MCP-сервер. Диспетчер остановится и поднимется сам, " +
    "запущенные стенды продолжат работать; эта страница переподключится.";

  function installerBlock(status) {
    return (status && status.installer) || {};
  }

  /** Подготовленный установщик, который ещё есть смысл ставить (его версия — не та,
   * что уже установлена), либо null. */
  function pendingInstaller(status) {
    const staged = installerBlock(status).staged;
    if (!staged || !staged.version) return null;
    const current = releasesBlock(status).current_version || "";
    if (current && String(current) === String(staged.version)) return null;
    return staged;
  }

  function installerPreviewText(status, staged) {
    const rel = releasesBlock(status);
    const mcpFrom = rel.current_version || "неизвестно";
    const hubFrom = hubVersion || "неизвестно";
    const hubTo = staged.standkit_version || "версия из комплекта установщика";
    return `MCP ${mcpFrom} → ${staged.version}, диспетчер ${hubFrom} → ${hubTo}.`;
  }

  function renderInstallerRow(status) {
    const staged = pendingInstaller(status);
    const rel = releasesBlock(status);
    const latest = rel.known_latest || "";
    const current = rel.current_version || "";
    const hasNew = !!(latest && current && String(latest) !== String(current));
    const installerRequired = hasNew && !!rel.requires_installer;

    const installBtn = byId("upd-installer-btn");
    if (installBtn) {
      installBtn.hidden = !staged;
      if (staged && !companionBusy && installBtn.dataset.idleLabel === undefined) {
        installBtn.textContent = `Установить обновление ${staged.version}`;
      }
    }
    // Установщик готов, а тихая подмена бинаря этой версии недоступна (GAP-463) —
    // две кнопки «Установить» рядом только путали бы: остаётся одна, установщиком.
    const replaceBtn = byId("upd-install-btn");
    if (replaceBtn) {
      replaceBtn.hidden = !!staged && ((status && status.actions) || {}).apply_update !== true;
    }
    const stageBtn = byId("upd-installer-stage-btn");
    if (stageBtn) stageBtn.hidden = !(installerRequired && !staged);

    const preview = byId("upd-installer-preview");
    if (preview) {
      preview.hidden = !staged;
      if (staged) preview.textContent = installerPreviewText(status, staged);
    }
    if (staged) {
      byId("upd-mcp-desc").textContent =
        `Установщик ${staged.version} скачан и проверен: он обновит MCP-сервер, диспетчер, ` +
        "скиллы и документацию. " + INSTALL_CLOSE_APPS_NOTE;
    }

    const launched = installerBlock(status).launched;
    if (launched && launched.running) {
      setDetail("upd-installer-detail",
        `Установщик ${launched.version || ""} выполняется — диспетчер перезапустится сам.`);
    } else {
      setDetail("upd-installer-detail", "");
    }
  }

  function showInstallOverlay(text, hint, closable) {
    const overlay = byId("install-overlay");
    if (!overlay) return;
    byId("install-overlay-text").textContent = text;
    const hintEl = byId("install-overlay-hint");
    hintEl.textContent = hint || "";
    hintEl.hidden = !hint;
    const footer = byId("install-overlay-footer");
    if (footer) footer.hidden = !closable;
    overlay.hidden = false;
  }

  function hideInstallOverlay() {
    const overlay = byId("install-overlay");
    if (overlay) overlay.hidden = true;
  }

  /** Опрос хаба ТОЛЬКО cookie сессии, без заголовка токена этой вкладки: после
   * перезапуска токен вкладки устарел, а cookie мог уже обновить новый процесс,
   * открывший дашборд в новой вкладке. "ok" | "unauthorized" | "down". */
  async function probeHubByCookie() {
    try {
      const resp = await fetch("/api/version", { credentials: "same-origin", cache: "no-store" });
      if (resp.ok) return "ok";
      if (resp.status === 401) return "unauthorized";
      return "down";
    } catch (e) {
      return "down";
    }
  }

  function rememberPendingInstall(status, staged) {
    try {
      sessionStorage.setItem(INSTALL_PENDING_KEY, JSON.stringify({
        mcpFrom: releasesBlock(status).current_version || "",
        mcpTo: staged.version || "",
        hubFrom: hubVersion || "",
        at: Date.now(),
      }));
    } catch (e) {
      // Приватный режим/запрет хранилища — итог просто не будет показан тостом.
    }
  }

  /** После перезагрузки страницы новым диспетчером — сказать, что поменялось. */
  function reportInstallOutcome() {
    let pending = null;
    try {
      pending = JSON.parse(sessionStorage.getItem(INSTALL_PENDING_KEY) || "null");
      sessionStorage.removeItem(INSTALL_PENDING_KEY);
    } catch (e) {
      pending = null;
    }
    if (!pending || !hubVersion) return;
    if (pending.hubFrom && pending.hubFrom === hubVersion) {
      toast(`Диспетчер перезапущен, но его версия прежняя (${hubVersion}) — проверьте журнал установки`);
      return;
    }
    toast(`Обновление установлено: диспетчер ${pending.hubFrom || "?"} → ${hubVersion}` +
      (pending.mcpTo ? `, MCP → ${pending.mcpTo}. Перезапустите Claude Desktop.` : "."));
  }

  async function waitForInstallerRestart(launch) {
    const logHint = launch && launch.log ? `Журнал установки: ${launch.log}` : "";
    showInstallOverlay(
      "Идёт установка обновления. Диспетчер остановится и поднимется заново — страница переподключится сама.",
      INSTALL_CLOSE_APPS_NOTE,
      false
    );
    const startedAt = Date.now();
    const deadline = startedAt + INSTALL_WAIT_MS;
    let oldHubGone = false;
    let unauthorizedSince = 0;
    while (Date.now() < deadline) {
      await sleep(INSTALL_POLL_MS);
      if (!oldHubGone) {
        try {
          const st = await apiGet("/api/companion/status");
          const launched = installerBlock(st).launched;
          if (launched && launched.running === false && Date.now() - startedAt > INSTALL_EXIT_GRACE_MS) {
            showInstallOverlay(
              "Установщик завершился, а диспетчер не перезапускался — обновление, скорее всего, не установлено.",
              "Частая причина — открытый Claude Desktop (MCP-сервер занят). Закройте его и повторите «Установить обновление». " +
                (launched.log ? `Журнал установки: ${launched.log}` : logHint),
              true
            );
            return;
          }
        } catch (e) {
          // Обрыв связи — старый диспетчер остановлен установщиком: ждём новый.
          // 401 — отвечает уже НОВЫЙ процесс (старая сессия ему не известна).
          if (isNetworkError(e) || (e && e.status === 401)) oldHubGone = true;
        }
        continue;
      }
      const probe = await probeHubByCookie();
      if (probe === "ok") {
        window.location.reload();
        return;
      }
      if (probe === "unauthorized") {
        if (!unauthorizedSince) unauthorizedSince = Date.now();
        if (Date.now() - unauthorizedSince > INSTALL_SESSION_WAIT_MS) {
          showInstallOverlay(
            "Обновление установлено, диспетчер перезапущен — он открылся в новой вкладке браузера.",
            "Эту вкладку можно закрыть. Если новой вкладки нет — откройте дашборд ярлыком «BPMkit — диспетчер стендов».",
            true
          );
          return;
        }
      }
    }
    showInstallOverlay(
      "Не дождались перезапуска диспетчера после установки.",
      (logHint ? logHint + ". " : "") + "Откройте дашборд ярлыком «BPMkit — диспетчер стендов» и проверьте версию в «О программе».",
      true
    );
  }

  async function installUpdateFlow(btn) {
    if (companionBusy) return;
    const status = lastCompanionStatus;
    const staged = pendingInstaller(status);
    const errorEl = byId("updates-error");
    errorEl.textContent = "";
    if (!staged) {
      errorEl.textContent = COMPANION_ACTION_REASONS.apply_installer;
      return;
    }
    const confirmed = await styledConfirm(
      "Установить обновление",
      `${installerPreviewText(status, staged)} ${INSTALL_CLOSE_APPS_NOTE}`,
      "Установить"
    );
    if (!confirmed) return;
    companionBusy = true;
    setButtonBusy(btn, COMPANION_ACTION_BUSY.apply_installer);
    updateCompanionActions(null);
    let launched = null;
    try {
      const data = await apiSend("POST", COMPANION_ACTION_PATHS.apply_installer,
        { version: staged.version });
      launched = (data && data.result) || {};
      rememberPendingInstall(status, staged);
    } catch (e) {
      errorEl.textContent = describeApiError(e);
    } finally {
      companionBusy = false;
      clearButtonBusy(btn);
    }
    if (launched) {
      closeUpdatesDialog();
      await waitForInstallerRestart(launched);
    }
    await refreshCompanionStatus({ quiet: true });
  }

  // --- уведомления браузера об обновлениях (GAP-279) ---
  //
  // Разрешение браузера спрашивается ТОЛЬКО по действию человека — переключателем
  // «Настройки → Обновления»; сам выбор хранится в браузере (это удобство конкретного
  // зрителя, а не настройка диспетчера). Каждое событие показывается один раз:
  // «найдено обновление X» и «X скачано, готово к установке».

  const NOTIFY_PREF_KEY = "standkit-updates-notify";
  const NOTIFY_SEEN_KEY = "standkit-updates-notified";
  const NOTIFY_SEEN_LIMIT = 20;

  function notificationsSupported() {
    return typeof window.Notification === "function";
  }

  function notificationsWanted() {
    try {
      return localStorage.getItem(NOTIFY_PREF_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function setNotificationsWanted(on) {
    try {
      localStorage.setItem(NOTIFY_PREF_KEY, on ? "1" : "0");
    } catch (e) {
      // Хранилище недоступно — переключатель просто не переживёт перезагрузку.
    }
  }

  function renderNotifyToggle(message) {
    const toggle = byId("updates-notify-toggle");
    const note = byId("updates-notify-note");
    if (!toggle) return;
    const supported = notificationsSupported();
    const permission = supported ? window.Notification.permission : "unsupported";
    toggle.disabled = !supported;
    toggle.checked = supported && permission === "granted" && notificationsWanted();
    let text = message || "";
    if (!text) {
      if (!supported) text = "Этот браузер не поддерживает уведомления — о новой версии скажут бейдж на кнопке «Обновления» и заголовок вкладки.";
      else if (permission === "denied") text = "Уведомления для этой страницы запрещены в настройках браузера — работают бейдж и заголовок вкладки.";
      else if (!toggle.checked) text = "Без уведомлений о новой версии скажут бейдж на кнопке «Обновления» и заголовок вкладки «(1) …».";
    }
    if (note) {
      note.textContent = text;
      note.hidden = !text;
    }
  }

  function setupUpdateNotifications() {
    const toggle = byId("updates-notify-toggle");
    if (!toggle) return;
    renderNotifyToggle();
    toggle.addEventListener("change", async () => {
      if (!toggle.checked) {
        setNotificationsWanted(false);
        renderNotifyToggle();
        return;
      }
      if (!notificationsSupported()) {
        renderNotifyToggle();
        return;
      }
      let permission = window.Notification.permission;
      if (permission === "default") {
        try {
          permission = await window.Notification.requestPermission();
        } catch (e) {
          permission = "denied";
        }
      }
      if (permission !== "granted") {
        setNotificationsWanted(false);
        renderNotifyToggle("Браузер не разрешил уведомления — о новой версии скажут бейдж и заголовок вкладки.");
        return;
      }
      setNotificationsWanted(true);
      renderNotifyToggle("Уведомления включены.");
      if (lastCompanionStatus) maybeNotifyUpdates(lastCompanionStatus);
    });
  }

  function notifiedKeys() {
    try {
      const raw = JSON.parse(localStorage.getItem(NOTIFY_SEEN_KEY) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch (e) {
      return [];
    }
  }

  function markNotified(key) {
    try {
      const keys = notifiedKeys().filter((k) => k !== key);
      keys.push(key);
      localStorage.setItem(NOTIFY_SEEN_KEY, JSON.stringify(keys.slice(-NOTIFY_SEEN_LIMIT)));
    } catch (e) {
      // см. setNotificationsWanted
    }
  }

  /** События уведомлений по снимку канала: найдено обновление / готово к установке. */
  function updateNotificationEvents(status) {
    const rel = releasesBlock(status);
    const current = rel.current_version || "";
    const latest = rel.known_latest || "";
    const events = [];
    if (latest && current && String(latest) !== String(current)) {
      events.push({
        key: `found:${latest}`,
        title: "BPMkit: найдено обновление",
        body: `Доступна версия ${latest} (установлена ${current}).`,
      });
    }
    const installer = pendingInstaller(status);
    const ready = installer ? installer.version : (rel.staged_version || "");
    if (ready && String(ready) !== String(current)) {
      events.push({
        key: `ready:${ready}`,
        title: "BPMkit: обновление готово к установке",
        body: `Версия ${ready} скачана и проверена. Откройте «Обновления» в диспетчере стендов.`,
      });
    }
    return events;
  }

  function maybeNotifyUpdates(status) {
    if (!notificationsSupported() || !notificationsWanted()) return;
    if (window.Notification.permission !== "granted") return;
    const seen = notifiedKeys();
    updateNotificationEvents(status).forEach((evt) => {
      if (seen.includes(evt.key)) return;
      markNotified(evt.key);
      try {
        const n = new window.Notification(evt.title, { body: evt.body, tag: `bpmkit-${evt.key}` });
        n.onclick = () => {
          window.focus();
          openUpdatesDialog();
          n.close();
        };
      } catch (e) {
        // Браузер отказал показать (например, политика) — остаются бейдж и заголовок.
      }
    });
  }

  function setDetail(id, text) {
    const node = byId(id);
    if (!node) return;
    node.textContent = text || "";
    node.hidden = !text;
  }

  // Четыре РАЗНЫЕ состояния канала паттернов (GAP-437) — раньше причина была видна
  // только при `cycle.halted`/`status === "error"`, а пустая (успешная!) дельта и
  // «ни разу не отрабатывал» выглядели ОДИНАКОВО, как «ещё не синхронизировались».
  function renderPatternsRow(status) {
    const block = patternsBlock(status);
    const summary = (status && status.patterns) || {};
    const cycle = ((status && status.cycles) || {}).patterns || {};
    const metaEl = byId("upd-patterns-meta");

    // 1. Остановлен/ошибка — причина видна ВСЕГДА, не только при этих двух условиях.
    const failed = !!cycle.halted || block.status === "error";
    if (failed) {
      metaEl.textContent = "Синхронизация паттернов остановлена";
      setDetail("upd-patterns-detail",
        cycle.halted ? (cycle.halt_reason || "повторы остановлены до вмешательства")
                     : (String(block.detail || "") || "ошибка синхронизации"));
      return;
    }

    // 2. Канал ни разу не отрабатывал — честно так и сказать.
    if (!block.last_run_at) {
      metaEl.textContent = "Первая синхронизация паттернов ещё не проходила";
      setDetail("upd-patterns-detail", "");
      return;
    }

    const version = summary.version || block.latest_version || "";
    const appliedCount = Number(summary.count ?? block.applied_count ?? 0);
    const totalAvailable = block.total_available === null || block.total_available === undefined
      ? null : Number(block.total_available);
    const whenChecked = describeMoment(block.last_run_at);

    // 3. Отработал успешно, новых у издателя нет — дельта пустая, это УСПЕХ, а не
    // «не синхронизировались»: счётчик при этом — фактически доступная база (поставочная
    // + всё, что применялось раньше), а не дельта последнего тика.
    if (!appliedCount && !version) {
      const parts = ["Все паттерны уже внутри BPMkit. Новых не появилось"];
      if (totalAvailable !== null) parts.push(pluralPatterns(totalAvailable));
      parts.push(`проверено ${whenChecked}`);
      metaEl.textContent = parts.join(" · ");
      setDetail("upd-patterns-detail", "");
      return;
    }

    // 4. Отработал, дельта применена.
    const parts = [];
    if (version) parts.push(`Версия базы ${version}`);
    parts.push(pluralPatterns(totalAvailable !== null ? totalAvailable : appliedCount));
    parts.push(`обновлено ${whenChecked}`);
    metaEl.textContent = parts.join(" · ");
    setDetail("upd-patterns-detail", "");
  }

  function renderMcpRow(status) {
    const rel = releasesBlock(status);
    const current = rel.current_version || "";
    const latest = rel.known_latest || "";
    const staged = rel.staged_version || "";
    const hasNew = !!(latest && current && String(latest) !== String(current));
    // GAP-463: издатель объявил, что версия `latest` ставится установщиком — канал её
    // никогда не подготовит (`staged` для неё не появится, см.
    // `standkit_companion.releases.stage`), поэтому смотрим на флаг, а не на `staged`.
    const installerRequired = hasNew && !!rel.requires_installer;

    const avail = byId("upd-mcp-avail");
    avail.hidden = !(hasNew || staged);
    if (!avail.hidden) {
      avail.textContent = installerRequired ? `нужен установщик ${latest}` : `доступна ${staged || latest}`;
    }

    const parts = [];
    parts.push(current ? `Установлено ${current}` : "Установленная версия неизвестна");
    if (installerRequired) parts.push(`требуется установщик ${latest}`);
    else if (staged) parts.push(`скачано ${staged}`);
    else if (hasNew) parts.push(`доступно ${latest}`);
    else if (current) parts.push("это последняя версия");
    if (rel.last_check_at) parts.push(`проверено ${describeMoment(rel.last_check_at)}`);
    byId("upd-mcp-meta").textContent = parts.join(" · ");

    // GAP-463: тихим обновлением эту версию доставить нельзя — текст говорит об этом
    // прямо, вместо обычного «скачивается заранее»/«скачана и проверена». Ссылку на
    // скачивание не обещаем: в снимке канала достоверного адреса поставки нет — только
    // общее «у издателя», как и в остальном тексте диспетчера (см. `COMPANION_ACTION_REASONS`,
    // "издатель" в описании циклов).
    byId("upd-mcp-desc").textContent = installerRequired
      ? `Версия ${latest} ставится установщиком — тихим обновлением её доставить нельзя. Скачайте новую поставку у издателя BPMkit и запустите установку.`
      : staged
        ? "Новая версия скачана и проверена. После установки перезапустите Claude Desktop — иначе продолжит работать прежняя версия."
        : "Новая версия проверяется и скачивается заранее; подмена файла происходит только по вашей команде.";

    setDetail("upd-mcp-detail", rel.status === "error" ? String(rel.detail || "") : "");

    const install = byId("upd-install-btn");
    // Пока кнопка занята, её подпись держит setButtonBusy — перерисовка статуса
    // не имеет права затереть спиннер (иначе он исчезает на середине действия).
    if (!companionBusy && install.dataset.idleLabel === undefined) {
      install.textContent = staged ? `Установить ${staged}` : "Установить";
    }

    // «Откатить» показывается только когда откат реально возможен: кнопка,
    // которая всегда выключена, — это вопрос без ответа, а не подсказка.
    byId("upd-rollback-btn").hidden = !rel.rollback_available;

    const note = byId("upd-restart-note");
    note.hidden = !rel.restart_required;
    if (rel.restart_required) {
      // GAP-447: если известна версия РЕАЛЬНО работающего процесса (маркер
      // `mcp_runtime.json`) и она отличается от установленной — прямо это и написать,
      // а не универсальную фразу «новая версия». Версия процесса неизвестна (сервер
      // ни разу не объявился после апгрейда до маркера) — прежний текст.
      const runningVersion = rel.running_version || "";
      const installedVersion = rel.current_version || "";
      byId("upd-restart-detail").textContent =
        runningVersion && installedVersion && runningVersion !== installedVersion
          ? `: сейчас работает версия ${runningVersion}, а установлена ${installedVersion} — ` +
            `перезагрузка плагина MCP-сервер заново не поднимает.`
          : installedVersion
            ? `, чтобы начала работать версия ${installedVersion}: перезагрузка плагина MCP-сервер заново не поднимает.`
            : ", чтобы начала работать новая версия: перезагрузка плагина MCP-сервер заново не поднимает.";
    }

    renderInstallerRow(status);
    renderWhatsNew(status);

    // Фактическая версия MCP — единая функция-источник правды (см.
    // renderMcpVersion): current из канала — один из двух источников, вызов
    // после его обновления актуализирует и «О программе», и статус-строку.
    renderMcpVersion();
  }

  /** Свёрнутый по умолчанию спойлер «Что нового в X.Y.Z» в карточке MCP-сервера
   * (GAP-442). Раскрытие — нативным `<details>`, состояние раскрытия не обязано
   * переживать перезагрузку страницы и здесь не сохраняется. */
  function renderWhatsNew(status) {
    const rel = releasesBlock(status);
    const box = byId("upd-whatsnew");
    if (!box) return;

    const staged = rel.staged_version || "";
    const current = rel.current_version || "";
    const latest = rel.known_latest || "";
    const hasNew = !!(latest && current && String(latest) !== String(current));
    // GAP-463: версия с установщиком никогда не станет `staged` (канал её не готовит —
    // см. `renderMcpRow`), но нотсы про НЕЁ пользователю нужны ровно тогда, когда он
    // читает «нужен установщик»: это ответ на «ради чего идти за установщиком».
    const installerRequired = hasNew && !!rel.requires_installer;
    const targetVersion = installerRequired ? latest : (staged || current);
    const notesVersion = rel.release_notes_version || "";
    const notes = Array.isArray(rel.release_notes) ? rel.release_notes : [];
    const issues = Array.isArray(rel.known_issues) ? rel.known_issues : [];

    // Нотсы — ТОЛЬКО когда они точно про целевую версию (staged/установщик, если есть,
    // иначе установленную): бэкенд всегда отдаёт `release_notes` про свою «latest», и
    // если она разошлась с тем, что видит канал доставки, показать её текст значило бы
    // выдумать состав чужой версии.
    const showNotes = !!targetVersion && !!notesVersion &&
      String(notesVersion) === String(targetVersion) && notes.length > 0;
    // Известные проблемы сервер уже отфильтровал по УСТАНОВЛЕННОЙ версии (`current` в
    // запросе) — показываем их независимо от совпадения с `notesVersion`.
    const showIssues = issues.length > 0;

    if (!showNotes && !showIssues) {
      box.hidden = true;
      return;
    }

    const summary = byId("upd-whatsnew-summary");
    if (summary) summary.textContent = `Что нового в ${targetVersion || notesVersion}`;

    const notesEl = byId("upd-whatsnew-notes");
    if (notesEl) {
      notesEl.innerHTML = "";
      notes.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = String(line);
        notesEl.appendChild(li);
      });
      notesEl.hidden = !showNotes;
    }

    const issuesBox = byId("upd-whatsnew-issues");
    const issuesEl = byId("upd-whatsnew-issues-list");
    if (issuesEl) {
      issuesEl.innerHTML = "";
      issues.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = String(line);
        issuesEl.appendChild(li);
      });
    }
    if (issuesBox) issuesBox.hidden = !showIssues;

    box.hidden = false;
  }

  function renderCompanionStatus(status) {
    lastCompanionStatus = status;
    companionAvailable = true;
    byId("updates-note").hidden = true;

    const enabled = status.enabled !== false && (status.settings || {}).enabled !== false;
    const note = byId("updates-note");
    if (!enabled) {
      note.hidden = false;
      note.textContent = COMPANION_DISABLED_REASON + ".";
    }

    renderPatternsRow(status);
    renderMcpRow(status);
    renderUpdatesBadge(status);
    updateCompanionActions(status);
    maybeNotifyUpdates(status);

    const rel = releasesBlock(status);
    byId("updates-checked-at").textContent = rel.last_check_at
      ? `проверено ${describeMoment(rel.last_check_at)}`
      : "";
    updateStatuslinePatterns(status);
  }

  // GAP-463: почему «Установить» недоступна, когда причина не «нечего ставить», а
  // «эту версию тихо не поставить» — тот же класс отказа, что `ChannelError(kind=
  // "requires_installer")` у канала, только сформулированный для кнопки, а не для лога.
  // Статическая `COMPANION_ACTION_REASONS` не подходит: для `apply_update` там уже есть
  // текст на случай «ничего не скачано», и он неверен, когда файл как раз ЕСТЬ, но
  // именно эту версию канал не подменит никогда (см. `releases.staged_requires_installer`).
  function actionUnavailableReason(action, status) {
    if (action === "apply_update" || action === "stage_update") {
      const rel = releasesBlock(status);
      const latest = rel.known_latest || "";
      const current = rel.current_version || "";
      const hasNew = !!(latest && current && String(latest) !== String(current));
      if (hasNew && rel.requires_installer) {
        return "Эта версия ставится установщиком — тихая доставка невозможна. " +
          "Скачайте новую поставку у издателя BPMkit и запустите установку.";
      }
    }
    return COMPANION_ACTION_REASONS[action] || "Сейчас действие недоступно";
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
        btn.title = actionUnavailableReason(action, status);
      } else {
        btn.title = "";
      }
    });
  }

  function showCompanionUnavailable(message) {
    companionAvailable = false;
    lastCompanionStatus = null;
    const note = byId("updates-note");
    if (note) {
      // Точку в конце ставим сами: серверный текст — это заголовок причины, он
      // приходит без завершающей точки, и без неё две фразы слипаются в одну.
      const reason = String(message || "").trim().replace(/[.\s]+$/, "");
      note.hidden = false;
      note.textContent =
        `${reason}. Канал доставки обновлений издателя (паттерны и обновления MCP) ` +
        "входит в платную редакцию BPMkit; управление стендами работает без него.";
    }
    const badge = byId("updates-badge");
    if (badge) badge.hidden = true;
    updateStatuslinePatterns(null);
  }

  async function refreshCompanionStatus(options) {
    const quiet = !!(options && options.quiet);
    const errorEl = byId("updates-error");
    if (!errorEl) return;
    if (!quiet) errorEl.textContent = "";
    try {
      const data = await apiGet("/api/companion/status");
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

  async function runCompanionAction(action, btn) {
    const path = COMPANION_ACTION_PATHS[action];
    if (!path) return;
    const errorEl = byId("updates-error");
    errorEl.textContent = "";
    byId("updates-check-status").textContent = "";
    companionBusy = true;
    setButtonBusy(btn, COMPANION_ACTION_BUSY[action] || "Выполняется…");
    updateCompanionActions(null);
    try {
      // Версию не запрашиваем: «Установить» и «Откатить» без неё берут
      // подготовленную версию и последний бэкап соответственно — ровно то, чего
      // ждёт человек, нажавший кнопку.
      const data = await apiSend("POST", path, {});
      toast(COMPANION_ACTION_DONE[action] || "Готово");
      if (action === "check_update") {
        byId("updates-check-status").textContent = "проверено только что";
      }
      if (data && data.status) {
        companionBusy = false;
        clearButtonBusy(btn);
        renderCompanionStatus(data.status);
      }
    } catch (e) {
      errorEl.textContent = describeApiError(e);
    } finally {
      companionBusy = false;
      clearButtonBusy(btn);
      // Свежий статус после ЛЮБОГО исхода: отказ мог изменить состояние
      // (например, снять подготовленное обновление), и кнопки обязаны это
      // отразить, а не остаться в картине «до».
      await refreshCompanionStatus({ quiet: true });
    }
  }

  function setupUpdatesDialog() {
    document.querySelectorAll("[data-companion-action]").forEach((btn) => {
      // «Установить обновление» установщиком — свой сценарий: предпросмотр версий,
      // подтверждение, ожидание перезапуска диспетчера (GAP-279).
      btn.addEventListener("click", () => (btn.dataset.companionAction === "apply_installer"
        ? installUpdateFlow(btn)
        : runCompanionAction(btn.dataset.companionAction, btn)));
    });
    const installOverlayCloseBtn = byId("install-overlay-close-btn");
    if (installOverlayCloseBtn) installOverlayCloseBtn.addEventListener("click", hideInstallOverlay);
    byId("btn-updates").addEventListener("click", openUpdatesDialog);
    byId("updates-close-btn").addEventListener("click", closeUpdatesDialog);
    byId("updates-close-footer-btn").addEventListener("click", closeUpdatesDialog);
    bindOverlayDismiss(byId("updates-overlay"), closeUpdatesDialog);
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && updatesDialogIsOpen()) closeUpdatesDialog();
    });
  }

  // --- лицензия BPMkit ---
  //
  // Хаб — тонкий прокси к CLI самого MCP (см. standkit_hub/license_api.py), а
  // экран лицензии — тонкий клиент этого прокси: своей трактовки состояния у
  // него нет, он показывает пришедший `status` и считает по `days_left`,
  // насколько громко об этом говорить.

  const LICENSE_POLL_MS = 600000; // 10 минут: срок меряется днями, чаще незачем
  const LICENSE_CRIT_STATUSES = ["expired", "revoked"];
  const LICENSE_UNKNOWN_STATUSES = ["none", "unavailable"];
  const LICENSE_CHANNEL_STATUSES = ["valid", "expiring"];
  const LICENSE_CRIT_SEEN_KEY = "standkit_license_crit_seen";

  const LICENSE_STATE_LABELS = {
    valid: ["действует", "lic-ok"],
    expiring: ["истекает", "lic-warn"],
    expired: ["истекла", "lic-crit"],
    revoked: ["отозвана", "lic-crit"],
    invalid: ["не принята", "lic-crit"],
  };

  const LICENSE_SOURCE_LABELS = {
    keyring: "хранилище ключей ОС",
    env: "переменная окружения",
    file: "файл лицензии",
    "env-file": "файл из переменной окружения",
    "well-known-file": "файл лицензии рядом с MCP",
    "state-cache": "кэш последней успешной проверки",
  };

  let lastLicense = null;

  function licenseCritSeen(key) {
    try {
      return sessionStorage.getItem(LICENSE_CRIT_SEEN_KEY) === key;
    } catch (e) {
      // Приватный режим / отключённое хранилище: показать окно один раз за
      // загрузку страницы всё равно лучше, чем не показать вовсе.
      return false;
    }
  }

  function rememberLicenseCrit(key) {
    try {
      sessionStorage.setItem(LICENSE_CRIT_SEEN_KEY, key);
    } catch (e) {
      /* см. licenseCritSeen */
    }
  }

  function closeLicenseCritModal() {
    const overlay = byId("license-crit-overlay");
    if (overlay) overlay.hidden = true;
  }

  function maybeShowLicenseCritModal(snapshot) {
    const status = snapshot.status;
    if (LICENSE_CRIT_STATUSES.indexOf(status) < 0) return;
    const key = `${status}:${snapshot.license_id_tail || ""}`;
    if (licenseCritSeen(key)) return;
    rememberLicenseCrit(key);
    const licensee = snapshot.licensee || "BPMkit";
    if (status === "revoked") {
      byId("license-crit-title").textContent = "Лицензия отозвана";
      byId("license-crit-body").textContent =
        `Издатель отозвал лицензию ${licensee}` +
        (snapshot.license_id_tail ? ` (ID …${snapshot.license_id_tail})` : "") +
        ". Платные инструменты BPMkit отключены, диспетчер продолжает управлять " +
        "стендами в свободной редакции.";
    } else {
      byId("license-crit-title").textContent = "Срок лицензии истёк";
      byId("license-crit-body").textContent =
        `Лицензия ${licensee} закончилась ${formatDate(snapshot.expires_at)}. ` +
        "Платные инструменты BPMkit отключены, диспетчер продолжает управлять стендами " +
        "в свободной редакции. Продлите лицензию и добавьте новый ключ.";
    }
    byId("license-crit-overlay").hidden = false;
  }

  function renderLicenseBanners(snapshot) {
    const warn = byId("license-banner-warn");
    const crit = byId("license-banner-crit");
    warn.hidden = true;
    crit.hidden = true;
    const status = snapshot.status;
    const days = Number(snapshot.days_left);
    const date = formatDate(snapshot.expires_at);

    if (status === "expiring" && Number.isFinite(days) && days >= 4 && days <= 7) {
      byId("license-banner-warn-title").textContent = `Лицензия истекает через ${pluralDays(days)}`;
      byId("license-banner-warn-text").textContent =
        ` — ${date}. Продлите её, чтобы BPMkit не перешёл в свободную редакцию.`;
      warn.hidden = false;
      return;
    }
    if (status === "expired") {
      byId("license-banner-crit-title").textContent = "Срок лицензии истёк";
      byId("license-banner-crit-text").textContent =
        ` ${date}. Платные инструменты BPMkit отключены — продлите лицензию.`;
      crit.hidden = false;
      return;
    }
    if (status === "revoked") {
      byId("license-banner-crit-title").textContent = "Лицензия отозвана издателем";
      byId("license-banner-crit-text").textContent = ". Платные инструменты BPMkit отключены.";
      crit.hidden = false;
      return;
    }
    if (status === "expiring" && Number.isFinite(days) && days <= 3) {
      byId("license-banner-crit-title").textContent =
        days <= 1 ? "Лицензия истекает завтра" : `Лицензия истекает через ${pluralDays(days)}`;
      byId("license-banner-crit-text").textContent =
        ` — ${date}. После этого платные инструменты BPMkit отключатся.`;
      crit.hidden = false;
    }
  }

  /**
   * Строка состояния про лицензию. Собирается из узлов, а не строкой innerHTML:
   * в неё попадает имя лицензиата и тариф — текст издателя.
   */
  function renderLicenseStatusline(snapshot) {
    const target = byId("sl-license");
    target.textContent = "";
    target.className = "";
    const status = snapshot.status;
    const date = formatDate(snapshot.expires_at);
    const tier = snapshot.tier_label || snapshot.tier || "";
    const days = Number(snapshot.days_left);

    function put(prefix, bold, cls) {
      if (cls) target.className = cls;
      target.appendChild(document.createTextNode(prefix));
      const b = document.createElement("b");
      b.textContent = bold;
      target.appendChild(b);
    }

    if (status === "valid") {
      put("лицензия ", tier ? `${tier} до ${date}` : `до ${date}`);
    } else if (status === "expiring") {
      const bold = days <= 1 ? "истекает завтра" : `истекает ${date}`;
      put("лицензия ", tier ? `${tier}, ${bold}` : bold, days <= 3 ? "lic-crit" : "lic-warn");
    } else if (status === "expired") {
      put("лицензия ", "истекла", "lic-crit");
    } else if (status === "revoked") {
      put("лицензия ", "отозвана", "lic-crit");
    } else if (status === "invalid") {
      put("лицензия ", "не принята", "lic-crit");
    } else if (status === "unavailable") {
      put("лицензия ", "не проверена");
    } else {
      put("лицензия ", "не активирована");
      target.appendChild(document.createTextNode(" · "));
      const link = document.createElement("button");
      link.type = "button";
      link.className = "linklike";
      link.textContent = "добавить";
      link.addEventListener("click", () => openSettings("license"));
      target.appendChild(link);
    }
  }

  function renderLicensePane(snapshot) {
    const status = snapshot.status;
    const unknown = LICENSE_UNKNOWN_STATUSES.indexOf(status) >= 0;
    byId("license-active").hidden = unknown;
    byId("license-free").hidden = !unknown;

    const unavailable = byId("license-unavailable");
    if (status === "unavailable") {
      unavailable.hidden = false;
      unavailable.textContent =
        `Проверить лицензию не удалось: ${snapshot.detail || "CLI BPMkit недоступен"}. ` +
        "Укажите путь к CLI в «Настройки → Основные».";
    } else {
      unavailable.hidden = true;
    }
    if (unknown) return;

    byId("lic-licensee").textContent = snapshot.licensee || "—";
    byId("lic-tier").textContent = snapshot.tier_label || snapshot.tier || "—";

    const [label, cls] = LICENSE_STATE_LABELS[status] || [status || "—", ""];
    const stateEl = byId("lic-state");
    const days = Number(snapshot.days_left);
    stateEl.textContent =
      status === "expiring" && Number.isFinite(days)
        ? (days <= 1 ? "истекает завтра" : `истекает через ${pluralDays(days)}`)
        : label;
    stateEl.className = `lic-v ${cls}`;

    byId("lic-until").textContent = snapshot.expires_at ? formatDate(snapshot.expires_at) : "бессрочно";
    byId("lic-activated").textContent = snapshot.activated
      ? [
          snapshot.fingerprint_label
            ? `этом компьютере (${snapshot.fingerprint_label})`
            : "этом компьютере",
          snapshot.activated_at ? formatDate(snapshot.activated_at) : "",
        ].filter(Boolean).join(" · ")
      : "не активирована у издателя";
    byId("lic-source").textContent =
      LICENSE_SOURCE_LABELS[snapshot.source] || snapshot.source || "—";
  }

  // «Сейчас используется: <команда>» / «Сейчас: CLI не найден» под полем «CLI
  // BPMkit» (Настройки → Основные). ``cli`` — итоговая команда строкой
  // (subprocess.list2cmdline на хабе) или null, если резолв ничего не нашёл.
  function renderCliHint(snapshot) {
    const hint = byId("cli-current-hint");
    if (!hint) return;
    hint.textContent = snapshot && snapshot.cli
      ? `Сейчас используется: ${snapshot.cli}`
      : "Сейчас: CLI не найден";
  }

  // «Обновления: подключены / не подключены — нет лицензии» (GAP-278 п.3).
  //
  // Источников два, и они приходят В РАЗНОЕ ВРЕМЯ: /api/version знает редакцию
  // сборки, /api/license — состояние лицензии. Поэтому функция зовётся из
  // обоих мест и каждый раз берёт то, что уже известно: иначе более поздний
  // ответ затирал бы более точный текст более общим.
  let lastEdition = null;

  function renderAboutUpdates(edition) {
    if (edition !== undefined && edition !== null) lastEdition = edition;
    const el = byId("about-edition");
    const link = byId("about-license-link");
    if (!el) return;

    if (lastEdition === null) {
      el.textContent = "—";
      if (link) link.hidden = true;
      return;
    }
    if (lastEdition !== "companion") {
      // Сборка без канала обновлений: лицензия тут ни при чём, и ссылка на
      // неё была бы ложным следом.
      el.textContent = "не подключены — сборка без канала обновлений";
      if (link) link.hidden = true;
      return;
    }
    const lic = lastLicense || {};
    const connected =
      LICENSE_CHANNEL_STATUSES.indexOf(lic.status) >= 0;
    el.textContent = connected
      ? "подключены"
      : lic.status
      ? `не подключены — лицензия ${(LICENSE_STATE_LABELS[lic.status] || [lic.status])[0]}`
      : "не подключены — нет лицензии";
    if (link) link.hidden = connected;
  }

  // Единственный источник правды для фактической версии MCP — читают и экран
  // лицензии (renderMcpRow не звонит до первого ответа канала обновлений, а
  // /api/license отвечает и в свободной редакции), и статус канала. Приоритет
  // (GAP-311/CLI-версия): (а) license.mcp_version — CLI сказал явно; (б)
  // current_version канала обновлений (тот же MCP, но по данным канала); (в)
  // иначе честно «неизвестна», с уточнением, если известно, что CLI вовсе не
  // найден (снимок лицензии cli === null).
  function renderMcpVersion() {
    const lic = lastLicense || {};
    const rel = releasesBlock(lastCompanionStatus);
    const version = String(lic.mcp_version || rel.current_version || "").trim();

    const mcpVersionEl = byId("about-mcp-version");
    if (mcpVersionEl) {
      mcpVersionEl.textContent = version
        || (lic.cli === null ? "неизвестна — не найден CLI BPMkit" : "неизвестна");
    }

    // Строка состояния внизу: сегмент не прячем даже без версии — «MCP
    // неизвестна» видно всегда, а подсказка (title) ведёт чинить это в
    // «Настройки → Основные», поле «CLI BPMkit».
    const slMcp = byId("sl-mcp");
    const slMcpVersion = byId("sl-mcp-version");
    if (slMcp && slMcpVersion) {
      slMcp.hidden = false;
      if (version) {
        slMcpVersion.textContent = version;
        slMcp.removeAttribute("title");
      } else {
        slMcpVersion.textContent = "неизвестна";
        slMcp.title = "Версия MCP неизвестна — укажите CLI BPMkit в «Настройки → Основные»";
      }
    }
  }

  function applyLicense(snapshot) {
    lastLicense = snapshot;
    // «Есть лицензия» = действующая (в т.ч. истекающая). Истёкшая/отозванная —
    // тоже «нет лицензии» для канала издателя: бэкенд её не примет.
    const known = snapshot.edition === "companion"
      && LICENSE_CHANNEL_STATUSES.indexOf(snapshot.status) >= 0;
    // Кнопка «Обновления» и одноимённый раздел настроек существуют только там,
    // где им есть что делать: без лицензии канал издателя не работает вовсе.
    byId("btn-updates").hidden = !known;
    byId("rail-updates").hidden = !known;
    if (!known && document.querySelector('.settings-pane[data-pane="updates"].active')) {
      selectSettingsPane("general");
    }
    renderLicensePane(snapshot);
    renderLicenseBanners(snapshot);
    renderLicenseStatusline(snapshot);
    renderCliHint(snapshot);
    renderMcpVersion();
    renderAboutUpdates();
    maybeShowLicenseCritModal(snapshot);
  }

  async function refreshLicense() {
    try {
      const data = await apiGet("/api/license");
      applyLicense(data);
    } catch (e) {
      byId("license-error").textContent = describeApiError(e);
    }
  }

  /** Ответ мутации лицензии имеет ту же форму, что GET, — применяем как снимок. */
  function applyLicenseMutation(data) {
    applyLicense(data);
    refreshCompanionStatus({ quiet: true });
  }

  function licenseErrorText(e) {
    const payload = e && e.data;
    if (payload && (payload.error || payload.detail)) {
      return [payload.error, payload.detail].filter(Boolean).join(": ");
    }
    return describeApiError(e);
  }

  async function activateLicenseToken() {
    const btn = byId("license-activate-btn");
    const statusEl = byId("license-free-status");
    const errorEl = byId("license-error");
    const token = byId("license-token").value.trim();
    errorEl.textContent = "";
    statusEl.textContent = "";
    if (!token) {
      errorEl.textContent = "Вставьте текст ключа или укажите файл лицензии.";
      return;
    }
    setButtonBusy(btn, "Активируем…");
    try {
      const data = await apiSend("PUT", "/api/license", { token });
      byId("license-token").value = "";
      applyLicenseMutation(data);
      toast("Лицензия активирована");
    } catch (e) {
      errorEl.textContent = licenseErrorText(e);
    } finally {
      clearButtonBusy(btn);
    }
  }

  /** Ключ из файла: на хаб уходит ПУТЬ, содержимое через страницу не едет. */
  async function activateLicenseFile(path) {
    const errorEl = byId("license-error");
    errorEl.textContent = "";
    try {
      const data = await apiSend("POST", "/api/license/file", { path });
      applyLicenseMutation(data);
      toast("Лицензия активирована");
    } catch (e) {
      errorEl.textContent = licenseErrorText(e);
    }
  }

  async function deleteLicense() {
    const confirmed = await styledConfirm(
      "Удалить лицензию",
      "Снять активацию с этого компьютера и удалить ключ? Ключ можно будет использовать на другой машине."
    );
    if (!confirmed) return;
    const btn = byId("lic-delete-btn");
    const errorEl = byId("license-error");
    errorEl.textContent = "";
    setButtonBusy(btn, "Удаляем…");
    try {
      const data = await apiSend("DELETE", "/api/license");
      applyLicenseMutation(data);
      // remote: released | unreachable | error — исход у издателя, локальное
      // удаление выполняется в любом случае, и умалчивать о разнице нельзя.
      toast(data && data.remote === "released"
        ? "Лицензия удалена, активация снята у издателя"
        : "Лицензия удалена локально; активация у издателя не снята");
    } catch (e) {
      errorEl.textContent = licenseErrorText(e);
    } finally {
      clearButtonBusy(btn);
    }
  }

  function setupLicensePane() {
    byId("license-activate-btn").addEventListener("click", activateLicenseToken);
    byId("lic-delete-btn").addEventListener("click", deleteLicense);
    byId("license-pick-btn").addEventListener("click", async () => {
      const path = await pickPath({
        kind: "file",
        title: "Файл лицензии BPMkit",
        filter: "Лицензия (*.lic)|*.lic|Все файлы (*.*)|*.*",
      });
      if (path) await activateLicenseFile(path);
    });

    // Перетаскивание файла: содержимое читается в браузере и уезжает тем же
    // PUT, что и вставленный текст — путь у dropped-файла недоступен в принципе.
    const drop = byId("license-drop");
    ["dragenter", "dragover"].forEach((type) => {
      drop.addEventListener(type, (evt) => {
        evt.preventDefault();
        drop.classList.add("license-drop-over");
      });
    });
    ["dragleave", "drop"].forEach((type) => {
      drop.addEventListener(type, () => drop.classList.remove("license-drop-over"));
    });
    drop.addEventListener("drop", (evt) => {
      evt.preventDefault();
      const file = evt.dataTransfer && evt.dataTransfer.files && evt.dataTransfer.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        byId("license-token").value = String(reader.result || "").trim();
        activateLicenseToken();
      };
      reader.onerror = () => {
        byId("license-error").textContent = "Не удалось прочитать файл лицензии.";
      };
      reader.readAsText(file);
    });

    byId("license-crit-close-btn").addEventListener("click", closeLicenseCritModal);
    bindOverlayDismiss(byId("license-crit-overlay"), closeLicenseCritModal);
  }

  // --- «Данные и телеметрия» (согласия MCP, GAP-332) ---
  //
  // Хаб — тонкий прокси к CLI (см. standkit_hub/consent_api.py): своей логики
  // согласий здесь нет. Раздел показывается ВСЕГДА, в т.ч. в свободной
  // редакции и без CLI рядом — там вместо переключателей #consent-unavailable
  // (тот же приём, что #license-unavailable).

  // Период опроса СОГЛАСОВАН с TTL кэша сводки на хабе (`consent_api.
  // CACHE_TTL_SEC` = 60 с, ревью Opus М9): опрашивать чаще бессмысленно —
  // ответ всё равно придёт из того же кэша, опрашивать заметно реже означало
  // бы, что переключённое из другой вкладки/CLI согласие видно с большой
  // задержкой. 60 секунд — ровно TTL, не быстрее и не медленнее его.
  const CONSENT_POLL_MS = 60000;

  const CONSENT_FIELDS = [
    ["analytics", "consent-analytics"],
    ["attach_logs", "consent-attach-logs"],
    ["pattern_submission", "consent-pattern-submission"],
    ["candidate_submission", "consent-candidate-submission"],
  ];

  let lastConsent = null;

  /** Ставит переключатели формы по ответу CLI. Каждое поле — объект
   * `{value, decided, decided_at}` (контракт `consent-info --json`); здесь
   * нужно только `value`, `decided`/`decided_at` идут в текст ниже. */
  function applyConsentToggles(snapshot) {
    CONSENT_FIELDS.forEach(([field, elementId]) => {
      const input = byId(elementId);
      if (!input) return;
      const entry = snapshot[field] || {};
      input.checked = Boolean(entry.value);
    });
  }

  /** Есть ли неотправленные накопленные данные телеметрии. CLI-контракт
   * переезжает с `pending_days` (число суток — по факту недостижимое, глубже
   * периода истории у CLI нет) на `has_pending` (bool); на время
   * рассинхронизации веток принимаются ОБА варианта, иначе ветки блокируют
   * друг друга (ревью Opus В5): `has_pending`, если он есть, иначе истинность
   * `pending_days`. */
  function consentHasPending(telemetry) {
    if (Object.prototype.hasOwnProperty.call(telemetry, "has_pending")) {
      return Boolean(telemetry.has_pending);
    }
    return Boolean(Number(telemetry.pending_days));
  }

  /** Строка состояния: «последняя отправка: … · накоплено: … · приёмник: …
   * · установка …<хвост>». Приёмник — ТОЛЬКО хост (см. contract
   * telemetry.backend_host), без схемы и пути — иначе строка выглядит как
   * кликабельный адрес, которым не является. */
  function renderConsentTelemetryStatus(snapshot) {
    const el = byId("consent-telemetry-status");
    if (!el) return;
    const telemetry = snapshot.telemetry || {};
    const lastSent = telemetry.last_sent_at ? formatDate(telemetry.last_sent_at) : "не было";
    const pendingText = consentHasPending(telemetry)
      ? "есть неотправленные данные"
      : "нет неотправленных данных";
    const backendHost = telemetry.backend_host || "—";
    const tail = snapshot.install_id_tail ? `…${snapshot.install_id_tail}` : "—";
    el.textContent =
      `последняя отправка: ${lastSent} · накоплено: ${pendingText} · ` +
      `приёмник: ${backendHost} · установка ${tail}`;
  }

  function renderConsentEulaStatus(snapshot) {
    const el = byId("consent-eula-status");
    if (!el) return;
    el.textContent = snapshot.eula_accepted_at
      ? `Соглашение принято: ${formatDate(snapshot.eula_accepted_at)}`
      : "Соглашение не принято";
  }

  /** Предупреждение о повреждённом файле согласий (ревью Opus В4): CLI при
   * `consent_file_corrupted: true` отдаёт умолчания, но САМ файл не
   * перезаписывает — первое же переключение здесь запишет его заново.
   * Раздел обязан сказать это ДО клика, иначе выглядит как «настоящие»
   * значения, которых на диске нет вовсе. */
  function renderConsentCorruptedWarning(snapshot) {
    const el = byId("consent-corrupted-warning");
    if (!el) return;
    if (!snapshot.consent_file_corrupted) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent =
      "Файл согласий повреждён — показаны умолчания. Изменение любого " +
      "переключателя перезапишет файл." +
      (snapshot.detail ? ` (${snapshot.detail})` : "");
  }

  function renderConsentPane(snapshot) {
    const unavailable = byId("consent-unavailable");
    const content = byId("consent-content");
    if (!snapshot.available) {
      content.hidden = true;
      unavailable.hidden = false;
      unavailable.textContent =
        `Проверить согласия не удалось: ${snapshot.reason || "CLI BPMkit недоступен"}. ` +
        "Укажите путь к CLI в «Настройки → Основные», поле «CLI BPMkit».";
      return;
    }
    unavailable.hidden = true;
    content.hidden = false;
    applyConsentToggles(snapshot);
    renderConsentTelemetryStatus(snapshot);
    renderConsentEulaStatus(snapshot);
    renderConsentCorruptedWarning(snapshot);
  }

  function applyConsent(snapshot) {
    lastConsent = snapshot;
    renderConsentPane(snapshot);
  }

  async function refreshConsent() {
    try {
      const data = await apiGet("/api/consent");
      applyConsent(data);
      byId("consent-error").textContent = "";
    } catch (e) {
      byId("consent-error").textContent = describeApiError(e);
    }
  }

  /** Один переключатель → один POST с ровно одним изменённым флагом.
   * Отдельные запросы, а не «собрать все четыре и отправить одним» — так
   * промах в одном чекбоксе откатывается сам собой (ответ несёт свежий
   * снимок), не трогая остальные три. */
  async function sendConsentFlag(field, value) {
    const statusEl = byId("consent-status");
    const errorEl = byId("consent-error");
    statusEl.textContent = "Сохранение…";
    try {
      const data = await apiSend("POST", "/api/consent", { [field]: value });
      applyConsent(data);
      statusEl.textContent = "Сохранено";
      errorEl.textContent = "";
    } catch (e) {
      // Откат чекбокса на предыдущее значение — иначе UI показывает состояние,
      // которого сервер не подтвердил.
      if (lastConsent) applyConsentToggles(lastConsent);
      statusEl.textContent = "";
      errorEl.textContent = describeApiError(e);
    }
  }

  function openConsentPreview() {
    const overlay = byId("consent-preview-overlay");
    if (!overlay) return;
    const text = (lastConsent && lastConsent.preview) || "Предпросмотр недоступен.";
    // textContent, НЕ innerHTML: preview — текст от CLI, доверять ему как
    // разметке нельзя (см. комментарий у самой модалки в index.html).
    byId("consent-preview-text").textContent = text;
    overlay.hidden = false;
  }

  function closeConsentPreview() {
    const overlay = byId("consent-preview-overlay");
    if (overlay) overlay.hidden = true;
  }

  function setupConsentPane() {
    CONSENT_FIELDS.forEach(([field, elementId]) => {
      const input = byId(elementId);
      if (!input) return;
      input.addEventListener("change", () => sendConsentFlag(field, input.checked));
    });
    const previewBtn = byId("consent-preview-btn");
    if (previewBtn) previewBtn.addEventListener("click", openConsentPreview);
    const closeBtn = byId("consent-preview-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", closeConsentPreview);
    const closeFooterBtn = byId("consent-preview-close-footer-btn");
    if (closeFooterBtn) closeFooterBtn.addEventListener("click", closeConsentPreview);
    const overlay = byId("consent-preview-overlay");
    if (overlay) bindOverlayDismiss(overlay, closeConsentPreview);
  }

  // --- нативный выбор файла/каталога (POST /api/pick) ---
  //
  // Браузерный <input type="file"> отдаёт содержимое файла, но не путь, а полям
  // настроек нужен именно путь. Диалог поднимает сама ОС (см.
  // standkit_hub/pick_dialog.py); отмена и «диалога на этой машине нет» —
  // разные исходы, и второй обязан сказать «введите путь руками».

  async function pickPath(options) {
    try {
      const data = await apiSend("POST", "/api/pick", {
        kind: options.kind || "file",
        title: options.title || "",
        initial: options.initial || "",
        filter: options.filter || "",
      });
      if (data && data.path) return data.path;
      if (data && data.error) {
        toast("Диалог выбора недоступен на этой машине — введите путь вручную.");
      }
      return null;
    } catch (e) {
      toast(`Диалог выбора не открыт: ${describeApiError(e)}`);
      return null;
    }
  }

  function setupPickButtons() {
    document.querySelectorAll(".pick-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const form = document.getElementById("settings-form");
        const input = form.elements.namedItem(btn.dataset.pickFor);
        if (!input) return;
        btn.disabled = true;
        try {
          const path = await pickPath({
            kind: btn.dataset.pick || "file",
            title: btn.dataset.pick === "dir" ? "Выберите папку" : "Выберите файл",
            initial: input.value || "",
          });
          if (path) input.value = path;
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  // --- строка состояния ---

  function updateStatuslinePatterns(status) {
    const target = byId("sl-patterns");
    if (!target) return;
    const block = patternsBlock(status);
    if (!status || !block.last_run_at) {
      target.hidden = true;
      target.textContent = "";
      return;
    }
    target.hidden = false;
    target.textContent = "";
    target.appendChild(document.createTextNode("паттерны "));
    const b = document.createElement("b");
    b.textContent = `обновлены ${describeMoment(block.last_run_at)}`;
    target.appendChild(b);
  }

  function updateStatuslineStands(stands) {
    const target = byId("sl-stands");
    if (!target) return;
    const total = (stands || []).length;
    const running = (stands || []).filter(
      (s) => s && s.process && s.process.state === "ok"
    ).length;
    target.textContent = "";
    target.appendChild(document.createTextNode("стендов: "));
    const totalEl = document.createElement("b");
    totalEl.textContent = String(total);
    target.appendChild(totalEl);
    target.appendChild(document.createTextNode(", работает "));
    const runEl = document.createElement("b");
    runEl.textContent = String(running);
    target.appendChild(runEl);
  }

  // --- настройки ---

  const SETTINGS_FIELDS = [
    "registry_path",
    "run_dir",
    "log_dir",
    "refresh_interval_sec",
    "idle_shutdown_min",
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
  //
  // GAP-241: из карты убраны три поля, которых больше нет ни в форме, ни в
  // ответе /api/settings (server._UI_HIDDEN_COMPANION_FIELDS): backend_url —
  // адрес издателя, revocations.* — цикл отзыва (он всегда включён и тикает с
  // частотой паттернов), require_pattern_signature — строгий режим, который
  // сегодня просто выключил бы доставку. Интервал паттернов показывается в
  // ЧАСАХ: дефолт 6 часов в минутах читается как 360 и выглядит опечаткой.
  const COMPANION_SETTINGS_MAP = [
    ["companion_enabled", ["enabled"], 0],
    ["companion_mcp_cli", ["mcp_cli"], 0],
    ["companion_patterns_interval_hours", ["patterns", "interval_sec"], 3600],
    ["companion_releases_enabled", ["releases", "enabled"], 0],
    ["companion_releases_interval_hours", ["releases", "interval_sec"], 3600],
    ["companion_auto_stage_release", ["auto_stage_release"], 0],
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
    const rail = document.getElementById("rail-updates");
    if (rail) {
      rail.title = data.enabled === false
        ? "Канал обновлений выключен"
        : "Канал обновлений включён";
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
      // Сервер для run_dir/log_dir отдаёт пустой дефолт (резолв каталога идёт
      // позже), а «не задано» читалось как «диспетчер работает без каталога».
      // Показываем фактический каталог по умолчанию (приёмка 16.09.2026).
      const KNOWN_DEFAULTS = {
        run_dir: "по умолчанию: ~\\.standkit\\run",
        log_dir: "по умолчанию: ~\\.standkit\\logs",
      };
      input.placeholder =
        fallback === undefined || fallback === null || fallback === ""
          ? (KNOWN_DEFAULTS[field] || "не задано")
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
    // если проблемное поле лежит в НЕАКТИВНОМ разделе рейки: браузеру некуда
    // показать подсказку у невидимого элемента, и кнопка «Сохранить» просто
    // перестаёт отвечать — ни ошибки, ни сохранения. Поймано на разделе
    // «Обновления» (интервалы с min), но касается любого скрытого раздела.
    // Событие invalid НЕ всплывает — слушаем на фазе захвата, открываем раздел
    // и передаём фокус, чтобы человек увидел и подсказку, и само поле.
    form.addEventListener(
      "invalid",
      (evt) => {
        const field = evt.target;
        if (!field || !field.closest) return;
        const pane = field.closest(".settings-pane");
        if (pane && pane.dataset.pane && !pane.classList.contains("active")) {
          selectSettingsPane(pane.dataset.pane);
        }
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
        toast("Настройки не сохранены — проверьте выделенные поля");
        const firstBad = form.querySelector('[aria-invalid="true"]');
        if (firstBad) {
          // Поле может лежать в ДРУГОМ разделе рейки: без переключения человек
          // видит только «не сохранено» и ни одного подсвеченного поля —
          // ровно та немая кнопка «Сохранить», из-за которой в форме появился
          // перехват события invalid (см. ниже).
          const pane = firstBad.closest(".settings-pane");
          if (pane && pane.dataset.pane) selectSettingsPane(pane.dataset.pane);
          firstBad.focus();
        }
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
        // Строка статуса живёт в разделе «Основные», а «Сохранить» есть в
        // каждом: без тоста подтверждение уезжало бы на невидимый экран.
        toast("Настройки сохранены");
        // Новый refresh_interval_sec применяем немедленно, без перезагрузки
        // страницы (старый таймер снимается внутри applyRefreshInterval).
        applyRefreshInterval(saved && saved.refresh_interval_sec);
        // Сервер мог поджать интервал канала к минимуму — показываем то, что
        // реально сохранено, иначе форма врала бы про собственное значение.
        fillCompanionSettings(form, saved && saved.companion);
        // Настройки канала могли измениться — обновляем вкладку «Обновления»
        // сразу, а не при следующем заходе на неё.
        if (companionAvailable) refreshCompanionStatus({ quiet: true });
        // Путь к CLI BPMkit мог измениться — сводка лицензии, снятая по старому
        // пути, устарела ровно в этот момент (сервер сбрасывает свой кэш там же).
        refreshLicense();
        await refreshSecretStatuses();
      } catch (e) {
        statusEl.textContent = `Ошибка сохранения: ${describeApiError(e)}`;
        toast(`Ошибка сохранения: ${describeApiError(e)}`);
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
    setupScenes();
    setupElevation();
    setupRegisterModal();
    setupAgentTab();
    setupUpdatesDialog();
    setupLicensePane();
    setupConsentPane();
    setupPickButtons();
    setupSettingsForm();
    setupStatePanel();
    setupHelpMenu();
    setupActionStatus();
    document.getElementById("refresh-stands-btn").addEventListener("click", refreshStandsWithFeedback);

    // Быстрая первая отрисовка (?probe=0) + полный статус вторым запросом.
    firstPaint();
    // Push-обновления; при их отсутствии работает резервный таймер ниже.
    setupEventStream();
    refreshAgentStatus();
    loadVersionInfo();
    // Статус канала спрашивается сразу, ещё до открытия окна обновлений: только
    // так бейдж «есть новая версия / нужен перезапуск» может зажечься на кнопке
    // в шапке у человека, который в окно не заглядывает.
    refreshCompanionStatus({ quiet: true });
    // GAP-279: редкий фоновый опрос канала — иначе уведомление «найдено обновление»
    // и заголовок вкладки срабатывали бы только при открытом окне «Обновления».
    setInterval(() => {
      if (companionAvailable && !updatesDialogIsOpen()) refreshCompanionStatus({ quiet: true });
    }, COMPANION_BACKGROUND_POLL_MS);
    setupUpdateNotifications();
    // Лицензия — тоже сразу: баннер «истекает через 3 дня» обязан появиться до
    // того, как человек что-то нажмёт, а не после захода в настройки.
    refreshLicense();
    setInterval(refreshLicense, LICENSE_POLL_MS);
    // Согласия — раздел независимый от лицензии (виден и без неё). Интервал —
    // CONSENT_POLL_MS, согласованный с TTL кэша сводки на хабе (60 с).
    refreshConsent();
    setInterval(refreshConsent, CONSENT_POLL_MS);
    refreshElevation();
    loadSettings().catch((e) => {
      document.getElementById("settings-status").textContent = `Ошибка загрузки настроек: ${describeApiError(e)}`;
    });

    // Стартовый период — дефолтный; реальный refresh_interval_sec приедет с
    // первым же ответом /api/stands и перезаведёт таймер (applyRefreshInterval).
    restartBackgroundTimer();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
