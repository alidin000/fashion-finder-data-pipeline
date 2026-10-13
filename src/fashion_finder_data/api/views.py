"""Public Django catalogue views with the existing JSON contract."""

from __future__ import annotations

import json
import os
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from PIL import Image, UnidentifiedImageError

from fashion_finder_data.analytics import record_metric
from fashion_finder_data.catalogue.models import RetailerOffer
from fashion_finder_data.public_catalogue import public_offer, search_public_catalogue
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import Database

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
_api_config: ContextVar[tuple[Database, Path | None] | None] = ContextVar(
    "api_config", default=None
)


@contextmanager
def api_context(database: Database, policy_path: Path | None = None):
    """Bind one test request sequence to an isolated catalogue and policy file."""
    token = _api_config.set((database, policy_path))
    try:
        yield
    finally:
        _api_config.reset(token)


def _config() -> tuple[Database, Path | None]:
    configured = _api_config.get()
    if configured is not None:
        return configured
    policy = os.environ.get("FASHION_FINDER_SOURCE_POLICIES")
    return Database("default", Path(settings.DATABASES["default"]["NAME"])), Path(
        policy
    ) if policy else None


def _error(detail: str, status: int) -> JsonResponse:
    return JsonResponse({"detail": detail}, status=status)


def health(request):
    if request.method != "GET":
        return _error("method not allowed", 405)
    database, _ = _config()
    checked = (
        RetailerOffer.objects.using(database.alias)
        .order_by("-source_fetched_at")
        .values_list("source_fetched_at", flat=True)
        .first()
    )
    return JsonResponse(
        {
            "status": "ok",
            "catalogue_timestamp": checked,
            "image_search": "unavailable",
            "analytics_enabled": os.environ.get("FASHION_FINDER_ANALYTICS_ENABLED") == "1",
            "demo_mode": os.environ.get("FASHION_FINDER_DEMO_MODE") == "1",
        }
    )


@csrf_exempt
def event(request):
    if request.method != "POST":
        return _error("method not allowed", 405)
    if os.environ.get("FASHION_FINDER_ANALYTICS_ENABLED") != "1":
        return _error("analytics disabled", 404)
    if request.content_type != "application/json":
        return _error("JSON required", 415)
    if len(request.body) > 1024:
        return _error("event too large", 413)
    try:
        payload = json.loads(request.body)
        database, policy_path = _config()
        record_metric(database, payload, load_policies(policy_path))
    except (TypeError, ValueError, UnicodeDecodeError):
        return _error("invalid event", 400)
    return JsonResponse({"status": "recorded"}, status=201)


def search(request):
    if request.method != "GET":
        return _error("method not allowed", 405)
    params = request.GET
    for key, maximum in {
        "q": 200,
        "category": 80,
        "colour": 80,
        "size": 40,
        "source": 40,
        "cursor": 500,
    }.items():
        if len(params.get(key, "")) > maximum:
            return _error(f"{key} is too long", 400)
    try:
        limit = int(params.get("limit", "20"))
        max_price = Decimal(params["max_price"]) if "max_price" in params else None
        database, policy_path = _config()
        result = search_public_catalogue(
            database,
            load_policies(policy_path),
            query=params.get("q"),
            category=params.get("category"),
            colour=params.get("colour"),
            size=params.get("size"),
            max_price=max_price,
            source=params.get("source"),
            sort=params.get("sort", "price-asc"),
            limit=limit,
            cursor=params.get("cursor"),
        )
    except (ValueError, InvalidOperation) as exc:
        return _error(str(exc), 400)
    return JsonResponse(result)


def offer(request, offer_id: int):
    if request.method != "GET":
        return _error("method not allowed", 405)
    database, policy_path = _config()
    result = public_offer(database, offer_id, load_policies(policy_path))
    return JsonResponse(result) if result is not None else _error("offer unavailable", 404)


@csrf_exempt
def search_image(request):
    if request.method != "POST":
        return _error("method not allowed", 405)
    file = request.FILES.get("file")
    if file is None:
        return _error("file required", 400)
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return _error("JPEG, PNG or WebP required", 415)
    data = file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return _error("image exceeds 5 MB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = image.format
                expected = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
                if image_format not in expected:
                    return _error("image encoding does not match allowed types", 415)
                if file.content_type != expected[image_format]:
                    return _error("image MIME type does not match data", 415)
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return _error("image dimensions are too large", 413)
                image.load()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return _error("cannot decode image", 422)
    return _error("image search is not configured", 503)
