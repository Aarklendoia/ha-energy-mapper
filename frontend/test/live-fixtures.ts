// A synthetic live event for the tests (no real data).

import type { LiveEvent } from "../src/live.ts";

const T0 = 1_800_000_000;

export function event(id: number, extra: Partial<LiveEvent> = {}): LiveEvent {
  return {
    id,
    seq: 1,
    t_on: T0 + id * 10,
    delta_w: 850,
    state: "confirmed",
    running: true,
    t_off: null,
    off_w: null,
    duration_s: null,
    power_w: 850,
    session: null,
    pv_moved: false,
    confidence: "unknown",
    candidates: [],
    ...extra,
  };
}
