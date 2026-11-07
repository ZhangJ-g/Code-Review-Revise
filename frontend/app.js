"use strict";

// 外观偏好只属于浏览器。启动时在样式加载前确定主题，避免刷新时闪回浅色。
const appearance = (() => {
  const themeKey = "code-agent.theme";
  const layoutKey = "code-agent.layout";
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  const narrowSidebar = matchMedia("(max-width: 760px)");
  const narrowInspector = matchMedia("(max-width: 1180px)");
  const read = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch { /* 隐私模式仍可切换。 */ } };
  let preference = read(themeKey) || "system";
  if (!["light", "dark", "system"].includes(preference)) preference = "system";
  let layout = { sidebarCollapsed: false, inspectorCollapsed: false };
  try {
    const stored = JSON.parse(read(layoutKey));
    if (stored) layout = { sidebarCollapsed: stored.sidebarCollapsed === true, inspectorCollapsed: stored.inspectorCollapsed === true };
  } catch { /* 无效偏好回到默认布局。 */ }
  let elements = null;
  let drawerTrigger = null;

  function applyTheme() {
    const theme = preference === "system" ? (systemTheme.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    if (!elements) return;
    const label = theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
    elements.theme.title = label;
    elements.theme.setAttribute("aria-label", label);
    elements.theme.querySelector("use").setAttribute("href", `/assets/icons.svg#${theme === "dark" ? "sun" : "moon"}`);
    document.querySelectorAll("[data-theme-option]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.themeOption === preference));
    });
  }

  function chooseTheme(value) {
    preference = value;
    save(themeKey, value);
    applyTheme();
  }

  function syncPanels() {
    if (!elements) return;
    const sidebarVisible = narrowSidebar.matches ? elements.sidebar.classList.contains("open") : !layout.sidebarCollapsed;
    const inspectorVisible = narrowInspector.matches ? elements.inspector.classList.contains("open") : !layout.inspectorCollapsed;
    elements.workspace.classList.toggle("sidebar-collapsed", layout.sidebarCollapsed);
    elements.workspace.classList.toggle("inspector-collapsed", layout.inspectorCollapsed);
    elements.sidebar.inert = !sidebarVisible;
    elements.inspector.inert = !inspectorVisible;
    elements.sidebar.setAttribute("aria-hidden", String(!sidebarVisible));
    elements.inspector.setAttribute("aria-hidden", String(!inspectorVisible));
    elements.menu.setAttribute("aria-expanded", String(sidebarVisible));
    elements.inspectorToggle.setAttribute("aria-expanded", String(inspectorVisible));
    const drawer = narrowSidebar.matches && elements.sidebar.classList.contains("open") ? elements.sidebar
      : narrowInspector.matches && elements.inspector.classList.contains("open") ? elements.inspector : null;
    elements.backdrop.hidden = !drawer;
    // 抽屉打开时，键盘和辅助技术只访问当前面板。
    elements.main.inert = Boolean(drawer === elements.sidebar);
    elements.center.inert = Boolean(drawer === elements.inspector);
    if (drawer === elements.inspector) elements.topbar.inert = true;
    else elements.topbar.inert = false;
    if (drawer === elements.inspector) elements.sidebar.inert = true;
    requestAnimationFrame(resizeComposer);
  }

  function closeDrawers(restoreFocus = false) {
    if (!elements) return;
    const wasOpen = elements.sidebar.classList.contains("open") || elements.inspector.classList.contains("open");
    elements.sidebar.classList.remove("open");
    elements.inspector.classList.remove("open");
    syncPanels();
    if (restoreFocus && wasOpen) drawerTrigger?.focus();
  }

  function togglePanel(kind) {
    const sidebar = kind === "sidebar";
    const panel = sidebar ? elements.sidebar : elements.inspector;
    const narrow = sidebar ? narrowSidebar.matches : narrowInspector.matches;
    const trigger = sidebar ? elements.menu : elements.inspectorToggle;
    if (narrow) {
      const open = !panel.classList.contains("open");
      closeDrawers();
      if (open) {
        panel.classList.add("open");
        drawerTrigger = trigger;
        syncPanels();
        panel.querySelector(sidebar ? "input" : "button")?.focus();
      } else trigger.focus();
    } else {
      const key = sidebar ? "sidebarCollapsed" : "inspectorCollapsed";
      layout[key] = !layout[key];
      save(layoutKey, JSON.stringify(layout));
      syncPanels();
      trigger.focus();
    }
  }

  function focusSearch() {
    if (narrowSidebar.matches && !elements.sidebar.classList.contains("open")) togglePanel("sidebar");
    else if (!narrowSidebar.matches && layout.sidebarCollapsed) togglePanel("sidebar");
    elements.sidebar.querySelector("input").focus();
  }

  function resizeComposer() {
    const input = document.getElementById("task-input");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(Math.max(input.scrollHeight, 60), 180)}px`;
  }

  function init() {
    const get = (id) => document.getElementById(id);
    elements = { workspace: get("workspace"), sidebar: get("sidebar"), inspector: get("inspector"),
      menu: get("menu-button"), inspectorToggle: get("inspector-toggle"), backdrop: get("sidebar-backdrop"),
      main: document.querySelector(".main-workspace"), center: get("conversation-pane"),
      topbar: document.querySelector(".topbar"), theme: get("theme-button") };
    applyTheme();
    syncPanels();
    elements.theme.addEventListener("click", () => chooseTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
    document.querySelectorAll("[data-theme-option]").forEach((button) => button.addEventListener("click", () => chooseTheme(button.dataset.themeOption)));
    const dialog = get("appearance-dialog");
    ["settings-button", "workspace-settings"].forEach((id) => get(id).addEventListener("click", () => dialog.showModal()));
    ["appearance-close", "appearance-done"].forEach((id) => get(id).addEventListener("click", () => dialog.close()));
    get("layout-reset").addEventListener("click", () => {
      layout = { sidebarCollapsed: false, inspectorCollapsed: false };
      save(layoutKey, JSON.stringify(layout));
      closeDrawers();
    });
    get("inspector-toggle").addEventListener("click", () => togglePanel("inspector"));
    get("inspector-close").addEventListener("click", () => togglePanel("inspector"));
    elements.backdrop.addEventListener("click", () => closeDrawers(true));
    narrowSidebar.addEventListener("change", () => closeDrawers(true));
    narrowInspector.addEventListener("change", () => closeDrawers(true));
    window.addEventListener("resize", resizeComposer);
    document.addEventListener("keydown", (event) => {
      if (document.querySelector("dialog[open]")) return;
      const drawer = narrowSidebar.matches && elements.sidebar.classList.contains("open") ? elements.sidebar
        : narrowInspector.matches && elements.inspector.classList.contains("open") ? elements.inspector : null;
      if (!drawer) return;
      if (event.key === "Escape") { event.preventDefault(); closeDrawers(true); }
      if (event.key === "Tab") {
        const focusable = [...drawer.querySelectorAll('a[href], button:not(:disabled), input, summary, [tabindex="0"]')]
          .filter((item) => item.getClientRects().length && !item.closest("[inert]"));
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
  }

  systemTheme.addEventListener("change", () => { if (preference === "system") applyTheme(); });
  window.addEventListener("storage", (event) => {
    if (event.key === themeKey) {
      preference = ["light", "dark"].includes(event.newValue) ? event.newValue : "system";
      applyTheme();
    }
  });
  applyTheme();
  return { init, togglePanel, focusSearch, resizeComposer, closeDrawers,
    closeSidebar: () => {
      if (elements?.sidebar.classList.contains("open")) {
        closeDrawers();
        document.getElementById("messages").focus({ preventScroll: true });
      }
    } };
})();

// 历史、任务和执行均由后端管理；页面只提交用户意图并消费快照。
const $ = (id) => document.getElementById(id);
const state = { conversations: [], snapshot: null, info: null, notices: [], request: 0, refreshing: false,
  mutating: false, submitting: false, fork: null, dialog: null, drafts: new Map(), approvalId: null, csrf: null };
const responseCache = new Map();
const pendingMutations = new Map();
let memoryPage = null;
let memoryPageLoading = null;
let memoryView = false;
const number = new Intl.NumberFormat("zh-CN");
const dateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
let toastTimer;
let noticeKey = "";

function node(tag, className = "", text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function icon(name, extraClass = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon ${extraClass}`);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `/assets/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

function time(value) {
  return value ? dateTime.format(new Date(value)) : "—";
}

function textValue(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3000);
}

function connection(online) {
  const element = $("connection-status");
  element.classList.toggle("online", online);
  element.classList.toggle("offline", !online);
  element.querySelector("span").textContent = online ? "本地服务已连接" : "服务连接中断";
}

function createRequestId() {
  const random = globalThis.crypto;
  if (typeof random?.randomUUID === "function") return random.randomUUID();
  // 局域网/公网 HTTP 页面可能没有 randomUUID；仍使用浏览器安全随机数生成 UUID。
  if (typeof random?.getRandomValues !== "function") {
    throw new Error("当前浏览器无法生成安全请求 ID，请更新浏览器或使用 HTTPS 访问。");
  }
  const bytes = random.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function fetchResponse(path, options) {
  try {
    return await fetch(path, options);
  } catch (cause) {
    if (!(cause instanceof TypeError) && !["TimeoutError", "AbortError"].includes(cause.name)) throw cause;
    // 只有实际写请求的网络失败才意味着执行结果未知；页面代码的 TypeError 不能冒充超时。
    const writing = ["POST", "PUT", "DELETE"].includes(options.method);
    const error = new Error(writing
      ? "操作结果尚未确认，请刷新检查点；再次提交会沿用原请求 ID。"
      : "无法读取服务，请检查网络连接和 Web 服务是否仍在运行。");
    error.code = writing ? "request_unconfirmed" : "connection_failed";
    throw error;
  }
}

async function api(path) {
  const cached = responseCache.get(path);
  const response = await fetchResponse(path, {
    cache: "no-store", credentials: "same-origin",
    signal: AbortSignal.timeout(12000),
    headers: cached?.etag ? { "If-None-Match": cached.etag } : {},
  });
  if (response.status === 304 && cached) return cached.data;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.error?.message || "本地服务暂时不可用，请稍后刷新。");
    error.code = payload?.error?.code;
    throw error;
  }
  responseCache.set(path, { etag: response.headers.get("ETag"), data: payload });
  return payload;
}

async function mutate(path, body, method = "POST") {
  const semantic = { ...body };
  delete semantic.expected_revision;
  // 版本号可能随刷新变化；判断是不是同一操作时，先忽略版本号。
  delete semantic.expected_memory_revision;
  const key = method + path + JSON.stringify(semantic);
  // 若上次请求结果未知，重试仍使用原 ID 和原参数，避免重复执行。
  const payload = pendingMutations.get(key) || { ...body, client_request_id: createRequestId() };
  pendingMutations.set(key, payload);
  if (!state.csrf) state.csrf = (await api("/api/session")).csrf_token;
  async function send() {
    const response = await fetchResponse(path, { method, credentials: "same-origin", cache: "no-store",
      signal: AbortSignal.timeout(15000),
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: JSON.stringify(payload) });
    return { response, result: await response.json().catch(() => null) };
  }
  let { response, result } = await send();
  if (response.status === 403 && result?.error?.code === "csrf") {
    // 服务重启后的 CSRF 拒绝发生在执行之前，只更新凭证并沿用原请求 ID。
    state.csrf = (await api("/api/session")).csrf_token;
    ({ response, result } = await send());
  }
  if (!response.ok) {
    if (response.status < 500) pendingMutations.delete(key);
    const error = new Error(result?.error?.message || "操作结果尚未确认，请刷新检查点后再处理。");
    error.code = result?.error?.code;
    throw error;
  }
  pendingMutations.delete(key);
  return result;
}

function runtime() {
  const snapshot = state.snapshot?.runtime || {};
  const info = state.info?.runtime || {};
  if (info.instance_id && info.instance_id !== snapshot.instance_id) return info;
  return (snapshot.version || 0) >= (info.version || 0) ? snapshot : info;
}

function writable() {
  return Boolean(state.info?.execution_connected && runtime().execution_connected);
}

function applyControls() {
  const live = runtime();
  const busy = Boolean(live.busy || state.mutating);
  const fork = Boolean(state.fork && state.snapshot && state.fork.conversationId === state.snapshot.conversation.id);
  const canSubmit = writable() && !busy && state.snapshot && (fork || state.snapshot.can_submit);
  $("new-button").disabled = !writable() || busy;
  $("task-input").disabled = !canSubmit;
  $("send-button").disabled = !canSubmit || !$("task-input").value.trim();
  $("send-button").classList.toggle("loading", Boolean(state.submitting));
  $("send-button").setAttribute("aria-busy", String(Boolean(state.submitting)));
  $("send-button").setAttribute("aria-label", state.submitting ? "正在提交任务" : "发送任务");
  $("send-button").querySelector("use").setAttribute("href", `/assets/icons.svg#${state.submitting ? "refresh" : "arrow"}`);
  $("composer-area").setAttribute("aria-busy", String(Boolean(state.submitting)));
  $("task-input").closest(".composer").classList.toggle("is-disabled", !canSubmit);
  appearance.resizeComposer();
  $("cancel-fork").disabled = busy;
  $("dialog-close").disabled = state.mutating;
  $("dialog-cancel").disabled = state.mutating;
  $("edition-tag").textContent = writable() ? "本地 Agent" : "历史浏览版";
  $("composer-note").textContent = !writable() ? (live.message || "执行服务未连接，当前可以浏览历史。")
    : live.busy ? "任务正在执行或等待审批，完成后可继续提交。"
    : fork ? "新任务接在选中消息后；沿用当前文件，不恢复历史文件版本。"
    : !state.snapshot ? "新建或选择一段对话，开始检查代码。"
    : !state.snapshot.can_submit ? "此位置有未结束的工作，请先恢复、核对或结束原任务。"
    : "描述任务，Ctrl / Cmd + Enter 发送；Enter 换行。";
  document.querySelectorAll("[data-fork-action]").forEach((button) => {
    button.disabled = !writable() || busy || button.dataset.canFork !== "true";
  });
  document.querySelectorAll("[data-task-action]").forEach((button) => { button.disabled = !writable() || busy; });
  document.querySelectorAll("[data-approval-action]").forEach((button) => { button.disabled = !writable() || state.mutating; });
  const phases = { starting: "正在准备任务", waiting_model: "正在请求模型", waiting_approval: "等待你确认工具代码",
    executing_tool: "正在执行已批准的工具", compacting: "正在压缩历史", syncing: "正在同步检查点", finished: "正在保存任务结果" };
  $("execution-banner").hidden = !live.busy;
  $("execution-banner").dataset.phase = live.phase || "idle";
  $("execution-text").textContent = phases[live.phase] || "任务正在执行";
  const elsewhere = live.conversation_id && (live.conversation_id !== state.snapshot?.conversation.id
    || state.snapshot?.selected_leaf_id !== state.snapshot?.conversation.current_leaf_id);
  $("active-task-button").hidden = !live.busy || !elsewhere;
  $("fork-context").hidden = !fork;
  if (fork) {
    $("fork-label").textContent = "从选中消息后继续 · 提交时创建分支";
    $("fork-preview").textContent = state.fork.preview;
  }
}

