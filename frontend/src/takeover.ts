// "Take over tracking" (#13): an appliance's statistic in place of a device of
// the Energy dashboard, from the Appliances tab (administrators only). The
// integration does the replacement (energy_mapper/take_over); this module
// tells what a card offers. Pure logic, unit tested, no DOM.

/** An individual device of the Energy dashboard that may be taken over. */
export interface TrackingDevice {
  stat_consumption: string;
  name: string;
  included_in_stat: string | null;
}

export interface TrackingSuggestion {
  stat_consumption: string;
  /** estimate: estimated from a relay of the appliance; device: on the relay's Home Assistant device */
  reason: "estimate" | "device" | string;
}

/** One appliance with a statistic of its own (energy_mapper/tracking). */
export interface TrackingAppliance {
  key: string;
  statistic_id: string;
  /** its statistic is an individual device of the Energy dashboard */
  in_dashboard: boolean;
  /** the device it replaced (what Undo puts back), null when none */
  taken_over: { stat_consumption: string; name: string } | null;
  suggested: TrackingSuggestion[];
}

export interface TrackingListing {
  devices: TrackingDevice[];
  appliances: TrackingAppliance[];
}

/** What the card of an appliance shows about the Energy dashboard:
 * - taken_over: it replaced ``device`` (Undo);
 * - tracked: its statistic is in the dashboard, added by hand (nothing to do);
 * - suggested: ``device`` is probably it (Take over tracking);
 * - available: no suggestion, but a device can be picked;
 * - none: nothing to offer (no statistic, no device, or not known). */
export type EnergyState =
  | { kind: "taken_over"; device: string }
  | { kind: "tracked" }
  | { kind: "suggested"; device: TrackingDevice }
  | { kind: "available" }
  | { kind: "none" };

export function energyState(listing: TrackingListing | null, key: string): EnergyState {
  const a = listing?.appliances.find((x) => x.key === key);
  if (!listing || !a) return { kind: "none" };
  if (a.in_dashboard && a.taken_over) return { kind: "taken_over", device: a.taken_over.name };
  if (a.in_dashboard) return { kind: "tracked" };
  const byStat = new Map(listing.devices.map((d) => [d.stat_consumption, d]));
  const first = a.suggested.map((s) => byStat.get(s.stat_consumption)).find((d) => d !== undefined);
  if (first) return { kind: "suggested", device: first };
  return listing.devices.length ? { kind: "available" } : { kind: "none" };
}

/** The devices an appliance may take over, in the order of the picker:
 * its suggestions first (in their order), then the others by name. */
export function pickList(listing: TrackingListing | null, key: string): { device: TrackingDevice; suggested: boolean }[] {
  if (!listing) return [];
  const a = listing.appliances.find((x) => x.key === key);
  const wanted = (a?.suggested ?? []).map((s) => s.stat_consumption);
  const suggested = wanted
    .map((stat) => listing.devices.find((d) => d.stat_consumption === stat))
    .filter((d): d is TrackingDevice => d !== undefined);
  const others = listing.devices
    .filter((d) => !wanted.includes(d.stat_consumption))
    .sort((x, y) => x.name.localeCompare(y.name) || x.stat_consumption.localeCompare(y.stat_consumption));
  return [...suggested.map((device) => ({ device, suggested: true })), ...others.map((device) => ({ device, suggested: false }))];
}

/** The device preselected in the picker: the first suggestion, else none. */
export function preselected(listing: TrackingListing | null, key: string): string {
  const first = pickList(listing, key)[0];
  return first?.suggested ? first.device.stat_consumption : "";
}
