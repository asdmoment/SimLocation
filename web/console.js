"use strict";

const $ = id => document.getElementById(id);
const token = new URLSearchParams(window.location.search).get("t") || "";
const actionNames = { discover: "扫描设备", doctor: "只读诊断", set: "设置定位", route: "启动运动轨迹",
  clear: "清除定位", clear_all: "清除全部会话", device_add: "保存别名", device_remove: "删除别名", device_default: "设置默认设备" };
let snapshot = null;
let online = false;
let submitting = false;
let initialized = false;
let renderedDevices = "";
let renderedChoices = "";
let refreshQueue = Promise.resolve();
let connectionError = "";
let renderedChecks = "";
let mapMode = "point";
let mapReady = false;
let point = [];
let points = [];
let pollTimer;

function errorMessage(message = "") {
  $("feedback").hidden = !message;
  $("feedback").textContent = message;
}

async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    headers: { "X-SimLocation-Token": token, "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败，请重试。");
  return result;
}

function targetDevice() {
  return $("device").value === "manual" ? $("manual-device").value.trim() : $("device").value;
}

function isBusy() {
  return submitting || !!(snapshot && snapshot.job && snapshot.job.status === "running");
}

async function perform(action, data = {}) {
  if (isBusy()) return;
  submitting = true;
  updateButtons();
  errorMessage();
  try {
    const job = await api("/api/action", { action, device: targetDevice(),
      connection: $("connection-mode").value, debug: $("debug").checked, ...data });
    if (snapshot) snapshot.job = job;
    renderJob(job);
  } catch (error) {
    // A lost response is ambiguous: poll the operation before allowing a retry.
    errorMessage(error.message + " 请先查看操作状态，再决定是否重试。");
  } finally {
    await refresh();
    submitting = false;
    updateButtons();
  }
}

function routeDistance(path, loop) {
  const list = loop && path.length > 1 ? [...path, path[0]] : path;
  let meters = 0;
  for (let i = 1; i < list.length; i++) {
    const [a, b] = list[i - 1].map(v => v * Math.PI / 180);
    const [c, d] = list[i].map(v => v * Math.PI / 180);
    const h = Math.sin((c - a) / 2) ** 2 + Math.cos(a) * Math.cos(c) * Math.sin((d - b) / 2) ** 2;
    meters += 12742017.6 * Math.asin(Math.sqrt(Math.min(1, h)));
  }
  return meters;
}

function validPoints(value) {
  return Array.isArray(value) && value.length <= 10000 && value.every(p =>
    Array.isArray(p) && p.length === 2 && p.every(Number.isFinite) && Math.abs(p[0]) <= 90 && Math.abs(p[1]) <= 180);
}

function saveDraft() {
  try {
    sessionStorage.setItem("simlocation-draft", JSON.stringify({ point, points,
      speed: $("speed").value, loop: $("loop").checked }));
  } catch (_error) { /* The editor still works when browser storage is disabled. */ }
}

function restoreDraft() {
  try {
    const draft = JSON.parse(sessionStorage.getItem("simlocation-draft"));
    if (!draft) return;
    if (validPoints(draft.point) && draft.point.length <= 1) point = draft.point;
    if (validPoints(draft.points)) points = draft.points;
    if (Number(draft.speed) > 0 && Number(draft.speed) <= 1000) $("speed").value = draft.speed;
    $("loop").checked = draft.loop === true;
    fillPoint();
  } catch (_error) { /* Ignore old or incomplete drafts. */ }
}

function fillPoint() {
  $("latitude").value = point.length ? point[0][0].toFixed(6) : "";
  $("longitude").value = point.length ? point[0][1].toFixed(6) : "";
}

function readPoint() {
  const lat = $("latitude"), lon = $("longitude");
  if (!lat.value || !lon.value || !lat.checkValidity() || !lon.checkValidity()) return [];
  return [[Number(lat.value), Number(lon.value)]];
}

