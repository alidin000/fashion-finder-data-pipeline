export type SearchType = "text" | "photo";

export type MetricEvent =
  | { event_type: "search"; search_type: SearchType; filtered: boolean; result_count: number }
  | { event_type: "click"; search_type: SearchType; source_key: string }
  | { event_type: "error"; search_type: SearchType; error_code: "http_4xx" | "http_5xx" | "network" };

export function searchEvent(searchType: SearchType, filtered: boolean, resultCount: number): MetricEvent {
  return { event_type: "search", search_type: searchType, filtered, result_count: Math.max(0, Math.min(10000, resultCount)) };
}

export function clickEvent(searchType: SearchType, sourceKey: string): MetricEvent {
  return { event_type: "click", search_type: searchType, source_key: sourceKey };
}

export function errorEvent(searchType: SearchType, status: number | null): MetricEvent {
  const errorCode = status === null ? "network" : status >= 500 ? "http_5xx" : "http_4xx";
  return { event_type: "error", search_type: searchType, error_code: errorCode };
}
