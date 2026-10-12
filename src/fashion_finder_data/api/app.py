"""HTTP read interface for approved, fresh Hungarian offers."""

from __future__ import annotations

import os
import warnings
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy import func, select

from ..public_catalogue import public_offer, search_public_catalogue
from ..source_policy import load_policies
from ..storage import offers, open_database

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class PublicOffer(BaseModel):
    id: int
    source_key: str
    source_product_id: str | None
    title: str
    category: str | None
    price_amount: str
    currency: Literal["HUF"]
    source_url: str
    image_urls: list[str]
    colour_names: list[str]
    size_options: list[str]
    availability_status: Literal["in_stock", "out_of_stock", "unknown"]
    last_checked_at: datetime
    match_type: Literal["same_item", "close_alternative", "style_inspiration"]
    attribution: str | None


class SearchResponse(BaseModel):
    items: list[PublicOffer]
    next_cursor: str | None


def create_app(database_path: Path, policy_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Fashion Finder catalogue", version="0.1.0")
    origins = [
        item.strip()
        for item in os.environ.get("FASHION_FINDER_CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    if "*" in origins:
        raise ValueError("CORS origins must be explicit")
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"])
    engine = open_database(database_path)

    @app.get("/api/v1/health")
    async def health() -> dict:
        with engine.connect() as connection:
            checked = connection.execute(select(func.max(offers.c.source_fetched_at))).scalar_one()
        return {"status": "ok", "catalogue_timestamp": checked, "image_search": "unavailable"}

    @app.get("/api/v1/search", response_model=SearchResponse)
    async def search(
        q: str | None = Query(default=None, max_length=200),
        category: str | None = Query(default=None, max_length=80),
        colour: str | None = Query(default=None, max_length=80),
        size: str | None = Query(default=None, max_length=40),
        max_price: Decimal | None = None,
        source: str | None = Query(default=None, max_length=40),
        sort: Literal["price-asc", "price-desc", "recent"] = "price-asc",
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = Query(default=None, max_length=500),
    ) -> dict:
        try:
            return search_public_catalogue(
                engine,
                load_policies(policy_path),
                query=q,
                category=category,
                colour=colour,
                size=size,
                max_price=max_price,
                source=source,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/offers/{offer_id}", response_model=PublicOffer)
    async def offer(offer_id: int) -> dict:
        result = public_offer(engine, offer_id, load_policies(policy_path))
        if result is None:
            raise HTTPException(status_code=404, detail="offer unavailable")
        return result

    @app.post("/api/v1/search/image")
    async def search_image(file: Annotated[UploadFile, File()]) -> dict:
        if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(status_code=415, detail="JPEG, PNG or WebP required")
        try:
            data = await file.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="image exceeds 5 MB")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as image:
                        image_format = image.format
                        if image_format not in {"JPEG", "PNG", "WEBP"}:
                            raise HTTPException(
                                status_code=415,
                                detail="image encoding does not match allowed types",
                            )
                        expected = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
                        if file.content_type != expected[image_format]:
                            raise HTTPException(
                                status_code=415, detail="image MIME type does not match data"
                            )
                        if image.width * image.height > MAX_IMAGE_PIXELS:
                            raise HTTPException(
                                status_code=413, detail="image dimensions are too large"
                            )
                        image.load()
            except (
                UnidentifiedImageError,
                OSError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                raise HTTPException(status_code=422, detail="cannot decode image") from exc
        finally:
            await file.close()
        # Deliberately fail closed until approved image sources and a measured index exist.
        raise HTTPException(status_code=503, detail="image search is not configured")

    return app


def app_from_environment() -> FastAPI:
    database = Path(os.environ.get("FASHION_FINDER_DB", "data/fashion_finder.sqlite"))
    configured_policy = os.environ.get("FASHION_FINDER_SOURCE_POLICIES")
    return create_app(database, Path(configured_policy) if configured_policy else None)


app = app_from_environment()
