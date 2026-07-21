"use strict";

const state = {
  csrf: null,
  socket: null,
  snapshot: null,
  setupRequired: false,
  reconnect: 1000,
  auditCursor: null,
  auditEvents: [],
  activeView: "dashboard",
  applications: [],
  applicationRuntime: new Map(),
  applicationBusy: new Set(),
  editingApplicationId: null,
  logSocket: null,
  applicationRefresh: null,
};
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
    const message = (typeof detail === "string" ? detail : detail.message) || (Array.isArray(detail) ? detail.map((item) => item.msg).join("; ") : null) || `Request failed (${response.status})`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.status === 204 ? null : response.json();
}

function showAuth(setupRequired, message = "") {
  state.setupRequired = setupRequired;
  $("dashboard-view").classList.add("hidden"); $("audit-view").classList.add("hidden"); $("auth-view").classList.remove("hidden"); $("logout").classList.add("hidden"); $("main-nav").classList.add("hidden"); closeAuditDrawer(); closeApplicationForm(); closeLogs();
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
  try { const [snapshot] = await Promise.all([api("/api/v1/dashboard"), loadApplications()]); render(snapshot); } catch (error) { if (error.status === 401) return showAuth(false); }
  connectSocket();
  clearInterval(state.applicationRefresh); state.applicationRefresh = setInterval(() => { if (state.activeView === "dashboard" && !state.editingApplicationId) loadApplications({ quiet: true }); }, 15000);
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

function applicationStatusClass(status) {
  const normalized = String(status || "UNKNOWN").toLowerCase();
  if (["running", "stopped", "failed", "unhealthy", "starting", "stopping", "disabled"].includes(normalized)) return normalized;
  return "unknown";
}

function applicationCanStop(status) { return ["RUNNING", "UNHEALTHY", "STARTING", "FAILED", "UNKNOWN"].includes(status); }
function applicationCanStart(status) { return ["STOPPED", "FAILED", "UNKNOWN"].includes(status); }

async function loadApplications({ quiet = false } = {}) {
  if (!quiet) $("application-error").textContent = "";
  try {
    const applications = await api("/api/v1/applications");
    state.applications = applications;
    const runtimePairs = await Promise.all(applications.map(async (application) => {
      const previous = state.applicationRuntime.get(application.id) || {};
      let runtime = { ...previous, status: application.status };
      try { runtime = { ...runtime, ...(await api(`/api/v1/applications/${encodeURIComponent(application.id)}/status`)) }; } catch (error) { runtime.statusError = error.message; }
      if (application.manifest.ports.length) {
        try {
          let endpoints = await api(`/api/v1/applications/${encodeURIComponent(application.id)}/endpoints?scope=lan`);
          if (!endpoints.endpoints.some((endpoint) => endpoint.url)) endpoints = await api(`/api/v1/applications/${encodeURIComponent(application.id)}/endpoints?scope=local`);
          runtime.endpoints = endpoints;
        } catch (error) { runtime.endpointError = error.message; }
      } else runtime.endpoints = { primary: null, endpoints: [] };
      return [application.id, runtime];
    }));
    state.applicationRuntime = new Map(runtimePairs); renderApplicationList();
  } catch (error) {
    if (error.status === 401) return showAuth(false, "Your session expired. Please sign in again.");
    if (!quiet) $("application-error").textContent = error.message;
  }
}

function createActionButton(label, action, applicationId, className = "secondary-button") {
  const button = document.createElement("button"); button.type = "button"; button.className = className; button.textContent = label; button.dataset.applicationAction = action; button.dataset.applicationId = applicationId; return button;
}

function renderApplicationList() {
  const root = $("application-list"); root.replaceChildren();
  if (!state.applications.length) {
    const empty = document.createElement("div"); empty.className = "empty-card application-empty"; empty.innerHTML = "<strong>No applications registered</strong><span>Add a Process or Docker Compose application to control it here.</span>"; root.appendChild(empty); return;
  }
  state.applications.forEach((application) => {
    const runtime = state.applicationRuntime.get(application.id) || { status: application.status };
    const status = application.enabled ? (runtime.status || application.status) : "DISABLED";
    const busy = state.applicationBusy.has(application.id);
    const card = document.createElement("article"); card.className = "application-card panel";
    const header = document.createElement("div"); header.className = "application-card-head";
    const identity = document.createElement("div"); const title = document.createElement("h4"); title.textContent = application.name; const metadata = document.createElement("p"); metadata.textContent = `${application.runtime_type === "compose" ? "Docker Compose" : "Process"} · ${application.id}`; identity.append(title, metadata);
    const badge = document.createElement("span"); badge.className = `status-badge ${applicationStatusClass(status)}`; badge.textContent = busy ? "WORKING" : status; header.append(identity, badge);
    const description = document.createElement("p"); description.className = "application-description"; description.textContent = application.description || "No description";
    const facts = document.createElement("div"); facts.className = "application-facts";
    const directory = document.createElement("span"); directory.title = application.manifest.runtime.working_dir; directory.textContent = application.manifest.runtime.working_dir;
    const ports = document.createElement("span"); ports.textContent = `${application.manifest.ports.length} declared port${application.manifest.ports.length === 1 ? "" : "s"}`; facts.append(directory, ports);
    if (runtime.error_message || runtime.statusError) { const runtimeError = document.createElement("div"); runtimeError.className = "runtime-error"; runtimeError.textContent = runtime.error_message || runtime.statusError; card.append(header, description, facts, runtimeError); } else card.append(header, description, facts);
    const endpointList = document.createElement("div"); endpointList.className = "endpoint-list";
    const endpoints = runtime.endpoints ? runtime.endpoints.endpoints : [];
    endpoints.forEach((endpoint) => {
      const item = document.createElement(endpoint.url ? "a" : "span"); item.className = `endpoint-chip ${String(endpoint.status).toLowerCase()}`; item.textContent = `${endpoint.name} · ${endpoint.status}`;
      if (endpoint.url) { item.href = endpoint.url; item.target = "_blank"; item.rel = "noopener noreferrer"; item.title = endpoint.url; }
      endpointList.appendChild(item);
    });
    if (endpoints.length) card.appendChild(endpointList);
    const controls = document.createElement("div"); controls.className = "application-controls";
    const start = createActionButton("Start", "start", application.id, "primary-button small-button"); start.disabled = busy || !application.enabled || !applicationCanStart(status);
    const stop = createActionButton("Stop", "stop", application.id); stop.disabled = busy || !applicationCanStop(status);
    const restart = createActionButton("Restart", "restart", application.id); restart.disabled = busy || !["RUNNING", "UNHEALTHY"].includes(status);
    const logs = createActionButton("Logs", "logs", application.id); logs.disabled = busy;
    const openEndpoint = runtime.endpoints && ((runtime.endpoints.primary && runtime.endpoints.primary.url && runtime.endpoints.primary) || endpoints.find((endpoint) => endpoint.url));
    const open = openEndpoint ? document.createElement("a") : null; if (open) { open.className = "secondary-button"; open.textContent = "Open Web UI"; open.href = openEndpoint.url; open.target = "_blank"; open.rel = "noopener noreferrer"; }
    const edit = createActionButton("Edit", "edit", application.id, "text-button"); edit.disabled = busy || !["STOPPED", "DISABLED", "FAILED", "UNKNOWN"].includes(status);
    const remove = createActionButton("Delete", "delete", application.id, "text-button danger-button"); remove.disabled = busy || !["STOPPED", "DISABLED", "FAILED", "UNKNOWN"].includes(status);
    controls.append(start, stop, restart, logs); if (open) controls.appendChild(open); controls.append(edit, remove); card.appendChild(controls); root.appendChild(card);
  });
}

function splitLines(value) { return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean); }

