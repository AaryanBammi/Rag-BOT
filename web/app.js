// Chat UI for the facts-only assistant (architecture.md §7). Plain DOM, no build step.
//
// User and model text is only ever set with textContent, never parsed as HTML (edge_case.md UI-01).
// Each thread tracks its own pending request, so an answer lands in the thread it belongs to even
// after the user switches away (UI-03), and only that thread's send button is disabled (D-11).
"use strict";

const STORAGE_CLIENT = "mf-faq-client-id";
const STORAGE_LAST_THREAD = "mf-faq-last-thread";
const SOURCE_MARKER = "\n\nSource:";

const state = {
  clientId: null,
  threads: [],             // [{thread_id, title, ...}] newest first, from GET /threads
  active: null,            // thread_id, or null for an unsaved "New chat"
  messages: new Map(),     // thread_id -> [{role, content, kind, source_url, source_date} | local items]
  pending: new Set(),      // thread_ids waiting for an answer
  drafts: new Map(),       // thread_id (or "") -> unsent input text
};

const $ = (id) => document.getElementById(id);

// --- storage (may be unavailable in private windows) -----------------------------------

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function save(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* the app still works, it just forgets on reload */ }
}

function getClientId() {
  let id = load(STORAGE_CLIENT);
  if (!id || !/^[A-Za-z0-9-]{8,64}$/.test(id)) {
    id = crypto.randomUUID ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
    save(STORAGE_CLIENT, id);
  }
  return id;
}

// --- API -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { "X-Client-Id": state.clientId, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : null;
    throw new ApiError(res.status, detail || "Something went wrong. Please try again.");
  }
  return data;
}

// --- threads ---------------------------------------------------------------------------

async function refreshThreads() {
  try {
    const data = await api("GET", "/threads");
    state.threads = data.threads;
    $("threads-error").hidden = true;
  } catch (e) {
    const note = $("threads-error");
    note.textContent = "Couldn't load your chats. ";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "link-button";
    retry.textContent = "Retry";
    retry.addEventListener("click", refreshThreads);
    note.append(retry);
    note.hidden = false;
  }
  renderSidebar();
}

async function openThread(threadId) {
  state.active = threadId;
  save(STORAGE_LAST_THREAD, threadId);
  closeDrawer();
  render();
  if (state.messages.has(threadId)) return;
  try {
    const data = await api("GET", `/threads/${threadId}/messages`);
    if (!state.messages.has(threadId)) state.messages.set(threadId, data.messages);
  } catch (e) {
    if (e.status === 404) return forgetThread(threadId);
    state.messages.set(threadId, [{ local: "error", text: e.message, retry: () => {
      state.messages.delete(threadId);
      openThread(threadId);
    } }]);
  }
  if (state.active === threadId) render();
}

function newChat() {
  state.active = null;
  save(STORAGE_LAST_THREAD, null);
  closeDrawer();
  render();
  $("input").focus();
}

async function deleteThread(threadId) {
  const thread = state.threads.find((t) => t.thread_id === threadId);
  if (!confirm(`Delete "${thread ? thread.title : "this chat"}"?`)) return;
  try {
    await api("DELETE", `/threads/${threadId}`);
  } catch (e) {
    if (e.status !== 404) return alert(e.message);
  }
  forgetThread(threadId);
}

function forgetThread(threadId) {
  state.threads = state.threads.filter((t) => t.thread_id !== threadId);
  state.messages.delete(threadId);
  state.pending.delete(threadId);
  state.drafts.delete(threadId);
  if (state.active === threadId) {
    // UI-05: move to the most recent remaining chat, or an empty "New chat".
    if (state.threads.length) openThread(state.threads[0].thread_id);
    else newChat();
  } else {
    renderSidebar();
  }
}

// --- sending ---------------------------------------------------------------------------

async function send(text) {
  text = text.trim();
  if (!text) return;
  let threadId = state.active;
  if (threadId && state.pending.has(threadId)) return;  // UI-04: ignored while this thread waits

  if (!threadId) {
    try {
      const thread = await api("POST", "/threads");
      threadId = thread.thread_id;
      state.threads.unshift(thread);
      state.messages.set(threadId, []);
      state.drafts.delete("");
      if (state.active === null) {
        state.active = threadId;
        save(STORAGE_LAST_THREAD, threadId);
      }
    } catch (e) {
      return showComposerError(e.message);
    }
  }

  const list = state.messages.get(threadId) || [];
  state.messages.set(threadId, list);
  const outgoing = { role: "user", content: text, local: "sending" };
  list.push(outgoing);
  state.pending.add(threadId);
  if (state.active === threadId) $("input").value = "";
  state.drafts.delete(threadId);
  render();

  try {
    const data = await api("POST", `/threads/${threadId}/messages`, { content: text });
    const current = state.messages.get(threadId);
    if (current) {
      // The server's copy of the user message may differ (PII messages are stored as a placeholder).
      current.splice(current.indexOf(outgoing), 1, data.user_message, data.reply);
    }
    refreshThreads();  // title and order change after a message
  } catch (e) {
    if (e.status === 404) {
      state.pending.delete(threadId);
      return forgetThread(threadId);
    }
    outgoing.local = "failed";
    const current = state.messages.get(threadId);
    if (current) {
      const retryable = e.status !== 422;
      current.splice(current.indexOf(outgoing) + 1, 0, {
        local: "error",
        text: e.message,
        retry: retryable ? () => {
          current.splice(current.indexOf(outgoing), 2);
          send(text);
        } : null,
      });
      if (!retryable && state.active === threadId) $("input").value = text;
    }
  } finally {
    state.pending.delete(threadId);
    if (state.messages.has(threadId)) render();
  }
}

