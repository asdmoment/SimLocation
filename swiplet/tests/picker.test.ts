import test from "node:test";
import assert from "node:assert/strict";
import { Centre } from "../src/picker";

const at = (latitude: number, zoom = 15) => ({ coordinates: { latitude, longitude: 2 }, zoom, latitudeDelta: 360 / 2 ** zoom });

test("sending the map somewhere keeps the exact point, and panning picks the centre", () => {
  const centre = new Centre();
  centre.aimAt(at(1).coordinates, 0);
  assert.equal(centre.moved(at(0, 2), 100), false); // the world, before the camera arrives
  assert.equal(centre.moved(at(1 + 1e-9), 400), false);
  assert.equal(centre.moved(at(1.002), 900), true);
});

test("zooming about the centre, or looking from far out, picks nothing", () => {
  const centre = new Centre();
  assert.equal(centre.moved(at(1), 0), true);
  assert.equal(centre.moved(at(1, 16), 100), false);
  assert.equal(centre.moved(at(30, 4), 200), false);
});

test("a move that never arrived does not swallow a later pan", () => {
  const centre = new Centre();
  centre.aimAt(at(1).coordinates, 0);
  assert.equal(centre.moved(at(1), 5_000), true);
});
