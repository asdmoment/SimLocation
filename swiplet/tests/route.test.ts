import test from "node:test";
import assert from "node:assert/strict";
import {
  checkPoint, checkRoute, distance, EARTH_RADIUS, offset, parseCoordinate, parseRoute, Playback, randomFromSeed,
  Route, RouteNoise, type RouteOptions,
} from "../src/route";
import { toGPS, toMap } from "../src/coordinates";

const options: RouteOptions = { points: [[0, 0], [0, 0.01]], speed: 5, loop: false, speedNoise: 0, positionNoise: 0 };
const near = (a: number, b: number, tolerance = 1e-7) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);

test("coordinates reject non-finite values, booleans, missing and out-of-range values", () => {
  for (const value of [NaN, Infinity, true, null, "", "1", 91, undefined]) {
    assert.throws(() => checkPoint({ latitude: value, longitude: 0 }));
  }
  assert.equal(parseCoordinate("0"), null);
  assert.equal(parseCoordinate("0, 181"), null);
  assert.equal(parseCoordinate("a, 1"), null);
  assert.deepEqual(parseCoordinate("22.28，114.15"), { latitude: 22.28, longitude: 114.15 });
  assert.deepEqual(parseCoordinate(" -33.9 151.2 "), { latitude: -33.9, longitude: 151.2 });
});

test("route validation removes consecutive duplicates and closes loops", () => {
  const r = new Route([[0, 0], [0, 0], [0, 1]]);
  assert.equal(r.points.length, 2);
  near(r.total, EARTH_RADIUS * Math.PI / 180);
  const loop = new Route([[0, 0], [0, 1]], true);
  assert.equal(loop.points.length, 3);
  near(loop.total, 2 * r.total);
  for (const points of [[[0, 0], [0, 0]], [[0, 0], [0, 180]], [[0, 0]]]) {
    assert.throws(() => new Route(points as [number, number][]));
  }
});

test("interpolation crosses the antimeridian and follows the great circle", () => {
  const r = new Route([[10, 179], [10, -179]]);
  const middle = r.position(r.total / 2);
  near(Math.abs(middle[1]), 180);
  assert.ok(middle[0] > 10);
  assert.deepEqual(r.position(-1), r.points[0]);
  assert.deepEqual(r.position(r.total + 1), r.points[1]);
});

test("elapsed-time playback does not accumulate polling drift", () => {
  const p = new Playback(options, 123);
  near(p.sample(1).traveled, 5 / 3.6);
  near(p.sample(17).traveled, 17 * 5 / 3.6);
  near(p.sample(3).traveled, 17 * 5 / 3.6); // clock rollback
});

test("two playbacks of one request agree, which lets the interface show the service's position", () => {
  const route = { ...options, speedNoise: 30, positionNoise: 20 };
  assert.deepEqual(new Playback(route, 42).sample(37.5), new Playback(route, 42).sample(37.5));
});

test("noise stays bounded, is smooth, and has a polling-independent integral", () => {
  const dense = new RouteNoise(randomFromSeed(123));
  const sparse = new RouteNoise(randomFromSeed(123));
  for (let t = 0; t <= 100; t += 0.25) {
    const n = dense.sample(t);
    assert.ok(Math.abs(n.speed) <= 1);
    assert.ok(Math.hypot(n.east, n.north) <= 1 + 1e-12);
  }
  assert.deepEqual(dense.sample(100), sparse.sample(100));
  const around = new RouteNoise(randomFromSeed(8));
  const a = around.sample(10 - 1e-6), b = around.sample(10 + 1e-6);
  near(a.speed, b.speed, 1e-9);
  near(b.integral - a.integral, a.speed * 2e-6, 1e-8);
});

test("noise bounds and exact endpoint survive route completion", () => {
  const p = new Playback({ ...options, speedNoise: 50, positionNoise: 100 }, 9);
  const v = p.sample(60);
  assert.ok(v.speed >= 2.5 && v.speed <= 7.5);
  assert.ok(distance([v.point.latitude, v.point.longitude], p.route.position(v.progress)) <= 100.001);
  const end = p.sample(100000);
  assert.equal(end.completed, true);
  assert.deepEqual(end.point, { latitude: 0, longitude: 0.01 });
  assert.equal(end.speed, 0);
});

test("loop progresses continuously through its closing segment", () => {
  const p = new Playback({ ...options, loop: true, speed: 36 }, 1);
  const seconds = p.route.total / 10;
  const before = p.sample(seconds - 0.001), after = p.sample(seconds + 0.001);
  assert.ok(distance([before.point.latitude, before.point.longitude], [after.point.latitude, after.point.longitude]) < 0.03);
  assert.equal(after.completed, false);
});

test("spherical offsets remain finite at both poles", () => {
  for (const lat of [-90, 90]) {
    const p = offset([lat, 0], 50, 50);
    assert.ok(p.every(Number.isFinite));
    near(distance([lat, 0], p), Math.sqrt(5000), 0.001);
  }
});

