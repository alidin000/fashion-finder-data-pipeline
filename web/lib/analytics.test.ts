import assert from "node:assert/strict";
import { clickEvent, errorEvent, searchEvent } from "./analytics.ts";

const search = searchEvent("text", true, 3);
assert.deepEqual(search, { event_type: "search", search_type: "text", filtered: true, result_count: 3 });
assert.equal(JSON.stringify(search).includes("query"), false);
assert.deepEqual(clickEvent("photo", "reserved"), { event_type: "click", search_type: "photo", source_key: "reserved" });
assert.deepEqual(errorEvent("text", null), { event_type: "error", search_type: "text", error_code: "network" });
assert.deepEqual(errorEvent("photo", 503), { event_type: "error", search_type: "photo", error_code: "http_5xx" });
