export type SearchInput = {
  query: string;
  maxPrice: string;
};

// Accept narrow, explicit budget phrases. The budget control remains editable.
// English and Hungarian word orders: "under 15000 Ft", "10 000 Ft alatt", "15000 Ft-ig".
const BUDGET_FIRST = /\b(?:under|below|max|maximum|alatt|legfeljebb)\s+(\d[\d\s.,]*)\s*(?:ft|huf)?\b/i;
const AMOUNT_FIRST = /(\d[\d\s.,]*?)\s*(ft|huf|forint)?(?:\s+alatt\b|-ig\b)/i;

export function parseSearchInput(raw: string, currentBudget: string): SearchInput {
  const before = raw.match(BUDGET_FIRST);
  const after = before ? null : raw.match(AMOUNT_FIRST);
  const match = before ?? after;
  if (!match) return { query: raw.trim(), maxPrice: currentBudget };
  const amount = match[1].replace(/[\s.,]/g, "");
  // "38 alatt" is more likely a size than a budget: without a currency, ask for four digits.
  if (!/^\d+$/.test(amount) || (after && !after[2] && amount.length < 4)) return { query: raw.trim(), maxPrice: currentBudget };
  return {
    query: raw.replace(match[0], " ").replace(/\s+/g, " ").trim(),
    maxPrice: amount,
  };
}
