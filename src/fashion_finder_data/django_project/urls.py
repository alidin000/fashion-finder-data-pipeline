from django.contrib import admin
from django.urls import path

from fashion_finder_data.api import ingest, views
from fashion_finder_data.practice.web import practice_image

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health", views.health),
    path("api/v1/events", views.event),
    path("api/v1/search", views.search),
    path("api/v1/offers/<int:offer_id>", views.offer),
    path("api/v1/search/image", views.search_image),
    path("api/v1/search/look", views.search_look),
    path("api/v1/demo/images/<str:sku>", views.demo_image),
    # Token-authenticated ingest for refresh runners; 503 unless the token is configured.
    path("api/v1/ingest/due", ingest.due),
    path("api/v1/ingest/known", ingest.known),
    path("api/v1/ingest/offers", ingest.offers),
    path("api/v1/ingest/photos", ingest.photos),
    path("api/v1/ingest/image-index", ingest.image_index),
    path("api/v1/practice/images/<path:name>", practice_image),
]
