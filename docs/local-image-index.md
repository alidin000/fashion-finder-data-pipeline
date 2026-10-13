# Local product-image index experiment

This is the next step after the five-image synthetic demo. It indexes **local
files that an operator has already obtained with permission** for a current
Hungarian product feed. The command never downloads images or accepts a user
image URL. The API reads the finished index; shopper requests never rebuild it.

The current feature is a coarse colour and layout descriptor, not a pretrained
fashion encoder. It is for local relevance testing only. It cannot establish
an exact product match or satisfy the [private-beta gate](release-gate-handoff.md).

## Prepare the files

1. Import the authorized product feed. Find each offer's numeric `id` and exact
   `image_urls` value through `GET /api/v1/search` or `GET /api/v1/offers/{id}`.
2. Put permitted JPEG, PNG, or WebP files under `data/images/`. Each file must
   be at most 5 MB and 20 million pixels. Keep the publisher's source and
   permission evidence outside the public API.
3. Set that source's policy to `approved` with `allow_public_listing`,
   `allow_image_display`, and `allow_image_processing` all true, plus a real
   `evidence_ref`. The tool reads the policy but **cannot verify the evidence**.
4. Create `data/image-manifest.json`. Replace all example values below with a
   current offer ID, its exact stored HTTPS image URL, and a relative file path:

```json
[
  {
    "offer_id": 123,
    "image_url": "<exact stored HTTPS image URL>",
    "image_path": "images/offer-123.jpg"
  }
]
```

The image path must stay inside the manifest directory. The indexer rejects
missing, duplicate, oversized, malformed, unapproved, stale, or mismatched
entries. It replaces the index only after **all** entries validate.

## Build and try it

From the repository root, using the same `data/` directory as the API:

```bash
.venv/bin/fashion-finder --data-dir data index-images \
  data/image-manifest.json --policy data/source-policies.json
```

Set `FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT=1` in the local `.env`, then run
`docker compose up -d --build api web`. Confirm
`GET http://127.0.0.1:8000/api/v1/health` reports
`"image_search": "available"`. Upload a photo in the shopper page or POST it:

```bash
curl -fsS -F file=@data/images/offer-123.jpg \
  http://127.0.0.1:8000/api/v1/search/image
```

`category`, `colour`, `size`, `max_price`, and `source` are optional multipart
fields. Results are capped at five and labeled **Style inspiration**. The
uploaded photo is decoded in memory and not stored by this route. The API
does not fetch retailer images during a shopper request.

Rebuild the index after feed image URLs or permissions change. At request time,
the API still checks current offer freshness, source policy, policy version,
and image URL. A revoked policy or changed URL removes that result immediately.
Setting the experiment flag to `0` makes the regular photo route return 503.
The bundled demo uses a separate database and index.