function updateDraft() {
  const meters = routeDistance(points, $("loop").checked);
  const speed = Number($("speed").value);
  $("route-distance").replaceChildren(document.createTextNode((meters / 1000).toFixed(2) + " "));
  const unit = document.createElement("small"); unit.textContent = "km"; $("route-distance").appendChild(unit);
  $("route-estimate").textContent = points.length < 2 ? "至少选择两个途经点" :
    `${points.length} 个途经点 / ${speed > 0 ? "约 " + (meters / (speed / 3.6) / 60).toFixed(1) + " 分钟" : "请填写速度"}${$("loop").checked ? "每圈" : ""}`;
  $("route-hint").textContent = $("loop").checked ? "途经点之间沿大圆路径移动，终点连回起点并循环。" : "途经点之间沿大圆路径移动，到达终点后保持定位。";
  $("route-undo").disabled = $("route-reset").disabled = !points.length;
  $("route-export").disabled = meters <= .001;
  $("save-point").disabled = !point.length;
  updateMapHint();
  updateButtons();
  saveDraft();
}

function updateMapHint() {
  $("map-hint").textContent = !mapReady ? "地图加载中；仍可输入坐标或导入路线。" : mapMode === "route" ?
    `${points.length} 个途经点 / 点击添加，拖动调整` : point.length ?
      `${point[0][0].toFixed(6)}, ${point[0][1].toFixed(6)}` : "点击地图选点，或搜索一个地点";
}

function sendDraft(focus = false) {
  if (!mapReady) return;
  $("map").contentWindow.postMessage({ type: "simlocation-draft", mode: mapMode,
    points: mapMode === "route" ? points : point, loop: $("loop").checked, focus }, window.location.origin);
}

function loadMap(mode) {
  mapMode = mode;
  mapReady = false;
  $("map-mode").textContent = mode === "route" ? "绘制路线" : "地图选点";
  $("map").src = "/picker?" + new URLSearchParams({ t: token, embed: "1", mode });
  updateMapHint();
}

function selectTab(tab) {
  for (const name of ["point", "route", "devices", "doctor"]) {
    $("view-" + name).hidden = tab !== name;
    $("tab-" + name).setAttribute("aria-pressed", String(tab === name));
  }
  if ((tab === "point" || tab === "route") && mapMode !== tab) loadMap(tab);
}

function updateButtons() {
  const locked = !online || isBusy();
  document.querySelectorAll("[data-operation]").forEach(button => { button.disabled = locked; });
  const device = targetDevice();
  for (const id of ["set-location", "clear-location", "set-default"]) $(id).disabled = locked || !device;
  $("start-route").disabled = locked || !device || routeDistance(points, $("loop").checked) <= .001 || !$("speed").checkValidity();
  $("alias-form").querySelector("button").disabled = locked || !device;
  $("clear-all").disabled = locked || !(snapshot && snapshot.devices.some(d => d.state.status === "ready"));
}

function renderSession() {
  const device = snapshot && snapshot.devices.find(d => d.udid === targetDevice());
  const state = device ? device.state : {};
  $("session-mode").textContent = state.status === "ready" ? (state.mode === "route" ? "运动轨迹" : "定点定位") : device ? "未运行" : "未选择设备";
  $("session-status").textContent = device ? (device.description === "—" ? "当前没有定位会话。" : device.description) : targetDevice() ? "尚无此设备的会话记录。" : "选择设备后可查看定位状态。";
  if (device && device.description.startsWith("ready (")) $("session-status").textContent = `保持定位 (${state.lat}, ${state.lon})`;
  if (state.error) $("session-status").textContent += "：" + state.error;
  $("session-progress").hidden = !(state.mode === "route" && state.status === "ready" && !device.description.startsWith("stale"));
  $("session-progress").value = state.progress || 0;
  $("device-hint").textContent = device ? (device.default ? "默认设备 / " : "") + device.udid : "连接设备后扫描，也可以手动输入 UDID。";
}

function chooseDevice(udid) {
  $("device").value = udid;
  if ($("device").value !== udid) {
    $("device").value = "manual";
    $("manual-device").value = udid;
  }
  $("manual-device-field").hidden = $("device").value !== "manual";
  renderSession();
  updateButtons();
}

