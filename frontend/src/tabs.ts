// The tabs of the panel (#47): Today, Appliances and To review (administrators).
// Pure helpers for the tab kept in the URL, unit tested.

import type { ReviewListing } from "./types.ts";

export type Tab = "today" | "appliances" | "review";

export const TABS: readonly Tab[] = ["today", "appliances", "review"];

const isTab = (value: string | null): value is Tab => value !== null && (TABS as readonly string[]).includes(value);

/** The tabs a user sees: To review only for administrators whose add-on has
 * a review (``hasReview``: the listing could be loaded, or is still loading). */
export function availableTabs(isAdmin: boolean, hasReview = true): Tab[] {
  return isAdmin && hasReview ? ["today", "appliances", "review"] : ["today", "appliances"];
}

/** The tab asked for by the URL: ``?tab=today|appliances|review``; the link of
 * the Repairs notice (``?review=1``) opens To review. Anything else: Today. */
export function tabFromSearch(search: string): Tab {
  const params = new URLSearchParams(search);
  const tab = params.get("tab");
  if (isTab(tab)) return tab;
  return params.has("review") ? "review" : "today";
}

/** The tab shown: the one asked for if the user has it, else Today. */
export function shownTab(wanted: Tab, tabs: readonly Tab[]): Tab {
  return tabs.includes(wanted) ? wanted : "today";
}

/** The query string with the tab set (the day and the other parameters are
 * kept; the one-shot ``review`` parameter of the Repairs link is dropped). */
export function searchWithTab(search: string, tab: Tab): string {
  const params = new URLSearchParams(search);
  params.delete("review");
  params.set("tab", tab);
  const out = params.toString();
  return out ? `?${out}` : "";
}

/** The tab a key moves to in the tab list (arrows, Home, End), or null. */
export function tabForKey(tabs: readonly Tab[], current: Tab, key: string): Tab | null {
  const i = Math.max(tabs.indexOf(current), 0);
  switch (key) {
    case "ArrowRight":
      return tabs[(i + 1) % tabs.length];
    case "ArrowLeft":
      return tabs[(i - 1 + tabs.length) % tabs.length];
    case "Home":
      return tabs[0];
    case "End":
      return tabs[tabs.length - 1];
    default:
      return null;
  }
}

/** The number of signatures waiting for a name (the badge of To review and
 * the banner of Today). */
export function toReviewCount(listing: ReviewListing | null | undefined): number {
  return (listing?.signatures ?? []).filter((s) => s.review_state === "proposed").length;
}
