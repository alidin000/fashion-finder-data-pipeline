"""Generic product extraction from schema.org JSON-LD, microdata, OpenGraph and state JSON.

A shop that publishes structured product data needs no parser code: its source
definition only names its hosts and, where needed, small overrides such as
localised ``@type`` names or a title suffix to strip. The output is the
source-native ``product`` dictionary that ``schema.normalize`` already accepts,
one per offer URL, with sizes and colours of the variants grouped onto it.

Rules that keep the data honest:
- a product's price must be one concrete amount; a price range (``AggregateOffer``
  with different low and high prices) or variants with different prices on one
  offer URL are rejected with a reason, not averaged or guessed;
- a regular price is taken only from an explicit strikethrough or list price;
- availability is normalised, and stays ``unknown`` when the page does not say.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from lxml import html as lxml_html

from .category_mapping import map_category
from .image_selection import rank_images
from .normalisation import (
    clean_colours,
    clean_list,
    clean_text,
    normalise_availability,
    parse_price_text,
)
from .schema import canonical_url
from .sources import ROBOTS_TOKEN, Source, allowed_url

PRODUCT_TYPES = {"Product", "ProductGroup", "IndividualProduct", "ProductModel", "SomeProducts"}
OFFER_TYPES = {"Offer", "AggregateOffer"}
# Localised type names seen on Hungarian shops (hungarian-shop-survey.md, Sports Direct).
DEFAULT_TYPE_ALIASES = {"termék": "Product", "ajánlat": "Offer"}
REGULAR_PRICE_TYPES = {"StrikethroughPrice", "ListPrice", "SRP", "MSRP"}
MAX_PAYLOAD_CHARS = 20_000
MISSING = "no Product structured data or product price metadata"


@dataclass(frozen=True)
class ExtractionSettings:
    type_aliases: Mapping[str, str] = field(default_factory=dict)
    title_suffix: re.Pattern[str] | None = None
    listing_structured_data: bool = False
    state: tuple[Mapping[str, Any], ...] = ()
    brand: str | None = None

    @classmethod
    def for_source(cls, source: Source) -> ExtractionSettings:
        config = source.extraction
        aliases = {**DEFAULT_TYPE_ALIASES, **dict(config.get("type_aliases") or {})}
        suffix = config.get("title_suffix")
        return cls(
            type_aliases={key.casefold(): value for key, value in aliases.items()},
            title_suffix=re.compile(suffix, re.IGNORECASE) if suffix else None,
            listing_structured_data=bool(config.get("listing_structured_data")),
            state=tuple(config.get("state") or ()),
            brand=config.get("brand"),
        )


@dataclass
class Extraction:
    """What one page yielded: complete product dictionaries plus rejection reasons."""

    products: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    method: str | None = None
    robots: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- parsing


def _types(node: Mapping[str, Any], aliases: Mapping[str, str]) -> set[str]:
    raw = node.get("@type", [])
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    result = set()
    for value in values:
        if isinstance(value, str):
            short = value.rstrip("/").rsplit("/", 1)[-1]
            result.add(aliases.get(short.casefold(), short))
    return result


def json_ld_documents(document: lxml_html.HtmlElement) -> list[Any]:
    """Every parseable JSON-LD block; a broken block is skipped, not fatal."""
    result = []
    for script in document.xpath("//script[@type]"):
        if script.get("type", "").split(";")[0].strip().lower() != "application/ld+json":
            continue
        text = (script.text or "").strip()
        text = re.sub(r"^\s*(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)\s*$", "", text)
        if not text:
            continue
        try:
            result.append(json.loads(text, parse_float=Decimal, strict=False))
        except (json.JSONDecodeError, ValueError):
            continue
    return result


def _walk(
    obj: Any,
    aliases: Mapping[str, str],
    products: list[dict],
    crumbs: list[list[str]],
    depth: int = 0,
) -> None:
    if depth > 12:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk(item, aliases, products, crumbs, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    types = _types(obj, aliases)
    if types & PRODUCT_TYPES:
        products.append(obj)  # related products inside it are recommendations: skip them
        return
    if "BreadcrumbList" in types:
        crumbs.append(_breadcrumbs(obj))
        return
    for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item", "about"):
        if key in obj:
            _walk(obj[key], aliases, products, crumbs, depth + 1)


def _breadcrumbs(node: Mapping[str, Any]) -> list[str]:
    items = node.get("itemListElement") or []
    items = items if isinstance(items, list) else [items]
    ordered = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        inner = item.get("item")
        if not name and isinstance(inner, dict):
            name = inner.get("name")
        try:
            position = float(item.get("position", index))
        except (TypeError, ValueError):
            position = float(index)
        text = clean_text(name)
        if text:
            ordered.append((position, text))
    return [text for _, text in sorted(ordered, key=lambda pair: pair[0])]


# Microdata (WHATWG): properties belong to the nearest ancestor with itemscope.
_URL_TAGS = {"a": "href", "area": "href", "link": "href"}
_SRC_TAGS = {"img", "audio", "embed", "iframe", "source", "track", "video"}


def _microdata_value(element: lxml_html.HtmlElement) -> Any:
    tag = element.tag if isinstance(element.tag, str) else ""
    if element.get("content") is not None:
        return element.get("content")
    if tag in _URL_TAGS:
        return element.get(_URL_TAGS[tag])
    if tag in _SRC_TAGS:
        return element.get("src")
    if tag == "object":
        return element.get("data")
    if tag in {"data", "meter"}:
        return element.get("value")
    if tag == "time" and element.get("datetime"):
        return element.get("datetime")
    return clean_text(element.text_content())


def _microdata_item(element: lxml_html.HtmlElement, depth: int = 0) -> dict[str, Any]:
    item: dict[str, Any] = {"@type": (element.get("itemtype") or "").split()}
    if depth > 8:
        return item

    def visit(parent: lxml_html.HtmlElement) -> None:
        for child in parent:
            if not isinstance(child.tag, str):
                continue
            names = (child.get("itemprop") or "").split()
            nested = child.get("itemscope") is not None
            if names:
                value = _microdata_item(child, depth + 1) if nested else _microdata_value(child)
                for name in names:
                    item.setdefault(name, []).append(value)
            if not nested:
                visit(child)

    visit(element)
    return {
        key: value[0] if isinstance(value, list) and len(value) == 1 and key != "@type" else value
        for key, value in item.items()
    }


def microdata_items(document: lxml_html.HtmlElement) -> list[dict[str, Any]]:
    """Top-level microdata items (those that are not a property of another item)."""
    return [
        _microdata_item(element)
        for element in document.xpath("//*[@itemscope]")
        if element.get("itemprop") is None
    ]


def meta_properties(document: lxml_html.HtmlElement) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for meta in document.xpath("//meta[@content]"):
        name = (meta.get("property") or meta.get("name") or "").strip().lower()
        if name:
            result.setdefault(name, []).append(meta.get("content").strip())
    return result


_VALUED_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"}


def robots_directives(
    document: lxml_html.HtmlElement | None, header: str | None = None
) -> set[str]:
    """Directives such as ``noindex`` or ``noai`` that apply to this crawler.

    Reads ``<meta name="robots">``, ``<meta name="fashionfinderbot">`` and the
    ``X-Robots-Tag`` header. A header group addressed to another crawler
    (``googlebot: noindex``) does not apply.
    """
    ours = ROBOTS_TOKEN.lower()
    directives: set[str] = set()
    values = []
    if document is not None:
        for meta in document.xpath("//meta[@name and @content]"):
            if meta.get("name", "").strip().lower() in {"robots", ours}:
                values.append(meta.get("content", ""))
    for value in values:
        directives |= {token for token in re.split(r"[,\s]+", value.lower()) if token}
    agent = None
    for token in (header or "").lower().split(","):
        match = re.match(r"\s*([a-z0-9_.-]+)\s*:\s*(.*)$", token)
        if match and match[1] not in _VALUED_DIRECTIVES:
            agent, token = match[1], match[2]
        token = token.strip()
        if token and agent in (None, ours):
            directives.add(token.split(":", 1)[0].strip())
    if "none" in directives:
        directives |= {"noindex", "nofollow"}
    return directives


# --------------------------------------------------------------------------- state JSON


def _path(value: Any, path: str) -> Any:
    """Follow ``a.b[0].c`` or map over ``images[].url``; missing steps give None."""
    current: list[Any] = [value]
    mapped = False
    for step in re.findall(r"[^.\[\]]+|\[\d*\]", path):
        following = []
        for item in current:
            if step == "[]":
                mapped = True
                following.extend(item if isinstance(item, list) else [])
            elif step.startswith("["):
                index = int(step[1:-1])
                if isinstance(item, list) and -len(item) <= index < len(item):
                    following.append(item[index])
            elif isinstance(item, dict) and step in item:
                following.append(item[step])
        current = following
    if mapped:
        return [item for item in current if item is not None]
    return current[0] if current else None


def state_products(
    document: lxml_html.HtmlElement, text: str, configs: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Products from embedded state JSON described by the source definition.

    Each config names ``script_selector`` (an ``id`` such as ``__NEXT_DATA__``) or
    ``script_pattern`` (a regex that ends where the JSON value starts), a
    ``product_path`` and a ``fields`` map from product keys to paths.
    """
    products = []
    for config in configs:
        payload: Any = None
        decoder = json.JSONDecoder(parse_float=Decimal)
        try:
            if config.get("script_id"):
                scripts = document.xpath("//script[@id=$id]", id=config["script_id"])
                payload = (
                    json.loads(scripts[0].text or "", parse_float=Decimal) if scripts else None
                )
            elif config.get("script_pattern"):
                match = re.search(config["script_pattern"], text)
                payload = decoder.raw_decode(text, match.end())[0] if match else None
        except (json.JSONDecodeError, ValueError, IndexError):
            payload = None
        if payload is None:
            continue
        found = (
            _path(payload, config.get("product_path", ""))
            if config.get("product_path")
            else payload
        )
        for node in found if isinstance(found, list) else [found]:
            if not isinstance(node, dict):
                continue
            fields = config.get("fields") or {}
            product = {key: _path(node, path) for key, path in fields.items()}
            products.append(
                {
                    "@type": ["Product"],
                    "name": product.get("name"),
                    "sku": product.get("sku"),
                    "productGroupID": product.get("item_group_id"),
                    "url": product.get("url"),
                    "brand": product.get("brand"),
                    "description": product.get("description"),
                    "image": product.get("images"),
                    "color": product.get("colours"),
                    "size": product.get("sizes"),
                    "category": product.get("category"),
                    "offers": {
                        "@type": "Offer",
                        "price": product.get("price"),
                        "priceCurrency": product.get("currency"),
                        "availability": product.get("availability"),
                        "_regular_price": product.get("original_price"),
                    },
                }
            )
    return products


