/* Edit AI — client logic. Vanilla JS, no dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);
const chat = $("chat");
let selected = null;

function addMsg(text, cls) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function fmt(t) {
  const m = Math.floor(t / 60), s = (t % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

/* ---------- plan timeline ---------- */
function drawPlan(state) {
  const tl = $("timeline");
  tl.innerHTML = "";
  $("plan-video").textContent = state.selected ? `— ${state.selected}` : "";
  $("plan-ops").innerHTML = "";
  $("plan-summary").textContent = "";

  const plan = state.plan;
  if (!state.selected) { $("plan-summary").textContent = "no video selected"; return; }
  if (!plan) { $("plan-summary").textContent = "no plan yet — ask the agent"; return; }

  const dur = Math.max(plan.result_duration || 0,
    plan.segments.length ? plan.segments[plan.segments.length - 1][1] : 1);
  let lastEnd = 0;
  for (const [s, e] of plan.segments) {
    if (s - lastEnd > 0.01) {                    // the cut gap before this segment
      const gap = document.createElement("div");
      gap.className = "seg cut-gap";
      gap.style.width = `${((s - lastEnd) / dur) * 100}%`;
      gap.title = `cut ${fmt(lastEnd)} → ${fmt(s)}`;
      gap.innerHTML = `<span class="lbl">cut</span>`;
      tl.appendChild(gap);
    }
    const seg = document.createElement("div");
    seg.className = "seg";
    seg.style.width = `${((e - s) / dur) * 100}%`;
    seg.title = `keep ${fmt(s)} → ${fmt(e)}`;
    tl.appendChild(seg);
    lastEnd = e;
  }
  // ops markers
  for (const op of plan.ops || []) {
    if (op.start == null) continue;
    const mark = document.createElement("div");
    mark.className = "op-mark";
    mark.style.left = `${(op.start / dur) * 100}%`;
    mark.title = `${op.op} @ ${fmt(op.start)}`;
    tl.appendChild(mark);
  }
  const kept = plan.segments.reduce((a, [s, e]) => a + (e - s), 0);
  $("plan-summary").textContent =
    ` → ${(plan.result_duration || kept).toFixed(1)}s final`;
  const opsUl = document.createElement("ul");
  for (const op of state.ops || []) {
    const li = document.createElement("li");
    li.textContent = JSON.stringify(op);
    opsUl.appendChild(li);
  }
  const box = $("plan-ops");
  box.innerHTML = "";
  box.appendChild(opsUl);
}

/* ---------- video lists ---------- */
function drawLists(state) {
  const vl = $("video-list");
  vl.innerHTML = "";
  for (const v of state.videos) {
    const li = document.createElement("li");
    li.textContent = v;
    if (state.transcripts.some((t) => t.startsWith(v + ".")))
      li.innerHTML += '<span class="tag">transcript ✓</span>';
    else li.innerHTML += '<span class="tag">no transcript</span>';
    if (v === state.selected) { li.classList.add("sel"); selected = v; }
    li.onclick = () => selectVideo(v);
    vl.appendChild(li);
  }
  const ol = $("outbox-list");
  ol.innerHTML = "";
  for (const name of state.outbox) {
    const li = document.createElement("li");
    li.innerHTML = `▶ ${name}`;
    li.onclick = () => play("/media/outbox/" + name, name);
    ol.appendChild(li);
  }
}

function play(url, label) {
  $("player").src = url;
  $("now-playing").textContent = label;
  $("player").play().catch(() => {});
}

/* ---------- server calls ---------- */
function updateBadge(state) {
  const badge = $("brain-badge");
  badge.textContent = "brain: " + (state.brain_on ? "ON" : "off");
  badge.className = "badge " + (state.brain_on ? "on" : "off");
}

async function refresh() {
  const r = await fetch("/api/state");
  const state = await r.json();
  updateBadge(state);
  drawLists(state);
  drawPlan(state);
  return state;
}

