import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../src/i18n.ts";
import { availableTabs, searchWithTab, shownTab, tabForKey, tabFromSearch, toReviewCount } from "../src/tabs.ts";
import type { ReviewListing, ReviewSignature } from "../src/types.ts";

test("the tab comes from ?tab=, the Repairs link opens To review, else Today", () => {
  assert.equal(tabFromSearch(""), "today");
  assert.equal(tabFromSearch("?day=2027-03-09"), "today");
  assert.equal(tabFromSearch("?tab=appliances&day=2027-03-09"), "appliances");
  assert.equal(tabFromSearch("?tab=review"), "review");
  assert.equal(tabFromSearch("?review=1"), "review");
  // an explicit tab wins over the Repairs parameter; unknown values are ignored
  assert.equal(tabFromSearch("?review=1&tab=today"), "today");
  assert.equal(tabFromSearch("?tab=nope"), "today");
  assert.equal(tabFromSearch("?tab=nope&review=1"), "review");
});

test("To review is for administrators with a review only", () => {
  assert.deepEqual(availableTabs(true), ["today", "appliances", "review"]);
  assert.deepEqual(availableTabs(false), ["today", "appliances"]);
  assert.deepEqual(availableTabs(true, false), ["today", "appliances"]);
  assert.equal(shownTab("review", availableTabs(false)), "today");
  assert.equal(shownTab("review", availableTabs(true)), "review");
  assert.equal(shownTab("appliances", availableTabs(false)), "appliances");
});

test("the tab is written to the URL next to the day", () => {
  assert.equal(searchWithTab("", "appliances"), "?tab=appliances");
  assert.equal(searchWithTab("?day=2027-03-09", "review"), "?day=2027-03-09&tab=review");
  assert.equal(searchWithTab("?tab=review&day=2027-03-09", "today"), "?tab=today&day=2027-03-09");
  // the one-shot Repairs parameter is dropped once used
  assert.equal(searchWithTab("?review=1&day=2027-03-09", "review"), "?day=2027-03-09&tab=review");
  assert.equal(tabFromSearch(searchWithTab("?review=1", "today")), "today");
});

test("arrow keys, Home and End move between the tabs", () => {
  const all = availableTabs(true);
  assert.equal(tabForKey(all, "today", "ArrowRight"), "appliances");
  assert.equal(tabForKey(all, "review", "ArrowRight"), "today");
  assert.equal(tabForKey(all, "today", "ArrowLeft"), "review");
  assert.equal(tabForKey(all, "appliances", "Home"), "today");
  assert.equal(tabForKey(all, "today", "End"), "review");
  assert.equal(tabForKey(availableTabs(false), "appliances", "ArrowRight"), "today");
  assert.equal(tabForKey(all, "today", "Enter"), null);
});

test("the badge counts the proposed signatures only", () => {
  const sig = (id: string, review_state: ReviewSignature["review_state"]) => ({ id, review_state }) as ReviewSignature;
  const listing = {
    signatures: [sig("a", "proposed"), sig("b", "waiting"), sig("c", "proposed"), sig("d", "ignored")],
  } as ReviewListing;
  assert.equal(toReviewCount(listing), 2);
  assert.equal(toReviewCount(null), 0);
});

test("tab and banner strings exist in English and French", () => {
  for (const lang of ["en", "fr"]) {
    const t = translator(lang);
    for (const key of ["tab_today", "tab_appliances", "tab_review", "review_start"] as const) {
      assert.notEqual(t(key), key);
    }
    assert.match(t("review_banner", { count: "34" }), /34/);
    assert.notEqual(t("review_banner_one"), t("review_banner", { count: "1" }));
  }
  assert.equal(translator("fr")("review_banner", { count: "34" }), "34 appareils attendent un nom");
});