function parseEnvironment(value) {
  const environment = {};
  splitLines(value).forEach((line) => { const separator = line.indexOf("="); if (separator < 1) throw new Error(`Environment entry must use NAME=value: ${line}`); environment[line.slice(0, separator).trim()] = line.slice(separator + 1); });
  return environment;
}

function collectPorts() {
  return [...$("port-list").querySelectorAll(".port-row")].map((row, index) => {
    const protocol = row.querySelector("[data-port=protocol]").value;
    const port = {
      id: row.querySelector("[data-port=id]").value.trim(),
      name: row.querySelector("[data-port=name]").value.trim(),
      protocol,
      host_port: Number(row.querySelector("[data-port=host_port]").value),
      bind_address: row.querySelector("[data-port=bind_address]").value.trim(),
      primary: row.querySelector("[data-port=primary]").checked,
      open_in_browser: row.querySelector("[data-port=open_in_browser]").checked,
    };
    const path = row.querySelector("[data-port=path]").value.trim(); if (path) port.path = path;
    if (!port.id || !port.name || !port.host_port || !port.bind_address) throw new Error(`Port ${index + 1} is incomplete.`);
    if (!["http", "https"].includes(protocol)) { port.primary = false; port.open_in_browser = false; delete port.path; }
    return port;
  });
}

