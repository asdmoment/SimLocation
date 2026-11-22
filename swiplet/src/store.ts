// The database the interface, the actions and the service share. Swipt sends
// writes from a connection opened with enableChangeListener to the interface.
import { checkPoint, distance, isPair, MAX_POINTS, pairOf, pointOf, type Pair, type Point, type RouteOptions } from "./route";

export const DATABASE = "simlocation.db";
export const MAX_FAVORITES = 200; // the web console's limits, so favorites.json moves both ways
export const MAX_RECENT = 10;
const MAX_NAME = 60;

export type Plan =
  | { mode: "clear" }
  | { mode: "point"; point: Point }
  | { mode: "route"; route: RouteOptions; seed: number };
// Only the newest request is kept. For a route, `created` is when it started.
export interface Request { id: number; created: number; plan: Plan }
export interface Favorite { id: string; name: string; lat: number; lon: number }
export interface Draft { points: Pair[]; speed: string; speedNoise: string; positionNoise: string; loop: boolean }

type Value = string | number | null;
// The part of expo-sqlite's SQLiteDatabase used here.
export interface Database {
  execSync(source: string): void;
  runSync(source: string, ...params: Value[]): { lastInsertRowId: number };
  getFirstSync<T>(source: string, ...params: Value[]): T | null;
  getAllSync<T>(source: string, ...params: Value[]): T[];
  withTransactionSync(task: () => void): void;
}

const MALFORMED = "收藏应为 SimLocation 的 favorites.json。";
// Names are saved, and looked up, with runs of whitespace collapsed.
const cleanName = (name: unknown) => (typeof name === "string" ? name.split(/\s+/).filter(Boolean).join(" ") : "");

export function checkFavorite(value: unknown): Favorite {
  const { id, name, lat, lon } = (value ?? {}) as Record<string, unknown>;
  if (typeof id !== "string" || !/^[0-9a-f]{12}$/.test(id)) throw new Error(MALFORMED);
  const clean = cleanName(name);
  if (!clean) throw new Error("请填写收藏名称。");
  if (clean.length > MAX_NAME) throw new Error(`名称最多 ${MAX_NAME} 个字符。`);
  if (/[\u0000-\u001f\u007f]/.test(clean)) throw new Error("名称包含不可显示的字符。");
  const point = checkPoint({ latitude: lat, longitude: lon });
  return { id, name: clean, lat: point.latitude, lon: point.longitude };
}

export const favoriteId = () => Array.from({ length: 12 }, () => Math.floor(Math.random() * 16).toString(16)).join("");

