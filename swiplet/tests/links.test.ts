import test from "node:test";
import assert from "node:assert/strict";
import { fromAMap, toGPS } from "../src/coordinates";
import { findPlace, parsePlace } from "../src/links";

const mainland = { latitude: 39.9, longitude: 116.4 }, hongKong = { latitude: 22.2853, longitude: 114.1593 };
const pointIn = (text: string) => {
  const place = parsePlace(text);
  return place && "point" in place ? place.point : null;
};

test("links are converted by their own service's datum", () => {
  assert.deepEqual(pointIn("39.9, 116.4"), mainland);
  assert.deepEqual(pointIn("https://maps.apple.com/place?coordinate=39.9,116.4&name=X"), toGPS(mainland));
  assert.deepEqual(pointIn("https://maps.apple.com/?ll=22.2853,114.1593&q=Pin"), hongKong);
  assert.deepEqual(pointIn("看这里 https://www.google.com/maps/search/?api=1&query=39.9%2C116.4"), toGPS(mainland));
  const shifted = pointIn("https://amap.com/?q=22.2853,114.1593,Place")!;
  assert.deepEqual(shifted, fromAMap(hongKong));
  assert.ok(Math.abs(shifted.longitude - hongKong.longitude) > 0.003); // AMap is shifted there too
});

test("AMap's orders, and its WGS-84 flags", () => {
  for (const link of [
    "https://uri.amap.com/marker?position=116.4,39.9&name=X",
    "https://ditu.amap.com/?lnglat=116.4,39.9",
    "https://uri.amap.com/navigation?to=116.4,39.9,X",
    "https://wb.amap.com/?p=B000000000,39.9,116.4,X",
    "iosamap://viewMap?lat=39.9&lon=116.4&dev=0",
  ]) assert.deepEqual(pointIn(link), fromAMap(mainland), link);
  assert.deepEqual(pointIn("https://uri.amap.com/marker?position=116.4,39.9&coordinate=wgs84"), mainland);
  assert.deepEqual(pointIn("iosamap://viewMap?lat=39.9&lon=116.4&dev=1"), mainland);
});

test("a Google place's pin beats the camera", () => {
  assert.deepEqual(pointIn("https://www.google.com/maps/place/X/@52,1,11z/data=!3d51.5!4d-0.1"), { latitude: 51.5, longitude: -0.1 });
  assert.equal(pointIn("https://www.google.com/maps/place/X/@52,1,11z"), null);
});

test("what does not name a place is not guessed", () => {
  for (const text of [
    "meet at 39.9, 116.4 tomorrow", "https://example.com/?q=39.9,116.4", "https://maps.apple.com.example.com/?ll=1,2",
    "https://maps.apple.com/?ll=91,2", "https://maps.apple.com/?sll=1,2", "https://map.baidu.com/?q=39.9,116.4",
    "https://google.com@example.com/maps?q=1,2",
  ]) assert.equal(parsePlace(text), null, text);
});

test("a short link is opened once, and has to end at a location", async () => {
  const opened: string[] = [];
  assert.deepEqual(await findPlace("地点 http://surl.amap.com/abc", async (link) => {
    opened.push(link);
    return "https://wb.amap.com/?p=B000000000,39.9,116.4,X";
  }), fromAMap(mainland));
  assert.deepEqual(opened, ["https://surl.amap.com/abc"]);
  await assert.rejects(findPlace("https://maps.app.goo.gl/abc", () => Promise.reject(new Error("offline"))), /短链接打不开/);
  await assert.rejects(findPlace("https://maps.apple/p/abc", async (link) => link), /没有坐标/);
});