# --------------------------------------------------------------------------- mapping


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return next((item for item in value if item not in (None, "", [], {})), None)
    return value


def _name(value: Any) -> str | None:
    value = _first(value)
    if isinstance(value, dict):
        value = value.get("name")
    return clean_text(value)


def _price_type(spec: Mapping[str, Any]) -> str:
    value = _first(spec.get("priceType")) or ""
    return str(value).rstrip("/").rsplit("/", 1)[-1] if value else ""


def _offers(value: Any, aliases: Mapping[str, str], depth: int = 0) -> list[dict[str, Any]]:
    """Flatten Offer, AggregateOffer and priceSpecification data into plain offers."""
    if depth > 4 or value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for node in values:
        if not isinstance(node, dict):
            continue
        types = _types(node, aliases) or {"Offer"}
        if "AggregateOffer" in types and node.get("offers"):
            result.extend(_offers(node["offers"], aliases, depth + 1))
            continue
        price = node.get("price")
        currency = node.get("priceCurrency")
        regular = node.get("_regular_price")
        specs = node.get("priceSpecification") or []
        for spec in specs if isinstance(specs, list) else [specs]:
            if not isinstance(spec, dict):
                continue
            kind = _price_type(spec)
            if kind in REGULAR_PRICE_TYPES:
                regular = regular if regular is not None else spec.get("price")
            elif price is None and kind in {"", "SalePrice"}:
                price = spec.get("price")
            currency = currency or spec.get("priceCurrency")
        aggregate = "AggregateOffer" in types and price is None
        if aggregate:
            low, high = node.get("lowPrice"), node.get("highPrice")
            price = low if low is not None and (high is None or str(high) == str(low)) else None
        item = node.get("itemOffered") if isinstance(node.get("itemOffered"), dict) else {}
        result.append(
            {
                "price": price,
                "currency": currency,
                "regular_price": regular,
                "availability": _first(node.get("availability")),
                "sku": _first(node.get("sku")) or _first(item.get("sku")),
                "url": _first(node.get("url")),
                "colour": item.get("color"),
                "size": _name(item.get("size")) if item else None,
                "range": aggregate and price is None,
            }
        )
    return result


