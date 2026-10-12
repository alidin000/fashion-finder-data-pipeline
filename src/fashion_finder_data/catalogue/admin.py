from django.contrib import admin

from .models import CollectionRun, PriceObservation, Retailer, RetailerOffer


@admin.register(Retailer)
class RetailerAdmin(admin.ModelAdmin):
    list_display = ("source_key", "market_code", "name", "enabled")
    list_filter = ("market_code", "enabled")
    search_fields = ("source_key", "name")
    readonly_fields = ("source_key", "market_code", "name", "enabled")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RetailerOffer)
class RetailerOfferAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "retailer",
        "price_amount",
        "currency",
        "availability_status",
        "last_seen_at",
    )
    list_filter = ("retailer", "status", "availability_status", "category")
    search_fields = ("title", "source_product_id", "canonical_url")
    readonly_fields = tuple(field.name for field in RetailerOffer._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PriceObservation)
class PriceObservationAdmin(admin.ModelAdmin):
    list_display = ("offer", "price_amount", "currency", "observed_at")
    readonly_fields = tuple(field.name for field in PriceObservation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CollectionRun)
class CollectionRunAdmin(admin.ModelAdmin):
    list_display = ("run_id", "source_key", "status", "started_at", "records_valid")
    list_filter = ("source_key", "status")
    readonly_fields = tuple(field.name for field in CollectionRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
