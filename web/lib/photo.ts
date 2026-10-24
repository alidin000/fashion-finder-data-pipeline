// What photo search understood about the shopper's photo, as the page shows it.
import type { Box } from "./crop.ts";

export type Reading = { value: string; confidence: number };
export type Alternative = { category: string; crop: Box };
export type PhotoQuery = {
  cropSource: "auto" | "manual" | "none";
  crop: Box | null;
  category: Reading | null;
  colour: Reading | null;
  alternatives: Alternative[];
};

// Below these the reading is shown with a "not sure" hint; the API omits weaker ones.
const SURE_CATEGORY = 0.8;
const SURE_COLOUR = 0.85;

const CATEGORY_NAMES: Record<string, string> = {
  coats: "Coat",
  jackets: "Jacket",
  dresses: "Dress",
  sweaters: "Sweater",
  shirts: "Shirt",
  blouses: "Blouse",
  tops: "Top",
  trousers: "Trousers",
  jeans: "Jeans",
  skirts: "Skirt",
  shorts: "Shorts",
  shoes: "Shoes",
  bags: "Bag",
  accessories: "Accessory",
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const fraction = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

function box(value: unknown): Box | null {
  if (!isRecord(value)) return null;
  const { x, y, width, height } = value;
  if (!fraction(x) || !fraction(y) || !fraction(width) || !fraction(height) || width <= 0 || height <= 0) return null;
  return { x, y, width: Math.min(width, 1 - x), height: Math.min(height, 1 - y) };
}

function reading(value: unknown): Reading | null {
  if (!isRecord(value) || typeof value.value !== "string" || !value.value || !fraction(value.confidence)) return null;
  return { value: value.value, confidence: value.confidence };
}

/** The response's `query` block, or null when the API sent none (older API, demo). Never throws. */
export function parsePhotoQuery(value: unknown): PhotoQuery | null {
  if (!isRecord(value)) return null;
  const cropSource = value.crop_source === "auto" || value.crop_source === "manual" ? value.crop_source : "none";
  const alternatives = Array.isArray(value.alternatives)
    ? value.alternatives.flatMap((item): Alternative[] => {
        const crop = isRecord(item) ? box(item.crop) : null;
        return crop && isRecord(item) && typeof item.category === "string" && item.category ? [{ category: item.category, crop }] : [];
      })
    : [];
  return { cropSource, crop: cropSource === "auto" ? box(value.crop) : null, category: reading(value.category), colour: reading(value.colour), alternatives };
}

/** "Jacket", or the raw value for a category this page does not know yet. */
export function categoryName(category: string): string {
  return CATEGORY_NAMES[category] ?? category.charAt(0).toUpperCase() + category.slice(1);
}

/** "Jacket · black" or "Something red", with `unsure` for a weak reading; null when nothing was read. */
export function understandingLabel(query: PhotoQuery | null): { text: string; unsure: boolean } | null {
  if (!query || (!query.category && !query.colour)) return null;
  const parts = query.category ? [categoryName(query.category.value), query.colour?.value].filter(Boolean) : [`Something ${query.colour?.value}`];
  const unsure = (query.category !== null && query.category.confidence < SURE_CATEGORY) || (query.colour !== null && query.colour.confidence < SURE_COLOUR);
  return { text: parts.join(" · "), unsure };
}

/** "+2 colours" when the collapsed offers name other colours, otherwise "+2 more". */
export function variantLabel(count: number, colours: string[] = []): string | null {
  if (!Number.isInteger(count) || count < 1) return null;
  if (colours.length) return `+${count} ${count === 1 ? "colour" : "colours"}`;
  return `+${count} more`;
}

const HOUR = 3_600_000;
// Prices are rechecked every 12 hours; older than this means a refresh was missed.
export const STALE_AFTER_HOURS = 18;

/** "Price checked 3 h ago"; `stale` after 18 hours; null for a missing or unreadable time. */
export function priceFreshness(checkedAt: string | null | undefined, now: Date): { text: string; stale: boolean } | null {
  if (!checkedAt) return null;
  const checked = Date.parse(checkedAt);
  if (Number.isNaN(checked)) return null;
  const hours = Math.max(0, (now.getTime() - checked) / HOUR);
  const text =
    hours < 1 ? "Price checked under an hour ago" : hours < 48 ? `Price checked ${Math.floor(hours)} h ago` : `Price checked ${Math.floor(hours / 24)} days ago`;
  return { text, stale: hours > STALE_AFTER_HOURS };
}
