export type SearchInput = {
  query: string;
  maxPrice: string;
};

// Accept a narrow, explicit budget phrase. The budget control remains editable.
export function parseSearchInput(raw: string, currentBudget: string): SearchInput {
  const pattern = /\b(?:under|below|max|maximum|alatt|legfeljebb)\s+(\d[\d\s.,]*)\s*(?:ft|huf)?\b/i;
  const match = raw.match(pattern);
  if (!match) return { query: raw.trim(), maxPrice: currentBudget };
  const amount = match[1].replace(/[\s.,]/g, "");
  if (!/^\d+$/.test(amount)) return { query: raw.trim(), maxPrice: currentBudget };
  return {
    query: raw.replace(match[0], " ").replace(/\s+/g, " ").trim(),
    maxPrice: amount,
  };
}
