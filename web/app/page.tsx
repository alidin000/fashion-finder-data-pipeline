"use client";

/* eslint-disable @next/next/no-img-element -- Do not proxy retailer images; previews are local blob URLs. */

import { FormEvent, PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";
import { clickEvent, errorEvent, MetricEvent, searchEvent, SearchType } from "../lib/analytics";
import { Box, boxFromPoints, containedRect, imagePoint, overlayRect, pixelBox, Point, Rect } from "../lib/crop";
import { parseSearchInput } from "../lib/query";

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
  last_checked_at: string;
  match_type: "same_item" | "close_alternative" | "style_inspiration";
  attribution: string | null;
};

type SearchResponse = { items: Offer[]; next_cursor: string | null; preferred_category?: string };

// The preview uses object-fit: contain, so find where the photo really sits in its frame.
function measure(element: HTMLImageElement | null): { frame: DOMRect; image: Rect } | null {
  if (!element || !element.naturalWidth) return null;
  const frame = element.getBoundingClientRect();
  return { frame, image: containedRect(frame.width, frame.height, element.naturalWidth, element.naturalHeight) };
}

// Crop in the browser so only the garment is compared, not the person or background.
async function croppedPhoto(file: File, box: Box | null): Promise<File> {
  if (!box) return file;
  const bitmap = await createImageBitmap(file);
  try {
    const { sx, sy, sw, sh } = pixelBox(box, bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = sw;
    canvas.height = sh;
    const context = canvas.getContext("2d");
    if (!context) return file;
    context.drawImage(bitmap, sx, sy, sw, sh, 0, 0, sw, sh);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    return blob ? new File([blob], "crop.jpg", { type: "image/jpeg" }) : file;
  } finally {
    bitmap.close();
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
const money = new Intl.NumberFormat("hu-HU", { maximumFractionDigits: 0 });

function formatHuf(amount: string) {
  const [whole, fraction] = amount.split(".");
  return `${money.format(BigInt(whole))}${fraction ? `,${fraction}` : ""} Ft`;
}

function sourceName(key: string) {
  const names: Record<string, string> = { hm: "H&M", pullandbear: "Pull&Bear", zara: "Zara", reserved: "Reserved", sinsay: "Sinsay", mohito: "Mohito", house: "House", cropp: "Cropp", aboutyou: "ABOUT YOU", answear: "Answear", demo: "Local demo" };
  return names[key] ?? key;
}

function matchName(value: Offer["match_type"]) {
  return {
    same_item: "Verified same item",
    close_alternative: "Close alternative",
    style_inspiration: "Style inspiration",
  }[value];
}

export default function Home() {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [colour, setColour] = useState("");
  const [size, setSize] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [source, setSource] = useState("");
  const [sort, setSort] = useState("price-asc");
  const [items, setItems] = useState<Offer[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [photoMessage, setPhotoMessage] = useState("");
  const [photoCapability, setPhotoCapability] = useState(false);
  const [lookCapability, setLookCapability] = useState(false);
  const [matchByLook, setMatchByLook] = useState(false);
  const [crop, setCrop] = useState<Box | null>(null);
  const [draftCrop, setDraftCrop] = useState<Box | null>(null);
  const [imageRect, setImageRect] = useState<Rect | null>(null);
  const previewImage = useRef<HTMLImageElement | null>(null);
  const dragStart = useRef<Point | null>(null);
  const [analyticsEnabled, setAnalyticsEnabled] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const requestSerial = useRef(0);
  const lastSearchType = useRef<SearchType>("text");

  function sendMetric(event: MetricEvent) {
    if (!analyticsEnabled) return;
    void fetch(`${API_BASE}/api/v1/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
      keepalive: true,
    }).catch(() => undefined);
  }

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
    const remeasure = () => setImageRect(measure(previewImage.current)?.image ?? null);
    window.addEventListener("resize", remeasure);
    return () => window.removeEventListener("resize", remeasure);
  }, []);

  function pointerPoint(event: ReactPointerEvent<HTMLDivElement>): Point | null {
    const measured = measure(previewImage.current);
    if (!measured) return null;
    setImageRect(measured.image);
    return imagePoint(event.clientX - measured.frame.left, event.clientY - measured.frame.top, measured.image);
  }

  function startCrop(event: ReactPointerEvent<HTMLDivElement>) {
    const point = pointerPoint(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStart.current = point;
    setDraftCrop(null);
  }

  function moveCrop(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart.current) return;
    const point = pointerPoint(event);
    if (point) setDraftCrop(boxFromPoints(dragStart.current, point));
  }

  function endCrop(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart.current) return;
    const point = pointerPoint(event);
    const box = point ? boxFromPoints(dragStart.current, point) : null;
    dragStart.current = null;
    setDraftCrop(null);
    if (box) setCrop(box);
  }

  async function search(cursor?: string, override?: { query: string; maxPrice: string }) {
    const serial = ++requestSerial.current;
    setStatus("loading");
    setError("");
    if (!cursor) setItems([]);
    const params = new URLSearchParams();
    const chosenQuery = override?.query ?? query;
    const chosenBudget = override?.maxPrice ?? maxPrice;
    if (chosenQuery.trim()) params.set("q", chosenQuery.trim());
    if (category) params.set("category", category);
    if (colour.trim()) params.set("colour", colour.trim());
    if (size.trim()) params.set("size", size.trim());
    if (chosenBudget) params.set("max_price", chosenBudget);
    if (source) params.set("source", source);
    params.set("sort", sort);
    params.set("limit", "20");
    if (cursor) params.set("cursor", cursor);
    let responseStatus: number | null = null;
    try {
      const response = await fetch(`${API_BASE}/api/v1/search?${params}`, { cache: "no-store" });
      responseStatus = response.status;
      if (!response.ok) throw new Error(`Search failed (${response.status}). Check the API and filters.`);
      const result = (await response.json()) as SearchResponse;
      if (serial !== requestSerial.current) return;
      if (result.items.some((item) => item.source_key === "demo")) setDemoMode(true);
      setItems((old) => (cursor ? [...old, ...result.items] : result.items));
      setNextCursor(result.next_cursor);
      setStatus("ready");
      if (!cursor) sendMetric(searchEvent("text", Boolean(category || colour || size || chosenBudget || source), result.items.length));
    } catch (cause) {
      if (serial !== requestSerial.current) return;
      setError(cause instanceof Error ? cause.message : "Search is unavailable.");
      setStatus("error");
      sendMetric(errorEvent("text", responseStatus));
    }
  }

  // Rank product photos by the description instead of matching words in titles.
  async function searchLook(override: { query: string; maxPrice: string }) {
    const serial = ++requestSerial.current;
    setStatus("loading");
    setError("");
    setItems([]);
    setNextCursor(null);
    const params = new URLSearchParams();
    params.set("q", override.query.trim());
    if (category) params.set("category", category);
    if (colour.trim()) params.set("colour", colour.trim());
    if (size.trim()) params.set("size", size.trim());
    if (override.maxPrice) params.set("max_price", override.maxPrice);
    if (source) params.set("source", source);
    let responseStatus: number | null = null;
    try {
      const response = await fetch(`${API_BASE}/api/v1/search/look?${params}`, { cache: "no-store" });
      responseStatus = response.status;
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Search failed (${response.status}).`);
      }
      const result = (await response.json()) as SearchResponse;
      if (serial !== requestSerial.current) return;
      setItems(result.items);
      setStatus("ready");
      sendMetric(searchEvent("text", Boolean(category || colour || size || override.maxPrice || source), result.items.length));
    } catch (cause) {
      if (serial !== requestSerial.current) return;
      setError(cause instanceof Error ? cause.message : "Search is unavailable.");
      setStatus("error");
      sendMetric(errorEvent("text", responseStatus));
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    lastSearchType.current = "text";
    const parsed = parseSearchInput(query, maxPrice);
    setQuery(parsed.query);
    setMaxPrice(parsed.maxPrice);
    void (matchByLook && lookCapability ? searchLook(parsed) : search(undefined, parsed));
  }

  function choosePhoto(file: File | null) {
    setPhoto(file);
    setPhotoMessage("");
    setCrop(null);
    setDraftCrop(null);
    setImageRect(null);
    setPreview(file ? URL.createObjectURL(file) : null);
  }

  async function searchPhoto() {
    if (!photo) return;
    if (!photoCapability) {
      setPhotoMessage("Photo matching is awaiting an approved image catalogue. Try text search for now.");
      return;
    }
    setPhotoMessage("Checking your photo…");
    let responseStatus: number | null = null;
    const data = new FormData();
    data.append("file", await croppedPhoto(photo, crop).catch(() => photo));
    if (category) data.append("category", category);
    if (colour.trim()) data.append("colour", colour.trim());
    if (size.trim()) data.append("size", size.trim());
    if (maxPrice) data.append("max_price", maxPrice);
    if (source) data.append("source", source);
    try {
      const response = await fetch(`${API_BASE}/api/v1/search/image`, {
        method: "POST",
        body: data,
      });
      responseStatus = response.status;
      if (response.status === 503) {
        setPhotoMessage("Photo matching is awaiting an approved image catalogue. Try text search for now.");
        sendMetric(errorEvent("photo", responseStatus));
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        setPhotoMessage(body.detail ?? "This photo could not be used.");
        sendMetric(errorEvent("photo", responseStatus));
        return;
      }
      const result = (await response.json()) as SearchResponse;
      setItems(result.items);
      setNextCursor(result.next_cursor);
      setStatus("ready");
      lastSearchType.current = "photo";
      sendMetric(searchEvent("photo", Boolean(category || colour || size || maxPrice || source), result.items.length));
      const grouped = result.preferred_category ? ` Most close matches are ${result.preferred_category}, so those come first.` : "";
      setPhotoMessage((demoMode ? "Showing visual matches from five CC0 sample images. Results are style inspiration, not verified products." : "Showing experimental visual matches. Check each product on the retailer website.") + grouped);
    } catch {
      setPhotoMessage("The API is unavailable. Start it locally and try again.");
      sendMetric(errorEvent("photo", responseStatus));
    }
  }

  const shownCrop = draftCrop ?? crop;
  const cropOverlay = preview && imageRect && shownCrop ? overlayRect(shownCrop, imageRect) : null;

  return (
    <main>
      <div className="site-shell">
        <header className="site-header">
          <div className="brand"><span className="brand-mark">F<span>F</span></span><span>FASHION FINDER</span></div>
          <span className="market-label">HUNGARY · HUF</span>
        </header>
        {demoMode && <div className="demo-banner" role="status">LOCAL DEMO · Synthetic products and prices. No retailer links or live stock.</div>}

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
              <div className="search-row"><input id="query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. black jacket under 15000 Ft" /><button className="primary-button" type="submit" disabled={status === "loading"}>Search <span aria-hidden>→</span></button></div>
              <p className="field-hint">Tip: type a budget in your search, or set it below.</p>
              {lookCapability && <label className="look-toggle"><input type="checkbox" checked={matchByLook} onChange={(event) => setMatchByLook(event.target.checked)} /> Match by look: rank product photos by your description</label>}
              <div className="filter-grid">
                <label>Category<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">All categories</option><option value="jackets">Jackets</option><option value="coats">Coats</option><option value="dresses">Dresses</option><option value="sweaters">Sweaters</option><option value="shirts">Shirts</option><option value="blouses">Blouses</option><option value="tops">Tops</option><option value="trousers">Trousers</option><option value="jeans">Jeans</option><option value="skirts">Skirts</option><option value="shorts">Shorts</option><option value="shoes">Shoes</option><option value="bags">Bags</option><option value="accessories">Accessories</option></select></label>
                <label>Colour<input value={colour} onChange={(event) => setColour(event.target.value)} placeholder="Any colour, e.g. black or fekete" /></label>
                <label>Maximum price · Ft<input type="number" min="0" step="1" value={maxPrice} onChange={(event) => setMaxPrice(event.target.value)} placeholder="No limit" /></label>
                <label>Size<input value={size} onChange={(event) => setSize(event.target.value)} placeholder="Any size" /></label>
                <label>Shop<select value={source} onChange={(event) => setSource(event.target.value)}><option value="">{demoMode ? "All demo items" : "All approved shops"}</option>{demoMode ? <option value="demo">Local demo</option> : <><option value="reserved">Reserved</option><option value="sinsay">Sinsay</option><option value="mohito">Mohito</option><option value="house">House</option><option value="cropp">Cropp</option><option value="zara">Zara</option><option value="hm">H&amp;M</option><option value="pullandbear">Pull&amp;Bear</option><option value="aboutyou">ABOUT YOU</option><option value="answear">Answear</option></>}</select></label>
                <label>Sort by<select value={sort} onChange={(event) => setSort(event.target.value)}><option value="price-asc">Price: low to high</option><option value="price-desc">Price: high to low</option><option value="recent">Recently checked</option></select></label>
              </div>
            </form>
            <div className="photo-panel"><span className="photo-icon" aria-hidden="true">◎</span><span className="eyebrow">HAVE A PHOTO?</span><h3>Start with a look.</h3><p>{demoMode ? "Upload a photo to compare it with five CC0 demo images. Colour and layout guide this prototype search." : photoCapability ? "Compare your photo with locally indexed, permitted offers. Visual ranking is experimental." : "Preview your photo here. Visual matching opens when approved product images are available."}</p><label className="upload-button" htmlFor="photo">Choose a photo <span aria-hidden>＋</span></label><input id="photo" type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => choosePhoto(event.target.files?.[0] ?? null)} /><div className={preview ? "photo-preview has-photo" : "photo-preview"} onPointerDown={preview ? startCrop : undefined} onPointerMove={preview ? moveCrop : undefined} onPointerUp={preview ? endCrop : undefined}>{preview ? <img ref={previewImage} src={preview} alt="Your selected clothing photo" draggable={false} onLoad={() => setImageRect(measure(previewImage.current)?.image ?? null)} /> : <span>Your preview appears here</span>}{cropOverlay && <div className="crop-box" style={{ left: cropOverlay.left, top: cropOverlay.top, width: cropOverlay.width, height: cropOverlay.height }} />}</div>{preview && <p className="photo-hint">{crop ? "Only the boxed area is compared." : "Drag across the garment to compare only that part."}{crop && <button className="clear-crop" type="button" onClick={() => setCrop(null)}>Clear crop</button>}</p>}{photo && <button className="photo-search" type="button" onClick={() => void searchPhoto()}>Find similar styles →</button>}{photoMessage && <p className="photo-message" role="status">{photoMessage}</p>}</div>
          </div>
        </section>

        <section className="results-section" aria-labelledby="results-title">
          <div className="section-heading"><div><span className="eyebrow">CURATED FOR YOUR SEARCH</span><h2 id="results-title">The finds</h2></div><span className="results-count">{status === "ready" ? `${items.length} ${items.length === 1 ? "offer" : "offers"} shown` : demoMode ? "DEMO CATALOGUE" : "LIVE CATALOGUE"}</span></div>
          {status === "idle" && <div className="empty-state"><span>✦</span><h3>Your next favourite starts here.</h3><p>{demoMode ? "Search synthetic items by style, category, colour, or budget." : "Search by style, category, colour, or budget to see approved offers."}</p></div>}
          {status === "loading" && <div className="empty-state" role="status"><h3>Finding your options…</h3></div>}
          {status === "error" && <div className="empty-state error-state" role="alert"><h3>Search is unavailable</h3><p>{error}</p></div>}
          {status === "ready" && items.length === 0 && <div className="empty-state"><span>◇</span><h3>{demoMode ? "No sample items found." : "No current offers found."}</h3><p>{demoMode ? "Try a wider budget or another category." : "Try a wider budget or category. Only approved, recently checked offers appear here."}</p></div>}
          {items.length > 0 && <div className="offer-grid">{items.map((item) => <article className="offer-card" key={item.id}><div className="offer-image">{item.image_urls[0] ? <img src={item.image_urls[0]} alt={item.title} loading="lazy" /> : <div className="image-placeholder"><span>F/F</span><small>IMAGE NOT AVAILABLE</small></div>}<span className="match-pill">{matchName(item.match_type)}</span></div><div className="offer-info"><span className="offer-source">{sourceName(item.source_key)}</span><h3>{item.title}</h3><div className="offer-bottom"><strong>{formatHuf(item.price_amount)}</strong>{item.source_key === "demo" ? <span className="demo-item-label">Sample item</span> : <a href={item.source_url} target="_blank" rel="noopener noreferrer" onClick={() => sendMetric(clickEvent(lastSearchType.current, item.source_key))} aria-label={`Open ${item.title} at ${sourceName(item.source_key)}`}>View item <span aria-hidden>↗</span></a>}</div><p className="offer-meta">Checked {new Intl.DateTimeFormat("hu-HU", { dateStyle: "medium", timeZone: "Europe/Budapest" }).format(new Date(item.last_checked_at))} · Stock {item.availability_status === "unknown" ? "not confirmed" : item.availability_status.replace("_", " ")}</p>{item.attribution && <p className="offer-meta">{item.attribution}</p>}</div></article>)}</div>}
          {status === "ready" && nextCursor && <button className="load-more" type="button" onClick={() => void search(nextCursor)}>Load more finds ↓</button>}
        </section>
        <footer><span>FASHION FINDER</span><p>{demoMode ? "Local demo uses synthetic offers and prices; photo search compares five CC0 examples and cannot verify product identity." : "Prices and availability are checked at the time shown. Final details are on the retailer website."}</p>{analyticsEnabled && <p>We count anonymous searches, errors, and retailer clicks. We do not store your query or photo.</p>}</footer>
      </div>
    </main>
  );
}
