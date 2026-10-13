# Local product-image index experiment

This is the next step after the five-image synthetic demo. It indexes **local
files that an operator has already obtained with permission** for a current
Hungarian product feed. The command never downloads images or accepts a user
image URL. The API reads the finished index; shopper requests never rebuild it.

The default feature is a coarse colour and layout descriptor. A pinned,
optional ViT image encoder provides a stronger visual representation for local
evaluation. Neither encoder is trained specifically for fashion, establishes
an exact product match, or satisfies the
[private-beta gate](release-gate-handoff.md) without real evidence.

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

## Optional pretrained ViT encoder

The optional encoder uses the Apache-2.0
[`onnx-community/vit-base-patch32-224-in21k-ONNX`](https://huggingface.co/onnx-community/vit-base-patch32-224-in21k-ONNX)
model at revision `36a2d12097fcbe6825ad782f885426181a401e5c`. The repository
does not bundle or automatically download the 88.6 MB model. Download the
INT8 artifact into the ignored local `data/models/` directory, then verify its
SHA-256 before use:

```bash
mkdir -p data/models
curl -fL -o data/models/vit-base-patch32-224-in21k-int8.onnx \
  https://huggingface.co/onnx-community/vit-base-patch32-224-in21k-ONNX/resolve/36a2d12097fcbe6825ad782f885426181a401e5c/onnx/model_int8.onnx
sha256sum data/models/vit-base-patch32-224-in21k-int8.onnx
```

The required digest is:

```text
c9ebea40814509fa2ff8d907408edfaa72ee13213e6ebdcbda0fcbe2ef7a5c92
```

Install the optional runtime and rebuild the index. The command rejects any
model file with a different digest or graph contract:

```bash
.venv/bin/python -m pip install -e '.[dev,image-model]'
.venv/bin/fashion-finder --data-dir data index-images \
  data/image-manifest.json --policy data/source-policies.json \
  --model-path data/models/vit-base-patch32-224-in21k-int8.onnx
```

For Docker, set these values in `.env` and rebuild the API image:

```dotenv
FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT=1
FASHION_FINDER_INSTALL_IMAGE_MODEL=1
FASHION_FINDER_IMAGE_MODEL_PATH=/data/models/vit-base-patch32-224-in21k-int8.onnx
```

```bash
docker compose up -d --build api web
```

The model path must be `/data/...` inside Docker because the local data folder
is mounted there. Build the index with the same model path used by the API.
Changing or removing the model makes the previous index unavailable until it
is rebuilt. The runtime disables ONNX Runtime telemetry and executes locally on
CPU. Shopper uploads remain in memory and are not sent to a model service.

On Windows PowerShell, install the same extra and pass a Windows file path to
`--model-path` when running outside Docker. Use `Get-FileHash -Algorithm SHA256`
to verify the downloaded model. Docker still uses the `/data/models/...` path.
