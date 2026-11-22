import assert from "node:assert/strict";
import { test } from "node:test";

import { frequentNames } from "../src/appliance-names.ts";

test("frequent names by power band, in the user's language", () => {
  assert.deepEqual(frequentNames(850, "fr"), ["Grille-pain", "Micro-ondes", "Cafetière", "Aspirateur"]);
  assert.deepEqual(frequentNames(2080, "fr-FR"), ["Bouilloire", "Four", "Chauffe-eau", "Radiateur"]);
  assert.deepEqual(frequentNames(-2080, "en", [], 2), ["Kettle", "Oven"]);
  assert.equal(frequentNames(7000, "de")[0], "Hob");
  assert.deepEqual(frequentNames(300, "en", [], 1), ["Television"]);
});

test("names already taken are left out", () => {
  assert.deepEqual(frequentNames(850, "fr", ["grille-pain", " Cafetière "]), ["Micro-ondes", "Aspirateur", "Sèche-cheveux"]);
});
