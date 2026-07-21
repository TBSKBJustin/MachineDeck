"use strict";

const state = { csrf: null, socket: null, snapshot: null, setupRequired: false, reconnect: 1000, auditCursor: null, auditEvents: [], activeView: "dashboard" };
const $ = (id) => document.getElementById(id);

function bytes(value, precision = 1) {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(Math.abs(value)) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 2 ? precision : 0)} ${units[index]}`;
}

function percent(value) { return value === null || value === undefined ? "—" : `${Math.round(value)}%`; }
function setMeter(id, value) { $(id).style.width = `${Math.max(0, Math.min(100, value || 0))}%`; }
function uptime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const days = Math.floor(seconds / 86400); const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (state.csrf && !["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    let payload = {}; try { payload = await response.json(); } catch (_) {}
    const detail = payload.detail || payload;
    const message = detail.message || (Array.isArray(detail) ? detail.map((item) => item.msg).join("; ") : null) || `Request failed (${response.status})`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.status === 204 ? null : response.json();
}

function showAuth(setupRequired, message = "") {
  state.setupRequired = setupRequired;
  $("dashboard-view").classList.add("hidden"); $("audit-view").classList.add("hidden"); $("auth-view").classList.remove("hidden"); $("logout").classList.add("hidden"); $("main-nav").classList.add("hidden"); closeAuditDrawer();
  $("auth-eyebrow").textContent = setupRequired ? "FIRST-RUN SETUP" : "SECURE ACCESS";
  $("auth-title").textContent = setupRequired ? "Create your administrator" : "Welcome back";
  $("auth-copy").textContent = setupRequired ? "This one-time setup is available only from the local machine." : "Sign in to monitor and control this MachineDeck host.";
  $("auth-submit").textContent = setupRequired ? "Create administrator" : "Sign in";
  $("password").autocomplete = setupRequired ? "new-password" : "current-password";
  $("auth-error").textContent = message;
}

async function authenticate(event) {
  event.preventDefault(); $("auth-submit").disabled = true; $("auth-error").textContent = "";
  try {
    const endpoint = state.setupRequired ? "/api/v1/auth/setup" : "/api/v1/auth/login";
    const result = await api(endpoint, { method: "POST", body: JSON.stringify({ username: $("username").value, password: $("password").value }) });
    state.csrf = result.csrf_token; $("password").value = ""; await openDashboard();
  } catch (error) { $("auth-error").textContent = error.message; }
  finally { $("auth-submit").disabled = false; }
}

async function bootstrap() {
  try {
    const status = await api("/api/v1/auth/status");
    if (!status.authenticated) return showAuth(status.setup_required);
    const session = await api("/api/v1/auth/session"); state.csrf = session.csrf_token; await openDashboard();
  } catch (error) { showAuth(false, error.message); }
}

async function openDashboard() {
  $("auth-view").classList.add("hidden"); $("logout").classList.remove("hidden"); $("main-nav").classList.remove("hidden"); switchView("dashboard");
  try { render(await api("/api/v1/dashboard")); } catch (error) { if (error.status === 401) return showAuth(false); }
  connectSocket();
}

function connectSocket() {
  if (state.socket) state.socket.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/v1/dashboard`); state.socket = socket;
  socket.onopen = () => { state.reconnect = 1000; };
  socket.onmessage = (event) => { const message = JSON.parse(event.data); if (message.type === "dashboard_snapshot") render(message.data); };
  socket.onclose = async (event) => {
    if (state.socket !== socket) return;
    state.socket = null; updateFreshness();
    if (event.code === 4401) { state.csrf = null; return showAuth(false, "Your session expired. Please sign in again."); }
    setTimeout(connectSocket, state.reconnect); state.reconnect = Math.min(15000, state.reconnect * 2);
  };
}

