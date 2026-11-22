// Which camera changes pick a location. expo-maps reports the end of every
// change, the programmatic ones too, and the centre of the map's safe area.
import type { Point } from "./route";

// Further out than this a centre is not a place anyone chose.
export const PICK_ZOOM = 10;
const SETTLE = 2_000; // longer than a camera animation, in ms

export interface Camera { coordinates: Point; zoom: number; latitudeDelta: number }

const near = (a: Point, b: Point, degrees: number) =>
  Math.abs(a.latitude - b.latitude) <= degrees && Math.abs(a.longitude - b.longitude) <= degrees;

export class Centre {
  private last: Point | undefined;
  private aim: { point: Point; until: number } | undefined;

  /** The map is being sent to `point`: arriving there picks nothing. */
  aimAt(point: Point, now: number) {
    this.aim = { point, until: now + SETTLE };
  }

  /** Whether this change picks its centre. */
  moved({ coordinates, zoom, latitudeDelta }: Camera, now: number): boolean {
    const last = this.last;
    this.last = coordinates;
    if (zoom < PICK_ZOOM) return false; // nor is it where an aimed move arrives
    if (this.aim && now < this.aim.until && near(coordinates, this.aim.point, latitudeDelta / 50)) {
      this.aim = undefined;
      return false;
    }
    // Zooming about the centre, or under a pixel, keeps an exact favorite or coordinate.
    return !last || !near(coordinates, last, latitudeDelta / 1000);
  }
}
