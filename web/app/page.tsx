"use client";

/* eslint-disable @next/next/no-img-element -- Do not proxy retailer images; previews are local blob URLs. */

import { FormEvent, KeyboardEvent as ReactKeyboardEvent, memo, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { clickEvent, errorEvent, MetricEvent, searchEvent, SearchType } from "../lib/analytics";
import { Box, boxFromPoints, containedRect, CROP_PRESETS, fitSide, imagePoint, keyBox, overlayRect, pixelBox, Point, Rect, sameGeometry } from "../lib/crop";
import { Alternative, categoryName, parsePhotoQuery, PhotoQuery, priceFreshness, understandingLabel, variantLabel } from "../lib/photo";
import { lookReading, LookReading, parseInterpretation } from "../lib/look";
import { parseSearchInput, SearchInput } from "../lib/query";

type Offer = {
  id: number;
  source_key: string;
  title: string;
  category: string | null;
  price_amount: string;
  currency: "HUF";
  source_url: string;
  image_urls: string[];
  colour_names: string[];
  size_options: string[];
  availability_status: "in_stock" | "out_of_stock" | "unknown";
  last_checked_at?: string;
  match_type: "same_item" | "close_alternative" | "style_inspiration";
  attribution: string | null;
  variant_count?: number;
  variant_colours?: string[];
};

type SearchResponse = { items: Offer[]; next_cursor: string | null; preferred_category?: string; query?: unknown; interpreted_query?: unknown; interpretation?: unknown };

// The preview uses object-fit: contain, so find where the photo really sits in its frame.
function measure(element: HTMLImageElement | null): { frame: DOMRect; image: Rect } | null {
  if (!element || !element.naturalWidth) return null;
  const frame = element.getBoundingClientRect();
  return { frame, image: containedRect(frame.width, frame.height, element.naturalWidth, element.naturalHeight) };
}

// Keep the old state object when the numbers are unchanged, so pointermove does not re-render for nothing.
const keepIfSame = <T extends Box | Rect>(next: T | null) => (old: T | null) => (sameGeometry(old, next) ? old : next);

const MAX_UPLOAD_SIDE = 1024;
// Photo search shows five matches, the number the release gate measures; up to 20 are fetched
// at once so "Show more" is instant and does not run the model again.
const PHOTO_SHOWN = 5;
const PHOTO_FETCHED = 20;

// Re-encode every upload: EXIF rotation is applied, metadata such as GPS is dropped, only the crop is kept
// and the long side is at most 1024 px. Throws when the browser cannot decode the photo.
async function normalisedPhoto(file: File, box: Box | null): Promise<File> {
  // An engine that predates the "from-image" value rejects the option itself (TypeError), not the photo; decode with its default then.
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" }).catch((cause: unknown) => {
    if (cause instanceof TypeError) return createImageBitmap(file);
    throw cause;
  });
  try {
    const { sx, sy, sw, sh } = box ? pixelBox(box, bitmap.width, bitmap.height) : { sx: 0, sy: 0, sw: bitmap.width, sh: bitmap.height };
    const { width, height } = fitSide(sw, sh, MAX_UPLOAD_SIDE);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas is unavailable.");
    // JPEG has no transparency: paint see-through PNG backgrounds white, not black.
    context.fillStyle = "#fff";
    context.fillRect(0, 0, width, height);
    context.imageSmoothingQuality = "high";
    context.drawImage(bitmap, sx, sy, sw, sh, 0, 0, width, height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
    if (!blob) throw new Error("The photo could not be encoded.");
    return new File([blob], "photo.jpg", { type: "image/jpeg" });
  } finally {
    bitmap.close();
  }
}

