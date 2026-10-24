"""Source records and the shared offer contract."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .normalisation import (
    clean_colours,
    clean_list,
    clean_text,
    normalise_availability,
    parse_price_text,
)
from .sources import SourceKey, is_hungarian_source_url
from .taxonomy import CATEGORIES, normalize_category


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("product URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("product URL must not contain credentials")
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"gclid", "fbclid", "msclkid"}
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def parse_money(value: Any, currency: str | None = None) -> Decimal | None:
    """Exact price from a number or text such as ``12 990 Ft``; None when absent.

    A currency word in the text must match ``currency``: ``29.99 EUR`` is never a
    HUF price.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError("price must be numeric")
    return parse_price_text(value, currency)[0]


class RawProductRecord(BaseModel):
    source_key: SourceKey
    market_code: Literal["HU"] = "HU"
    source_url: str
    fetched_at: datetime = Field(default_factory=utc_now)
    adapter_version: str = "1"
    product: dict[str, Any]

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        canonical_url(value)
        return value.strip()


class RetailerOffer(BaseModel):
    source_key: SourceKey
    market_code: Literal["HU"] = "HU"
    source_product_id: str | None = None
    source_url: str
    canonical_url: str
    title: str = Field(min_length=1)
    brand: str | None = None
    category: str | None = None
    source_category: str | None = None
    description: str | None = None
    price_amount: Decimal | None = None
    currency: str | None = None
    original_price_amount: Decimal | None = None
    image_urls: list[str] = Field(default_factory=list)
    colour_names: list[str] = Field(default_factory=list)
    size_options: list[str] = Field(default_factory=list)
    availability_status: Literal["in_stock", "out_of_stock", "unknown"] = "unknown"
    source_fetched_at: datetime

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        canonical_url(value)
        return value.strip()

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str) -> str:
        return canonical_url(value)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a three-letter ISO code")
        return value

    @model_validator(mode="after")
    def check_price_pair(self) -> RetailerOffer:
        if self.price_amount is not None and self.currency is None:
            raise ValueError("currency is required when price is present")
        if self.price_amount is None and self.currency is not None:
            raise ValueError("price is required when currency is present")
        return self


def image_urls(value: Any, page_url: str) -> list[str]:
    """Absolute, de-duplicated photo URLs; an unusable photo is dropped, not the offer.

    Accepts plain URLs, relative paths and schema.org ``ImageObject`` entries.
    """
    values = value if isinstance(value, list) else [value] if value else []
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl")
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            url = canonical_url(urljoin(page_url, item.strip()))
        except ValueError:
            continue
        if url not in result:
            result.append(url)
    return result


def normalize(raw: RawProductRecord) -> RetailerOffer:
    data = raw.product
    source_url = str(data.get("url") or raw.source_url).strip()
    canonical = canonical_url(source_url)
    host = urlsplit(source_url).hostname or ""
    source_host = urlsplit(raw.source_url).hostname or ""
    if host != source_host or not is_hungarian_source_url(raw.source_key, source_url):
        raise ValueError("product URL is outside the retailer's Hungarian storefront")
    currency = str(data["currency"]).strip().upper() if data.get("currency") else None
    price = parse_money(data.get("price"), currency)
    original_price = parse_money(data.get("original_price"), currency)
    if original_price is not None and (price is None or original_price <= price):
        original_price = None  # a "regular" price at or below the current one is no discount
    source_category = clean_text(data.get("category"))
    category_key = data.get("category_key")
    return RetailerOffer(
        source_key=raw.source_key,
        market_code=raw.market_code,
        source_product_id=str(data["sku"]).strip() if data.get("sku") else None,
        source_url=source_url,
        canonical_url=canonical,
        title=clean_text(data.get("name")) or "",
        brand=clean_text(data.get("brand")),
        category=category_key
        if category_key in CATEGORIES
        else normalize_category(source_category)
        if source_category
        else None,
        source_category=source_category,
        description=clean_text(data.get("description")),
        price_amount=price,
        currency=currency,
        original_price_amount=original_price,
        image_urls=image_urls(data.get("images"), source_url),
        colour_names=clean_colours(data.get("colours")),
        size_options=clean_list(data.get("sizes")),
        availability_status=normalise_availability(data.get("availability")),
        source_fetched_at=raw.fetched_at,
    )
