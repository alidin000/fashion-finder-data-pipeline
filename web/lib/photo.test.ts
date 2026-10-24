import assert from "node:assert/strict";
import test from "node:test";
import { categoryName, parsePhotoQuery, priceFreshness, understandingLabel, variantLabel } from "./photo.ts";

const response = {
  crop_source: "auto",
  crop: { x: 0.12, y: 0.3, width: 0.55, height: 0.4 },
  category: { value: "jackets", confidence: 0.71 },
  colour: { value: "black", confidence: 0.92 },
  alternatives: [{ category: "trousers", crop: { x: 0.1, y: 0.6, width: 0.6, height: 0.4 } }],
};

test("reads the API's query block into what the page shows", () => {
  const query = parsePhotoQuery(response);
  assert.ok(query);
  assert.equal(query.cropSource, "auto");
  assert.deepEqual(query.crop, response.crop);
  assert.deepEqual(query.alternatives, [{ category: "trousers", crop: { x: 0.1, y: 0.6, width: 0.6, height: 0.4 } }]);
  assert.deepEqual(understandingLabel(query), { text: "Jacket · black", unsure: true });
  assert.deepEqual(understandingLabel({ ...query, category: { value: "jackets", confidence: 0.95 } }), { text: "Jacket · black", unsure: false });
  assert.deepEqual(understandingLabel({ ...query, category: null }), { text: "Something black", unsure: false });
});

test("ignores a missing, partial or malformed query block without throwing", () => {
  assert.equal(parsePhotoQuery(undefined), null);
  assert.equal(parsePhotoQuery("auto"), null);
  const odd = parsePhotoQuery({ crop_source: "sideways", crop: { x: 2, y: 0, width: 1, height: 1 }, category: { value: "jackets", confidence: 7 }, alternatives: [{ category: "shoes", crop: null }, 5] });
  assert.deepEqual(odd, { cropSource: "none", crop: null, category: null, colour: null, alternatives: [] });
  assert.equal(understandingLabel(odd), null);
  // A manual crop is the shopper's own box; the API sends no box for it.
  assert.equal(parsePhotoQuery({ ...response, crop_source: "manual" })?.crop, null);
  // A box that runs past the edge is clipped to the photo.
  const clipped = parsePhotoQuery({ ...response, crop: { x: 0.8, y: 0.5, width: 0.4, height: 0.5 } })?.crop;
  assert.ok(clipped && Math.abs(clipped.x + clipped.width - 1) < 1e-9 && clipped.height === 0.5);
});

test("names categories for shoppers and keeps unknown ones readable", () => {
  assert.equal(categoryName("trousers"), "Trousers");
  assert.equal(categoryName("bags"), "Bag");
  assert.equal(categoryName("swimwear"), "Swimwear");
});

test("labels collapsed variants by colour only when other colours exist", () => {
  assert.equal(variantLabel(2, ["Bézs", "Zöld"]), "+2 colours");
  assert.equal(variantLabel(1, ["Bézs"]), "+1 colour");
  assert.equal(variantLabel(3), "+3 more");
  assert.equal(variantLabel(0), null);
});

test("says how fresh a price is and flags a missed 12-hour refresh", () => {
  const now = new Date("2026-10-24T12:00:00Z");
  assert.deepEqual(priceFreshness("2026-10-24T09:00:00+00:00", now), { text: "Price checked 3 h ago", stale: false });
  assert.deepEqual(priceFreshness("2026-10-24T11:40:00Z", now), { text: "Price checked under an hour ago", stale: false });
  assert.equal(priceFreshness("2026-10-23T17:00:00Z", now)?.stale, true); // 19 h
  assert.equal(priceFreshness("2026-10-23T18:30:00Z", now)?.stale, false); // 17.5 h
  assert.deepEqual(priceFreshness("2026-10-21T12:00:00Z", now), { text: "Price checked 3 days ago", stale: true });
  assert.equal(priceFreshness(undefined, now), null);
  assert.equal(priceFreshness("yesterday", now), null);
});