function renderDevices(state) {
  const signature = JSON.stringify(state.devices.map(d => [d.udid, d.aliases, d.default, d.discovered, d.description]));
  if (signature === renderedDevices) return;
  renderedDevices = signature;
  const choiceSignature = JSON.stringify(state.devices.map(d => [d.udid, d.aliases, d.default]));
  if (choiceSignature !== renderedChoices) {
    renderedChoices = choiceSignature;
    const selection = $("device").value;
    $("device").replaceChildren(new Option("选择设备", ""));
    for (const d of state.devices) $("device").add(new Option((d.aliases.join(" / ") || d.udid) + (d.default ? "（默认）" : ""), d.udid));
    $("device").add(new Option("输入 UDID…", "manual"));
    $("device").value = selection;
    if (!$("device").value && selection && selection !== "manual") chooseDevice(selection);
    if (!$("device").value) chooseDevice(state.preferred || (state.devices.length === 1 ? state.devices[0].udid : ""));
  }
  const list = $("devices-list"); list.replaceChildren();
  if (!state.devices.length) { const p = document.createElement("p"); p.className = "note"; p.textContent = "尚未发现设备。可以扫描设备，或在上方手动输入 UDID 注册别名。"; list.appendChild(p); }
  for (const d of state.devices) {
    const card = document.createElement("article"); card.className = "device-card";
    const title = document.createElement("strong"); title.textContent = (d.aliases.join(" / ") || "未命名设备") + (d.default ? "（默认）" : "");
    card.appendChild(title);
    for (const text of [d.udid, d.description === "—" ? "无定位会话" : d.description, d.discovered ? "上次扫描已发现" : "上次扫描未发现"]) {
      const p = document.createElement("p"); p.textContent = text; card.appendChild(p);
    }
    const select = document.createElement("button"); select.textContent = "选择设备"; select.onclick = () => chooseDevice(d.udid); card.appendChild(select);
    for (const alias of d.aliases) {
      const remove = document.createElement("button"); remove.textContent = "删除别名 " + alias;
      remove.setAttribute("data-operation", "");
      remove.onclick = () => perform("device_remove", { alias }); card.appendChild(remove);
    }
    list.appendChild(card);
  }
}

function renderJob(job) {
  if (!job) return;
  $("operation").hidden = false;
  $("operation").dataset.status = job.status;
  const status = { running: "进行中", succeeded: "已完成", failed: "失败" }[job.status];
  $("operation-title").textContent = `${actionNames[job.action] || job.action} / ${status}`;
  $("operation-detail").textContent = job.status === "running" ? "连接设备可能需要数十秒，请等待结果。" :
    job.action === "discover" && job.result ? `发现 ${job.result.count} 台设备。` : job.device ? `设备 ${job.device}` : "";
  $("operation-log").textContent = job.messages.join("\n") || (job.status === "running" ? "等待操作结果…" : "操作已完成。");
  if (job.status === "failed") $("operation").querySelector("details").open = true;
  if (job.action === "doctor" && job.result && renderedChecks !== String(job.id)) {
    renderedChecks = String(job.id);
    const list = $("doctor-results"); list.replaceChildren();
    const target = document.createElement("p"); target.className = "note";
    target.textContent = job.device ? `诊断设备：${job.device}` : "诊断环境与 CLI 默认设备"; list.appendChild(target);
    for (const check of job.result.checks) {
      const item = document.createElement("div"); item.className = "check"; item.dataset.status = check.status;
      const title = document.createElement("strong"); title.textContent = ({ ok: "通过", warn: "注意", error: "异常" }[check.status] || check.status) + " / " + check.label;
      const detail = document.createElement("p"); detail.textContent = check.detail;
      item.append(title, detail); list.appendChild(item);
    }
  }
}

function refresh() {
  // Serialize polling and post-action refreshes so old reads cannot replace newer state.
  refreshQueue = refreshQueue.then(fetchState, fetchState);
  return refreshQueue;
}