async function afterWrite(conversationId) {
  const [info, list] = await Promise.all([api("/api/info"), api("/api/conversations")]);
  state.info = info;
  state.conversations = list.conversations;
  state.notices = list.notices;
  renderConversations();
  await selectConversation(conversationId, null, { replace: true });
}

async function perform(action, { dialog = false } = {}) {
  if (state.mutating) return;
  state.mutating = true;
  state.operationError = null;
  renderNotices();
  applyControls();
  try {
    await action();
  } catch (error) {
    const message = error instanceof TypeError ? `页面处理失败：${error.message}` : error.message;
    state.operationError = message;
    renderNotices(message);
    toast(message);
    if (dialog) {
      $("dialog-error").textContent = message;
      $("dialog-error").hidden = false;
    }
    await refresh();
  } finally {
    state.mutating = false;
    applyControls();
    $("dialog-submit").disabled = false;
  }
}

function chooseFork(message) {
  state.fork = { conversationId: state.snapshot.conversation.id, id: message.id,
    preview: message.content || JSON.stringify(message.tool_calls, null, 2) };
  applyControls();
  $("task-input").focus();
}

function forkButton(message) {
  const button = node("button", "message-fork");
  button.type = "button";
  button.dataset.forkAction = "true";
  button.dataset.canFork = String(message.can_fork);
  button.title = message.fork_reason || "在这条消息后创建分支并继续";
  button.setAttribute("aria-label", button.title);
  button.append(icon("branch"), node("span", "", "从这里继续"));
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    chooseFork(message);
  });
  return button;
}

