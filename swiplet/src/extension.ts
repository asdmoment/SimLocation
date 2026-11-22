import { info, pasteboard, setPasteboard, simulatedLocation } from "@swipt/api/device";
import { defineAction, defineExtension, type Schema } from "@swipt/api/swiplet";
import { openDatabaseSync } from "expo-sqlite";
import { apply, probeConnection, serve, whenConnected, type Probe, type SetLocation } from "./location";
import { checkPoint, checkRoute, MAX_POINTS, type Point, type RouteOptions } from "./route";
import { createStore, DATABASE, type Plan, type Store } from "./store";
import { runLocationShortcut, type ShortcutResult } from "./shortcut";

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
const favorite = schema<{ name: string }>((value) => {
  const name = (value as { name?: unknown } | null)?.name;
  if (typeof name !== "string" || !name.trim()) throw new Error('需要 {"name": "收藏名称"}。');
  return { name };
}, { type: "object", properties: { name: { type: "string" } }, required: ["name"] });
const shortcutInput = schema<{ mode: "favorite" | "clear"; name?: string; reconnect?: boolean }>((value) => {
  const input = value as { mode?: unknown; name?: unknown; reconnect?: unknown } | null;
  if (input?.mode !== "favorite" && input?.mode !== "clear") throw new Error("mode 必须是 favorite 或 clear。");
  if (input.mode === "favorite" && (typeof input.name !== "string" || !input.name.trim())) throw new Error("请填写收藏名称。");
  if (input.reconnect !== undefined && typeof input.reconnect !== "boolean") throw new Error("reconnect 必须是布尔值。");
  return { mode: input.mode, name: typeof input.name === "string" ? input.name : undefined, reconnect: input.reconnect === true };
}, {
  type: "object", properties: { mode: { enum: ["favorite", "clear"] }, name: { type: "string" }, reconnect: { type: "boolean" } }, required: ["mode"],
});
const shortcutOutput = schema<ShortcutResult>((value) => value as ShortcutResult, {
  type: "object", properties: { status: { enum: ["ok", "retry", "error"] }, message: { type: "string" } }, required: ["status", "message"],
});
const text = schema<string>((value) => {
  if (typeof value !== "string") throw new Error("需要一段文本。");
  return value;
}, { type: "string" });

// Point and clear take effect right away, without the service: Swipt keeps
// the location and sets it again after a reconnect or its own restart.
const applyPlan = (plan: Exclude<Plan, { mode: "route" }>, signal: AbortSignal) =>
  apply(plan, {
    submit: (plan) => store().submit(plan),
    latest: () => store().latest(),
    set,
    settled: () => store().markSettled(),
  }, signal);

const shortcuts = ["shortcuts"] as const;

export default defineExtension({
  run: ({ signal }) =>
    serve({
      latestId: () => store().latestId(),
      latest: () => store().latest(),
      set,
      settled: () => store().markSettled(),
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
      run: (point, { signal }) => applyPlan({ mode: "point", point }, signal),
    }),
    // SimLocationCellular with this in place of each Check Cellular Connection
    // goes straight to a favorite on cellular.
    setFavorite: defineAction({
      title: "Simulate Favorite",
      description: "Waits up to 20 seconds for the phone, then simulates the favorite with this name.",
      input: favorite,
      output: probe,
      expose: shortcuts,
      run: ({ name }, { signal }) =>
        whenConnected(
          (signal) => info({ signal }),
          (signal) => applyPlan({ mode: "point", point: store().favoritePoint(name) }, signal),
          signal,
        ),
    }),
    locationShortcut: defineAction({
      title: "Apply SimLocation Shortcut",
      description: "Sets a favorite or restores real location using the current Wi-Fi or cellular connection; returns a status and a message.",
      input: shortcutInput,
      output: shortcutOutput,
      expose: shortcuts,
      async run({ mode, name, reconnect }, { signal }): Promise<ShortcutResult> {
        // Invalid favorites must not trigger a cellular-data cycle.
        let plan: Exclude<Plan, { mode: "route" }>;
        try {
          plan = mode === "clear" ? { mode: "clear" } : { mode: "point", point: store().favoritePoint(name!) };
        } catch (reason) {
          return { status: "error", message: `操作未完成：${reason instanceof Error ? reason.message : String(reason)}` };
        }
        return runLocationShortcut(
          (signal) => info({ signal }),
          (signal) => applyPlan(plan, signal),
          signal,
          mode === "clear" ? "已恢复真实定位。" : `已设置${name === "默认位置" ? "默认位置" : `收藏「${name}」`}。`,
          reconnect ? 20_000 : 4_000,
        );
      },
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
      run: (_, { signal }) => applyPlan({ mode: "clear" }, signal),
    }),
    // The interface cannot import the device module. Not for Shortcuts.
    heldLocation: defineAction({
      title: "Read Simulated Location",
      run: (_, { signal }) => simulatedLocation.get({ signal }),
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