function collectManifest() {
  const runtimeType = $("application-runtime").value;
  const runtime = runtimeType === "process" ? { type: "process", working_dir: $("application-working-dir").value.trim(), command: splitLines($("application-command").value) } : { type: "compose", working_dir: $("application-working-dir").value.trim(), compose_file: $("application-compose-file").value.trim() || "compose.yaml" };
  if (runtimeType === "compose" && $("application-project-name").value.trim()) runtime.project_name = $("application-project-name").value.trim();
  return { version: 1, id: $("application-id").value.trim(), name: $("application-name").value.trim(), description: $("application-description").value.trim(), enabled: $("application-enabled").checked, runtime, environment: parseEnvironment($("application-environment").value), ports: collectPorts(), tags: $("application-tags").value.split(",").map((tag) => tag.trim()).filter(Boolean) };
}

function addPortRow(port = {}) {
  const row = document.createElement("div"); row.className = "port-row";
  row.innerHTML = `<div class="port-row-grid"><label>ID<input data-port="id" required placeholder="web" /></label><label>Name<input data-port="name" required placeholder="Web UI" /></label><label>Protocol<select data-port="protocol"><option value="http">HTTP</option><option value="https">HTTPS</option><option value="tcp">TCP</option><option value="udp">UDP</option></select></label><label>Host port<input data-port="host_port" required type="number" min="1" max="65535" placeholder="8188" /></label><label>Bind address<input data-port="bind_address" required placeholder="127.0.0.1" /></label><label>Path<input data-port="path" placeholder="/" /></label></div><div class="port-row-options"><label class="toggle-label"><input data-port="primary" type="checkbox" /><span>Primary Web UI</span></label><label class="toggle-label"><input data-port="open_in_browser" type="checkbox" /><span>Show Open link</span></label><button class="text-button danger-button" data-port-remove type="button">Remove</button></div>`;
  ["id", "name", "protocol", "host_port", "bind_address", "path"].forEach((field) => { const input = row.querySelector(`[data-port=${field}]`); const value = field === "host_port" ? (port.host_port || port.host) : port[field]; if (value !== undefined && value !== null) input.value = value; });
  if (!port.bind_address) row.querySelector("[data-port=bind_address]").value = "127.0.0.1";
  row.querySelector("[data-port=primary]").checked = Boolean(port.primary); row.querySelector("[data-port=open_in_browser]").checked = Boolean(port.open_in_browser);
  const updateProtocol = () => { const web = ["http", "https"].includes(row.querySelector("[data-port=protocol]").value); row.querySelector("[data-port=path]").disabled = !web; row.querySelector("[data-port=primary]").disabled = !web; row.querySelector("[data-port=open_in_browser]").disabled = !web; };
  row.querySelector("[data-port=protocol]").addEventListener("change", updateProtocol); row.querySelector("[data-port-remove]").addEventListener("click", () => row.remove()); updateProtocol(); $("port-list").appendChild(row);
}

