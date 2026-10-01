"use strict";

const $ = (sel) => document.querySelector(sel);
const transcript = $("#transcript");
const scroller = $("#scroll");
const input = $("#input");
const sendBtn = $("#send");
const stopBtn = $("#stop");
const statusEl = $("#status");
const costEl = $("#cost");
const banner = $("#banner");
const emptyState = $("#empty");

let ws = null;
let busy = false;
let settings = { max_order_total: 100, currency: "USD", notes: "" };
let currentSteps = null; // the <div class="steps"> that new actions are appended to
const cards = new Map(); // interaction id -> card element
const baseTitle = document.title;
let viewUrl = null; // the agent's browser (noVNC) when it runs on a server, else null

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe Markdown subset: paragraphs, lists, headings, **bold**, *italic*, `code`, links.
function renderMarkdown(src) {
  const inline = (text) => {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    html = html.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      (_, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    return html;
  };
  const out = [];
  let list = null; // "ul" | "ol"
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of src.split("\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flushPara(); closeList(); continue; }
    if ((m = line.match(/^\s*#{1,6}\s+(.*)$/))) { flushPara(); closeList(); out.push(`<h3>${inline(m[1])}</h3>`); continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      const kind = /^\s*\d/.test(line) ? "ol" : "ul";
      flushPara();
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara();
  closeList();
  return out.join("");
}

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
}

function add(node, { steps = false } = {}) {
  const stick = nearBottom();
  emptyState.hidden = true;
  transcript.append(node);
  if (!steps) currentSteps = null;
  if (stick) scroller.scrollTop = scroller.scrollHeight;
  return node;
}

function money(value, currency) {
  const n = Number(value);
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return true;
  }
  showBanner("Not connected to the agent - is it still running?");
  return false;
}

function showBanner(text) {
  banner.textContent = text;
  banner.hidden = !text;
}

function setStatus(state) {
  busy = state === "working" || state === "waiting";
  statusEl.dataset.state = state;
  statusEl.textContent = { idle: "Ready", working: "Working…", waiting: "Needs you", offline: "Offline" }[state] || state;
  sendBtn.hidden = busy;
  stopBtn.hidden = !busy;
  document.title = state === "waiting" ? `(!) ${baseTitle}` : baseTitle;
}

function notify(text) {
  if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  new Notification("Shopping Agent", { body: text });
}

// ------------------------------------------------------------------ rendering events

function renderEvent(ev, live) {
  switch (ev.type) {
    case "user":
      add(el("div", { class: "msg user", text: ev.text }));
      break;
    case "assistant": {
      const node = el("div", { class: "msg assistant" });
      node.innerHTML = renderMarkdown(ev.text);
      add(node);
      break;
    }
    case "progress":
      add(el("div", { class: "progress", text: ev.text }));
      break;
    case "action": {
      if (!currentSteps) currentSteps = add(el("div", { class: "steps" }));
      const step = el("div", { class: "step", "data-state": "running", "data-id": ev.id },
        el("span", { class: "icon" }), el("span", { text: ev.text }));
      const stick = nearBottom();
      currentSteps.append(step);
      if (stick) scroller.scrollTop = scroller.scrollHeight;
      break;
    }
    case "action_done": {
      const step = transcript.querySelector(`.step[data-id="${CSS.escape(ev.id)}"]`);
      if (!step) break;
      step.dataset.state = ev.ok ? "ok" : "fail";
      if (!ev.ok && ev.error) step.append(el("span", { class: "err", text: `- ${ev.error}` }));
      break;
    }
    case "notice":
      add(el("div", { class: "notice", text: ev.text }));
      break;
    case "error":
      add(el("div", { class: "error", text: ev.text }));
      break;
    case "question":
      add(questionCard(ev));
      if (live) notify(`Question: ${ev.question}`);
      break;
    case "handover":
      add(handoverCard(ev));
      if (live) notify(viewUrl ? "The agent needs you: open the shop browser." : "The agent needs you in the browser window.");
      break;
    case "approval":
      add(approvalCard(ev));
      if (live) notify(`Approve purchase: ${money(ev.total, ev.currency)} at ${ev.store}?`);
      break;
    case "decision": {
      const card = cards.get(ev.id);
      if (card) finishCard(card, ev.approved ? "Approved - placing the order" : "Declined", ev.approved ? "ok" : "no");
      break;
    }
    case "resolved": {
      const card = cards.get(ev.id);
      if (card && !card.classList.contains("done")) finishCard(card, card.dataset.doneText || "Closed", "");
      break;
    }
    case "settings":
      settings = { max_order_total: ev.max_order_total, currency: ev.currency, notes: ev.notes };
      if (live) add(el("div", { class: "notice", text: "Settings saved." }));
      break;
    case "usage":
      costEl.textContent = ev.cost_usd == null ? "" : `≈ $${ev.cost_usd.toFixed(2)}`;
      break;
    case "status":
      setStatus(ev.state);
      break;
    case "reset":
      clearTranscript();
      break;
  }
}

function finishCard(card, text, kind) {
  card.classList.add("done");
  card.querySelectorAll("input, button").forEach((n) => { n.disabled = true; });
  const result = el("div", { class: `result ${kind}`, text });
  const slot = card.querySelector(".actions") || card.querySelector(".result");
  if (slot) slot.replaceWith(result);
}

function questionCard(ev) {
  const answer = el("input", { type: "text", placeholder: "Your answer" });
  const submit = () => {
    const text = answer.value.trim();
    if (!text) { answer.focus(); return; }
    card.dataset.doneText = `You answered: ${text}`;
    send({ type: "answer", id: ev.id, text });
  };
  answer.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  const card = el("div", { class: "card" },
    el("h3", { text: "❓ Question" }),
    el("div", { text: ev.question }),
    el("div", { class: "row actions" }, answer, el("button", { class: "primary", type: "button", onclick: submit }, "Answer")));
  cards.set(ev.id, card);
  setTimeout(() => answer.focus(), 50);
  return card;
}

function viewButton() {
  return el("a", { class: "button primary", href: viewUrl, target: "_blank", rel: "noopener" }, "Open the shop browser ↗");
}

function handoverCard(ev) {
  const note = el("input", { type: "text", placeholder: "Optional note for the agent" });
  const card = el("div", { class: "card" },
    el("h3", { text: viewUrl ? "🙋 Your turn: open the shop browser" : "🙋 Your turn in the browser window" }),
    el("div", { text: ev.reason }),
    viewUrl ? el("div", { class: "row" }, viewButton()) : null,
    viewUrl ? el("div", { class: "meta", text: "It opens in a new page (enter the browser password if asked). When you're finished there, come back here and click \"I'm done\"." }) : null,
    el("div", { class: "row actions" }, note,
      el("button", {
        class: "primary", type: "button",
        onclick: () => { card.dataset.doneText = "Done - handed back to the agent"; send({ type: "handover_done", id: ev.id, text: note.value.trim() }); },
      }, "I'm done")));
  cards.set(ev.id, card);
  return card;
}

function approvalCard(ev) {
  const comment = el("input", { type: "text", placeholder: "Optional note (e.g. why you declined)" });
  const decide = (approved) => send({ type: "approval", id: ev.id, approved, text: comment.value.trim() });
  const warnings = [];
  if (ev.currency_mismatch) {
    warnings.push(`This order is in ${ev.currency}, but your limit is set in ${ev.limit_currency}. Check the amount carefully.`);
  }
  let shot = null;
  if (ev.screenshot) {
    const src = `data:image/jpeg;base64,${ev.screenshot}`;
    shot = el("img", {
      src, alt: "Screenshot of the order page",
      onclick: () => { $("#lightbox-img").src = src; $("#lightbox").showModal(); },
    });
  }
  const card = el("div", { class: "card approval" },
    el("h3", { text: `🧾 Approve this purchase at ${ev.store}?` }),
    el("div", { class: "total", text: money(ev.total, ev.currency) }),
    el("pre", { class: "items", text: ev.items }),
    ev.details ? el("div", { text: ev.details }) : null,
    ...warnings.map((w) => el("div", { class: "warn", text: `⚠️ ${w}` })),
    el("div", { class: "meta", text: `Page: ${ev.page_title || ""} - ${ev.url}` }),
    shot,
    el("div", { class: "meta", text: viewUrl
      ? "Check the screenshot (click to enlarge) before approving. To see the live page, open the shop browser."
      : "Check the screenshot (click to enlarge) and the browser window before approving." }),
    viewUrl ? el("div", { class: "row" }, viewButton()) : null,
    el("div", { class: "row actions" }, comment,
      el("button", { class: "ok", type: "button", onclick: () => decide(true) }, "Approve & buy"),
      el("button", { class: "danger", type: "button", onclick: () => decide(false) }, "Decline")));
  cards.set(ev.id, card);
  return card;
}

function clearTranscript() {
  cards.clear();
  currentSteps = null;
  transcript.replaceChildren(emptyState);
  emptyState.hidden = false;
  costEl.textContent = "";
}

// ------------------------------------------------------------------ connection

function setViewUrl(port) {
  viewUrl = port ? `http://${location.hostname}:${port}/vnc.html?autoconnect=1&resize=scale` : null;
  const link = $("#view-link");
  link.hidden = !viewUrl;
  if (viewUrl) link.href = viewUrl;
  $("#empty-where").textContent = viewUrl
    ? "I'll use my own browser on the server (watch it any time with \"Shop browser\" at the top)"
    : "I'll use the browser window that opened next to this one";
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => showBanner("");
  ws.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "hello") {
      setViewUrl(ev.browser_view_port);
      clearTranscript();
      settings = ev.settings;
      for (const past of ev.events) renderEvent(past, false);
      if (ev.usage) renderEvent(ev.usage, false);
      setStatus(ev.status);
      scroller.scrollTop = scroller.scrollHeight;
      return;
    }
    renderEvent(ev, true);
  };
  ws.onclose = (e) => {
    setStatus("offline");
    if (e.code === 4403) {
      showBanner("Not authorized. Open the link printed in the terminal where you started the agent.");
      return;
    }
    showBanner("Lost connection to the agent. Reconnecting…");
    setTimeout(connect, 2000);
  };
}

// ------------------------------------------------------------------ composer & settings

function autoGrow() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 192)}px`;
}

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  if (send({ type: "message", text })) {
    input.value = "";
    autoGrow();
  }
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
input.addEventListener("input", autoGrow);
stopBtn.addEventListener("click", () => send({ type: "stop" }));
$("#reset-btn").addEventListener("click", () => {
  if (busy && !confirm("Stop the current task and start a new chat?")) return;
  send({ type: "reset" });
});
for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => { input.value = button.textContent; autoGrow(); input.focus(); });
}

$("#settings-btn").addEventListener("click", () => {
  $("#set-limit").value = settings.max_order_total;
  $("#set-currency").value = settings.currency;
  $("#set-notes").value = settings.notes;
  $("#settings").showModal();
});
$("#settings").addEventListener("close", () => {
  if ($("#settings").returnValue !== "save") return;
  send({
    type: "settings",
    max_order_total: Number($("#set-limit").value),
    currency: $("#set-currency").value.trim().toUpperCase() || "USD",
    notes: $("#set-notes").value,
  });
});

connect();
