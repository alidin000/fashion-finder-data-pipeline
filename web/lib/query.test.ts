import assert from "node:assert/strict";
import test from "node:test";
import { parseSearchInput } from "./query.ts";

test("extracts a HUF budget from an English query", () => {
  assert.deepEqual(parseSearchInput("black jacket under 15000 Ft", ""), {
    query: "black jacket",
    maxPrice: "15000",
  });
});

test("preserves the explicit budget when no phrase is present", () => {
  assert.deepEqual(parseSearchInput("fekete kabát", "22000"), {
    query: "fekete kabát",
    maxPrice: "22000",
  });
});