test("JSON and single-segment GPX import match the CLI's formats", () => {
  assert.deepEqual(parseRoute(JSON.stringify(options.points)), options.points);
  assert.deepEqual(parseRoute(JSON.stringify({ points: options.points })), options.points);
  assert.deepEqual(parseRoute('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg><trkpt lat="0" lon="0"/><trkpt lat="0" lon="0.01"/></trkseg></trk></gpx>'), options.points);
  assert.deepEqual(parseRoute('<g:gpx xmlns:g="urn:test"><g:rte><g:rtept lat="0" lon="0"/><g:rtept lat="0" lon="0.01"/></g:rte></g:gpx>'), options.points);
  // Comments, empty segments, either quote and either attribute order.
  assert.deepEqual(parseRoute(`<?xml version="1.0"?>
    <gpx><!-- <trkseg><trkpt lat="9" lon="9"/></trkseg> --><trk><name>a &amp; b</name><trkseg/>
    <trkseg><trkpt lon='0' lat='0'><ele>3</ele></trkpt><trkpt lon="0.01" lat="0"></trkpt></trkseg></trk></gpx>`), options.points);
});

test("GPX rejects DTD, missing coordinates, multi-segment and malformed documents", () => {
  for (const text of [
    '<!DOCTYPE gpx [<!ENTITY x "value">]><gpx/>',
    '<gpx><rte><rtept lon="0"/><rtept lat="1" lon="1"/></rte></gpx>',
    '<gpx><rte><rtept lat="" lon="0"/><rtept lat="1" lon="1"/></rte></gpx>',
    '<gpx><rte><rtept lat="0" lon="0"/></rte><rte><rtept lat="1" lon="1"/></rte></gpx>',
    '<gpx><rte><rtept lat="north" lon="0"/><rtept lat="1" lon="1"/></rte></gpx>',
    '<gpx><rte></gpx>',
    '<gpx><wpt lat="0" lon="0"/></gpx>',
    "not a route",
    JSON.stringify(Array.from({ length: 10001 }, (_, i) => [0, i / 1000])),
    " ".repeat(4 * 1024 * 1024 + 1),
  ]) assert.throws(() => parseRoute(text), Error, text.slice(0, 60));
});

test("route requests are validated before they replace anything, with the CLI's defaults", () => {
  for (const invalid of [{ speed: 0 }, { speed: Infinity }, { speed: "5" }, { speedNoise: 101 }, { positionNoise: -1 }, { loop: "yes" }]) {
    assert.throws(() => checkRoute({ ...options, ...invalid }), Error, JSON.stringify(invalid));
  }
  assert.throws(() => checkRoute({ ...options, points: [[0, 0], [0, 180]] }));
  assert.throws(() => checkRoute({ ...options, points: [[0, 0], [true, 1]] }), /第 2 个点/);
  assert.deepEqual(checkRoute({ points: options.points }), options);
});

test("the mainland side of the Hong Kong and Macau borders is converted, and round-trips", () => {
  // Public landmarks from OpenStreetMap.
  for (const [latitude, longitude] of [
    [22.4883612, 113.9104918], // Sea World
    [22.4797136, 113.9073872], // Shekou port
    [22.2185803, 113.5489252], // Gongbei port
    [22.1405453, 113.5429628], // Hengqin port
    [22.532, 114.1135], // Luohu port
    [22.5463725, 114.0545223], // Shenzhen civic centre
  ]) {
    const p = { latitude, longitude }, map = toMap(p);
    const moved = distance([latitude, longitude], [map.latitude, map.longitude]);
    assert.ok(moved > 500 && moved < 700, `${latitude}, ${longitude}: ${moved} m`);
    const gps = toGPS(map);
    near(gps.latitude, latitude, 1e-8);
    near(gps.longitude, longitude, 1e-8);
  }
  for (const [latitude, longitude] of [
    [22.2853006, 114.1592777], // IFC
    [22.501, 114.128], // Sheung Shui, north of the old rectangle
    [22.197457, 113.5408602], // St Paul's
    [22.148, 113.564], // Cotai
  ]) {
    const p = { latitude, longitude };
    assert.deepEqual(toMap(p), p);
    assert.deepEqual(toGPS(p), p);
  }
});

test("Apple map coordinates round-trip, with Hong Kong and Macau unchanged", () => {
  const beijing = { latitude: 39.9, longitude: 116.4 };
  const map = toMap(beijing);
  assert.ok(map.longitude !== beijing.longitude);
  const gps = toGPS(map);
  near(gps.latitude, beijing.latitude, 1e-8);
  near(gps.longitude, beijing.longitude, 1e-8);
  for (const p of [{ latitude: 22.28, longitude: 114.15 }, { latitude: 22.19, longitude: 113.54 }, { latitude: 51.5, longitude: -0.1 }]) {
    assert.deepEqual(toMap(p), p);
    assert.deepEqual(toGPS(p), p);
  }
});