// Errors arrive as {"detail": "..."}, but a proxy may answer with HTML or nothing (e.g. a 413), so never assume JSON.
async function errorDetail(response: Response): Promise<string | null> {
  const body: unknown = await response.json().catch(() => null);
  return body && typeof body === "object" && "detail" in body && typeof body.detail === "string" && body.detail.trim() ? body.detail : null;
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
const OFFLINE = "The API is unavailable. Start it locally and try again.";
const CATALOGUE_PENDING = "Photo matching is awaiting an approved image catalogue. Try text search for now.";
const FILTER_KEYS = ["category", "colour", "size", "max_price", "source"];
const money = new Intl.NumberFormat("hu-HU", { maximumFractionDigits: 0 });
const checkedDate = new Intl.DateTimeFormat("hu-HU", { dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Budapest" });
const pct = (value: number) => Math.round(value * 100);

function formatHuf(amount: string) {
  const [whole, fraction] = amount.split(".");
  return `${money.format(BigInt(whole))}${fraction ? `,${fraction}` : ""} Ft`;
}

function sourceName(key: string) {
  const names: Record<string, string> = { hm: "H&M", pullandbear: "Pull&Bear", zara: "Zara", reserved: "Reserved", sinsay: "Sinsay", mohito: "Mohito", house: "House", cropp: "Cropp", aboutyou: "ABOUT YOU", answear: "Answear", demo: "Local demo", practice: "Practice data" };
  return names[key] ?? key;
}

// Demo and practice offers are not for sale and their links do not resolve.
const SAMPLE_LABELS: Record<string, string> = { demo: "Sample item", practice: "Practice item" };

function matchName(value: Offer["match_type"]) {
  return {
    same_item: "Verified same item",
    close_alternative: "Close alternative",
    style_inspiration: "Style inspiration",
  }[value];
}

// Retailer images get no referrer and fall back to the placeholder when the CDN refuses them or the URL is gone.
function OfferCard({ item, onOpen, now }: { item: Offer; onOpen: (sourceKey: string) => void; now: Date | null }) {
  const [imageFailed, setImageFailed] = useState(false);
  const image = imageFailed ? undefined : item.image_urls[0];
  const variants = variantLabel(item.variant_count ?? 0, item.variant_colours);
  const freshness = now ? priceFreshness(item.last_checked_at, now) : null;
  const sample = SAMPLE_LABELS[item.source_key];
  const stock = item.availability_status === "unknown" ? "not confirmed" : item.availability_status.replace("_", " ");
  return (
    <article className="offer-card">
      <div className="offer-image">
        {image ? <img src={image} alt={item.title} lang="hu" loading="lazy" referrerPolicy="no-referrer" onError={() => setImageFailed(true)} /> : <div className="image-placeholder"><span>F/F</span><small>IMAGE NOT AVAILABLE</small></div>}
        <span className="match-pill">{matchName(item.match_type)}</span>
        {variants && <span className="variant-pill" title={item.variant_colours?.length ? `Also in: ${item.variant_colours.join(", ")}` : "Similar listings of this product from the same shop"}>{variants}<span className="visually-hidden">{item.variant_colours?.length ? `: also in ${item.variant_colours.join(", ")}` : " similar listings of this product"}</span></span>}
      </div>
      <div className="offer-info">
        <span className="offer-source">{sourceName(item.source_key)}</span>
        <h3 lang="hu">{item.title}</h3>
        <div className="offer-bottom">
          <strong>{formatHuf(item.price_amount)}</strong>
          {sample ? <span className="demo-item-label">{sample}</span> : <a href={item.source_url} target="_blank" rel="noopener noreferrer" onClick={() => onOpen(item.source_key)}>View item<span className="visually-hidden">: <span lang="hu">{item.title}</span> at {sourceName(item.source_key)}</span> <span aria-hidden>↗</span></a>}
        </div>
        <p className="offer-meta">
          {freshness ? <time className={freshness.stale ? "freshness stale" : "freshness"} dateTime={item.last_checked_at} title={`Checked ${checkedDate.format(new Date(item.last_checked_at ?? ""))}`}>{freshness.stale && <span aria-hidden>! </span>}{freshness.text}{freshness.stale && <span className="visually-hidden">; the price may have changed</span>}</time> : null}
          {freshness ? " · " : ""}Stock {stock}
        </p>
        {item.attribution && <p className="offer-meta">{item.attribution}</p>}
      </div>
    </article>
  );
}

// Memoised so typing in the search form does not re-render every card.
const OfferGrid = memo(function OfferGrid({ items, onOpen, now }: { items: Offer[]; onOpen: (sourceKey: string) => void; now: Date | null }) {
  return <div className="offer-grid">{items.map((item) => <OfferCard key={item.id} item={item} onOpen={onOpen} now={now} />)}</div>;
});

function LoadingGrid() {
  return <div className="offer-grid loading-grid" aria-hidden="true">{[0, 1, 2, 3].map((key) => <div className="offer-card skeleton" key={key}><div className="offer-image" /><div className="offer-info"><span /><span /><span /></div></div>)}</div>;
}

export default function Home() {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [colour, setColour] = useState("");
  const [size, setSize] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [source, setSource] = useState("");
  const [sortChoice, setSortChoice] = useState<string | null>(null);
  const [items, setItems] = useState<Offer[]>([]);
  const [shown, setShown] = useState<number | null>(null);
  const [resultsAt, setResultsAt] = useState<Date | null>(null);
  const [look, setLook] = useState<LookReading | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [photoMessage, setPhotoMessage] = useState("");
  const [photoBusy, setPhotoBusy] = useState(false);
  const [photoQuery, setPhotoQuery] = useState<PhotoQuery | null>(null);
  const [highlight, setHighlight] = useState<Box | null>(null);
  const [photoCapability, setPhotoCapability] = useState(false);
  const [lookCapability, setLookCapability] = useState(false);
  const [matchByLook, setMatchByLook] = useState(false);
  const [cropMode, setCropMode] = useState(false);
  const [crop, setCrop] = useState<Box | null>(null);
  const [draftCrop, setDraftCrop] = useState<Box | null>(null);
  const [imageRect, setImageRect] = useState<Rect | null>(null);
  const previewImage = useRef<HTMLImageElement | null>(null);
  const drag = useRef<{ pointerId: number; start: Point } | null>(null);
  const [analyticsEnabled, setAnalyticsEnabled] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const requestSerial = useRef(0);
  const inFlight = useRef<AbortController | null>(null);
  const lastTextParams = useRef<URLSearchParams | null>(null);
  const lastSearchType = useRef<SearchType>("text");

  const sendMetric = useCallback((event: MetricEvent) => {
    if (!analyticsEnabled) return;
    void fetch(`${API_BASE}/api/v1/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
      keepalive: true,
    }).catch(() => undefined);
  }, [analyticsEnabled]);

  const openOffer = useCallback((sourceKey: string) => sendMetric(clickEvent(lastSearchType.current, sourceKey)), [sendMetric]);

  useEffect(() => {
    return () => {
      if (preview) URL.revokeObjectURL(preview);
    };
  }, [preview]);

  useEffect(() => {
    void fetch(`${API_BASE}/api/v1/health`)
      .then((response) => response.json())
      .then((health: { image_search?: string; text_to_image?: string; analytics_enabled?: boolean; demo_mode?: boolean }) => {
        setPhotoCapability(health.image_search === "available");
        setLookCapability(health.text_to_image === "available");
        setAnalyticsEnabled(health.analytics_enabled === true);
        setDemoMode(health.demo_mode === true);
      })
      .catch(() => setPhotoCapability(false));
  }, []);

  useEffect(() => {
    const remeasure = () => setImageRect(keepIfSame(measure(previewImage.current)?.image ?? null));
    window.addEventListener("resize", remeasure);
    return () => window.removeEventListener("resize", remeasure);
  }, []);

  function pointerPoint(event: ReactPointerEvent<HTMLDivElement>): Point | null {
    const measured = measure(previewImage.current);
    if (!measured) return null;
    setImageRect(keepIfSame(measured.image));
    return imagePoint(event.clientX - measured.frame.left, event.clientY - measured.frame.top, measured.image);
  }

  // The shopper's own box replaces what the last search focused on.
  function chooseCrop(box: Box | null) {
    setCrop(box);
    setHighlight(null);
  }

  // Track one pointer and the primary button only, so a second finger or a right-click cannot hijack the crop.
  function startCrop(event: ReactPointerEvent<HTMLDivElement>) {
    if (drag.current || event.button !== 0) return;
    const point = pointerPoint(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointerId: event.pointerId, start: point };
    setDraftCrop(null);
  }

  function moveCrop(event: ReactPointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (!active || active.pointerId !== event.pointerId) return;
    const point = pointerPoint(event);
    if (point) setDraftCrop(keepIfSame(boxFromPoints(active.start, point)));
  }

  function endCrop(event: ReactPointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (!active || active.pointerId !== event.pointerId) return;
    const point = pointerPoint(event);
    const box = point ? boxFromPoints(active.start, point) : null;
    drag.current = null;
    setDraftCrop(null);
    if (box) chooseCrop(box);
  }

  // The browser took the gesture or capture was lost: drop the drag and keep the previous crop.
  function cancelCrop(event: ReactPointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    setDraftCrop(null);
  }

  function nudgeCrop(event: ReactKeyboardEvent<HTMLDivElement>) {
    const next = crop && keyBox(crop, event.key, event.shiftKey);
    if (!next) return;
    event.preventDefault();
    setCrop(next);
  }

  function toggleCropMode() {
    drag.current = null;
    setDraftCrop(null);
    setCropMode((on) => !on);
  }

  // Text, look, photo and load-more share one controller: a newer request aborts the older one,
  // and a late answer from an outdated request is ignored.
  function begin() {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    const serial = ++requestSerial.current;
    setBusy(true);
    return { signal: controller.signal, stale: () => serial !== requestSerial.current };
  }

  function filterParams(chosen: SearchInput) {
    const params = new URLSearchParams();
    if (chosen.query.trim()) params.set("q", chosen.query.trim());
    if (category) params.set("category", category);
    if (colour.trim()) params.set("colour", colour.trim());
    if (size.trim()) params.set("size", size.trim());
    if (chosen.maxPrice) params.set("max_price", chosen.maxPrice);
    if (source) params.set("source", source);
    return params;
  }

  // "search/look" ranks product photos by the description instead of matching words in titles; it has no cursor.
  async function runSearch(path: "search" | "search/look", params: URLSearchParams, cursor?: string) {
    const { signal, stale } = begin();
    setStatus("loading");
    setError("");
    setPhotoMessage("");
    setPhotoBusy(false);
    if (!cursor) {
      setItems([]);
      setShown(null);
      setLook(null);
      setNextCursor(null);
    }
    const request = new URLSearchParams(params);
    if (cursor) request.set("cursor", cursor);
    const fail = (message: string, code: number | null) => {
      if (stale()) return;
      setError(message);
      setStatus("error");
      sendMetric(errorEvent("text", code));
    };
    try {
      const response = await fetch(`${API_BASE}/api/v1/${path}?${request}`, { cache: "no-store", signal });
      if (!response.ok) return fail((await errorDetail(response)) ?? `Search failed (${response.status}).`, response.status);
      const result = (await response.json().catch(() => null)) as SearchResponse | null;
      if (!result) return fail("The search response could not be read.", null);
      if (stale()) return;
      if (result.items.some((item) => item.source_key === "demo")) setDemoMode(true);
      setItems((old) => (cursor ? [...old, ...result.items] : result.items));
      setResultsAt(new Date());
      setNextCursor(path === "search" ? result.next_cursor : null);
      // Look search reports the English words the model received and how it read the rest.
      if (path === "search/look") {
        const interpretation = parseInterpretation(result.interpretation);
        setLook(lookReading(result.interpreted_query, interpretation, params.get("q") ?? ""));
        // A budget the server found in the words fills an empty budget field for the next search.
        if (interpretation?.maxPrice != null && !params.has("max_price")) setMaxPrice(String(Math.round(interpretation.maxPrice)));
      }
      setStatus("ready");
      lastSearchType.current = "text";
      if (cursor) return;
      // The server cursor is bound to these exact filters, so Load more must reuse them, not the live form.
      if (path === "search") lastTextParams.current = params;
      sendMetric(searchEvent("text", FILTER_KEYS.some((key) => params.has(key)), result.items.length));
    } catch {
      fail(OFFLINE, null);
    } finally {
      if (!stale()) setBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = parseSearchInput(query, maxPrice);
    setQuery(parsed.query);
    setMaxPrice(parsed.maxPrice);
    const params = filterParams(parsed);
    params.set("limit", "20");
    if (matchByLook && lookCapability) {
      void runSearch("search/look", params);
      return;
    }
    // Best match ranks by words, so it is the default with a query; without one, cheapest first.
    const hasQuery = params.has("q");
    params.set("sort", sortChoice && (hasQuery || sortChoice !== "relevance") ? sortChoice : hasQuery ? "relevance" : "price-asc");
    void runSearch("search", params);
  }

  function loadMore() {
    if (lastTextParams.current && nextCursor) void runSearch("search", lastTextParams.current, nextCursor);
  }

  function choosePhoto(file: File | null) {
    setPhoto(file);
    setPhotoMessage("");
    setPhotoQuery(null);
    setHighlight(null);
    setCrop(null);
    setDraftCrop(null);
    setCropMode(false);
    drag.current = null;
    setImageRect(null);
    setPreview(file ? URL.createObjectURL(file) : null);
  }

  // `box` is the area to send (null: the whole photo). A shopper's box, an alternative garment or
  // "Use whole photo" are sent as crop=manual, so the server does not crop again.
  async function searchPhoto(box: Box | null = crop, manual = box !== null) {
    if (!photo) return;
    if (!photoCapability) {
      setPhotoMessage(CATALOGUE_PENDING);
      return;
    }
    const { signal, stale } = begin();
    setPhotoBusy(true);
    // What the last search saw no longer describes this one, even if this one fails.
    setPhotoQuery(null);
    setHighlight(null);
    setPhotoMessage("Looking at your photo…");
    setStatus("loading");
    setError("");
    setItems([]);
    setLook(null);
    let responseStatus: number | null = null;
    const fail = (message: string) => {
      setPhotoMessage(message);
      setStatus("idle");
      sendMetric(errorEvent("photo", responseStatus));
    };
    try {
      const upload = await normalisedPhoto(photo, box).catch(() => null);
      if (stale()) return;
      if (!upload) return fail("This photo could not be read. Try a JPEG, PNG or WebP photo.");
      const data = new FormData();
      data.append("file", upload);
      const filters = filterParams({ query: "", maxPrice });
      for (const [key, value] of filters) data.append(key, value);
      if (manual) data.append("crop", "manual");
      data.append("limit", String(PHOTO_FETCHED));
      const response = await fetch(`${API_BASE}/api/v1/search/image`, { method: "POST", body: data, signal });
      responseStatus = response.status;
      if (!response.ok) {
        const detail = await errorDetail(response);
        if (stale()) return;
        const notConfigured = response.status === 503 && (!detail || /not configured/i.test(detail));
        return fail(notConfigured ? CATALOGUE_PENDING : detail ?? (response.status === 413 ? "This photo is too large to upload." : `Photo search failed (${response.status}).`));
      }
      const result = (await response.json()) as SearchResponse;
      if (stale()) return;
      const understood = parsePhotoQuery(result.query);
      setPhotoQuery(understood);
      setHighlight(null);
      setItems(result.items);
      setShown(Math.min(PHOTO_SHOWN, result.items.length));
      setResultsAt(new Date());
      setNextCursor(null);
      setStatus("ready");
      lastSearchType.current = "photo";
      sendMetric(searchEvent("photo", FILTER_KEYS.some((key) => filters.has(key)), Math.min(PHOTO_SHOWN, result.items.length)));
      const grouped = result.preferred_category ? ` Most close matches are ${result.preferred_category}, so those come first.` : "";
      setPhotoMessage((demoMode ? "Showing visual matches from five CC0 sample images. Results are style inspiration, not verified products." : "Showing experimental visual matches. Check each product on the retailer website.") + grouped);
    } catch {
      if (stale()) return;
      fail(responseStatus === null ? OFFLINE : "The photo search response could not be read.");
    } finally {
      if (!stale()) {
        setBusy(false);
        setPhotoBusy(false);
      }
    }
  }

  function searchAlternative(alternative: Alternative) {
    chooseCrop(alternative.crop);
    setCropMode(false);
    void searchPhoto(alternative.crop, true);
  }

  function searchWholePhoto() {
    chooseCrop(null);
    void searchPhoto(null, true);
  }

  const sort = sortChoice ?? (query.trim() ? "relevance" : "price-asc");
  const shownCrop = draftCrop ?? crop;
  const cropOverlay = preview && imageRect && shownCrop ? overlayRect(shownCrop, imageRect) : null;
  // What the server focused on, drawn only while the shopper has no box of their own.
  const autoBox = !shownCrop && photoQuery?.cropSource === "auto" ? photoQuery.crop : null;
  const autoOverlay = preview && imageRect && autoBox ? overlayRect(autoBox, imageRect) : null;
  const highlightOverlay = preview && imageRect && highlight ? overlayRect(highlight, imageRect) : null;
  const cropLabel = crop ? `Crop box: ${pct(crop.x)}% from left, ${pct(crop.y)}% from top, ${pct(crop.width)}% wide, ${pct(crop.height)}% tall` : undefined;
  const visibleItems = useMemo(() => (shown === null ? items : items.slice(0, shown)), [items, shown]);
  const resultCount = `${visibleItems.length} ${visibleItems.length === 1 ? "offer" : "offers"} shown`;
  const emptyTitle = demoMode ? "No sample items found." : "No current offers found.";
  const reading = understandingLabel(photoQuery);
  const sawSomething = photoQuery && (reading || photoQuery.cropSource !== "none" || photoQuery.alternatives.length > 0);
  // One always-mounted live region; screen readers announce changes to its text.
  const understoodText = reading ? ` We searched for ${reading.text}${reading.unsure ? ", but we are not sure" : ""}.` : "";
  const announcement = photoMessage ? photoMessage + (photoBusy ? "" : understoodText) : status === "loading" ? "Finding your options…" : status === "error" ? `Search failed. ${error}` : status === "ready" ? (items.length ? `${resultCount}.` : emptyTitle) : "";

  return (
    <main>
      <div className="site-shell">
        <p className="visually-hidden" role="status" aria-live="polite">{announcement}</p>
        <header className="site-header">
          <div className="brand"><span className="brand-mark">F<span>F</span></span><span>FASHION FINDER</span></div>
          <span className="market-label">HUNGARY · HUF</span>
        </header>
        {demoMode && <div className="demo-banner">LOCAL DEMO · Synthetic products and prices. No retailer links or live stock.</div>}

        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-copy">
            <span className="eyebrow">THE LOOK YOU LOVE, WITHIN REACH</span>
            <h1 id="hero-title">Find the piece.<br /><em>Make it yours.</em></h1>
            <p>{demoMode ? "Explore synthetic sample items to test search, filters, and budgets locally." : "Explore current fashion offers from approved Hungarian shops. Start with a style, a budget, or a photo."}</p>
            <a className="text-link" href="#search">Explore the catalogue <span aria-hidden>↗</span></a>
          </div>
          <div className="hero-art" aria-hidden="true">
            <div className="art-orbit orbit-one" />
            <div className="art-orbit orbit-two" />
            <div className="art-card art-card-one"><span>STYLE / 01</span><div className="art-silhouette coat" /></div>
            <div className="art-card art-card-two"><span>FIND YOUR FIT</span><div className="art-silhouette shirt" /></div>
            <div className="art-note">More style.<br />Less searching.</div>
          </div>
        </section>

        <section className="search-section" id="search" aria-labelledby="search-title">
          <div className="section-heading"><div><span className="eyebrow">DISCOVER</span><h2 id="search-title">Search the catalogue</h2></div><span className="step-label">01 / FIND YOUR STYLE</span></div>
          <div className="search-layout">
            <form className="search-panel" onSubmit={submit}>
              <label className="field-label" htmlFor="query">What are you looking for?</label>
              <div className="search-row"><input id="query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. black jacket under 15000 Ft" /><button className="primary-button" type="submit" disabled={busy}>Search <span aria-hidden>→</span></button></div>
              <p className="field-hint">Tip: type a budget in your search, or set it below.</p>
              {lookCapability && <label className="look-toggle"><input type="checkbox" checked={matchByLook} onChange={(event) => setMatchByLook(event.target.checked)} /> Match by look: rank product photos by your description</label>}
              <div className="filter-grid">
                <label>Category<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">All categories</option><option value="jackets">Jackets</option><option value="coats">Coats</option><option value="dresses">Dresses</option><option value="sweaters">Sweaters</option><option value="shirts">Shirts</option><option value="blouses">Blouses</option><option value="tops">Tops</option><option value="trousers">Trousers</option><option value="jeans">Jeans</option><option value="skirts">Skirts</option><option value="shorts">Shorts</option><option value="shoes">Shoes</option><option value="bags">Bags</option><option value="accessories">Accessories</option></select></label>
                <label>Colour<input value={colour} onChange={(event) => setColour(event.target.value)} placeholder="Any colour, e.g. black or fekete" /></label>
                <label>Maximum price · Ft<input type="number" min="0" step="1" value={maxPrice} onChange={(event) => setMaxPrice(event.target.value)} placeholder="No limit" /></label>
                <label>Size<input value={size} onChange={(event) => setSize(event.target.value)} placeholder="Any size" /></label>
                <label>Shop<select value={source} onChange={(event) => setSource(event.target.value)}><option value="">{demoMode ? "All demo items" : "All approved shops"}</option>{demoMode ? <option value="demo">Local demo</option> : <><option value="reserved">Reserved</option><option value="sinsay">Sinsay</option><option value="mohito">Mohito</option><option value="house">House</option><option value="cropp">Cropp</option><option value="zara">Zara</option><option value="hm">H&amp;M</option><option value="pullandbear">Pull&amp;Bear</option><option value="aboutyou">ABOUT YOU</option><option value="answear">Answear</option></>}</select></label>
                <label>Sort by<select value={sort} onChange={(event) => setSortChoice(event.target.value)}><option value="relevance">Best match</option><option value="price-asc">Price: low to high</option><option value="price-desc">Price: high to low</option><option value="recent">Recently checked</option></select></label>
              </div>
            </form>
            <div className="photo-panel" aria-busy={photoBusy}>
              <span className="photo-icon" aria-hidden="true">◎</span>
              <span className="eyebrow">HAVE A PHOTO?</span>
              <h3>Start with a look.</h3>
              <p>{demoMode ? "Upload a photo to compare it with five CC0 demo images. Colour and layout guide this prototype search." : photoCapability ? "Compare your photo with locally indexed, permitted offers. We focus on the main garment; you can crop it yourself. Visual ranking is experimental." : "Preview your photo here. Visual matching opens when approved product images are available."}</p>
              <label className="upload-button" htmlFor="photo">{photo ? "Choose another photo" : "Choose a photo"} <span aria-hidden>＋</span></label>
              <input id="photo" type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => choosePhoto(event.target.files?.[0] ?? null)} />
              {preview && (
                <div className="crop-tools">
                  <button className="crop-toggle" type="button" aria-pressed={cropMode} onClick={toggleCropMode}>Crop</button>
                  {cropMode && <><button className="text-button" type="button" onClick={() => chooseCrop(CROP_PRESETS.top)}>Top half</button><button className="text-button" type="button" onClick={() => chooseCrop(CROP_PRESETS.bottom)}>Bottom half</button><button className="text-button" type="button" onClick={() => chooseCrop(CROP_PRESETS.centre)}>Centre</button></>}
                  {crop && <button className="text-button" type="button" onClick={() => chooseCrop(null)}>Clear crop</button>}
                </div>
              )}
              <div className={`photo-preview${preview ? " has-photo" : ""}${cropMode ? " cropping" : ""}${photoBusy ? " scanning" : ""}`} onPointerDown={cropMode ? startCrop : undefined} onPointerMove={cropMode ? moveCrop : undefined} onPointerUp={cropMode ? endCrop : undefined} onPointerCancel={cropMode ? cancelCrop : undefined} onLostPointerCapture={cropMode ? cancelCrop : undefined}>
                {preview ? <img ref={previewImage} src={preview} alt="Your selected clothing photo" draggable={false} onLoad={() => setImageRect(keepIfSame(measure(previewImage.current)?.image ?? null))} /> : <span>Your preview appears here</span>}
                {autoOverlay && <div className="auto-box" style={{ left: autoOverlay.left, top: autoOverlay.top, width: autoOverlay.width, height: autoOverlay.height }} aria-hidden="true"><span>Auto focus</span></div>}
                {highlightOverlay && <div className="alt-box" style={{ left: highlightOverlay.left, top: highlightOverlay.top, width: highlightOverlay.width, height: highlightOverlay.height }} aria-hidden="true" />}
                {cropOverlay && <div className="crop-box" style={{ left: cropOverlay.left, top: cropOverlay.top, width: cropOverlay.width, height: cropOverlay.height }} tabIndex={cropMode && crop ? 0 : undefined} role={cropMode && crop ? "group" : undefined} aria-label={cropMode ? cropLabel : undefined} aria-describedby={cropMode && crop ? "crop-hint" : undefined} onKeyDown={cropMode ? nudgeCrop : undefined} />}
              </div>
              {preview && <p className="photo-hint" id="crop-hint">{cropMode ? "Drag across the garment or pick a preset. Arrow keys move the box; Shift+arrows resize it." : crop ? "Only the boxed area is compared." : autoBox ? "We compared the boxed garment. Use Crop to choose another area." : "We look for the main garment. Use Crop to choose it yourself."}</p>}
              {photo && <button className="photo-search" type="button" disabled={busy} onClick={() => void searchPhoto()}>{photoBusy ? "Looking…" : "Find similar styles →"}</button>}
              {photoMessage && <p className="photo-message">{photoMessage}</p>}
              {sawSomething && !photoBusy && (
                <div className="understood" role="group" aria-label="What the photo search saw">
                  <div className="understood-row">
                    <span className="understood-label">We searched for</span>
                    {reading ? <span className={`chip${reading.unsure ? " chip-unsure" : ""}`}>{reading.text}{reading.unsure && <span aria-hidden> ?</span>}</span> : <span className="chip chip-unsure">this look</span>}
                    {photoQuery?.cropSource === "auto" && <button className="text-button" type="button" disabled={busy} onClick={searchWholePhoto}>Use whole photo</button>}
                  </div>
                  {reading?.unsure && <p className="understood-hint">Not sure we got it right. Crop the garment to steer the search.</p>}
                  {photoQuery && photoQuery.alternatives.length > 0 && (
                    <div className="understood-row">
                      <span className="understood-label">Also in this photo</span>
                      {photoQuery.alternatives.map((alternative) => (
                        <button key={`${alternative.category}-${alternative.crop.x}-${alternative.crop.y}`} className="chip-button" type="button" disabled={busy} onClick={() => searchAlternative(alternative)} onMouseEnter={() => setHighlight(alternative.crop)} onMouseLeave={() => setHighlight(null)} onFocus={() => setHighlight(alternative.crop)} onBlur={() => setHighlight(null)}>
                          {categoryName(alternative.category)}<span className="visually-hidden">: search for this item in your photo instead</span> <span aria-hidden>→</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </section>

        <section className="results-section" aria-labelledby="results-title" aria-busy={status === "loading"}>
          <div className="section-heading"><div><span className="eyebrow">CURATED FOR YOUR SEARCH</span><h2 id="results-title">The finds</h2></div><span className="results-count">{status === "ready" ? resultCount : demoMode ? "DEMO CATALOGUE" : "LIVE CATALOGUE"}</span></div>
          {look && status === "ready" && (
            <div className="interpreted" role="group" aria-label="How we read your search">
              <p>Searching for: <strong>{look.searchingFor}</strong>{look.from && <> (from &apos;<span lang="hu">{look.from}</span>&apos;)</>}</p>
              {(look.chips.length > 0 || look.rankedFor) && (
                <div className="understood-row">
                  {look.chips.map((chip) => <span className="chip chip-light" key={chip}>{chip}</span>)}
                  {look.rankedFor && <span className="ranked-for">Ranked for: {look.rankedFor}</span>}
                </div>
              )}
              {look.notUnderstood && <p className="not-understood">Not understood: <span lang="hu">{look.notUnderstood}</span>. Try other words.</p>}
            </div>
          )}
          {status === "idle" && <div className="empty-state"><span>✦</span><h3>Your next favourite starts here.</h3><p>{demoMode ? "Search synthetic items by style, category, colour, or budget." : "Search by style, category, colour, or budget to see approved offers."}</p></div>}
          {status === "loading" && items.length === 0 && <><p className="loading-note">{photoBusy ? "Looking at your photo and finding similar styles…" : "Finding your options…"}</p><LoadingGrid /></>}
          {status === "error" && <div className="empty-state error-state" role="alert"><h3>Search failed</h3><p>{error}</p></div>}
          {status === "ready" && items.length === 0 && <div className="empty-state"><span>◇</span><h3>{emptyTitle}</h3><p>{shown !== null ? "No permitted offer looks close enough. Try a wider budget, fewer filters, or crop the garment." : demoMode ? "Try a wider budget or another category." : "Try a wider budget or category. Only approved, recently checked offers appear here."}</p></div>}
          {visibleItems.length > 0 && <OfferGrid items={visibleItems} onOpen={openOffer} now={resultsAt} />}
          {status === "ready" && nextCursor && <button className="load-more" type="button" disabled={busy} onClick={loadMore}>Load more finds ↓</button>}
          {status === "ready" && shown !== null && shown < items.length && <button className="load-more" type="button" onClick={() => setShown(items.length)}>Show {items.length - shown} more similar styles ↓</button>}
        </section>
        <footer><span>FASHION FINDER</span><p>{demoMode ? "Local demo uses synthetic offers and prices; photo search compares five CC0 examples and cannot verify product identity." : "Prices and availability are checked at the time shown. Final details are on the retailer website."}</p>{analyticsEnabled && <p>We count anonymous searches, errors, and retailer clicks. We do not store your query or photo.</p>}</footer>
      </div>
    </main>
  );
}
