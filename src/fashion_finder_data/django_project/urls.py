from django.contrib import admin
from django.urls import path

from fashion_finder_data.api import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health", views.health),
    path("api/v1/events", views.event),
    path("api/v1/search", views.search),
    path("api/v1/offers/<int:offer_id>", views.offer),
    path("api/v1/search/image", views.search_image),
    path("api/v1/search/look", views.search_look),
    path("api/v1/demo/images/<str:sku>", views.demo_image),
]
