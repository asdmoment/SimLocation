// Route playback ported from bin/simlocation.py: WGS-84 waypoints, great-circle
// interpolation, smooth speed and position noise, and the route file formats.
export const MAX_POINTS = 10_000;
export const MAX_BYTES = 4 * 1024 * 1024;
export const EARTH_RADIUS = 6_371_008.8;
const rad = Math.PI / 180;

export interface Point { latitude: number; longitude: number }
export type Pair = [latitude: number, longitude: number];
export interface RouteOptions {
  points: Pair[];
  speed: number; // km/h
  loop: boolean;
  speedNoise: number; // ±% of speed
  positionNoise: number; // meters
}

// typeof rules out booleans and strings; the comparisons rule out NaN and Infinity.
const within = (value: unknown, min: number, max: number): value is number =>
  typeof value === "number" && value >= min && value <= max;

export const isPair = (value: unknown): value is Pair =>
  Array.isArray(value) && value.length === 2 && within(value[0], -90, 90) && within(value[1], -180, 180);
export const pairOf = (p: Point): Pair => [p.latitude, p.longitude];
export const pointOf = ([latitude, longitude]: Pair): Point => ({ latitude, longitude });

export function checkPoint(value: unknown): Point {
  const { latitude, longitude } = (value ?? {}) as Record<string, unknown>;
  if (!within(latitude, -90, 90) || !within(longitude, -180, 180)) {
    throw new Error("坐标应为纬度 -90 到 90、经度 -180 到 180 的数字。");
  }
  return { latitude, longitude };
}

export function checkPoints(value: unknown): Pair[] {
  if (!Array.isArray(value) || value.length < 2 || value.length > MAX_POINTS) {
    throw new Error(`路线需要 2 到 ${MAX_POINTS} 个点。`);
  }
  return value.map((p, index) => {
    if (!isPair(p)) throw new Error(`第 ${index + 1} 个点应为 [纬度, 经度]。`);
    return [p[0], p[1]];
  });
}

// Speed and noise default to the CLI's: 5 km/h, no noise.
export function checkRoute(value: unknown): RouteOptions {
  const { points, speed = 5, loop = false, speedNoise = 0, positionNoise = 0 } =
    (value ?? {}) as Record<string, unknown>;
  if (!within(speed, 0, 1000) || speed === 0) throw new Error("速度应大于 0、不超过 1000 km/h。");
  if (typeof loop !== "boolean") throw new Error("循环应为 true 或 false。");
  if (!within(speedNoise, 0, 100)) throw new Error("速度波动应为 0 到 100%。");
  if (!within(positionNoise, 0, 100)) throw new Error("位置偏移应为 0 到 100 米。");
  const pairs = checkPoints(points);
  new Route(pairs, loop); // rejects routes that cannot be played
  return { points: pairs, speed, loop, speedNoise, positionNoise };
}

export function parseCoordinate(text: string): Point | null {
  const parts = text.trim().split(/[\s,，]+/);
  if (parts.length !== 2 || parts.includes("")) return null;
  const [latitude, longitude] = parts.map(Number);
  return within(latitude, -90, 90) && within(longitude, -180, 180) ? { latitude, longitude } : null;
}

export function distance(a: Pair, b: Pair): number {
  const h = Math.sin((b[0] - a[0]) * rad / 2) ** 2
    + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin((b[1] - a[1]) * rad / 2) ** 2;
  return 2 * EARTH_RADIUS * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
}

const vector = ([lat, lon]: Pair) =>
  [Math.cos(lat * rad) * Math.cos(lon * rad), Math.cos(lat * rad) * Math.sin(lon * rad), Math.sin(lat * rad)];
const coordinate = ([x, y, z]: number[]): Pair => [Math.atan2(z, Math.hypot(x, y)) / rad, Math.atan2(y, x) / rad];

export class Route {
  readonly points: Pair[] = [];
  readonly cumulative = [0];
  readonly total: number;

  constructor(input: Pair[], readonly loop = false) {
    for (const p of input) {
      if (!this.points.length || distance(this.points[this.points.length - 1], p) > 0.001) this.points.push(p);
    }
    if (this.points.length < 2) throw new Error("路线需要至少两个不同的位置。");
    if (loop) {
      if (distance(this.points[this.points.length - 1], this.points[0]) > 0.001) this.points.push(this.points[0]);
      else this.points[this.points.length - 1] = this.points[0];
    }
    for (let i = 1; i < this.points.length; i++) {
      const d = distance(this.points[i - 1], this.points[i]);
      // Antipodal points lie on infinitely many great circles: no single path.
      if (d / EARTH_RADIUS >= Math.PI - 1e-6) throw new Error("路线包含地球正对两端的点，请增加途经点。");
      this.cumulative.push(this.cumulative[i - 1] + d);
    }
    this.total = this.cumulative[this.cumulative.length - 1];
  }

  position(meters: number): Pair {
    if (meters <= 0) return this.points[0];
    if (meters >= this.total) return this.points[this.points.length - 1];
    let lo = 0, hi = this.cumulative.length - 1;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (this.cumulative[mid] <= meters) lo = mid;
      else hi = mid;
    }
    const length = this.cumulative[lo + 1] - this.cumulative[lo];
    const f = (meters - this.cumulative[lo]) / length;
    const angle = length / EARTH_RADIUS;
    const a = vector(this.points[lo]), b = vector(this.points[lo + 1]);
    return coordinate(a.map((v, i) => (Math.sin((1 - f) * angle) * v + Math.sin(f * angle) * b[i]) / Math.sin(angle)));
  }
}