function updateRuntimeFields() {
  const process = $("application-runtime").value === "process"; $("process-fields").classList.toggle("hidden", !process); $("compose-fields").classList.toggle("hidden", process); $("application-command").required = process; $("application-compose-file").required = !process;
}

function openApplicationForm(application = null) {
  state.editingApplicationId = application ? application.id : null; $("application-form").reset(); $("port-list").replaceChildren(); $("application-enabled").checked = true;
  $("application-form-title").textContent = application ? `Edit ${application.name}` : "Add application"; $("application-save").textContent = application ? "Save changes" : "Register application"; $("application-id").disabled = Boolean(application); $("application-form-message").textContent = "";
  if (application) {
    const manifest = application.manifest; $("application-id").value = manifest.id; $("application-name").value = manifest.name; $("application-description").value = manifest.description; $("application-enabled").checked = manifest.enabled; $("application-runtime").value = manifest.runtime.type; $("application-working-dir").value = manifest.runtime.working_dir;
    if (manifest.runtime.type === "process") $("application-command").value = manifest.runtime.command.join("\n"); else { $("application-compose-file").value = manifest.runtime.compose_file; $("application-project-name").value = manifest.runtime.project_name || ""; }
    $("application-environment").value = Object.entries(manifest.environment).map(([name, value]) => `${name}=${value}`).join("\n"); $("application-tags").value = manifest.tags.join(", "); manifest.ports.forEach(addPortRow);
  } else { $("application-runtime").value = "process"; $("application-compose-file").value = "compose.yaml"; }
  updateRuntimeFields(); $("application-form-drawer").classList.remove("hidden"); $("application-form-backdrop").classList.remove("hidden");
}

function closeApplicationForm() { state.editingApplicationId = null; $("application-form-drawer").classList.add("hidden"); $("application-form-backdrop").classList.add("hidden"); }

function formatValidation(result) {
  const items = [...result.errors, ...result.warnings]; if (!items.length) return "Manifest is valid.";
  return items.map((issue) => `${issue.code}: ${issue.message}`).join("\n");
}

async function validateApplicationForm() {
  const manifest = collectManifest(); const result = await api("/api/v1/applications/validate", { method: "POST", body: JSON.stringify(manifest) }); $("application-form-message").className = `form-message ${result.valid ? "success" : "error"}`; $("application-form-message").textContent = formatValidation(result); return { manifest, result };
}

async function saveApplication(event) {
  event.preventDefault(); const button = $("application-save"); button.disabled = true; $("application-form-message").textContent = "";
  try {
    const { manifest, result } = await validateApplicationForm(); if (!result.valid) return;
    const editingId = state.editingApplicationId; const path = editingId ? `/api/v1/applications/${encodeURIComponent(editingId)}` : "/api/v1/applications";
    await api(path, { method: editingId ? "PUT" : "POST", body: JSON.stringify(manifest) }); closeApplicationForm(); await loadApplications();
  } catch (error) { $("application-form-message").className = "form-message error"; $("application-form-message").textContent = error.message; }
  finally { button.disabled = false; }
}