function render(snapshot) {
  state.snapshot = snapshot; const host = snapshot.host; const apps = snapshot.applications;
  $("cpu-value").textContent = percent(host.cpu_percent); setMeter("cpu-meter", host.cpu_percent);
  $("cpu-detail").textContent = `${host.cpu_per_core_percent.length || 0} logical cores`;
  $("memory-value").textContent = percent(host.memory_percent); setMeter("memory-meter", host.memory_percent);
  $("memory-detail").textContent = `${bytes(host.memory_used_bytes)} / ${bytes(host.memory_total_bytes)}`;
  $("uptime-value").textContent = uptime(host.uptime_seconds);
  $("load-detail").textContent = host.load_average ? `Load ${host.load_average.map((v) => v.toFixed(2)).join(" · ")}` : "Load average unavailable";
  $("apps-total").textContent = apps.total; $("apps-detail").textContent = `${apps.running} running · ${apps.failed} failed`;
  $("updated-at").textContent = `Collected ${new Date(snapshot.collected_at).toLocaleTimeString()} in ${Math.round(snapshot.collection_duration_ms)} ms`;
  renderCollectors(snapshot.collectors); renderGpus(snapshot.gpus, snapshot.collectors.nvml); renderDisks(host.disks); renderApplications(apps); updateFreshness();
}

function renderCollectors(collectors) {
  const root = $("collector-summary"); root.replaceChildren();
  Object.entries(collectors).forEach(([name, collector]) => { const pill = document.createElement("span"); pill.className = `collector-pill${collector.available ? "" : " warning"}`; pill.textContent = `${name} · ${collector.available ? "ready" : "degraded"}`; pill.title = collector.message || ""; root.appendChild(pill); });
}

function renderGpus(gpus, collector) {
  const root = $("gpu-grid"); root.replaceChildren(); $("gpu-count").textContent = `${gpus.length} detected`;
  if (!gpus.length) { const empty = document.createElement("div"); empty.className = "empty-card"; empty.textContent = collector && collector.message ? `GPU metrics unavailable · ${collector.error_code}` : "No NVIDIA GPUs detected"; root.appendChild(empty); return; }
  gpus.forEach((gpu) => {
    const card = document.createElement("article"); card.className = "gpu-card panel";
    const used = gpu.memory_used_bytes; const total = gpu.memory_total_bytes; const memoryPercent = total ? used / total * 100 : 0;
    card.innerHTML = `<div class="card-head"><div><h4></h4><p></p></div><span class="badge"></span></div><div class="gpu-stats"><div class="gpu-stat"><span>UTILIZATION</span><strong></strong></div><div class="gpu-stat"><span>TEMPERATURE</span><strong></strong></div><div class="gpu-stat"><span>POWER</span><strong></strong></div></div><div class="gpu-memory"><div class="meter"><span></span></div><small></small></div>`;
    card.querySelector("h4").textContent = `GPU ${gpu.index} — ${gpu.name}`; card.querySelector("p").textContent = `${gpu.process_count} active GPU process${gpu.process_count === 1 ? "" : "es"}`;
    const badge = card.querySelector(".badge"); badge.textContent = gpu.available ? "AVAILABLE" : "DEGRADED"; if (!gpu.available) badge.classList.add("error");
    const values = card.querySelectorAll(".gpu-stat strong"); values[0].textContent = percent(gpu.utilization_percent); values[1].textContent = gpu.temperature_celsius === null ? "—" : `${Math.round(gpu.temperature_celsius)}°C`; values[2].textContent = gpu.power_usage_watts === null ? "—" : `${Math.round(gpu.power_usage_watts)} W`;
    card.querySelector(".gpu-memory span").style.width = `${memoryPercent}%`; card.querySelector(".gpu-memory small").textContent = `${bytes(used)} / ${bytes(total)} VRAM`;
    root.appendChild(card);
  });
}

function renderDisks(disks) {
  const root = $("disk-grid"); root.replaceChildren();
  disks.forEach((disk) => { const card = document.createElement("article"); card.className = "disk-card panel"; if (!disk.available) { card.innerHTML = `<div class="card-head"><div><h4></h4><p>Unavailable</p></div><span class="badge error">OFFLINE</span></div><div class="disk-value">Path unavailable</div>`; card.querySelector("h4").textContent = disk.mountpoint; } else { card.innerHTML = `<div class="card-head"><div><h4></h4><p></p></div><span class="badge">MOUNTED</span></div><div class="disk-value"></div><div class="meter"><span></span></div>`; card.querySelector("h4").textContent = disk.mountpoint; card.querySelector("p").textContent = disk.filesystem || "filesystem"; card.querySelector(".disk-value").textContent = `${bytes(disk.used_bytes)} used · ${bytes(disk.free_bytes)} free`; card.querySelector(".meter span").style.width = `${disk.percent || 0}%`; } root.appendChild(card); });
}

