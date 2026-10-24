"""Show local practice photos on the shopper page; inactive outside practice mode.

GLAMI photos have no public URL, so practice offers store them under the reserved
``https://practice.invalid/images/`` prefix. With ``FASHION_FINDER_PRACTICE_MODE=1``
and a practice data directory, the API serves those files and rewrites the prefix in
its JSON responses. Stored offers, the index and search code never see the rewrite.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from django.http import FileResponse, JsonResponse

from . import LOCAL_IMAGE_PREFIX
from .loader import IMAGE_DIR, MARKER, MARKER_FILE

ROUTE = "/api/v1/practice/images/"
_NAME = re.compile(r"(abo|glami)/[A-Za-z0-9_-]+\.jpg")


def _practice_data_dir() -> Path | None:
    if os.environ.get("FASHION_FINDER_PRACTICE_MODE") != "1":
        return None
    from ..api.views import _config

    data_dir = _config()[0].path.parent
    marker = data_dir / MARKER_FILE
    if not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER:
        return None
    return data_dir


def practice_image(request, name: str):
    if request.method != "GET":
        return JsonResponse({"detail": "method not allowed"}, status=405)
    data_dir = _practice_data_dir()
    path = data_dir / IMAGE_DIR / name if data_dir and _NAME.fullmatch(name) else None
    if path is None or not path.is_file():
        return JsonResponse({"detail": "image unavailable"}, status=404)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=86400"
    return response


class PracticeImageMiddleware:
    """Point practice photo URLs in API JSON at :func:`practice_image`."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.path.startswith("/api/v1/")
            and not response.streaming
            and response.get("Content-Type", "").startswith("application/json")
            and _practice_data_dir() is not None
        ):
            response.content = response.content.replace(LOCAL_IMAGE_PREFIX.encode(), ROUTE.encode())
        return response
