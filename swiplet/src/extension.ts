import { info, pasteboard, setPasteboard, simulatedLocation } from "@swipt/api/device";
import { defineAction, defineExtension, type Schema } from "@swipt/api/swiplet";
import { openDatabaseSync } from "expo-sqlite";
import { probeConnection, serve, settle, type Probe, type SetLocation } from "./location";
import { checkPoint, checkRoute, MAX_POINTS, type Point, type RouteOptions } from "./route";
import { createStore, DATABASE, type Plan, type Store } from "./store";

let opened: Store | undefined;
// Opened on first use: the build evaluates this module where there is no database.
const store = () => (opened ??= createStore(openDatabaseSync(DATABASE, { enableChangeListener: true })));
const set: SetLocation = (point, signal) => simulatedLocation.set(point, { signal });

// Swipt validates action input with the Standard Schema check and shows the
// JSON Schema to Shortcuts.
function schema<Input, Output = Input>(check: (value: unknown) => Output, json: Record<string, unknown>): Schema<Input, Output> {
  return {
    "~standard": {
      version: 1,
      vendor: "simlocation",
      validate(value) {
        try {
          return { value: check(value) };
        } catch (error) {
          return { issues: [{ message: (error as Error).message }] };
        }
      },
      jsonSchema: { input: () => json, output: () => json },
    },
  };
}

const latitude = { type: "number", minimum: -90, maximum: 90 };
const longitude = { type: "number", minimum: -180, maximum: 180 };
const point = schema<Point>(checkPoint, {
  type: "object",
  properties: { latitude, longitude },
  required: ["latitude", "longitude"],
});
const route = schema<Pick<RouteOptions, "points"> & Partial<RouteOptions>, RouteOptions>(checkRoute, {
  type: "object",
  properties: {
    points: {
      type: "array", minItems: 2, maxItems: MAX_POINTS,
      items: { type: "array", prefixItems: [latitude, longitude], minItems: 2, maxItems: 2 },
    },
    speed: { type: "number", exclusiveMinimum: 0, maximum: 1000, default: 5, description: "km/h" },
    loop: { type: "boolean", default: false },
    speedNoise: { type: "number", minimum: 0, maximum: 100, default: 0, description: "±%" },
    positionNoise: { type: "number", minimum: 0, maximum: 100, default: 0, description: "m" },
  },
  required: ["points"],
});
const probe = schema<Probe>((value) => value as Probe, {
  type: "object",
  properties: { connected: { type: "boolean" }, error: { type: ["string", "null"] } },
  required: ["connected", "error"],
});
const text = schema<string>((value) => {
  if (typeof value !== "string") throw new Error("需要一段文本。");
  return value;
}, { type: "string" });

// Point and clear take effect right away, without the service: Swipt keeps
// the location and sets it again after a reconnect or its own restart.
async function apply(plan: Exclude<Plan, { mode: "route" }>, signal: AbortSignal) {
  const id = store().submit(plan);
  await settle({ id, created: Date.now(), plan }, () => store().latest(), set, signal);
}

const shortcuts = ["shortcuts"] as const;

export default defineExtension({
  run: ({ signal }) =>
    serve({
      latestId: () => store().latestId(),
      latest: () => store().latest(),
      set,
      now: Date.now,
      sleep: (ms, signal) => sleep(ms, { signal }),
    }, signal),
  actions: {
    // The SimLocationCellular shortcut calls this as simlocation/probeConnection.
    probeConnection: defineAction({
      title: "Check Cellular Connection",
      expose: shortcuts,
      output: probe,
      run: (_, { signal }) => probeConnection((signal) => info({ signal }), signal),
    }),
    setPoint: defineAction({
      title: "Set Simulated Location",
      input: point,
      expose: shortcuts,
      run: (point, { signal }) => apply({ mode: "point", point }, signal),
    }),
    startRoute: defineAction({
      title: "Play SimLocation Route",
      description: "Plays while the SimLocation service is on.",
      input: route,
      expose: shortcuts,
      run(route) {
        store().submit({ mode: "route", route, seed: Math.floor(Math.random() * 2 ** 32) });
      },
    }),
    clear: defineAction({
      title: "Restore Real Location",
      expose: shortcuts,
      run: (_, { signal }) => apply({ mode: "clear" }, signal),
    }),
    // The interface has no pasteboard of its own.
    readClipboard: defineAction({
      title: "Read Clipboard",
      run: (_, { signal }) => pasteboard({ signal }),
    }),
    writeClipboard: defineAction({
      title: "Copy to Clipboard",
      input: text,
      run: (value, { signal }) => setPasteboard(value, { signal }),
    }),
  },
});