function renderNotices(error = null) {
  const warnings = [...state.notices];
  if (state.info?.config_error) warnings.push(state.info.config_error);
  if (state.operationError) warnings.push(state.operationError);
  if (error && !warnings.includes(error)) warnings.push(error);
  const key = JSON.stringify(warnings);
  if (key === noticeKey) return;
  noticeKey = key;
  const notice = $("notice");
  notice.replaceChildren();
  notice.hidden = warnings.length === 0;
  if (!warnings.length) return;
  if (warnings.length === 1) {
    notice.textContent = warnings[0];
  } else {
    const details = node("details");
    details.append(node("summary", "", `有 ${warnings.length} 条读取提示，点击查看`));
    const list = node("ul");
    warnings.forEach((warning) => list.append(node("li", "", warning)));
    details.append(list);
    notice.append(details);
  }
}

function renderConversations() {
  const query = $("conversation-search").value.trim().toLocaleLowerCase();
  const filtered = state.conversations.filter((item) => (item.title || "新对话").toLocaleLowerCase().includes(query));
  const list = $("conversation-list");
  list.replaceChildren();
  list.setAttribute("aria-busy", "false");
  $("conversation-count").textContent = state.conversations.length;
  if (!filtered.length) {
    list.append(node("p", "list-empty", query ? "没有匹配的对话，试试其他关键词。" : "还没有保存的对话，点击上方新建对话开始。"));
    return;
  }
  filtered.forEach((item) => {
    const button = node("button", "conversation-item");
    button.type = "button";
    button.setAttribute("aria-current", String(item.id === state.snapshot?.conversation.id));
    button.title = item.title;
    const text = node("span");
    text.append(node("strong", "", item.title || "未命名对话"), node("small", "", time(item.updated_at)));
    button.append(icon("chat"), text);
    button.addEventListener("click", () => selectConversation(item.id));
    list.append(button);
  });
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板");
  } catch {
    toast("无法访问剪贴板，请选择文字后手动复制。");
  }
}

