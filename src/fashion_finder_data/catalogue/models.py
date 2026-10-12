"""Django's managed schema matches the existing SQLite catalogue tables."""

from django.db import models


class Retailer(models.Model):
    source_key = models.CharField(max_length=255)
    market_code = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "retailer"
        constraints = (
            models.UniqueConstraint(
                fields=["source_key", "market_code"], name="retailer_source_market"
            ),
        )

    def __str__(self):
        return f"{self.source_key} ({self.market_code})"


class RetailerOffer(models.Model):
    retailer = models.ForeignKey(Retailer, on_delete=models.PROTECT, db_column="retailer_id")
    source_product_id = models.CharField(max_length=255, null=True, blank=True)
    source_url = models.TextField()
    canonical_url = models.TextField()
    title = models.TextField()
    brand = models.CharField(max_length=255, null=True, blank=True)
    category = models.CharField(max_length=255, null=True, blank=True)
    source_category = models.CharField(max_length=255, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    price_amount = models.CharField(max_length=255, null=True, blank=True)
    currency = models.CharField(max_length=255, null=True, blank=True)
    original_price_amount = models.CharField(max_length=255, null=True, blank=True)
    image_urls = models.TextField()
    colour_names = models.TextField()
    size_options = models.TextField()
    availability_status = models.CharField(max_length=255)
    source_fetched_at = models.CharField(max_length=255)
    created_at = models.CharField(max_length=255)
    updated_at = models.CharField(max_length=255)
    last_seen_at = models.CharField(max_length=255)
    status = models.CharField(max_length=255, default="active")

    class Meta:
        db_table = "retailer_offer"
        constraints = (
            models.UniqueConstraint(
                fields=["retailer", "source_product_id"], name="offer_retailer_product"
            ),
            models.UniqueConstraint(
                fields=["retailer", "canonical_url"], name="offer_retailer_url"
            ),
        )
        indexes = (
            models.Index(
                fields=["retailer", "status", "currency", "category"],
                name="offer_public_filter_idx",
            ),
        )

    def __str__(self):
        return self.title


class PriceObservation(models.Model):
    offer = models.ForeignKey(RetailerOffer, on_delete=models.PROTECT, db_column="offer_id")
    price_amount = models.CharField(max_length=255)
    currency = models.CharField(max_length=255)
    observed_at = models.CharField(max_length=255)
    source_fetched_at = models.CharField(max_length=255)

    class Meta:
        db_table = "price_observation"


class CollectionRun(models.Model):
    run_id = models.CharField(max_length=255, primary_key=True)
    source_key = models.CharField(max_length=255)
    market_code = models.CharField(max_length=255)
    started_at = models.CharField(max_length=255)
    finished_at = models.CharField(max_length=255)
    status = models.CharField(max_length=255)
    records_discovered = models.IntegerField()
    records_parsed = models.IntegerField()
    records_seen = models.IntegerField()
    records_valid = models.IntegerField()
    records_rejected = models.IntegerField()
    records_upserted = models.IntegerField()
    records_deduplicated = models.IntegerField()
    error_summary = models.TextField()
    completeness = models.TextField()
    adapter_version = models.CharField(max_length=255)

    class Meta:
        db_table = "collection_run"
