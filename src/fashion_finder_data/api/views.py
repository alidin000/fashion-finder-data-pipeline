"""Public Django catalogue views with the existing JSON contract."""

from __future__ import annotations

import functools
import json
import os
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from PIL import Image, UnidentifiedImageError

from fashion_finder_data.analytics import record_metric
from fashion_finder_data.catalogue.models import RetailerOffer
from fashion_finder_data.demo_images import demo_image_bytes, demo_index_ready, search_demo_image
from fashion_finder_data.image_index import (
    image_index_ready,
    search_indexed_image,
    search_indexed_text,
    text_index_ready,
)
from fashion_finder_data.public_catalogue import public_offer, search_public_catalogue
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import Database

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BODY = MAX_IMAGE_BYTES + 256 * 1024  # the photo plus small form fields
# The web app sends at most 1024 px on the long side; this leaves room for other
# clients while bounding the memory one decode can take (about 12 MB as RGB).
MAX_IMAGE_PIXELS = 2048 * 2048
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


def _text_model_paths() -> dict[str, Path | None]:
    model = os.environ.get("FASHION_FINDER_TEXT_MODEL_PATH")
    tokenizer = os.environ.get("FASHION_FINDER_TOKENIZER_PATH")
    return {
        "model_path": _image_model_path(),
        "text_model_path": Path(model) if model else None,
        "tokenizer_path": Path(tokenizer) if tokenizer else None,
    }


def _look_search_ready(database: Database, policy_path: Path | None) -> bool:
    return (
        policy_path is not None
        and policy_path.is_file()
        and os.environ.get("FASHION_FINDER_DEMO_MODE") != "1"
        and os.environ.get("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT") == "1"
        and text_index_ready(database, load_policies(policy_path), **_text_model_paths())
    )


def _busy_as_503(view):
    """A locked or busy SQLite catalogue is temporary: answer 503, not a server error."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except DatabaseError:
            return _error("the catalogue is busy; try again shortly", 503)

    return wrapper


def _error(detail: str, status: int) -> JsonResponse:
    return JsonResponse({"detail": detail}, status=status)


@_busy_as_503
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
            "text_to_image": "available"
            if image_search_available and _look_search_ready(database, policy_path)
            else "unavailable",
            "analytics_enabled": os.environ.get("FASHION_FINDER_ANALYTICS_ENABLED") == "1",
            "demo_mode": os.environ.get("FASHION_FINDER_DEMO_MODE") == "1",
        }
    )


@csrf_exempt
@_busy_as_503
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


@_busy_as_503
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


@_busy_as_503
def search_look(request):
    """Rank indexed product photos by a written description, e.g. ``black puffer jacket``."""
    if request.method != "GET":
        return _error("method not allowed", 405)
    params = request.GET
    for key, maximum in {
        "q": 200,
        "category": 80,
        "colour": 80,
        "size": 40,
        "source": 40,
        "max_price": 40,
    }.items():
        if len(params.get(key, "")) > maximum:
            return _error(f"{key} is too long", 400)
    database, policy_path = _config()
    if not _look_search_ready(database, policy_path):
        return _error("text-to-photo search is not configured", 503)
    try:
        result = search_indexed_text(
            database,
            load_policies(policy_path),
            params.get("q", ""),
            limit=int(params.get("limit", "20")),
            source=params.get("source") or None,
            category=params.get("category") or None,
            colour=params.get("colour") or None,
            size=params.get("size") or None,
            max_price=Decimal(params["max_price"]) if params.get("max_price") else None,
            **_text_model_paths(),
        )
    except (ValueError, InvalidOperation) as exc:
        return _error(str(exc), 400)
    return JsonResponse(result)


@_busy_as_503
def offer(request, offer_id: int):
    if request.method != "GET":
        return _error("method not allowed", 405)
    database, policy_path = _config()
    result = public_offer(database, offer_id, load_policies(policy_path))
    return JsonResponse(result) if result is not None else _error("offer unavailable", 404)


@csrf_exempt
@_busy_as_503
def search_image(request):
    if request.method != "POST":
        return _error("method not allowed", 405)
    # Reject oversized bodies before Django parses (and buffers) the multipart form.
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return _error("invalid Content-Length", 400)
    if declared > MAX_UPLOAD_BODY:
        return _error("image exceeds 5 MB", 413)
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
    expected = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    # Only image work sits in these try blocks: a server-side fault must not look like
    # a bad photo.
    bad_image = (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            decoded = Image.open(BytesIO(data))  # reads the header, not the pixels
    except bad_image:
        return _error("cannot decode image", 422)
    with decoded:
        image_format = decoded.format
        if image_format not in expected:
            return _error("image encoding does not match allowed types", 415)
        if file.content_type != expected[image_format]:
            return _error("image MIME type does not match data", 415)
        if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
            return _error("image dimensions are too large", 413)
        # Decoding the pixels is the costly step, so first confirm photo search can run.
        database, policy_path = _config()
        if policy_path is None or not policy_path.is_file():
            return _error("image search is not configured", 503)
        policies = load_policies(policy_path)
        demo_mode = os.environ.get("FASHION_FINDER_DEMO_MODE") == "1"
        if demo_mode:
            available = demo_index_ready(database, policies)
        else:
            available = os.environ.get("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT") == "1" and (
                image_index_ready(database, policies, model_path=_image_model_path())
            )
        if not available:
            return _error("image search is not configured", 503)
        try:
            decoded.load()
            image = decoded.copy()
        except bad_image:
            return _error("cannot decode image", 422)
    try:
        budget = Decimal(request.POST["max_price"]) if "max_price" in request.POST else None
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
        result = search_images(database, policies, image, **options)
    except (InvalidOperation, ValueError) as exc:
        return _error(str(exc), 400)
    return JsonResponse(result)


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
