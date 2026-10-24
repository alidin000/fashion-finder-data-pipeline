// What look search understood from the shopper's words, as the page shows it.

export type Interpretation = {
  language: string | null;
  translatedBy: "none" | "dictionary" | "dictionary+mt" | "mt";
  colour: string | null;
  category: string | null;
  gender: string | null;
  size: string | null;
  maxPrice: number | null;
  minPrice: number | null;
  untranslated: string[];
};

export type LookReading = {
  searchingFor: string;
  // The shopper's own words, shown when they were translated: "(from 'piros nyári ruha')".
  from: string | null;
  chips: string[];
  rankedFor: string | null;
  notUnderstood: string | null;
};

const TRANSLATIONS = new Set(["none", "dictionary", "dictionary+mt", "mt"]);
const forint = new Intl.NumberFormat("hu-HU", { maximumFractionDigits: 0 });

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value.trim() : null);

// Prices may arrive as JSON numbers or as decimal strings ("9990.00").
function price(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" && /^\d+(\.\d+)?$/.test(value.trim()) ? Number(value) : NaN;
  return Number.isFinite(number) && number >= 0 ? number : null;
}

/** The `interpretation` block of a look-search response, or null. Never throws. */
export function parseInterpretation(value: unknown): Interpretation | null {
  if (!isRecord(value)) return null;
  const translatedBy = typeof value.translated_by === "string" && TRANSLATIONS.has(value.translated_by) ? (value.translated_by as Interpretation["translatedBy"]) : "none";
  return {
    language: text(value.language),
    translatedBy,
    colour: text(value.colour),
    category: text(value.category),
    gender: text(value.gender),
    size: text(value.size),
    maxPrice: price(value.max_price),
    minPrice: price(value.min_price),
    untranslated: Array.isArray(value.untranslated) ? value.untranslated.flatMap((word) => (text(word) ? [text(word) as string] : [])) : [],
  };
}

/** "under 10 000 Ft", "from 5 000 Ft", "5 000–10 000 Ft", or null. */
export function priceLabel(minPrice: number | null, maxPrice: number | null): string | null {
  const ft = (value: number) => `${forint.format(value)} Ft`;
  if (minPrice !== null && maxPrice !== null) return `${forint.format(minPrice)}–${ft(maxPrice)}`;
  if (maxPrice !== null) return `under ${ft(maxPrice)}`;
  if (minPrice !== null) return `from ${ft(minPrice)}`;
  return null;
}

/** Everything the results header says about a look search; null without `interpreted_query`. */
export function lookReading(interpretedQuery: unknown, interpretation: Interpretation | null, typed: string): LookReading | null {
  const searchingFor = text(interpretedQuery);
  if (!searchingFor) return null;
  const translated = interpretation !== null && (interpretation.language === "hu" || interpretation.translatedBy !== "none");
  const own = typed.trim();
  const price = interpretation ? priceLabel(interpretation.minPrice, interpretation.maxPrice) : null;
  const ranked = interpretation ? [interpretation.colour, interpretation.category, interpretation.gender].filter((part): part is string => part !== null) : [];
  return {
    searchingFor,
    from: translated && own && own.toLowerCase() !== searchingFor.toLowerCase() ? own : null,
    chips: [price, interpretation?.size ? `Size ${interpretation.size}` : null].filter((chip): chip is string => chip !== null),
    rankedFor: ranked.length ? ranked.join(" · ") : null,
    notUnderstood: interpretation?.untranslated.length ? interpretation.untranslated.join(", ") : null,
  };
}
