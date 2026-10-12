"""Explicit local cross-origin access for the Next.js shopper UI."""

import os

from django.http import HttpResponse
from django.utils.cache import patch_vary_headers


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin")
        allowed = {
            value.strip()
            for value in os.environ.get("FASHION_FINDER_CORS_ORIGINS", "").split(",")
            if value.strip() and value.strip() != "*"
        }
        api = request.path.startswith("/api/v1/")
        if api and origin in allowed and request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        if api and origin in allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response["Access-Control-Allow-Headers"] = "Content-Type"
            patch_vary_headers(response, ["Origin"])
        return response