async function runApplicationAction(applicationId, action) {
  const application = state.applications.find((item) => item.id === applicationId); if (!application) return;
  if (action === "edit") return openApplicationForm(application);
  if (action === "logs") return openLogs(application);
  if (action === "delete" && !window.confirm(`Delete ${application.name} from MachineDeck? Its managed unit and application files will be retained.`)) return;
  state.applicationBusy.add(applicationId); $("application-error").textContent = ""; renderApplicationList();
  try {
    if (action === "delete") await api(`/api/v1/applications/${encodeURIComponent(applicationId)}`, { method: "DELETE" });
    else { const result = await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/${action}`, { method: "POST" }); if (!result.succeeded) throw new Error(result.message || result.error_code || `${action} failed`); }
  } catch (error) { $("application-error").textContent = `${application.name}: ${error.message}`; }
  finally { state.applicationBusy.delete(applicationId); await loadApplications({ quiet: true }); }
}

function openLogs(application) {
  closeLogs(); $("log-title").textContent = `${application.name} logs`; $("log-output").textContent = ""; $("log-status").textContent = "Connecting…"; $("log-drawer").classList.remove("hidden"); $("log-drawer-backdrop").classList.remove("hidden");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:"; const socket = new WebSocket(`${protocol}//${location.host}/ws/v1/applications/${encodeURIComponent(application.id)}/logs?history=200&follow=true`); state.logSocket = socket;
  socket.onmessage = (message) => {
    let envelope; try { envelope = JSON.parse(message.data); } catch (_) { return; }
    if (envelope.type === "log") {
      const event = envelope.data; const prefix = `${new Date(event.timestamp).toLocaleTimeString()} ${event.service ? `[${event.service}] ` : ""}`; const output = $("log-output"); output.textContent += `${prefix}${event.message}\n`;
      const lines = output.textContent.split("\n"); if (lines.length > 2001) output.textContent = lines.slice(-2001).join("\n"); if ($("log-follow").checked) output.scrollTop = output.scrollHeight;
    } else if (envelope.type === "status") $("log-status").textContent = "Live stream connected";
    else if (envelope.type === "warning") $("log-status").textContent = `Warning: ${envelope.data.code}`;
    else if (envelope.type === "error") $("log-status").textContent = envelope.data.message || envelope.data.code;
    else if (envelope.type === "eof") $("log-status").textContent = "Log stream ended";
  };
  socket.onclose = (event) => { if (state.logSocket === socket) { state.logSocket = null; if (event.code === 4401) $("log-status").textContent = "Session expired"; else if ($("log-status").textContent === "Live stream connected") $("log-status").textContent = "Disconnected"; } };
}

function closeLogs() { if (state.logSocket) { const socket = state.logSocket; state.logSocket = null; socket.close(); } $("log-drawer").classList.add("hidden"); $("log-drawer-backdrop").classList.add("hidden"); }

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
  clearInterval(state.applicationRefresh); state.applicationRefresh = null; state.applications = []; state.applicationRuntime.clear(); state.auditEvents = []; state.auditCursor = null; showAuth(false);
}

$("auth-form").addEventListener("submit", authenticate);
$("logout").addEventListener("click", logout);
document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$("audit-filters").addEventListener("submit", (event) => { event.preventDefault(); state.auditCursor = null; loadAudit(true); });
$("audit-clear").addEventListener("click", clearAuditFilters); $("audit-more").addEventListener("click", () => loadAudit(false)); $("audit-drawer-close").addEventListener("click", closeAuditDrawer); $("audit-drawer-backdrop").addEventListener("click", closeAuditDrawer);
$("application-add").addEventListener("click", () => openApplicationForm()); $("application-form").addEventListener("submit", saveApplication); $("application-form-close").addEventListener("click", closeApplicationForm); $("application-form-backdrop").addEventListener("click", closeApplicationForm); $("application-runtime").addEventListener("change", updateRuntimeFields); $("port-add").addEventListener("click", () => addPortRow());
$("application-validate").addEventListener("click", async () => { try { await validateApplicationForm(); } catch (error) { $("application-form-message").className = "form-message error"; $("application-form-message").textContent = error.message; } });
$("application-list").addEventListener("click", (event) => { const button = event.target.closest("[data-application-action]"); if (button && !button.disabled) runApplicationAction(button.dataset.applicationId, button.dataset.applicationAction); });
$("log-close").addEventListener("click", closeLogs); $("log-drawer-backdrop").addEventListener("click", closeLogs); $("log-clear").addEventListener("click", () => { $("log-output").textContent = ""; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeAuditDrawer(); closeApplicationForm(); closeLogs(); } });
setInterval(updateFreshness, 1000); bootstrap();
