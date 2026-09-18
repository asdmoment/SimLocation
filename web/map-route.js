/* Shared route editing for both map providers.
 *
 * Coordinates handled here are map-native: Leaflet hands us WGS-84 already,
 * Amap hands us GCJ-02. Each provider supplies routeToWgs() so the payload
 * posted back to the CLI is always WGS-84.
 */
var routeMode = "{{ROUTE_MODE}}" === "true";
var routeLoop = "{{ROUTE_LOOP}}" === "true";
var routePickOnly = "{{PICK_ONLY}}" === "true";
var routeMaxPoints = Number("{{MAX_ROUTE_POINTS}}");
var routePoints = [];
var routeSubmitted = false;
var drawRoute, routeToWgs;

function routePayload() {
  return { points: routePoints.map(function (point) { return routeToWgs(point); }) };
}

function routeLength(points) {
  var total = 0;
  var path = points.slice();
  if (routeLoop && path.length > 1) path.push(path[0]);
  for (var i = 1; i < path.length; i++) {
    var a = path[i - 1].map(function (v) { return v * Math.PI / 180; });
    var b = path[i].map(function (v) { return v * Math.PI / 180; });
    var h = Math.pow(Math.sin((b[0] - a[0]) / 2), 2) +
      Math.cos(a[0]) * Math.cos(b[0]) * Math.pow(Math.sin((b[1] - a[1]) / 2), 2);
    total += 12742017.6 * Math.asin(Math.sqrt(Math.min(1, h)));
  }
  return total;
}

function refreshRoute() {
  drawRoute(routePoints);
  var meters = routeLength(routePayload().points);
  var minutes = meters / (Number("{{ROUTE_SPEED}}") / 3.6) / 60;
  document.getElementById("coords").textContent = routePoints.length < 2
    ? "在地图上依次选择至少两个途经点，可拖动调整"
    : routePoints.length + " 个途经点 / " + (meters / 1000).toFixed(2) +
      " km / 约 " + minutes.toFixed(1) + " 分钟" + (routeLoop ? "每圈" : "");
  document.getElementById("confirm-btn").disabled = routeSubmitted || meters <= 0.001;
  document.getElementById("route-save").disabled = routeSubmitted || meters <= 0.001;
  document.getElementById("route-undo").disabled = routeSubmitted || !routePoints.length;
  document.getElementById("route-reset").disabled = routeSubmitted || !routePoints.length;
}

function addRoutePoint(lat, lon) {
  if (routeSubmitted) return;
  if (routePoints.length >= routeMaxPoints) {
    alert("路线最多支持 " + routeMaxPoints + " 个途经点。");
    return;
  }
  routePoints.push([lat, lon]);
  refreshRoute();
}

function moveRoutePoint(index, lat, lon) {
  if (routeSubmitted) return;
  routePoints[index] = [lat, lon];
  refreshRoute();
}

/* Called by each provider with its own draw + coordinate conversion. */
function initRouteEditor(draw, toWgs) {
  if (!routeMode) return;
  drawRoute = draw;
  routeToWgs = toWgs;
  document.title = "SimLocation - 运动轨迹";
  document.getElementById("panel").style.display = "block";
  document.getElementById("route-controls").hidden = false;
  document.getElementById("route-hint").textContent = "{{ROUTE_SPEED}} km/h / " +
    (routeLoop ? "循环移动，终点连回起点" : "到达终点后保持定位") + "，途经点之间直线移动";
  document.getElementById("confirm-btn").textContent = routePickOnly ? "确认路线" : "开始移动";
  document.getElementById("route-undo").onclick = function () {
    routePoints.pop();
    refreshRoute();
  };
  document.getElementById("route-reset").onclick = function () {
    routePoints = [];
    refreshRoute();
  };
  /* Saving is local-only: it never contacts the CLI, so a route can be drawn
     and kept without starting playback. */
  document.getElementById("route-save").onclick = function () {
    var url = URL.createObjectURL(new Blob([JSON.stringify(routePayload(), null, 2) + "\n"],
      { type: "application/json" }));
    var link = document.createElement("a");
    link.href = url;
    link.download = "route.json";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  };
  refreshRoute();
}
