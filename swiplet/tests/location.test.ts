import test from "node:test";
import assert from "node:assert/strict";
import { probeConnection, serve, settle, type ServiceIO } from "../src/location";
import { distance, pairOf, type Point, type RouteOptions } from "../src/route";
import type { Plan, Request } from "../src/store";

// 10 m/s along about 1112 m: done after 112 one-second updates.
const route: RouteOptions = { points: [[0, 0], [0, 0.01]], speed: 36, loop: false, speedNoise: 0, positionNoise: 0 };
const playing: Request = { id: 1, created: 0, plan: { mode: "route", route, seed: 1 } };
const A = { latitude: 1, longitude: 2 };
const near = (a: number, b: number, tolerance: number) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);

// Runs the service on a fake clock until its `ticks`-th sleep, returning every
// location it set. `between` may submit requests while the service sleeps.
async function service(initial: Request[], ticks: number, between?: (tick: number, submit: (plan: Plan) => void) => void) {
  const rows = [...initial];
  const sets: (Point | null)[] = [];
  const controller = new AbortController();
  let clock = 0, tick = 0;
  const io: ServiceIO = {
    latestId: () => rows.at(-1)?.id ?? 0,
    latest: () => rows.at(-1) ?? null,
    async set(point) {
      sets.push(point);
    },
    now: () => clock,
    async sleep(ms) {
      clock += ms;
      tick++;
      between?.(tick, (plan) => rows.push({ id: (rows.at(-1)?.id ?? 0) + 1, created: clock, plan }));
      if (tick >= ticks) controller.abort();
      controller.signal.throwIfAborted();
    },
  };
  await assert.rejects(serve(io, controller.signal), { name: "AbortError" });
  return sets;
}

test("a route plays by wall time and then holds its exact end", async () => {
  const sets = await service([playing], 200);
  assert.equal(sets.length, 113);
  near(distance([0, 0], pairOf(sets[50]!)), 500, 0.01);
  assert.deepEqual(sets.at(-1), { latitude: 0, longitude: 0.01 });
  assert.notDeepEqual(sets.at(-2), sets.at(-1));
});

test("a restarted service picks the route up where wall time puts it", async () => {
  const [first] = await service([{ ...playing, created: -50_000 }], 1);
  near(distance([0, 0], pairOf(first!)), 500, 0.01);
});

test("a point or clear found at startup is left to Swipt, which restores it", async () => {
  for (const plan of [{ mode: "point", point: A }, { mode: "clear" }] satisfies Plan[]) {
    assert.deepEqual(await service([{ id: 1, created: 0, plan }], 3), []);
  }
});

test("a point that ends a route is set again after the route's last update", async () => {
  const sets = await service([playing], 6, (tick, submit) => {
    if (tick === 3) submit({ mode: "point", point: A });
  });
  assert.equal(sets.length, 4);
  assert.deepEqual(sets.at(-1), A);
});

test("a newer route starts from its own beginning", async () => {
  const other: RouteOptions = { ...route, points: [[10, 10], [10, 10.01]] };
  const sets = await service([playing], 4, (tick, submit) => {
    if (tick === 2) submit({ mode: "route", route: other, seed: 2 });
  });
  assert.deepEqual(pairOf(sets[2]!), [10, 10]);
  assert.ok(distance([10, 10], pairOf(sets[3]!)) > 9);
});

test("a failed set call ends the service, for Swipt to restart once the phone is back", async () => {
  const failure = Object.assign(new Error("the device is unavailable"), { name: "UnavailableError" });
  const io: ServiceIO = {
    latestId: () => 1,
    latest: () => playing,
    set: () => Promise.reject(failure),
    now: () => 0,
    sleep: async () => {},
  };
  await assert.rejects(serve(io, new AbortController().signal), failure);
});

test("the newest point or clear wins when set calls finish out of order", async () => {
  const rows: Request[] = [{ id: 1, created: 0, plan: { mode: "point", point: A } }];
  const sets: (Point | null)[] = [];
  await settle(rows[0], () => rows.at(-1)!, async (point) => {
    sets.push(point);
    if (sets.length === 1) rows.push({ id: 2, created: 0, plan: { mode: "clear" } });
  }, new AbortController().signal);
  assert.deepEqual(sets, [A, null]);
});

test("a route that arrives meanwhile is left to the service", async () => {
  const rows: Request[] = [{ id: 1, created: 0, plan: { mode: "clear" } }];
  const sets: (Point | null)[] = [];
  await settle(rows[0], () => rows.at(-1)!, async (point) => {
    sets.push(point);
    rows.push({ ...playing, id: 2 });
  }, new AbortController().signal);
  assert.deepEqual(sets, [null]);
});

test("a successful probe reports the connection without touching the location", async () => {
  let calls = 0;
  const result = await probeConnection(async () => calls++, new AbortController().signal);
  assert.deepEqual(result, { connected: true, error: null });
  assert.equal(calls, 1);
});

test("a failed probe is a result, so the shortcut goes on to restore cellular data", async () => {
  const result = await probeConnection(() => Promise.reject(new Error("offline")), new AbortController().signal, 10);
  assert.deepEqual(result, { connected: false, error: "offline" });
});

test("a call that never answers cannot hold the probe past its budget", async () => {
  const result = await probeConnection(() => new Promise(() => {}), new AbortController().signal, 10);
  assert.equal(result.connected, false);
});

test("a probe cancelled beforehand makes no device call", async () => {
  const controller = new AbortController();
  controller.abort();
  let called = false;
  const result = await probeConnection(async () => {
    called = true;
  }, controller.signal);
  assert.equal(called, false);
  assert.equal(result.connected, false);
});