export function offset(p: Pair, east: number, north: number): Pair {
  const d = Math.hypot(east, north);
  if (!d) return p;
  const [lat, lon] = p.map((v) => v * rad);
  const r = vector(p), e = [-Math.sin(lon), Math.cos(lon), 0];
  const n = [-Math.sin(lat) * Math.cos(lon), -Math.sin(lat) * Math.sin(lon), Math.cos(lat)];
  return coordinate(r.map((v, i) => Math.cos(d / EARTH_RADIUS) * v + Math.sin(d / EARTH_RADIUS) * (east * e[i] + north * n[i]) / d));
}

// A fixed seed replays the same noise, so every runtime that samples a route
// at the same moment computes the same position.
export function randomFromSeed(seed: number) {
  let value = seed >>> 0;
  return () => {
    value += 0x6D2B79F5;
    let n = Math.imul(value ^ value >>> 15, 1 | value);
    n ^= n + Math.imul(n ^ n >>> 7, 61 | n);
    return ((n ^ n >>> 14) >>> 0) / 4294967296;
  };
}

export class RouteNoise {
  private start = 0;
  private integral = 0;
  private previous = [0, 0, 0];
  private target: number[];

  constructor(private random: () => number) {
    this.target = this.next();
  }

  private next() {
    const radius = Math.sqrt(this.random()), angle = this.random() * 2 * Math.PI;
    return [this.random() * 2 - 1, radius * Math.cos(angle), radius * Math.sin(angle)];
  }

  sample(elapsed: number) {
    while (elapsed >= this.start + 10) {
      this.integral += 5 * (this.previous[0] + this.target[0]);
      this.start += 10;
      this.previous = this.target;
      this.target = this.next();
    }
    const t = (elapsed - this.start) / 10, w = t * t * (3 - 2 * t);
    const [speed, east, north] = this.previous.map((v, i) => v + (this.target[i] - v) * w);
    const integral = this.integral + 10 * (this.previous[0] * t + (this.target[0] - this.previous[0]) * (t ** 3 - t ** 4 / 2));
    return { speed, east, north, integral };
  }
}

export class Playback {
  readonly route: Route;
  private readonly noise: RouteNoise;
  private elapsed = 0;

  constructor(readonly options: RouteOptions, seed: number) {
    this.route = new Route(options.points, options.loop);
    this.noise = new RouteNoise(randomFromSeed(seed));
  }

  // Seconds since the route started; a clock that steps back holds the position.
  sample(seconds: number) {
    this.elapsed = Math.max(this.elapsed, seconds, 0);
    const o = this.options;
    const n = this.noise.sample(this.elapsed);
    const traveled = (this.elapsed + o.speedNoise / 100 * n.integral) * o.speed / 3.6;
    const completed = !o.loop && traveled >= this.route.total;
    const progress = o.loop ? traveled % this.route.total : Math.min(traveled, this.route.total);
    const p = this.route.position(progress);
    return {
      point: pointOf(completed ? p : offset(p, n.east * o.positionNoise, n.north * o.positionNoise)),
      completed,
      progress,
      total: this.route.total,
      speed: completed ? 0 : o.speed * (1 + o.speedNoise / 100 * n.speed),
      traveled,
    };
  }
}

// What bin/simlocation.py reads: a JSON list of [latitude, longitude], bare or
// as {"points": [...]}, or a GPX with exactly one track segment or route.
export function parseRoute(text: string): Pair[] {
  if (text.length > MAX_BYTES) throw new Error("路线不能超过 4 MiB。");
  const points = text.trimStart().startsWith("<") ? gpxPoints(text) : jsonPoints(text);
  return new Route(checkPoints(points)).points;
}

function jsonPoints(text: string): unknown {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("路线应为 JSON 坐标列表或 GPX。");
  }
  return Array.isArray(data) ? data : (data as { points?: unknown } | null)?.points;
}

const prefix = "(?:[\\w.-]+:)?";

// A pattern match rather than an XML parser: GPX coordinates are attributes of
// trkpt/rtept, and refusing DTDs keeps entity expansion out of the picture.
function gpxPoints(text: string): number[][] {
  const xml = text.replace(/<!--[\s\S]*?-->/g, "");
  if (/<!(?:DOCTYPE|ENTITY)/i.test(xml)) throw new Error("GPX 不支持 DTD 或实体声明。");
  const opened = xml.match(new RegExp(`<${prefix}(?:trkseg|rte)\\b`, "g"))?.length ?? 0;
  const segments = [...xml.matchAll(new RegExp(`<(${prefix}(trkseg|rte))\\b[^>]*?(?:/>|>([\\s\\S]*?)</\\1\\s*>)`, "g"))];
  if (segments.length !== opened) throw new Error("GPX 格式无效：有未闭合的 trkseg 或 rte。");
  const found = segments
    .map(([, , kind, body = ""]) =>
      [...body.matchAll(new RegExp(`<${prefix}${kind === "rte" ? "rtept" : "trkpt"}\\b([^>]*)>`, "g"))]
        .map(([, attributes]) => [attribute(attributes, "lat"), attribute(attributes, "lon")]))
    .filter((points) => points.length);
  if (found.length !== 1) throw new Error("GPX 需要且只能包含一条连续的 track segment 或 route。");
  return found[0];
}

function attribute(attributes: string, name: string): number {
  const match = attributes.match(new RegExp(`(?:^|\\s)${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`));
  const value = (match?.[1] ?? match?.[2] ?? "").trim();
  if (!value) throw new Error("GPX 坐标不完整。");
  return Number(value);
}
