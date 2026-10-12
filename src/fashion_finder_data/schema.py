"""Source records and the shared offer contract."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .taxonomy import normalize_category


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


def is_hungarian_source_url(source_key: str, value: str) -> bool:
    """Confirm that a public product link stays on its Hungarian retailer host."""
    expected = {
        "zara": "zara.com",
        "hm": "hm.com",
        "reserved": "reserved.com",
        "pullandbear": "pullandbear.com",
    }.get(source_key)
    if expected is None:
        return False
    parts = urlsplit(value)
    host = parts.hostname or ""
    return (
        parts.scheme.lower() == "https"
        and not parts.username
        and not parts.password
        and (host == expected or host.endswith("." + expected))
        and parts.path.startswith(("/hu/", "/hu_hu/"))
    )


def parse_money(value: Any, currency: str | None = None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError("price must be numeric")
    if isinstance(value, float):
        raise TypeError("binary floating-point prices are not accepted")
    if isinstance(value, Decimal):
        result = value
    else:
        raw = str(value).replace("\u00a0", " ").replace("\u202f", " ").strip()
        raw = re.sub(r"\s*(Ft|HUF|EUR|€)\s*$", "", raw, flags=re.IGNORECASE)
        raw = raw.replace(" ", "")
        if currency == "HUF" and re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", raw):
            raw = raw.replace(",", "").replace(".", "")
        if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw):
            raise ValueError(f"invalid or ambiguous price: {value!r}")
        try:
            result = Decimal(raw.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("invalid price") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("price must be finite and nonnegative")
    return result


class RawProductRecord(BaseModel):
    source_key: Literal["zara", "hm", "reserved", "pullandbear"]
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
    source_key: Literal["zara", "hm", "reserved", "pullandbear"]
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
    availability = str(data.get("availability") or "").lower()
    if availability.endswith("/instock"):
        availability = "in_stock"
    elif availability.endswith("/outofstock"):
        availability = "out_of_stock"
    elif availability not in {"in_stock", "out_of_stock"}:
        availability = "unknown"

    def strings(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        return [str(item).strip() for item in values if item and str(item).strip()]

    images = [canonical_url(item) for item in strings(data.get("images"))]
    return RetailerOffer(
        source_key=raw.source_key,
        market_code=raw.market_code,
        source_product_id=str(data["sku"]).strip() if data.get("sku") else None,
        source_url=source_url,
        canonical_url=canonical,
        title=str(data.get("name") or "").strip(),
        brand=str(data["brand"]).strip() if data.get("brand") else None,
        category=normalize_category(str(data["category"])) if data.get("category") else None,
        source_category=str(data["category"]).strip() if data.get("category") else None,
        description=str(data["description"]).strip() if data.get("description") else None,
        price_amount=price,
        currency=currency,
        original_price_amount=original_price,
        image_urls=images,
        colour_names=strings(data.get("colours")),
        size_options=strings(data.get("sizes")),
        availability_status=availability,
        source_fetched_at=raw.fetched_at,
    )