function copyButton(text, label = "复制内容") {
  const button = node("button", "icon-button");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon("copy"));
  button.addEventListener("click", () => copy(text));
  return button;
}

const results = {
  success: ["执行成功", ""], error: ["执行异常", "error"], timeout: ["执行超时", "warning"],
  unknown: ["结果待核对", "warning"], cancelled: ["未执行 · 已取消", "neutral"],
  not_started: ["未开始执行", "neutral"], pending: ["尚无结果", "warning"], recorded: ["已记录", "neutral"],
};

function resultBadge(result) {
  const [label, style] = results[result?.status || "pending"] || results.recorded;
  return statusBadge(label, style);
}

function statusBadge(label, style = "") {
  const badge = node("span", `status-pill ${style}`);
  badge.append(icon(style === "error" ? "close" : style === "warning" ? "clock" : style === "neutral" ? "info" : "check"),
    node("span", "", label));
  return badge;
}

function scrollBehavior() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

function codeCard(code, language = "Python", call = null) {
  const card = node("div", "code-card");
  const heading = node("div", "code-heading");
  heading.append(icon("code"), node("span", "", call ? `${language} · ${call.name}` : language));
  if (call) heading.append(resultBadge(call.result));
  heading.append(copyButton(code, "复制完整代码"));
  const pre = node("pre");
  pre.append(node("code", "", code));
  card.append(heading, pre);
  if (call) card.append(node("div", "tool-id", `调用 ID · ${call.id}`));
  return card;
}