function showComposerError(message) {
  const list = $("message-list");
  list.append(errorItem({ text: message }));
  scrollToEnd();
}

// --- rendering -------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeUrl(url) {
  // UI-02: only http(s) links are rendered as links.
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function renderSidebar() {
  const list = $("thread-list");
  list.replaceChildren();
  for (const t of state.threads) {
    const item = el("li", "thread" + (t.thread_id === state.active ? " active" : ""));
    const open = el("button", "thread-open", t.title || "New chat");
    open.type = "button";
    open.title = t.title || "New chat";
    open.addEventListener("click", () => openThread(t.thread_id));
    if (t.thread_id === state.active) open.setAttribute("aria-current", "true");
    if (state.pending.has(t.thread_id)) open.prepend(el("span", "pending-dot", ""));
    const del = el("button", "thread-delete", "×");
    del.type = "button";
    del.setAttribute("aria-label", `Delete chat: ${t.title || "New chat"}`);
    del.addEventListener("click", () => deleteThread(t.thread_id));
    item.append(open, del);
    list.append(item);
  }
}

function messageItem(m) {
  if (m.local === "error") return errorItem(m);
  if (m.role === "user") {
    const li = el("li", "msg user" + (m.local === "failed" ? " failed" : ""));
    li.append(el("div", "bubble", m.content));
    return li;
  }
  const li = el("li", "msg assistant" + (m.kind === "service_unavailable" ? " notice" : ""));
  const bubble = el("div", "bubble");
  const cut = m.content.indexOf(SOURCE_MARKER);
  bubble.append(el("p", "answer", cut === -1 ? m.content : m.content.slice(0, cut)));
  const href = m.source_url && safeUrl(m.source_url);
  if (href) {
    const source = el("p", "source");
    source.append("Source: ");
    const a = el("a", null, href);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";  // UI-10
    source.append(a);
    bubble.append(source);
  }
  if (m.source_date) bubble.append(el("p", "updated", `Last updated from sources: ${m.source_date}`));
  li.append(bubble);
  return li;
}

function errorItem(m) {
  const li = el("li", "msg error");
  const bubble = el("div", "bubble", m.text);
  if (m.retry) {
    const retry = el("button", "link-button", "Retry");
    retry.type = "button";
    retry.addEventListener("click", m.retry);
    bubble.append(" ", retry);
  }
  li.append(bubble);
  return li;
}

function pendingItem() {
  const li = el("li", "msg assistant pending");
  const bubble = el("div", "bubble");
  bubble.setAttribute("aria-label", "Looking this up");
  bubble.append(el("span", "typing"), el("span", "typing"), el("span", "typing"));
  li.append(bubble);
  return li;
}

function render() {
  renderSidebar();
  const thread = state.threads.find((t) => t.thread_id === state.active);
  $("chat-title").textContent = thread ? thread.title : "New chat";

  const messages = state.active ? state.messages.get(state.active) : [];
  const list = $("message-list");
  list.replaceChildren(...(messages || []).map(messageItem));
  const pending = state.active !== null && state.pending.has(state.active);
  if (pending) list.append(pendingItem());
  $("welcome").hidden = Boolean(messages === undefined || messages.length || pending);

  $("send").disabled = pending;  // typing stays allowed; Enter is ignored until the answer arrives
  for (const b of document.querySelectorAll(".example")) b.disabled = pending;

  const input = $("input");
  if (!pending) {
    const draft = state.drafts.get(state.active || "");
    if (draft !== undefined && input.value === "") input.value = draft;
  }
  autoGrow();
  scrollToEnd();
}

function scrollToEnd() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

function autoGrow() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

// --- mobile drawer ---------------------------------------------------------------------

function openDrawer() {
  document.body.classList.add("drawer-open");
  $("scrim").hidden = false;
  $("menu").setAttribute("aria-expanded", "true");
}

function closeDrawer() {
  document.body.classList.remove("drawer-open");
  $("scrim").hidden = true;
  $("menu").setAttribute("aria-expanded", "false");
}

// --- wiring ----------------------------------------------------------------------------

function rememberDraft() {
  state.drafts.set(state.active || "", $("input").value);
}

document.addEventListener("DOMContentLoaded", async () => {
  state.clientId = getClientId();

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    send($("input").value);
  });
  $("input").addEventListener("keydown", (e) => {
    // UI-09: Enter sends, Shift+Enter adds a new line.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send($("input").value);
    }
  });
  $("input").addEventListener("input", () => {
    autoGrow();
    rememberDraft();
  });
  for (const b of document.querySelectorAll(".example")) {
    b.addEventListener("click", () => send(b.textContent));
  }
  $("new-chat").addEventListener("click", () => {
    rememberDraft();
    $("input").value = "";
    newChat();
  });
  $("thread-list").addEventListener("click", (e) => {
    if (e.target.closest(".thread-open")) {
      rememberDraft();
      $("input").value = "";
    }
  }, true);
  $("menu").addEventListener("click", () => {
    if (document.body.classList.contains("drawer-open")) closeDrawer();
    else openDrawer();
  });
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  await refreshThreads();
  // UI-08: after a refresh, reopen the last thread if it still exists.
  const last = load(STORAGE_LAST_THREAD);
  if (last && state.threads.some((t) => t.thread_id === last)) openThread(last);
  else newChat();
});