export function createStore(db: Database) {
  db.execSync(`
    CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL, plan TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS favorites (id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
    -- The first version made other ids, which the web console skips.
    UPDATE favorites SET id = lower(hex(randomblob(6))) WHERE length(id) != 12 OR id GLOB '*[^0-9a-f]*';
  `);
  const read = (key: string): unknown => {
    const row = db.getFirstSync<{ value: string }>("SELECT value FROM state WHERE key = ?", key);
    return row && JSON.parse(row.value);
  };
  const favorites = () => db.getAllSync<Favorite>("SELECT id, name, lat, lon FROM favorites ORDER BY name, id");
  const recent = (): Point[] => {
    const saved = read("recent");
    return Array.isArray(saved) ? saved.filter(isPair).slice(0, MAX_RECENT).map(pointOf) : [];
  };
  const saveFavorite = (value: unknown) => {
    const item = checkFavorite(value);
    const exists = db.getFirstSync("SELECT 1 FROM favorites WHERE id = ?", item.id);
    const count = db.getFirstSync<{ n: number }>("SELECT count(*) AS n FROM favorites")?.n ?? 0;
    if (!exists && count >= MAX_FAVORITES) throw new Error(`收藏最多 ${MAX_FAVORITES} 个，请先删除不用的。`);
    db.runSync("INSERT OR REPLACE INTO favorites (id, name, lat, lon) VALUES (?, ?, ?, ?)", item.id, item.name, item.lat, item.lon);
  };

  return {
    submit(plan: Plan): number {
      const { lastInsertRowId: id } = db.runSync("INSERT INTO requests (created, plan) VALUES (?, ?)", Date.now(), JSON.stringify(plan));
      db.runSync("DELETE FROM requests WHERE id < ?", id);
      return id;
    },
    // Cheap enough for the service to check every second.
    latestId: () => db.getFirstSync<{ id: number | null }>("SELECT max(id) AS id FROM requests")?.id ?? 0,
    latest(): Request | null {
      const row = db.getFirstSync<{ id: number; created: number; plan: string }>(
        "SELECT id, created, plan FROM requests ORDER BY id DESC LIMIT 1");
      return row && { id: row.id, created: row.created, plan: JSON.parse(row.plan) as Plan };
    },

    // Also reads the draft the first version saved, which had extra fields.
    draft(): Draft {
      const saved = (read("draft") ?? {}) as Record<string, unknown>;
      const text = (value: unknown, fallback: string) =>
        typeof value === "string" || typeof value === "number" ? String(value) : fallback;
      return {
        points: Array.isArray(saved.points) ? saved.points.filter(isPair).slice(0, MAX_POINTS) : [],
        speed: text(saved.speed, "5"),
        speedNoise: text(saved.speedNoise, "0"),
        positionNoise: text(saved.positionNoise, "0"),
        loop: saved.loop === true,
      };
    },
    saveDraft(draft: Draft) {
      db.runSync("INSERT OR REPLACE INTO state (key, value) VALUES ('draft', ?)", JSON.stringify(draft));
    },

    // Changes whenever a device call ends, for the interface to read the location again.
    markSettled() {
      db.runSync("INSERT OR REPLACE INTO state (key, value) VALUES ('settled', ?)", JSON.stringify(favoriteId()));
    },
    settled: () => read("settled"),

    // Points that were set successfully, the newest first.
    recent,
    recordRecent(point: Point) {
      const others = recent().filter((p) => distance(pairOf(p), pairOf(point)) >= 1);
      const saved = [point, ...others].slice(0, MAX_RECENT).map(pairOf);
      db.runSync("INSERT OR REPLACE INTO state (key, value) VALUES ('recent', ?)", JSON.stringify(saved));
    },

    satellite: () => read("satellite") === true,
    saveSatellite(on: boolean) {
      db.runSync("INSERT OR REPLACE INTO state (key, value) VALUES ('satellite', ?)", JSON.stringify(on));
    },

    favorites,
    addFavorite(name: string, point: Point) {
      const same = favorites().find((f) => Math.abs(f.lat - point.latitude) < 1e-6 && Math.abs(f.lon - point.longitude) < 1e-6);
      if (same) throw new Error(`这个位置已收藏为「${same.name}」。`);
      saveFavorite({ id: favoriteId(), name, lat: Number(point.latitude.toFixed(7)), lon: Number(point.longitude.toFixed(7)) });
    },
    removeFavorite(id: string) {
      db.runSync("DELETE FROM favorites WHERE id = ?", id);
    },
    // Shortcuts name a favorite, so a name two favorites share is refused.
    favoritePoint(name: string): Point {
      const clean = cleanName(name);
      const found = db.getAllSync<Favorite>("SELECT id, name, lat, lon FROM favorites WHERE name = ?", clean);
      if (found.length === 0) throw new Error(`没有名为「${clean}」的收藏。`);
      if (found.length > 1) throw new Error(`有 ${found.length} 个收藏都叫「${clean}」，删掉多余的再试。`);
      return { latitude: found[0].lat, longitude: found[0].lon };
    },
    // Merges by id; one bad item rolls the whole import back.
    importFavorites(text: string): number {
      let data: unknown;
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(MALFORMED);
      }
      const items = Array.isArray(data) ? data : (data as { items?: unknown } | null)?.items;
      if (!Array.isArray(items)) throw new Error(MALFORMED);
      db.withTransactionSync(() => items.forEach(saveFavorite));
      return items.length;
    },
    exportFavorites: () => JSON.stringify({ version: 1, items: favorites() }, null, 2),
  };
}

export type Store = ReturnType<typeof createStore>;