function appendContent(container, content) {
  // 仅识别代码围栏；所有历史文字均通过 textContent 放入 DOM，不渲染 HTML。
  const pattern = /```([^\n`]*)\n([\s\S]*?)(?:```|$)/g;
  let offset = 0;
  for (const match of content.matchAll(pattern)) {
    const before = content.slice(offset, match.index).trim();
    if (before) container.append(node("p", "message-text", before));
    container.append(codeCard(match[2].trimEnd(), match[1].trim() || "代码"));
    offset = match.index + match[0].length;
  }
  const after = content.slice(offset).trim();
  if (after) container.append(node("p", "message-text", after));
}

function renderMessage(message) {
  if (message.role === "system") {
    const details = node("details", "message system");
    details.id = `msg-${message.id}`;
    details.dataset.detailKey = message.id;
    const summary = node("summary", "system-summary");
    summary.append(icon("shield"), node("span", "", "系统提示词"), node("span", "", "· 已保存"), forkButton(message), icon("chevron", "chevron"));
    details.append(summary, node("div", "system-content", message.content));
    return details;
  }
  const labels = { user: "你", assistant: "代码助手", tool: "工具结果" };
  const article = node("article", `message ${message.role}`);
  article.id = `msg-${message.id}`;
  const heading = node("header", "message-heading");
  const avatar = node("span", `avatar ${message.role}`);
  avatar.append(icon(message.role === "assistant" ? "logo" : message.role === "user" ? "user" : "terminal"));
  const timestamp = node("time", "", time(message.created_at));
  timestamp.dateTime = message.created_at;
  const copier = copyButton(message.content || JSON.stringify(message.tool_calls, null, 2), "复制消息");
  copier.classList.add("copy-message");
  heading.append(avatar, node("strong", "", labels[message.role] || message.role), timestamp, forkButton(message), copier);
  const body = node("div", "message-body");
  if (message.role === "tool") {
    const details = node("details", "tool-result");
    details.dataset.detailKey = message.id;
    details.open = ["error", "unknown", "timeout"].includes(message.tool_result?.status);
    const summary = node("summary");
    summary.append(icon("terminal"), node("span", "", "查看执行输出"), resultBadge(message.tool_result));
    details.append(summary);
    const result = message.tool_result;
    if (result && ("output" in result || "error" in result)) {
      if (result.output) details.append(node("pre", "", textValue(result.output)));
      if (result.error) details.append(node("pre", "result-error", textValue(result.error)));
      if (!result.output && !result.error) details.append(node("pre", "", "（工具未输出文本）"));
    } else {
      details.append(node("pre", "", message.content));
    }
    body.append(details);
  } else {
    appendContent(body, message.content);
    message.tool_calls.forEach((call) => {
      const code = typeof call.arguments.code === "string" ? call.arguments.code : JSON.stringify(call.arguments, null, 2);
      const record = node("details", "tool-call");
      record.dataset.detailKey = `call-${call.id}`;
      record.open = !call.result || ["error", "unknown", "timeout"].includes(call.result.status);
      const summary = node("summary");
      summary.append(icon("terminal"), node("span", "tool-call-name", call.name), resultBadge(call.result), icon("chevron", "chevron"));
      record.append(summary, codeCard(code, call.name === "run_code" ? "Python" : "参数", call));
      body.append(record);
    });
  }
  article.append(heading, body);
  return article;
}

function renderBranches(snapshot) {
  $("branch-count").textContent = snapshot.branches.length;
  $("branch-list").replaceChildren();
  snapshot.branches.forEach((branch, index) => {
    const button = node("button", "branch-button");
    button.type = "button";
    button.setAttribute("aria-pressed", String(branch.is_selected));
    button.title = branch.last_user_message || "尚未开始对话";
    const text = node("span");
    const name = node("span", "branch-name", branch.last_user_message || `分支 ${index + 1}`);
    if (branch.is_current) name.append(node("span", "branch-tag", "· 上次保存"));
    text.append(name, node("span", "branch-description", branch.last_assistant_message || "暂无助手回答"));
    button.append(node("span", "branch-radio"), text);
    button.addEventListener("click", () => {
      state.fork = null;
      if (writable() && !runtime().busy) {
        perform(async () => {
          await mutate(`/api/conversations/${encodeURIComponent(snapshot.conversation.id)}/branches/select`,
            { leaf_id: branch.leaf_id, expected_revision: snapshot.history_revision });
          await afterWrite(snapshot.conversation.id);
        });
      } else {
        selectConversation(snapshot.conversation.id, branch.leaf_id);
      }
    });
    $("branch-list").append(button);
  });
  $("branch-preview-note").hidden = false;
  $("branch-preview-note").textContent = writable() && !runtime().busy
    ? "选择后保存分支，下次打开沿用此位置。" : "当前仅预览，不改变执行中的分支。";
}

function taskBadge(task) {
  const live = runtime();
  if (live.execution_connected && live.busy && live.task_id === task.id) {
    const phases = { waiting_model: "正在请求模型", waiting_approval: "等待审批", executing_tool: "正在执行工具",
      compacting: "正在压缩历史", starting: "正在准备", syncing: "同步检查点中", finished: "保存结果中" };
    return statusBadge(phases[live.phase] || "正在执行", "warning");
  }
  const states = {
    completed: ["已完成", ""], cancelled: ["已取消", "neutral"], interrupted: ["已中断", "warning"],
    needs_review: ["结果待核对", "warning"], limit: ["达到轮数上限", "warning"],
  };
  const phases = { waiting_model: "记录：等待模型", waiting_approval: "记录：待审批", executing_tool: "记录：执行工具" };
  const [label, style] = states[task.status] || [phases[task.phase] || "已保存", "warning"];
  return statusBadge(label, style);
}

function renderTasks(snapshot) {
  const target = $("task-detail");
  target.replaceChildren();
  const task = snapshot.tasks.at(-1);
  if (!task) {
    target.append(node("p", "muted empty-detail", "此分支暂无任务检查点"));
    return;
  }
  target.append(node("p", "task-title", task.description), taskBadge(task));
  const attempts = node("div", "task-row");
  attempts.append(node("span", "", "模型请求尝试"), node("span", "", `${task.rounds} 次`));
  const updated = node("div", "task-row");
  updated.append(node("span", "", "最近保存"), node("span", "", time(task.updated_at)));
  target.append(attempts, updated);
  if (task.error) target.append(node("p", "task-error", task.error));
  const managed = runtime().busy && runtime().task_id === task.id && runtime().execution_connected;
  target.append(node("p", "tiny-note", managed ? "已连接后台任务，页面刷新不会重新提交。" : "已保存的任务检查点。"));
  if (task.can_recover && !managed) {
    const actions = node("div", "task-actions");
    const needsReview = task.phase === "executing_tool" || task.status === "needs_review";
    const kinds = needsReview ? [["review", "核对执行结果"]] : [["resume", "恢复任务"], ["cancel", "结束任务"]];
    kinds.forEach(([kind, label]) => {
      const button = node("button", "subtle-button", label);
      button.type = "button";
      button.dataset.taskAction = kind;
      button.addEventListener("click", () => openTaskDialog(kind, task));
      actions.append(button);
    });
    target.append(actions);
  }
}

function renderMetrics(metrics) {
  $("token-estimate").textContent = number.format(metrics.estimated_input_tokens);
  $("token-window").textContent = metrics.context_window === null ? "未配置" : number.format(metrics.context_window);
  $("token-output").textContent = metrics.max_output_tokens === null ? "未配置" : number.format(metrics.max_output_tokens);
  $("token-budget").textContent = metrics.input_budget === null ? "预算未配置" : `预算 ${number.format(metrics.input_budget)}`;
  const ratio = metrics.input_budget ? metrics.estimated_input_tokens / metrics.input_budget : null;
  $("token-percent").textContent = ratio === null ? "仅显示输入估算" : `${(ratio * 100).toFixed(1)}% 输入预算`;
  $("token-meter").max = metrics.input_budget || 1;
  $("token-meter").value = metrics.input_budget ? Math.min(metrics.estimated_input_tokens, metrics.input_budget) : 0;
  $("token-meter").classList.toggle("over", Boolean(metrics.over_budget));
  const source = metrics.source === "context_preview" ? "模型上下文预览估算" : "原始分支估算";
  const warning = metrics.over_budget ? "当前估算超出输入预算。" : "";
  $("token-note").textContent = metrics.warning || `${source}，包含工具定义。${warning}字符估算不等于 API 实测用量。`;
  $("token-usage").title = metrics.usage_note;
  const usage = metrics.usage;
  $("token-usage").textContent = usage
    ? `输入 ${usage.prompt_tokens === null ? "未知" : number.format(usage.prompt_tokens)} / 输出 ${usage.completion_tokens === null ? "未知" : number.format(usage.completion_tokens)}`
    : metrics.latest_request ? "本次未提供" : "暂无实测记录";
  if (metrics.latest_request) {
    $("token-note").textContent += ` 最近请求输入估算 ${number.format(metrics.latest_request.estimated_input_tokens)} token。`;
  }
  $("token-note").textContent += metrics.memory_name ? ` 包含长期记忆「${metrics.memory_name}」。` : " 未应用长期记忆。";
}

function renderCompactions(snapshot) {
  $("compaction-count").textContent = snapshot.compactions.length;
  const list = $("compaction-list");
  list.replaceChildren();
  if (!snapshot.compactions.length) {
    const empty = node("div", "no-compaction");
    empty.append(icon("layers"), node("p", "", "此分支暂无压缩记录"), node("small", "", "原始历史始终完整保留"));
    list.append(empty);
    return;
  }
  snapshot.compactions.forEach((record, index) => {
    const details = node("details", "compaction-record");
    details.dataset.detailKey = `compaction-${record.id}`;
    const summary = node("summary");
    summary.append(node("span", "", `累计摘要 ${snapshot.compactions.length - index}`), icon("chevron"));
    details.append(summary);
    if (record.selected_for_preview) details.append(node("span", "status-pill", "用于当前预览"));
    if (record.used_by_last_request) details.append(node("span", "status-pill", "最近请求使用"));
    details.append(node("p", "", record.summary));
    const jump = node("button", "boundary-button");
    jump.type = "button";
    jump.append(icon("branch"), node("span", "", "定位摘要覆盖的最后消息"));
    jump.addEventListener("click", () => {
      const target = document.getElementById(`msg-${record.through_node_id}`);
      if (!target) return;
      target.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1600);
    });
    details.append(jump, node("div", "compaction-time", time(record.created_at)));
    list.append(details);
  });
}

function renderApproval(approval) {
  const panel = $("approval-panel");
  panel.hidden = !approval;
  document.body.classList.toggle("awaiting-approval", Boolean(approval));
  if (!approval) {
    state.approvalId = null;
    panel.replaceChildren();
    return;
  }
  if (state.approvalId === approval.id) return;
  state.approvalId = approval.id;
  panel.replaceChildren();
  const heading = node("div", "approval-heading");
  heading.append(icon("shield"), node("strong", "", "等待你确认执行代码"), node("span", "", "仅批准本次调用"));
  const details = node("details", "approval-code");
  details.open = true;
  details.append(node("summary", "", "查看本次执行的完整 Python 代码"), codeCard(approval.code));
  const actions = node("div", "approval-actions");
  actions.append(node("span", "", "已有文件修改不会因取消而回滚。"));
  [["reject", "取消执行", "subtle-button"], ["approve", "批准执行", "primary-button"]].forEach(([decision, label, style]) => {
    const button = node("button", style, label);
    button.type = "button";
    button.dataset.approvalAction = decision;
    button.addEventListener("click", () => perform(async () => {
      await mutate(`/api/conversations/${encodeURIComponent(approval.conversation_id)}/tasks/${encodeURIComponent(approval.task_id)}/approval`, {
        approval_id: approval.id, tool_call_id: approval.tool_call_id, decision,
        expected_revision: approval.history_revision,
      });
      await afterWrite(approval.conversation_id);
      toast(decision === "approve" ? "本次审批已提交，正在执行。" : "取消决定已提交。");
    }));
    actions.append(button);
  });
  panel.append(heading, details, actions);
}

function openTaskDialog(kind, task) {
  state.dialog = { kind, taskId: task.id, conversationId: state.snapshot.conversation.id,
    revision: state.snapshot.history_revision };
  const texts = {
    resume: ["恢复原任务", "继续原 TaskRun，保留用户消息和已保存的工具结果。恢复前请确认旧程序已停止。", "确认恢复"],
    review: ["核对工具执行结果", "工具可能已产生部分修改。请先检查实际文件与进程，再记录结果；保存核对记录后可恢复或结束任务。", "保存核对记录"],
    cancel: ["结束原任务", "结束后不再继续原任务。此前已经产生的文件修改会保留，不会自动回滚。", "结束任务"],
  };
  const [title, description, action] = texts[kind];
  $("dialog-title").textContent = title;
  $("dialog-description").textContent = description;
  $("dialog-submit").textContent = action;
  $("dialog-submit").disabled = false;
  $("review-note-label").hidden = kind !== "review";
  $("review-note").value = "";
  $("confirm-stopped-label").hidden = kind === "cancel";
  $("confirm-stopped").checked = false;
  $("dialog-error").hidden = true;
  $("task-dialog").showModal();
}

async function submitTask() {
  if ($("send-button").disabled || !state.snapshot) return;
  const snapshot = state.snapshot;
  const text = $("task-input").value.trim();
  const parent = state.fork?.id || snapshot.selected_leaf_id;
  state.submitting = true;
  try {
    await perform(async () => {
      await mutate(`/api/conversations/${encodeURIComponent(snapshot.conversation.id)}/tasks`, {
        text, parent_id: parent, expected_revision: snapshot.history_revision,
      });
      $("task-input").value = "";
      state.drafts.delete(snapshot.conversation.id);
      state.fork = null;
      await afterWrite(snapshot.conversation.id);
      toast("任务已保存，后台正在处理。");
    });
  } finally {
    state.submitting = false;
    applyControls();
  }
}

function closeTaskDialog() {
  if (state.mutating) return;
  $("task-dialog").close();
  state.dialog = null;
}

function renderSnapshot(snapshot, preserveScroll) {
  const messages = $("messages");
  const scroll = messages.scrollTop;
  const nearEnd = messages.scrollHeight - messages.clientHeight - scroll < 90;
  const opened = new Map([...document.querySelectorAll("[data-detail-key]")].map((item) => [item.dataset.detailKey, item.open]));
  const previousId = state.snapshot?.conversation.id;
  if (previousId && previousId !== snapshot.conversation.id) state.drafts.set(previousId, $("task-input").value);
  if (previousId !== snapshot.conversation.id) {
    state.fork = null;
    $("task-input").value = state.drafts.get(snapshot.conversation.id) || "";
  }
  state.snapshot = snapshot;
  document.title = `${snapshot.conversation.title || "新对话"} · 代码检查助手`;
  $("conversation-title").textContent = snapshot.conversation.title || "新对话";
  $("conversation-title").title = snapshot.conversation.title;
  const toolCount = snapshot.messages.reduce((count, message) => count + message.tool_calls.length, 0);
  $("conversation-meta").textContent = `${snapshot.messages.length} 条消息 · ${toolCount} 次工具调用 · 更新于 ${time(snapshot.conversation.updated_at)}`;
  $("branch-ribbon").hidden = false;
  const preview = snapshot.selected_leaf_id !== snapshot.conversation.current_leaf_id;
  $("branch-ribbon-text").textContent = preview ? "正在预览其他分支 · 上次保存的位置保持不变" : "当前分支 · 新任务将接在末尾，原始历史完整保留";
  const fragment = document.createDocumentFragment();
  snapshot.messages.forEach((message) => fragment.append(renderMessage(message)));
  messages.replaceChildren(fragment);
  renderBranches(snapshot);
  renderTasks(snapshot);
  renderMetrics(snapshot.metrics);
  renderCompactions(snapshot);
  renderApproval(snapshot.pending_approval || null);
  document.querySelectorAll("[data-detail-key]").forEach((item) => {
    if (opened.has(item.dataset.detailKey)) item.open = opened.get(item.dataset.detailKey);
  });
  messages.scrollTop = !preserveScroll || nearEnd ? messages.scrollHeight : scroll;
  $("latest-button").disabled = false;
  renderConversations();
  applyControls();
}

function updateLocation(id, leafId, replace = false) {
  const url = new URL(location.href);
  url.searchParams.set("conversation", id);
  if (leafId) url.searchParams.set("branch", leafId);
  else url.searchParams.delete("branch");
  if (url.href !== location.href) history[replace ? "replaceState" : "pushState"]({}, "", url);
}

async function selectConversation(id, leafId = null, { replace = false, preserveScroll = false, fallback = true } = {}) {
  if (memoryView && !preserveScroll) {
    if (!await showChat()) return false;
  }
  const request = ++state.request;
  $("messages").setAttribute("aria-busy", "true");
  if (!preserveScroll) $("messages").classList.add("loading-view");
  let path = `/api/conversations/${encodeURIComponent(id)}`;
  if (leafId) path += `?${new URLSearchParams({ leaf_id: leafId })}`;
  try {
    const snapshot = await api(path);
    if (request !== state.request) return;
    if (snapshot.revision !== state.snapshot?.revision) renderSnapshot(snapshot, preserveScroll);
    updateLocation(id, leafId, replace);
    connection(true);
    renderNotices();
    if (!preserveScroll) closeSidebar();
    return true;
  } catch (error) {
    if (request !== state.request) return;
    if (error.code === "invalid_branch" && leafId && fallback) {
      toast("分支已更新，已返回上次保存的位置。");
      return selectConversation(id, null, { replace: true, preserveScroll, fallback: false });
    }
    renderNotices(error.message);
    toast(error.message);
    return false;
  } finally {
    if (request === state.request) {
      $("messages").setAttribute("aria-busy", "false");
      $("messages").classList.remove("loading-view");
    }
  }
}

function closeSidebar() {
  appearance.closeSidebar();
}

async function refresh(manual = false) {
  if (state.refreshing) return;
  const navigationAtStart = state.request;
  state.refreshing = true;
  $("refresh-button").classList.add("loading");
  try {
    const [info, list] = await Promise.all([api("/api/info"), api("/api/conversations")]);
    state.info = info;
    state.conversations = list.conversations;
    state.notices = list.notices;
    connection(true);
    renderNotices();
    renderConversations();
    applyControls();
    if (memoryView) {
      await memoryPage?.refresh();
      return;
    }
    // 轮询期间用户已选择其他对话时，不用旧选择覆盖新的导航。
    if (state.request !== navigationAtStart) return;
    const url = new URL(location.href);
    const selected = state.snapshot?.conversation.id || url.searchParams.get("conversation");
    const exists = state.conversations.some((item) => item.id === selected);
    if (exists) {
      const followingSaved = state.snapshot && state.snapshot.selected_leaf_id === state.snapshot.conversation.current_leaf_id;
      const leaf = followingSaved ? null : (state.snapshot?.selected_leaf_id || url.searchParams.get("branch"));
      const loaded = await selectConversation(selected, leaf, { replace: true, preserveScroll: Boolean(state.snapshot) });
      if (!loaded) return;
    } else if (state.conversations.length) {
      const loaded = await selectConversation(state.conversations[0].id, null, { replace: true });
      if (!loaded) return;
    } else if (state.snapshot) {
      // 原文件被其他入口移走后刷新页面，避免继续展示已不存在的会话。
      location.replace("/");
    }
    if (manual) toast("历史已刷新");
  } catch (error) {
    connection(false);
    renderNotices("无法读取本地服务，请确认 Web 入口仍在运行。");
    $("conversation-list").setAttribute("aria-busy", "false");
    if (manual) toast(error.message);
  } finally {
    state.refreshing = false;
    $("refresh-button").classList.remove("loading");
    applyControls();
  }
}

function bindWorkspaceEvents() {
  appearance.init();
  $("memory-button").addEventListener("click", () => showMemories());
  $("conversation-search").addEventListener("input", renderConversations);
  $("refresh-button").addEventListener("click", () => refresh(true));
  $("new-button").addEventListener("click", () => perform(async () => {
    if (memoryView && !await showChat()) return;
    const result = await mutate("/api/conversations", {});
    state.fork = null;
    await afterWrite(result.conversation_id);
    toast("新对话已创建，可以输入任务。");
  }));
  $("task-input").addEventListener("input", applyControls);
  $("task-input").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitTask();
    }
  });
  $("send-button").addEventListener("click", submitTask);
  $("cancel-fork").addEventListener("click", () => { state.fork = null; applyControls(); });
  $("active-task-button").addEventListener("click", () => {
    const active = runtime().conversation_id;
    if (active) selectConversation(active);
  });
  $("dialog-close").addEventListener("click", closeTaskDialog);
  $("dialog-cancel").addEventListener("click", closeTaskDialog);
  $("task-dialog").addEventListener("cancel", (event) => {
    if (state.mutating) event.preventDefault();
    else state.dialog = null;
  });
  $("task-dialog-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const operation = state.dialog;
    if (!operation || state.mutating) return;
    let message = null;
    if (operation.kind !== "cancel" && !$("confirm-stopped").checked) message = "请先确认旧程序及工具子进程已停止。";
    if (operation.kind === "review" && !$("review-note").value.trim()) message = "请记录实际核对结果。";
    if (message) {
      $("dialog-error").textContent = message;
      $("dialog-error").hidden = false;
      return;
    }
    $("dialog-submit").disabled = true;
    perform(async () => {
      const body = { expected_revision: operation.revision };
      if (operation.kind !== "cancel") body.confirmed_stopped = true;
      if (operation.kind === "review") body.note = $("review-note").value.trim();
      await mutate(`/api/conversations/${encodeURIComponent(operation.conversationId)}/tasks/${encodeURIComponent(operation.taskId)}/${operation.kind}`, body);
      $("task-dialog").close();
      state.dialog = null;
      await afterWrite(operation.conversationId);
      toast(operation.kind === "review" ? "核对记录已保存，可恢复或结束原任务。" : "操作已提交。");
    }, { dialog: true });
  });
  $("latest-button").addEventListener("click", () => $("messages").scrollTo({ top: $("messages").scrollHeight, behavior: scrollBehavior() }));
  $("menu-button").addEventListener("click", () => appearance.togglePanel("sidebar"));
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      appearance.focusSearch();
    }
  });
  window.addEventListener("popstate", async () => {
    const url = new URL(location.href);
    if (url.searchParams.get("view") === "memories") { await showMemories(false); return; }
    if (memoryView && !await showChat(false)) {
      const retained = new URL(location.href);
      retained.searchParams.set("view", "memories");
      history.replaceState({}, "", retained);
      return;
    }
    const id = url.searchParams.get("conversation");
    if (id) selectConversation(id, url.searchParams.get("branch"), { replace: true });
  });
  if (new URL(location.href).searchParams.get("view") === "memories") showMemories(false);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  async function poll() {
    if (!document.hidden) await refresh();
    setTimeout(poll, runtime().busy ? 1000 : 5000);
  }
  poll();
}

async function showMemories(push = true) {
  // 第一次打开才加载页面模块；聊天区域只是隐藏，原草稿和会话仍保留。
  try {
    if (!memoryPageLoading) memoryPageLoading = import("/assets/memory-page.js").then(({ createMemoryPage }) => {
      memoryPage = createMemoryPage({ api, mutate, toast, leave: showChat, onChange: () => refresh() });
      return memoryPage;
    }).catch((error) => { memoryPageLoading = null; throw error; });
    await memoryPageLoading;
    const opening = !memoryView;
    memoryView = true;
    appearance.closeDrawers();
    $("chat-workspace").hidden = true;
    $("memory-page").hidden = false;
    $("inspector-toggle").hidden = true;
    $("memory-button").setAttribute("aria-current", "page");
    document.title = "记忆管理 · 代码检查助手";
    $("workspace-page-label").textContent = "记忆管理";
    if (push && opening) {
      const url = new URL(location.href);
      url.searchParams.set("view", "memories");
      history.pushState({}, "", url);
    }
    await memoryPage.refresh();
  } catch (error) { toast(error.message); }
}

async function showChat(push = true) {
  // 先确认是否放弃记忆草稿，再显示原聊天区域并刷新后端状态。
  if (memoryView && memoryPage && !memoryPage.canLeave()) return false;
  memoryView = false;
  $("memory-page").hidden = true;
  $("chat-workspace").hidden = false;
  $("inspector-toggle").hidden = false;
  $("memory-button").removeAttribute("aria-current");
  document.title = `${state.snapshot?.conversation.title || "会话工作台"} · 代码检查助手`;
  $("workspace-page-label").textContent = "会话工作台";
  if (push) {
    const url = new URL(location.href);
    url.searchParams.delete("view");
    history.pushState({}, "", url);
  }
  await refresh();
  return true;
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindWorkspaceEvents, { once: true });
else bindWorkspaceEvents();
