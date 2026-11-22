// Map links, read into WGS-84 by the datum of the service they come from.
// Only fields that name the place are read: a camera's centre next to a place,
// or numbers in other text, would silently be a wrong location.
import { fromAMap, toGPS } from "./coordinates";
import { checkPoint, parseCoordinate, type Point } from "./route";

/** A location, or a short link that has to be opened to learn it. */
export type Place = { point: Point } | { open: string };

const decode = (text: string) => {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
};
// By hand: React Native's URL is not a complete one.
function split(link: string) {
  const match = link.match(/^([a-z][\w+.-]*):\/\/([^/?#]*)([^?#]*)(?:\?([^#]*))?/i);
  if (!match) return null;
  const query = new Map<string, string>();
  for (const part of (match[4] ?? "").split("&")) {
    const [key, ...value] = part.split("=");
    if (key && !query.has(key)) query.set(key, decode(value.join("=").replace(/\+/g, " ")));
  }
  return { scheme: match[1].toLowerCase(), host: match[2].toLowerCase(), path: decode(match[3]), query };
}

function pointFrom(latitude: string | undefined, longitude: string | undefined): Point | null {
  if (![latitude, longitude].every((v) => v !== undefined && /^\s*-?\d{1,3}(?:\.\d+)?\s*$/.test(v))) return null;
  try {
    return checkPoint({ latitude: Number(latitude), longitude: Number(longitude) });
  } catch {
    return null;
  }
}
const latLon = (value = "") => {
  const parts = value.replace(/^loc:/, "").split(",");
  return parts.length === 2 ? pointFrom(parts[0], parts[1]) : null;
};

function apple(query: Map<string, string>) {
  for (const key of ["coordinate", "daddr", "ll", "q"]) {
    const point = latLon(query.get(key));
    if (point) return toGPS(point);
  }
  return null;
}

// AMap writes both orders, and is shifted in Hong Kong and Macau too.
function amap(query: Map<string, string>) {
  const lonFirst = (query.get("lnglat") ?? query.get("position") ?? query.get("to"))?.split(",");
  const id = query.get("p")?.split(","), named = query.get("q")?.split(",");
  const point = lonFirst ? pointFrom(lonFirst[1], lonFirst[0])
    : id ? pointFrom(id[1], id[2])
    : named ? pointFrom(named[0], named[1])
    : pointFrom(query.get("dlat") ?? query.get("lat"), query.get("dlon") ?? query.get("lon"));
  if (!point) return null;
  return query.get("coordinate") === "wgs84" || query.get("dev") === "1" ? point : fromAMap(point);
}

function google(path: string, query: Map<string, string>) {
  const pins = [...path.matchAll(/!3d(-?[\d.]+)!4d(-?[\d.]+)/g)].pop();
  const typed = path.match(/\/(?:place|search)\/(-?[\d.]+),\s*(-?[\d.]+)(?:\/|$)/);
  // With a place, @ is only the camera, which can be kilometres off.
  const bare = path.match(/^\/maps\/@(-?[\d.]+),(-?[\d.]+),/);
  const found = pins ?? typed ?? bare;
  let point = found ? pointFrom(found[1], found[2]) : null;
  for (const key of ["query", "q", "destination", "ll"]) point ??= latLon(query.get(key));
  return point && toGPS(point);
}

const GOOGLE = /^(?:www\.|maps\.)?google\.(?:com(?:\.[a-z]{2})?|co\.[a-z]{2}|[a-z]{2})$/;

/** Typed WGS-84 coordinates, or the first link in shared text. */
export function parsePlace(text: string): Place | null {
  const typed = parseCoordinate(text);
  if (typed) return { point: typed };
  const link = text.match(/(?:https?|iosamap):\/\/[^\s<>"“”「」【】]+/i)?.[0].replace(/[。，、；！）)]+$/, "");
  const url = link && split(link);
  if (!link || !url) return null;
  const { scheme, host, path, query } = url;
  let point: Point | null = null;
  if (scheme === "iosamap") point = amap(query);
  else if (host === "surl.amap.com" || host === "maps.app.goo.gl" || host === "maps.apple"
    || (host === "maps.apple.com" && path.startsWith("/p/"))) return { open: link.replace(/^http:/i, "https:") };
  else if (host === "maps.apple.com") point = apple(query);
  else if (host === "amap.com" || host.endsWith(".amap.com")) point = amap(query);
  else if (GOOGLE.test(host) && (host.startsWith("maps.") || path.startsWith("/maps"))) point = google(path, query);
  else if (host === "consent.google.com") {
    const next = query.get("continue"), inner = next ? split(next) : null;
    if (inner && GOOGLE.test(inner.host)) point = google(inner.path, inner.query);
  }
  return point && { point };
}

/** parsePlace, opening a short link once with `follow`, which answers where it leads. */
export async function findPlace(text: string, follow: (link: string) => Promise<string>): Promise<Point> {
  const place = parsePlace(text);
  if (!place) throw new Error("没有认出坐标。支持 WGS-84 的“纬度, 经度”，以及带坐标的 Apple、高德、Google 地图链接。");
  if ("point" in place) return place.point;
  let end: string;
  try {
    end = await follow(place.open);
  } catch {
    throw new Error("短链接打不开。请在浏览器里打开它，再复制完整地址。");
  }
  const found = parsePlace(end);
  if (found && "point" in found) return found.point;
  throw new Error("这个短链接里没有坐标。请在浏览器里打开它，再复制完整地址。");
}
