// What the extension does with the phone, kept apart from the Swipt modules so
// it can be tested with fakes.
import { Playback, type Point } from "./route";
import type { Request } from "./store";

export type SetLocation = (point: Point | null, signal: AbortSignal) => Promise<void>;

/**
 * Sets what a point or clear request asks for, then any newer point or clear
 * request: set calls from different runtimes can finish out of order, and the
 * newest request is the one Swipt has to keep.
 */
export async function settle(request: Request, latest: () => Request | null, set: SetLocation, signal: AbortSignal) {
  for (let current: Request | null = request; current && current.plan.mode !== "route";) {
    await set(current.plan.mode === "point" ? current.plan.point : null, signal);
    const newest = latest();
    current = newest && newest.id !== current.id ? newest : null;
  }
}

export interface ServiceIO {
  latestId(): number;
  latest(): Request | null;
  set: SetLocation;
  now(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
}

/**
 * The service. While the newest request is a route it sets one position a
 * second, placed by the wall time since the route started, so a slow call or a
 * restart jumps ahead instead of falling behind. A failed set call ends the
 * service: Swipt starts it again once the phone is back.
 */
export async function serve(io: ServiceIO, signal: AbortSignal): Promise<never> {
  let seen = -1;
  let route: { playback: Playback; started: number; done: boolean } | undefined;
  for (;;) {
    if (io.latestId() !== seen) {
      const request = io.latest();
      seen = request?.id ?? 0;
      if (request?.plan.mode === "route") {
        route = { playback: new Playback(request.plan.route, request.plan.seed), started: request.created, done: false };
      } else {
        // Point and clear requests set the location themselves. One that ends
        // a route is set again here, in case our last update landed after it.
        // One found at startup is left alone: Swipt restores the location.
        if (route && request) await settle(request, io.latest, io.set, signal);
        route = undefined;
        continue;
      }
    }
    if (route && !route.done) {
      const sample = route.playback.sample((io.now() - route.started) / 1000);
      await io.set(sample.point, signal);
      route.done = sample.completed;
    }
    await io.sleep(1000, signal);
  }
}

export interface Probe { connected: boolean; error: string | null }

/**
 * Whether Swipt reaches the phone within `budget` ms, retrying every 500 ms.
 * A failure is a result rather than an error, so the shortcut that turned
 * cellular data off goes on to turn it back on.
 */
export async function probeConnection(
  info: (signal: AbortSignal) => Promise<unknown>,
  signal: AbortSignal,
  budget = 20_000,
): Promise<Probe> {
  const deadline = AbortSignal.any([signal, AbortSignal.timeout(budget)]);
  // Also bounds a call that ignores its signal.
  const ended = new Promise<never>((_, reject) =>
    deadline.addEventListener("abort", () => reject(deadline.reason), { once: true }));
  ended.catch(() => {});
  let error = "连接检测超时或已取消。";
  while (!deadline.aborted) {
    try {
      await Promise.race([info(deadline), ended]);
      return { connected: true, error: null };
    } catch (reason) {
      if (!deadline.aborted) error = reason instanceof Error ? reason.message : String(reason);
    }
    await Promise.race([new Promise((resolve) => setTimeout(resolve, 500)), ended]).catch(() => {});
  }
  return { connected: false, error };
}

/**
 * probeConnection, then `task` once Swipt reaches the phone, each within
 * `budget` ms. A failed task is a result too: a shortcut runs this while
 * cellular data is off and still has to turn it back on.
 */
export async function whenConnected(
  info: (signal: AbortSignal) => Promise<unknown>,
  task: (signal: AbortSignal) => Promise<void>,
  signal: AbortSignal,
  budget = 20_000,
): Promise<Probe> {
  const probe = await probeConnection(info, signal, budget);
  if (!probe.connected) return probe;
  const deadline = AbortSignal.any([signal, AbortSignal.timeout(budget)]);
  let onAbort: () => void = () => {};
  try {
    deadline.throwIfAborted();
    const ended = new Promise<never>((_, reject) => {
      onAbort = () => reject(deadline.reason);
      deadline.addEventListener("abort", onAbort, { once: true });
    });
    await Promise.race([Promise.resolve().then(() => {
      deadline.throwIfAborted();
      return task(deadline);
    }), ended]);
    return probe;
  } catch (reason) {
    return { connected: true, error: reason instanceof Error ? reason.message : String(reason) };
  } finally {
    deadline.removeEventListener("abort", onAbort);
  }
}
