import assert from "node:assert/strict";
import test from "node:test";
import { lookReading, parseInterpretation, priceLabel } from "./look.ts";

// Intl puts a no-break space between thousands; compare with plain spaces.
const plain = (value: string | null) => value?.replace(/ /g, " ") ?? null;

const hungarian = {
  language: "hu",
  translated_by: "dictionary",
  colour: "red",
  category: "dresses",
  gender: "women",
  size: "M",
  max_price: 10000,
  min_price: null,
  untranslated: ["nyári"],
};

test("explains a translated Hungarian look search", () => {
  const reading = lookReading("red summer dress", parseInterpretation(hungarian), "piros nyári ruha 10000 Ft alatt");
  assert.ok(reading);
  assert.equal(reading.searchingFor, "red summer dress");
  assert.equal(reading.from, "piros nyári ruha 10000 Ft alatt");
  assert.deepEqual(reading.chips.map(plain), ["under 10 000 Ft", "Size M"]);
  assert.equal(reading.rankedFor, "red · dresses · women");
  assert.equal(reading.notUnderstood, "nyári");
});

test("shows English searches without a translation note or empty parts", () => {
  const english = parseInterpretation({ language: "en", translated_by: "none", colour: null, category: "coats", untranslated: [] });
  const reading = lookReading("black coat", english, "black coat");
  assert.deepEqual(reading, { searchingFor: "black coat", from: null, chips: [], rankedFor: "coats", notUnderstood: null });
  // An older API sends only interpreted_query; a response without it shows nothing.
  assert.equal(lookReading("black coat", null, "black coat")?.from, null);
  assert.equal(lookReading(undefined, english, "black coat"), null);
});

test("reads prices as numbers or decimal strings and ignores junk", () => {
  const parsed = parseInterpretation({ translated_by: "sideways", max_price: "9990.00", min_price: "cheap", untranslated: ["", 3, "xyz"] });
  assert.ok(parsed);
  assert.equal(parsed.translatedBy, "none");
  assert.equal(parsed.maxPrice, 9990);
  assert.equal(parsed.minPrice, null);
  assert.deepEqual(parsed.untranslated, ["xyz"]);
  assert.equal(parseInterpretation("hu"), null);
  assert.equal(plain(priceLabel(5000, 10000)), "5000–10 000 Ft"); // hu-HU groups from five digits
  assert.equal(plain(priceLabel(5000, null)), "from 5000 Ft");
  assert.equal(priceLabel(null, null), null);
});