function renderApplications(apps) {
  const root = $("application-states"); root.replaceChildren();
  [["Running",apps.running],["Stopped",apps.stopped],["Starting",apps.starting],["Stopping",apps.stopping],["Queued",apps.queued],["Unhealthy",apps.unhealthy],["Failed",apps.failed],["Unknown",apps.unknown],["Disabled",apps.disabled]].forEach(([label,value]) => { const card = document.createElement("div"); card.className = "state-card"; const name = document.createElement("span"); name.textContent = label; const count = document.createElement("strong"); count.textContent = value; card.append(name,count); root.appendChild(card); });
}

function switchView(view) {
  state.activeView = view;
  $("dashboard-view").classList.toggle("hidden", view !== "dashboard");
  $("audit-view").classList.toggle("hidden", view !== "audit");
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  if (view === "audit" && !state.auditEvents.length) loadAudit(true);
}

function auditParameters(reset) {
  const parameters = new URLSearchParams(); parameters.set("limit", "25");
  const fields = [["keyword","audit-keyword"],["category","audit-category"],["result","audit-result"],["action","audit-action"],["application_id","audit-application"],["actor","audit-actor"],["execution_id","audit-execution-id"]];
  fields.forEach(([name,id]) => { const value = $(id).value.trim(); if (value) parameters.set(name, value); });
  [["start","audit-start"],["end","audit-end"]].forEach(([name,id]) => { const value = $(id).value; if (value) parameters.set(name, new Date(value).toISOString()); });
  if (!reset && state.auditCursor) parameters.set("cursor", state.auditCursor);
  return parameters;
}

async function loadAudit(reset = false) {
  $("audit-error").textContent = ""; $("audit-more").disabled = true;
  try {
    const page = await api(`/api/v1/audit-events?${auditParameters(reset)}`);
    state.auditEvents = reset ? page.events : state.auditEvents.concat(page.events);
    state.auditCursor = page.next_cursor; renderAuditRows();
    $("audit-more").classList.toggle("hidden", !page.has_more);
  } catch (error) { $("audit-error").textContent = error.message; }
  finally { $("audit-more").disabled = false; }
}

function auditPreview(event) {
  if (event.execution && event.execution.error_code) return event.execution.error_code;
  if (event.details && event.details.error_code) return event.details.error_code;
  if (event.details && event.details.reason) return event.details.reason;
  const keys = Object.keys(event.details || {}); return keys.length ? keys.slice(0, 3).join(", ") : "—";
}

function renderAuditRows() {
  const root = $("audit-rows"); root.replaceChildren();
  state.auditEvents.forEach((event) => {
    const row = document.createElement("tr"); row.tabIndex = 0;
    const values = [
      { text: new Date(event.timestamp).toLocaleString(), className: "audit-time" },
      { badge: event.result || "unknown" },
      { text: event.category || "other", className: "audit-category" },
      { text: event.action || event.raw_action || "UNKNOWN", className: "audit-action" },
      { target: event.target || { id: "unknown", type: "unknown" } },
      { text: event.actor ? event.actor.id : "unknown" },
      { text: auditPreview(event), className: "audit-detail-preview" },
    ];
    values.forEach((value) => {
      const cell = document.createElement("td");
      if (value.badge) { const badge = document.createElement("span"); const kind = ["success","failure"].includes(value.badge) ? value.badge : "unknown"; badge.className = `result-badge ${kind}`; badge.textContent = value.badge; cell.appendChild(badge); }
      else if (value.target) { cell.className = "audit-target"; const name = document.createElement("strong"); name.textContent = value.target.name || value.target.id; const type = document.createElement("small"); type.textContent = `${value.target.type} · ${value.target.id}`; cell.append(name,type); }
      else { cell.textContent = value.text; if (value.className) cell.className = value.className; }
      row.appendChild(cell);
    });
    row.addEventListener("click", () => openAuditDetail(event.id)); row.addEventListener("keydown", (keyboard) => { if (keyboard.key === "Enter" || keyboard.key === " ") openAuditDetail(event.id); }); root.appendChild(row);
  });
  $("audit-empty").classList.toggle("hidden", state.auditEvents.length !== 0); $("audit-count").textContent = `${state.auditEvents.length} event${state.auditEvents.length === 1 ? "" : "s"}`;
}