def _images(node: Mapping[str, Any]) -> list[Any]:
    value = node.get("image")
    return value if isinstance(value, list) else [value] if value else []


def _gtin(node: Mapping[str, Any]) -> str | None:
    for key in ("gtin", "gtin13", "gtin14", "gtin12", "gtin8", "isbn"):
        value = _first(node.get(key))
        if value:
            return str(value).strip()
    return None


def _unit(
    node: Mapping[str, Any], aliases: Mapping[str, str], parent: Mapping | None = None
) -> dict:
    parent = parent or {}
    group = (
        _first(node.get("isVariantOf"))
        if isinstance(node.get("isVariantOf"), dict | list)
        else None
    )
    return {
        "name": clean_text(_first(node.get("name"))) or parent.get("name"),
        "description": clean_text(_first(node.get("description"))) or parent.get("description"),
        "sku": clean_text(_first(node.get("sku"))) or clean_text(_first(node.get("mpn"))),
        "item_group_id": clean_text(_first(node.get("inProductGroupWithID")))
        or clean_text(_first(node.get("productGroupID")))
        or (clean_text(group.get("productGroupID")) if isinstance(group, dict) else None)
        or parent.get("item_group_id"),
        "url": _first(node.get("url")) if isinstance(_first(node.get("url")), str) else None,
        "brand": _name(node.get("brand")) or parent.get("brand"),
        "gtin": _gtin(node),
        "images": _images(node) or list(parent.get("images") or []),
        "colours": clean_colours(node.get("color")) or [],
        "sizes": clean_list(
            _name(node.get("size"))
            if isinstance(_first(node.get("size")), dict)
            else node.get("size")
        ),
        "category": _name(node.get("category")) or parent.get("category"),
        "offers": _offers(node.get("offers"), aliases),
    }


