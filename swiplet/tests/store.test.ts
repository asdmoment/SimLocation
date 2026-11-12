import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createStore, MAX_FAVORITES } from "../src/store";
import { open } from "./sqlite";

// Each call opens another connection to one temporary database, the way the
// interface, an action and the service each open their own.
function database(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "simlocation-"));
  const connections: ReturnType<typeof open>[] = [];
  t.after(() => {
    for (const connection of connections) connection.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return () => {
    const connection = open(join(directory, "simlocation.db"));
    connections.push(connection);
    return connection;
  };
}

const item = (n: number) => ({ id: n.toString(16).padStart(12, "0"), name: `P${n}`, lat: 0, lon: n / 1000 });

test("requests are shared between runtimes, and only the newest is kept", (t) => {
  const connect = database(t);
  const raw = connect();
  const a = createStore(raw), b = createStore(connect());
  assert.equal(b.latestId(), 0);
  assert.equal(b.latest(), null);
  const point = a.submit({ mode: "point", point: { latitude: 1, longitude: 2 } });
  assert.equal(b.latestId(), point);
  assert.deepEqual(b.latest()?.plan, { mode: "point", point: { latitude: 1, longitude: 2 } });
  const clear = b.submit({ mode: "clear" });
  assert.ok(clear > point);
  assert.equal(a.latest()?.plan.mode, "clear");
  assert.equal(raw.getFirstSync<{ n: number }>("SELECT count(*) AS n FROM requests")?.n, 1);
});

test("the draft the first version saved still loads", (t) => {
  const raw = database(t)();
  const store = createStore(raw);
  assert.deepEqual(store.draft(), { points: [], speed: "5", speedNoise: "0", positionNoise: "0", loop: false });
  raw.runSync("INSERT INTO state (key, value) VALUES ('draft', ?)", JSON.stringify({
    coordinates: "0, 1", points: [[0, 0], [0, 1], ["x", 1]], speed: "6", speedNoise: "10", positionNoise: "0",
    loop: true, convertMap: true,
  }));
  assert.deepEqual(store.draft(), { points: [[0, 0], [0, 1]], speed: "6", speedNoise: "10", positionNoise: "0", loop: true });
  store.saveDraft({ ...store.draft(), speed: "7" });
  assert.equal(store.draft().speed, "7");
});

test("favorites move to and from the web console's favorites.json", (t) => {
  const connect = database(t);
  const a = createStore(connect()), b = createStore(connect());
  const file = { version: 1, items: [{ id: "0123456789ab", name: "  Home   base ", lat: 22.28, lon: 114.15 }] };
  assert.equal(a.importFavorites(JSON.stringify(file)), 1);
  assert.deepEqual(b.favorites(), [{ id: "0123456789ab", name: "Home base", lat: 22.28, lon: 114.15 }]);
  assert.deepEqual(JSON.parse(b.exportFavorites()), { version: 1, items: b.favorites() });
  b.addFavorite("Office", { latitude: 1.123456789, longitude: 2 });
  const office = a.favorites().find((f) => f.name === "Office")!;
  assert.match(office.id, /^[0-9a-f]{12}$/);
  assert.equal(office.lat, 1.1234568);
  assert.throws(() => a.addFavorite("Again", { latitude: 1.1234568, longitude: 2 }), /已收藏为「Office」/);
  assert.throws(() => a.addFavorite("  ", { latitude: 5, longitude: 5 }), /名称/);
  assert.throws(() => a.addFavorite("x".repeat(61), { latitude: 5, longitude: 5 }), /60/);
  a.removeFavorite(office.id);
  assert.equal(b.favorites().length, 1);
});

test("a shortcut names the favorite it wants", (t) => {
  const store = createStore(database(t)());
  store.importFavorites(JSON.stringify([{ ...item(1), name: "默认位置" }, { ...item(2), name: "Twin" }, { ...item(3), name: "Twin" }]));
  assert.deepEqual(store.favoritePoint(" 默认位置  "), { latitude: 0, longitude: 0.001 });
  assert.throws(() => store.favoritePoint("Nowhere"), /没有名为「Nowhere」的收藏/);
  assert.throws(() => store.favoritePoint("Twin"), /2 个收藏都叫「Twin」/);
});

test("a bad item or the limit rolls the whole import back", (t) => {
  const store = createStore(database(t)());
  assert.throws(() => store.importFavorites(JSON.stringify({ version: 1, items: [item(1), { ...item(2), lat: 100 }] })));
  assert.throws(() => store.importFavorites("{}"));
  assert.equal(store.favorites().length, 0);
  store.importFavorites(JSON.stringify({ items: Array.from({ length: MAX_FAVORITES }, (_, n) => item(n)) }));
  assert.throws(() => store.importFavorites(JSON.stringify([item(MAX_FAVORITES), item(MAX_FAVORITES + 1)])), /最多/);
  assert.equal(store.favorites().length, MAX_FAVORITES);
  store.importFavorites(JSON.stringify([{ ...item(0), name: "renamed" }])); // same id: merged, not added
  assert.equal(store.favorites().length, MAX_FAVORITES);
  assert.ok(store.favorites().some((f) => f.name === "renamed"));
});

test("favorites saved by the first version get ids the web console accepts", (t) => {
  const raw = database(t)();
  raw.execSync("CREATE TABLE favorites (id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL)");
  raw.runSync("INSERT INTO favorites VALUES (?, ?, ?, ?)", "1762543200000-k3", "Old", 1, 2);
  raw.runSync("INSERT INTO favorites VALUES (?, ?, ?, ?)", "00000000000a", "Kept", 3, 4);
  const favorites = createStore(raw).favorites();
  assert.match(favorites.find((f) => f.name === "Old")!.id, /^[0-9a-f]{12}$/);
  assert.equal(favorites.find((f) => f.name === "Kept")!.id, "00000000000a");
});
