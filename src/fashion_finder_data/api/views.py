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
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from PIL import Image, UnidentifiedImageError

from fashion_finder_data.analytics import record_metric
from fashion_finder_data.catalogue.models import RetailerOffer
from fashion_finder_data.demo_images import demo_image_bytes, demo_index_ready, search_demo_image
from fashion_finder_data.image_index import image_index_ready, search_indexed_image
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


def _image_model_path() -> Path | None:
    configured = os.environ.get("FASHION_FINDER_IMAGE_MODEL_PATH")
    return Path(configured) if configured else None


def _error(detail: str, status: int) -> JsonResponse:
    return JsonResponse({"detail": detail}, status=status)


def health(request):
    if request.method != "GET":
        return _error("method not allowed", 405)
    database, policy_path = _config()
    checked = (
        RetailerOffer.objects.using(database.alias)
        .order_by("-source_fetched_at")
        .values_list("source_fetched_at", flat=True)
        .first()
    )
    image_search_available = False
    if policy_path is not None and policy_path.is_file():
        if os.environ.get("FASHION_FINDER_DEMO_MODE") == "1":
            image_search_available = demo_index_ready(database, load_policies(policy_path))
        elif os.environ.get("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT") == "1":
            image_search_available = image_index_ready(
                database, load_policies(policy_path), model_path=_image_model_path()
            )
    return JsonResponse(
        {
            "status": "ok",
            "catalogue_timestamp": checked,
            "image_search": "available" if image_search_available else "unavailable",
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
    for key, maximum in {
        "source": 40,
        "category": 80,
        "colour": 80,
        "size": 40,
        "max_price": 40,
    }.items():
        if len(request.POST.get(key, "")) > maximum:
            return _error(f"{key} is too long", 400)
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
                database, policy_path = _config()
                if policy_path is None or not policy_path.is_file():
                    return _error("image search is not configured", 503)
                policies = load_policies(policy_path)
                demo_mode = os.environ.get("FASHION_FINDER_DEMO_MODE") == "1"
                if demo_mode:
                    available = demo_index_ready(database, policies)
                else:
                    available = os.environ.get(
                        "FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT"
                    ) == "1" and image_index_ready(
                        database, policies, model_path=_image_model_path()
                    )
                if not available:
                    return _error("image search is not configured", 503)
                try:
                    budget = (
                        Decimal(request.POST["max_price"]) if "max_price" in request.POST else None
                    )
                    search_images = search_demo_image if demo_mode else search_indexed_image
                    options = {
                        "category": request.POST.get("category") or None,
                        "colour": request.POST.get("colour") or None,
                        "size": request.POST.get("size") or None,
                        "max_price": budget,
                    }
                    if not demo_mode:
                        options["source"] = request.POST.get("source") or None
                        options["model_path"] = _image_model_path()
                    result = search_images(
                        database,
                        policies,
                        image,
                        **options,
                    )
                except (InvalidOperation, ValueError) as exc:
                    return _error(str(exc), 400)
                return JsonResponse(result)
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return _error("cannot decode image", 422)
    return _error("image search is not configured", 503)


def demo_image(request, sku: str):
    if request.method != "GET":
        return _error("method not allowed", 405)
    database, policy_path = _config()
    if (
        os.environ.get("FASHION_FINDER_DEMO_MODE") != "1"
        or policy_path is None
        or not policy_path.is_file()
        or not demo_index_ready(database, load_policies(policy_path))
    ):
        return _error("image unavailable", 404)
    data = demo_image_bytes(sku)
    if data is None:
        return _error("image unavailable", 404)
    return FileResponse(BytesIO(data), content_type="image/jpeg")
