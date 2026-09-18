// Contract tests for map editing and coordinate submission, with map SDK stubs.
// Ported from hoicau's work in PR #1.
//
// Run with: node tests/test_map_routes.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function loadMap(provider, routeMode, loop = false) {
  const nodes = new Map();
  function element() {
    return { style: {}, listeners: {}, disabled: false, hidden: true,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      appendChild() {}, contains() { return false; }, click() {} };
  }
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    createElement: element, createTextNode: text => text,
    addEventListener() {}, head: { appendChild() {} },
  };
  function layer(options = {}) {
    return { options, position: options.position, listeners: {},
      addTo() { return this; }, setMap() {}, setView() { return this; },
      setZoomAndCenter() {}, on(name, fn) { this.listeners[name] = fn; },
      clearLayers() {}, bindTooltip() {},
      setPosition(value) { this.position = value; },
      setLatLng(value) { this.position = value; },
      getLatLng() { return { lat: this.position[0], lng: this.position[1] }; },
      getPosition() { return { getLng: () => this.position[0], getLat: () => this.position[1] }; },
    };
  }
  const L = {
    map: () => layer(), tileLayer: () => layer(), layerGroup: () => layer(),
    polyline: () => layer(), marker: position => layer({ position }),
  };
  const AMap = {
    Map: function () { return layer(); }, Marker: function (opts) { return layer(opts); },
    Polyline: function (opts) { return layer(opts); }, plugin() {},
  };
  // window.location.search carries the map picker access token, which the
  // confirm handler appends to the POST URL.
  const context = vm.createContext({ document, L, AMap, console, Blob, URL,
    setTimeout() {}, clearTimeout() {}, alert() {},
    window: { close() {}, location: { search: "" } } });
  context.XMLHttpRequest = function () {
    this.open = (method, url) => { context.postedTo = url; };
    this.setRequestHeader = () => {};
    this.send = body => { context.sent = JSON.parse(body); };
  };
  let html = fs.readFileSync(path.join(__dirname, "../web/map-" + provider + ".html"), "utf8");
  html = html.replace("{{ROUTE_SCRIPT}}",
    fs.readFileSync(path.join(__dirname, "../web/map-route.js"), "utf8"));
  for (const [key, value] of Object.entries({ ROUTE_MODE: routeMode, ROUTE_LOOP: loop,
    PICK_ONLY: false, ROUTE_SPEED: 5, MAX_ROUTE_POINTS: 10000, AMAP_KEY: "test-key" })) {
    html = html.replaceAll("{{" + key + "}}", String(value));
  }
  assert.ok(!html.includes("{{"), "every template placeholder must be substituted");
  for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(match[1], context);
  if (provider === "amap") context._onAMapLoaded();
  function add(lat, lon) {
    if (provider === "osm") context.placeMarker(lat, lon);
    else context.placeMarker(lon, lat);
  }
  return { context, nodes, add };
}

for (const provider of ["osm", "amap"]) {
  test(provider + ": draw, undo, drag, reset, and submit a route", () => {
    const { context, nodes, add } = loadMap(provider, true, true);
    assert.equal(nodes.get("confirm-btn").disabled, true);
    add(1.28, -103.85);
    assert.equal(nodes.get("confirm-btn").disabled, true);
    add(1.29, -103.85);
    assert.equal(nodes.get("confirm-btn").disabled, false);
    const payload = JSON.parse(JSON.stringify(context.routePayload()));
    assert.deepEqual(payload.points, [[1.28, -103.85], [1.29, -103.85]]);
    add(1.29, -103.86);
    nodes.get("route-undo").onclick();
    assert.equal(context.routePoints.length, 2);
    context.moveRoutePoint(1, 1.3, -103.86);
    assert.equal(context.routePayload().points[1][0], 1.3);
    nodes.get("route-reset").onclick();
    assert.equal(context.routePoints.length, 0);
    assert.equal(nodes.get("confirm-btn").disabled, true);
    add(1.28, -103.85);
    add(1.29, -103.85);
    nodes.get("confirm-btn").listeners.click();
    assert.deepEqual(context.sent, payload);
    assert.equal(context.routeSubmitted, true);
    // A submitted route is frozen: late clicks must not change what was sent.
    add(1.3, -103.85);
    assert.equal(context.routePoints.length, 2);
  });

  test(provider + ": single-point picker still submits one location", () => {
    const { context, nodes, add } = loadMap(provider, false);
    add(1.28, -103.85);
    nodes.get("confirm-btn").listeners.click();
    assert.deepEqual(context.sent, { lat: 1.28, lon: -103.85 });
  });

  test(provider + ": the access token survives the confirm POST", () => {
    const { context, nodes, add } = loadMap(provider, false);
    context.window.location.search = "?t=secret-token";
    add(1.28, -103.85);
    nodes.get("confirm-btn").listeners.click();
    assert.equal(context.postedTo, "/confirm?t=secret-token");
  });

  test(provider + ": saving a route produces reusable JSON without starting playback", async () => {
    const { context, nodes, add } = loadMap(provider, true);
    let savedBlob;
    context.URL = { createObjectURL(blob) { savedBlob = blob; return "blob:test"; }, revokeObjectURL() {} };
    add(1.28, -103.85);
    add(1.29, -103.85);
    nodes.get("route-save").onclick();
    assert.deepEqual(JSON.parse(await savedBlob.text()), { points: [[1.28, -103.85], [1.29, -103.85]] });
    assert.equal(context.routeSubmitted, false);
    assert.equal(context.sent, undefined);
  });
}

test("every pinned CDN resource carries an integrity hash", () => {
  // Catches a new CDN reference added without SRI, or an integrity attribute
  // dropped while editing. A version bump that keeps the stale hash still has
  // to be caught by actually loading the page — the hash cannot be checked
  // offline.
  const html = fs.readFileSync(path.join(__dirname, "../web/map-osm.html"), "utf8");
  const tags = html.match(/<(?:script|link)\b[^>]*https:\/\/unpkg\.com[^>]*>/g) || [];
  assert.ok(tags.length >= 2, "expected the Leaflet script and stylesheet");
  for (const tag of tags) {
    assert.match(tag, /integrity="sha\d{3}-[A-Za-z0-9+/=]+"/, "missing integrity: " + tag);
    assert.match(tag, /crossorigin="anonymous"/, "SRI needs crossorigin: " + tag);
  }
});

test("Amap converts every route point from GCJ-02 to WGS-84", () => {
  const { context, add } = loadMap("amap", true);
  add(39.909, 116.397);
  add(39.919, 116.407);
  const points = context.routePayload().points;
  assert.ok(Math.abs(points[0][0] - 39.9076) < 0.0002);
  assert.ok(Math.abs(points[0][1] - 116.3908) < 0.0002);
  assert.notEqual(points[1][0], 39.919);
  assert.notEqual(points[1][1], 116.407);
});
