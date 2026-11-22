// WGS-84 <-> Apple Maps (GCJ-02) conversion, adapted from simulate-location/src/coordinates.ts
// in https://github.com/swipt-automation/official-swiplets at c15eefc38bced2c3414cb584903328bd2bac5b19.
// Copyright (C) 2026 nekohasekai <i@sekai.icu>, GPL-3.0-or-later. The package carries this in assets/NOTICE.txt.
// Changed here: Hong Kong and Macau are their borders, in place of two rectangles that also held
// Shekou, Gongbei and Hengqin and left out the north of the New Territories.
import { REGIONS } from "./regions";
import type { Point } from "./route";
const axis = 6378245, eccentricity = 0.00669342162296594323;
const shifted = [
  [49.2204, 79.4462, 42.8899, 96.33], [54.1415, 109.6872, 39.3742, 135.0002],
  [42.8899, 73.1246, 29.5297, 124.143255], [29.5297, 82.9684, 26.7186, 97.0352],
  [29.5297, 97.0253, 20.414096, 124.367395], [20.414096, 107.975793, 17.871542, 111.744104],
];
const unshifted = [
  [25.398623, 119.921265, 21.785006, 122.497559], [22.284, 101.8652, 20.0988, 106.665],
  [21.5422, 106.4525, 20.4878, 108.051], [55.8175, 109.0323, 50.3257, 119.127],
  [55.8175, 127.4568, 49.5574, 137.0227], [44.8922, 131.2662, 42.5692, 137.0227],
];
const inside = (p: Point, [n, w, s, e]: number[]) => p.latitude <= n && p.latitude >= s && p.longitude >= w && p.longitude <= e;
const china = (p: Point) => shifted.some((b) => inside(p, b)) && !unshifted.some((b) => inside(p, b));
function within(p: Point, ring: [number, number][]) {
  let result = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [a, b] = ring[i], [c, d] = ring[j];
    if ((a > p.latitude) !== (c > p.latitude) && p.longitude < b + (p.latitude - a) * (d - b) / (c - a)) result = !result;
  }
  return result;
}
const needsShift = (p: Point) => china(p) && !REGIONS.some((ring) => within(p, ring));
function shift(p: Point): Point {
  const x = p.longitude - 105, y = p.latitude - 35, pi = Math.PI;
  const wave = (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3;
  const lat = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x)) + wave
    + (20 * Math.sin(y * pi) + 40 * Math.sin(y / 3 * pi)) * 2 / 3
    + (160 * Math.sin(y / 12 * pi) + 320 * Math.sin(y * pi / 30)) * 2 / 3;
  const lon = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x)) + wave
    + (20 * Math.sin(x * pi) + 40 * Math.sin(x / 3 * pi)) * 2 / 3
    + (150 * Math.sin(x / 12 * pi) + 300 * Math.sin(x / 30 * pi)) * 2 / 3;
  const radians = p.latitude / 180 * pi, magic = 1 - eccentricity * Math.sin(radians) ** 2, root = Math.sqrt(magic);
  return {
    latitude: p.latitude + lat * 180 / ((axis * (1 - eccentricity) / (magic * root)) * pi),
    longitude: p.longitude + lon * 180 / (axis / root * Math.cos(radians) * pi),
  };
}
export const toMap = (p: Point): Point => needsShift(p) ? shift(p) : p;
function unshift(p: Point): Point {
  let gps = p;
  for (let i = 0; i < 8; i++) {
    const moved = shift(gps);
    gps = { latitude: gps.latitude + p.latitude - moved.latitude, longitude: gps.longitude + p.longitude - moved.longitude };
  }
  return gps;
}
// The shift carries the mainland's edge up to 600 m over the border, so a map
// point there can stand for either side. The mainland, built up to the line, wins.
export function toGPS(p: Point): Point {
  if (!china(p)) return p;
  const gps = unshift(p);
  return needsShift(gps) || needsShift(p) ? gps : p;
}