async function selectVideo(name) {
  const r = await fetch("/api/select", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video: name }),
  });
  const data = await r.json();
  addMsg(`(selected ${name})`, "sys");
  if (data.result && data.result.error) addMsg("⚠ " + data.result.error, "bot");
  applyState(data.state);
}

function applyState(state) {
  drawLists(state);
  drawPlan(state);
  if (state.selected) playInboxPreview(state.selected);
}

function playInboxPreview(name) {
  play("/media/inbox/" + encodeURIComponent(name) + ".mp4", name + " (source)");
}

async function sendChat(text) {
  addMsg(text, "you");
  const r0 = await fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const start = await r0.json();
  if (start.error) { addMsg("⚠ " + start.error, "bot"); return; }
  if (start.busy) { addMsg("(agent is already thinking — one thought at a time)", "sys"); return; }
  await watchThinking(text);
}

/* Poll the backend while the brain works. The turn survives page reloads:
   on load, if a turn is running, a bubble is re-created automatically. */
let pollTimer = null;

async function watchThinking(text) {
  const thinking = addMsg("thinking…", "bot");
  const t0 = Date.now();
  $("chat-input").disabled = true;
  $("btn-stop-brain").disabled = true;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let data;
    try {
      const r = await fetch("/api/chat-status");
      data = await r.json();
    } catch { return; /* transient network hiccup — keep polling */ }
    const secs = Math.round((Date.now() - t0) / 1000);
    if (data.running) {
      if (secs % 15 === 0 && secs > 0)
        thinking.textContent =
          `thinking locally… ${secs}s — the model runs on this PC. ` +
          `First turn after a cold start can take 1–3 min (Spark) — it is ` +
          `NOT frozen; watch the terminal for tool calls.`;
      if (data.state) updateBadge(data.state);
      return;
    }
    clearInterval(pollTimer);
    pollTimer = null;
    $("chat-input").disabled = false;
    $("btn-stop-brain").disabled = false;
    thinking.textContent = data.error
      ? ("⚠ " + data.error)
      : (data.reply || "(empty reply)");
    if (data.state) {
      updateBadge(data.state);
      drawLists(data.state);
      drawPlan(data.state);
    }
  }, 2000);
}

/* ---------- events ---------- */
$("chat-form").onsubmit = (e) => {
  e.preventDefault();
  const t = $("chat-input").value.trim();
  if (!t) return;
  $("chat-input").value = "";
  sendChat(t);
};

$("btn-stop-brain").onclick = async () => {
  $("btn-stop-brain").disabled = true;
  const r = await fetch("/api/stop-brain", { method: "POST" });
  const data = await r.json();
  $("btn-stop-brain").disabled = false;
  if (data.error) addMsg("⚠ " + data.error, "sys");
  else addMsg(data.stopped ? "(brain stopped — RAM freed)" : "(no brain running)",
              "sys");
  updateBadge(data.state);
  drawLists(data.state);
  drawPlan(data.state);
};

$("btn-add").onclick = () => $("file-input").click();
$("file-input").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  addMsg(`(uploading ${file.name}…)`, "sys");
  const r = await fetch("/api/add-video", { method: "POST", body: fd });
  const data = await r.json();
  if (data.error) addMsg("⚠ " + data.error, "bot");
  else addMsg(`(added ${data.added} to inbox)`, "sys");
  drawLists(data.state);
  e.target.value = "";
};

refresh().then((state) => {
  addMsg("Edit AI ready. Select a video, then tell it what to edit. " +
         "Nothing renders until you say render.\n" +
         "(This chat has its own session — the terminal agent (agent.py) " +
         "has a separate one. They share files, not conversation.)", "sys");
  // a turn may already be running (e.g. the page was reloaded mid-thought)
  fetch("/api/chat-status").then((r) => r.json()).then((s) => {
    if (s.running) watchThinking(s.text || "(previous question)");
  });
});