function addDetail(meta, label, value) {
  const term = document.createElement("dt"); term.textContent = label; const description = document.createElement("dd"); description.textContent = value === null || value === undefined || value === "" ? "—" : String(value); meta.append(term, description);
}

async function openAuditDetail(eventId) {
  try {
    const event = await api(`/api/v1/audit-events/${encodeURIComponent(eventId)}`); const meta = $("audit-detail-meta"); meta.replaceChildren();
    addDetail(meta, "Timestamp", new Date(event.timestamp).toLocaleString()); addDetail(meta, "Result", event.result); addDetail(meta, "Category", event.category); addDetail(meta, "Action", event.action); addDetail(meta, "Raw action", event.raw_action); addDetail(meta, "Target", `${event.target.type} · ${event.target.name || event.target.id}`); addDetail(meta, "Target ID", event.target.id); addDetail(meta, "Actor", `${event.actor.type} · ${event.actor.id}`); addDetail(meta, "Event ID", event.id); addDetail(meta, "Request", event.request ? `${event.request.method || ""} ${event.request.path || ""}`.trim() : null);
    $("audit-detail-json").textContent = JSON.stringify(event.details || {}, null, 2);
    const executionSection = $("audit-execution-section"); if (event.execution) { executionSection.classList.remove("hidden"); $("audit-execution").textContent = `${event.execution.action} · ${event.execution.status}\nID ${event.execution.id}${event.execution.error_code ? `\nError ${event.execution.error_code}` : ""}`; } else { executionSection.classList.add("hidden"); }
    $("audit-drawer").classList.remove("hidden"); $("audit-drawer-backdrop").classList.remove("hidden");
  } catch (error) { $("audit-error").textContent = error.message; }
}

function closeAuditDrawer() { $("audit-drawer").classList.add("hidden"); $("audit-drawer-backdrop").classList.add("hidden"); }

function clearAuditFilters() {
  ["audit-keyword","audit-category","audit-result","audit-action","audit-application","audit-actor","audit-execution-id","audit-start","audit-end"].forEach((id) => { $(id).value = ""; }); state.auditCursor = null; loadAudit(true);
}

function updateFreshness() {
  const element = $("freshness"); let value = "OFFLINE";
  if (state.snapshot) { const age = (Date.now() - new Date(state.snapshot.collected_at).getTime()) / 1000; value = age <= 5 ? "LIVE" : age <= 15 ? "STALE" : "OFFLINE"; }
  element.className = `freshness ${value.toLowerCase()}`; element.querySelector("span").textContent = value;
}

async function logout() {
  try { await api("/api/v1/auth/logout", { method: "POST" }); } catch (_) {}
  state.csrf = null; state.snapshot = null; if (state.socket) { const socket = state.socket; state.socket = null; socket.close(); }
  state.auditEvents = []; state.auditCursor = null; showAuth(false);
}

$("auth-form").addEventListener("submit", authenticate); $("logout").addEventListener("click", logout); document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view))); $("audit-filters").addEventListener("submit", (event) => { event.preventDefault(); state.auditCursor = null; loadAudit(true); }); $("audit-clear").addEventListener("click", clearAuditFilters); $("audit-more").addEventListener("click", () => loadAudit(false)); $("audit-drawer-close").addEventListener("click", closeAuditDrawer); $("audit-drawer-backdrop").addEventListener("click", closeAuditDrawer); document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeAuditDrawer(); }); setInterval(updateFreshness, 1000); bootstrap();