def _units(node: Mapping[str, Any], aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    """One unit per variant of a ProductGroup, or the product itself."""
    parent = _unit(node, aliases)
    parent["item_group_id"] = parent["item_group_id"] or (
        parent["sku"] if "ProductGroup" in _types(node, aliases) else None
    )
    variants = node.get("hasVariant") or []
    variants = [item for item in (variants if isinstance(variants, list) else [variants])]
    variants = [item for item in variants if isinstance(item, dict) and len(item) > 1]
    if not variants:
        return [parent]
    units = []
    for variant in variants:
        unit = _unit(variant, aliases, parent)
        if not unit["offers"]:
            unit["offers"] = parent["offers"]
        units.append(unit)
    return units


def _decimal(value: Any, currency: Any) -> tuple[Decimal, str | None] | None:
    if value is None or value == "":
        return None
    try:
        return parse_price_text(value if not isinstance(value, float) else str(value), currency)
    except (TypeError, ValueError):
        return None


def _choose_price(offers: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """One concrete price for an offer URL, or a rejection reason."""
    priced = []
    for offer in offers:
        parsed = _decimal(offer["price"], offer["currency"])
        if parsed is not None:
            priced.append((parsed, normalise_availability(offer["availability"]), offer))
    if not priced:
        if any(offer.get("range") for offer in offers):
            return None, "price range without one concrete price"
        return None, None
    distinct = {parsed for parsed, _, _ in priced}
    if len(distinct) > 1:
        distinct = {parsed for parsed, stock, _ in priced if stock == "in_stock"}
    if len(distinct) != 1:
        return None, "variants on one offer URL have different prices"
    amount = distinct.pop()
    chosen = next(offer for parsed, _, offer in priced if parsed == amount)
    return chosen, None


def _availability(offers: list[dict[str, Any]]) -> str:
    states = [normalise_availability(offer["availability"]) for offer in offers]
    if "in_stock" in states:
        return "in_stock"
    if states and all(state == "out_of_stock" for state in states):
        return "out_of_stock"
    return "unknown"


def _clean_title(name: str | None, settings: ExtractionSettings) -> str | None:
    if name and settings.title_suffix is not None:
        name = settings.title_suffix.sub("", name).strip()
    return name or None


def _payload(node: Any) -> Any:
    text = json.dumps(node, default=str, ensure_ascii=False)
    if len(text) <= MAX_PAYLOAD_CHARS:
        return json.loads(text)
    if isinstance(node, dict):
        keys = ("@type", "name", "sku", "productGroupID", "url", "offers", "color", "size")
        return json.loads(
            json.dumps({key: node.get(key) for key in keys if key in node}, default=str)
        )
    return None


def build_products(
    nodes: list[dict[str, Any]],
    *,
    source: Source,
    page_url: str,
    settings: ExtractionSettings,
    breadcrumbs: list[str] | None = None,
    listing_category: str | None = None,
    method: str = "jsonld",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Group the units of structured product nodes into one product dict per offer URL."""
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = {}
    rejected: list[str] = []
    for node in nodes:
        for unit in _units(node, settings.type_aliases):
            by_url: dict[str, list[dict[str, Any]]] = {}
            for offer in unit["offers"] or [
                {
                    "price": None,
                    "currency": None,
                    "availability": None,
                    "regular_price": None,
                    "url": None,
                    "sku": None,
                    "colour": None,
                    "size": None,
                    "range": False,
                }
            ]:
                target = unit["url"] or offer.get("url") or page_url
                by_url.setdefault(str(urljoin(page_url, str(target))), []).append(offer)
            for url, offers in by_url.items():
                try:
                    key = canonical_url(url)
                except ValueError:
                    rejected.append("product URL is not an absolute HTTP(S) URL")
                    continue
                groups.setdefault(key, []).append((node, unit | {"url": url}, {"offers": offers}))
    products = []
    for key, members in groups.items():
        url = members[0][1]["url"]
        if not allowed_url(source, url):
            rejected.append("product URL is outside the Hungarian storefront")
            continue
        units = [unit for _, unit, _ in members]
        offers = [offer for _, _, holder in members for offer in holder["offers"]]
        chosen, problem = _choose_price(offers)
        if problem:
            rejected.append(problem)
            continue
        first = units[0]
        name = _clean_title(next((unit["name"] for unit in units if unit["name"]), None), settings)
        if not name:
            rejected.append("product has no name")
            continue
        group_id = next((unit["item_group_id"] for unit in units if unit["item_group_id"]), None)
        skus = {unit["sku"] or (chosen or {}).get("sku") for unit in units} - {None}
        sku = skus.pop() if len(skus) == 1 else group_id
        regular = None
        if chosen is not None and chosen.get("regular_price") is not None:
            parsed = _decimal(chosen["regular_price"], chosen["currency"])
            regular = str(parsed[0]) if parsed else None
        colours: list[str] = []
        sizes: list[str] = []
        images: list[Any] = []
        for unit in units:
            colours += [colour for colour in unit["colours"] if colour not in colours]
            sizes += [size for size in unit["sizes"] if size not in sizes]
            images += [image for image in unit["images"] if image not in images]
        for offer in offers:
            if offer.get("size") and offer["size"] not in sizes:
                sizes.append(offer["size"])
        label, category_key = map_category(
            breadcrumbs=breadcrumbs or [],
            product_category=first["category"],
            listing_category=listing_category,
            product_name=name,
        )
        products.append(
            {
                "url": url,
                "sku": sku,
                "item_group_id": group_id,
                "name": name,
                "brand": first["brand"] or settings.brand,
                "category": label,
                "category_key": category_key,
                "breadcrumbs": breadcrumbs or [],
                "description": first["description"],
                "gtin": first["gtin"],
                "price": None
                if chosen is None
                else str(_decimal(chosen["price"], chosen["currency"])[0]),
                "currency": None
                if chosen is None
                else _decimal(chosen["price"], chosen["currency"])[1],
                "original_price": regular,
                "images": rank_images(
                    [
                        urljoin(page_url, image) if isinstance(image, str) else image
                        for image in images
                    ]
                ),
                "colours": colours,
                "sizes": sizes,
                "availability": _availability(offers),
                "variant_count": len(units),
                "_extraction": method,
                "_source_payload": _payload(members[0][0]),
            }
        )
    return products, rejected


def _merge_meta(product: dict[str, Any], meta: Mapping[str, list[str]], source: Source) -> None:
    """Fill fields the structured data left empty from OpenGraph/product meta tags."""

    def one(name: str) -> str | None:
        values = meta.get(name)
        return values[0] if values else None

    if not product.get("images"):
        product["images"] = rank_images(meta.get("og:image", []))
    if product.get("price") is None and one("product:price:amount"):
        product["price"] = one("product:sale_price:amount") or one("product:price:amount")
        product["currency"] = one("product:price:currency") or one("product:sale_price:currency")
        if one("product:sale_price:amount"):
            product["original_price"] = one("product:price:amount")
    if product.get("original_price") is None and one("product:original_price:amount"):
        product["original_price"] = one("product:original_price:amount")
    if product.get("availability") in (None, "unknown") and one("product:availability"):
        product["availability"] = normalise_availability(one("product:availability"))
    product["brand"] = product.get("brand") or clean_text(one("product:brand") or one("og:brand"))
    product["sku"] = product.get("sku") or clean_text(one("product:retailer_item_id"))
    if not product.get("colours") and one("product:color"):
        product["colours"] = clean_colours(one("product:color"))


def _meta_product(
    meta: Mapping[str, list[str]], page_url: str, source: Source, settings: ExtractionSettings
) -> dict[str, Any] | None:
    def one(name: str) -> str | None:
        values = meta.get(name)
        return values[0] if values else None

    if not one("product:price:amount"):
        return None
    url = one("og:url") or page_url
    url = url if allowed_url(source, urljoin(page_url, url)) else page_url
    product = {
        "url": urljoin(page_url, url),
        "sku": clean_text(one("product:retailer_part_no") or one("product:retailer_item_id")),
        "item_group_id": clean_text(one("product:item_group_id")),
        "name": _clean_title(clean_text(one("og:title")), settings),
        "brand": None,
        "category": None,
        "category_key": None,
        "breadcrumbs": [],
        "description": clean_text(one("og:description")),
        "gtin": None,
        "price": None,
        "currency": None,
        "original_price": None,
        "images": [],
        "colours": [],
        "sizes": clean_list(meta.get("product:size", [])),
        "availability": "unknown",
        "variant_count": 1,
        "_extraction": "opengraph",
        "_source_payload": {
            key: value for key, value in meta.items() if key.startswith(("og:", "product:"))
        },
    }
    _merge_meta(product, meta, source)
    return product


def parse_document(text: str) -> lxml_html.HtmlElement | None:
    try:
        return lxml_html.fromstring(text) if text and text.strip() else None
    except (ValueError, lxml_html.etree.ParserError):
        return None


def extract(
    text: str,
    page_url: str,
    source: Source,
    *,
    listing_category: str | None = None,
    listing: bool = False,
    robots_header: str | None = None,
) -> Extraction:
    """Extract every complete product a page publishes.

    On a product page (``listing=False``) one product node with its variants is
    expected; OpenGraph tags fill gaps. On a listing page only complete product
    nodes are kept (own product URL, name and one concrete price), so a priceless
    category-level ``Product``/``AggregateOffer`` never stands in for the products.
    """
    result = Extraction()
    document = parse_document(text)
    if document is None:
        result.rejected.append(MISSING)
        return result
    result.robots = robots_directives(document, robots_header)
    settings = ExtractionSettings.for_source(source)
    nodes: list[dict] = []
    crumbs: list[list[str]] = []
    for item in json_ld_documents(document):
        _walk(item, settings.type_aliases, nodes, crumbs)
    method = "jsonld"
    if not nodes:
        for item in microdata_items(document):
            _walk(item, settings.type_aliases, nodes, crumbs)
        method = "microdata"
    if not nodes and settings.state:
        nodes = state_products(document, text, settings.state)
        method = "state"
    breadcrumbs = next((trail for trail in crumbs if trail), [])
    meta = meta_properties(document)
    if listing:
        products, rejected = build_products(
            nodes,
            source=source,
            page_url=page_url,
            settings=settings,
            listing_category=listing_category,
            method=method,
        )
        page = canonical_url(page_url)
        for product in products:
            if (
                product["price"] is None
                or product["currency"] is None
                or canonical_url(product["url"]) == page
                or (
                    source.product_pattern
                    and not re.search(source.product_pattern, product["url"], re.IGNORECASE)
                )
            ):
                continue
            result.products.append(product)
        result.method = method if result.products else None
        return result
    products, rejected = build_products(
        nodes,
        source=source,
        page_url=page_url,
        settings=settings,
        breadcrumbs=breadcrumbs if len(nodes) == 1 else [],
        listing_category=listing_category,
        method=method,
    )
    result.rejected.extend(rejected)
    if nodes and len(products) == 1:
        _merge_meta(products[0], meta, source)
    if not products and not rejected:
        fallback = _meta_product(meta, page_url, source, settings)
        if fallback is not None:
            label, key = map_category(
                breadcrumbs=breadcrumbs,
                listing_category=listing_category,
                product_name=fallback["name"],
            )
            fallback["category"], fallback["category_key"] = label, key
            products = [fallback]
            method = "opengraph"
    if not products and not result.rejected:
        result.rejected.append(MISSING)
    result.products = [product for product in products if product.get("name")]
    if products and not result.products:
        result.rejected.append("product has no name")
    result.method = method if result.products else None
    return result