async function fetchState() {
  try {
    const state = await api("/api/state");
    snapshot = state;
    if (connectionError && $("feedback").textContent === connectionError) errorMessage();
    connectionError = "";
    online = true;
    $("connection-status").textContent = "控制台已连接";
    $("version").textContent = state.version;
    $("map-provider").textContent = state.provider === "amap" ? "高德地图 / 转换为 WGS-84" : "OpenStreetMap / WGS-84";
    if (!initialized) {
      $("connection-mode").value = state.connection;
      $("debug").checked = state.debug;
    }
    renderDevices(state);
    initialized = true;
    $("scan-time").textContent = state.scanned_at ? "上次扫描 " + new Date(state.scanned_at * 1000).toLocaleTimeString() : "尚未扫描设备";
    renderSession(); renderJob(state.job);
  } catch (error) {
    online = false;
    $("connection-status").textContent = "控制台连接中断";
    connectionError = error.message + " 定位会话可能仍在继续，请检查终端服务。";
    errorMessage(connectionError);
  }
  $("connection-status").dataset.online = String(online);
  updateButtons();
}

async function poll() {
  await refresh();
  pollTimer = setTimeout(poll, 2000);
}

function download(name, payload) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2) + "\n"], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function confirmClear(title, description, action) {
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  const dialog = $("confirm-dialog");
  dialog.returnValue = "cancel";
  dialog.onclose = () => { if (dialog.returnValue === "confirm") action(); };
  dialog.showModal();
}

for (const tab of ["point", "route", "devices", "doctor"]) $("tab-" + tab).onclick = () => selectTab(tab);
$("device").onchange = () => { $("manual-device-field").hidden = $("device").value !== "manual"; renderSession(); updateButtons(); };
$("manual-device").oninput = () => { renderSession(); updateButtons(); };
$("discover").onclick = () => perform("discover");
$("doctor").onclick = () => perform("doctor");
$("set-default").onclick = () => perform("device_default");
$("alias-form").onsubmit = event => { event.preventDefault(); perform("device_add", { alias: $("alias").value.trim() }); };
$("clear-location").onclick = () => perform("clear");
$("clear-all").onclick = () => confirmClear("清除全部活跃会话？", "所有设备上的运动轨迹和定点定位都将结束，恢复真实位置。", () => perform("clear_all", { confirm: true }));
$("point-form").onsubmit = event => {
  event.preventDefault(); point = readPoint(); updateDraft(); sendDraft(true);
  if (point.length) perform("set", { lat: point[0][0], lon: point[0][1] });
};
for (const id of ["latitude", "longitude"]) $(id).oninput = () => { point = readPoint(); updateDraft(); sendDraft(true); };
$("save-point").onclick = () => { if (point.length) download("location.json", { lat: point[0][0], lon: point[0][1] }); };
$("speed").oninput = updateDraft;
$("loop").onchange = () => { updateDraft(); sendDraft(); };
$("route-undo").onclick = () => { points.pop(); updateDraft(); sendDraft(); };
$("route-reset").onclick = () => { points = []; updateDraft(); sendDraft(); };
$("route-export").onclick = () => download("route.json", { points });
$("start-route").onclick = () => perform("route", { points, speed: Number($("speed").value), loop: $("loop").checked });
$("route-import").onclick = () => $("route-file").click();
$("route-file").onchange = async () => {
  const file = $("route-file").files[0];
  if (!file) return;
  $("route-import").disabled = true;
  try {
    if (file.size > (snapshot ? snapshot.max_route_bytes : 4194304)) throw new Error("路线文件不能超过 4 MiB。");
    if (!/\.(json|gpx)$/i.test(file.name)) throw new Error("请选择 JSON 或 GPX 文件。");
    const result = await api("/api/route", { format: /\.gpx$/i.test(file.name) ? "gpx" : "json", content: await file.text() });
    points = result.points;
    updateDraft(); sendDraft(true); errorMessage();
  } catch (error) { errorMessage(error.message); }
  finally { $("route-file").value = ""; $("route-import").disabled = false; }
};
window.addEventListener("message", event => {
  if (event.source !== $("map").contentWindow || event.origin !== window.location.origin || !event.data) return;
  const data = event.data;
  if (data.type === "simlocation-map-ready") { mapReady = true; sendDraft(true); updateMapHint(); }
  if (data.type !== "simlocation-selection" || data.mode !== mapMode || !validPoints(data.points)) return;
  if (mapMode === "route") points = data.points;
  else { if (data.points.length !== 1) return; point = data.points; fillPoint(); }
  updateDraft();
});
window.addEventListener("pagehide", () => clearTimeout(pollTimer));
restoreDraft(); updateDraft(); loadMap("point");
poll();
