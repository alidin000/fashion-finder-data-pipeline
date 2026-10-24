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

test("reads Hungarian budgets that put the amount first", () => {
  assert.deepEqual(parseSearchInput("fekete kabát 10 000 Ft alatt", ""), { query: "fekete kabát", maxPrice: "10000" });
  assert.deepEqual(parseSearchInput("piros ruha 15000 Ft-ig", "9000"), { query: "piros ruha", maxPrice: "15000" });
  assert.deepEqual(parseSearchInput("nyári ruha 12000 forint alatt", ""), { query: "nyári ruha", maxPrice: "12000" });
  assert.deepEqual(parseSearchInput("farmer 20000 alatt", ""), { query: "farmer", maxPrice: "20000" });
});

test("does not mistake a size for a budget", () => {
  assert.deepEqual(parseSearchInput("cipő 38 alatt", "5000"), { query: "cipő 38 alatt", maxPrice: "5000" });
});
