# Local product-image index experiment

This is the next step after the five-image synthetic demo. It indexes **local
files** for current Hungarian offers. `index-images` never downloads images or
accepts a user image URL; `fetch-images` (below) is the separate download step.
The API reads the finished index; shopper requests never rebuild it.

The default feature is a coarse colour and layout descriptor. Two pinned,
optional ONNX encoders give stronger results: Marqo-FashionSigLIP, which is
trained on fashion products and is the recommended choice, and a general
ImageNet ViT. None of them establishes an exact product match or satisfies the
[private-beta gate](release-gate-handoff.md) without real evidence.

An index holds up to 20,000 photos. Indexes built before embeddings were stored
as compact float32 (index version 2) must be rebuilt once.

## Download photos with fetch-images

`fetch-images` downloads up to `--per-offer` photos (default 2, usually front and
back) for each stored active offer, then writes `data/images/manifest.json` in the
format `index-images` expects. It only accepts HTTPS photos on the retailer's own
domains, such as `static.sinsay.com`, and follows each image host's `robots.txt`.
It waits `--delay` seconds (default 2) between requests. A rerun reuses files it
already has, and drops manifest entries whose offer or photo URL is gone.

```bash
.venv/bin/fashion-finder --data-dir data collect sinsay --limit 500
.venv/bin/fashion-finder --data-dir data fetch-images --source sinsay --limit 500
```

Collect shortly before indexing: the indexer rejects offers older than the
source policy's `freshness_hours` (default 72).

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

## Recommended: Marqo-FashionSigLIP encoder

[Marqo-FashionSigLIP](https://huggingface.co/Marqo/marqo-fashionSigLIP) (Apache-2.0)
is fine-tuned from SigLIP on fashion products; the ViT below is a general
ImageNet model. Marqo's own benchmarks show it ahead of FashionCLIP; it has not
been measured on this catalogue yet. The encoder uses its ONNX vision tower at
revision `c56244cc94f92419e8369fa71efdaf403b124ce8`. Pick one file; INT8 is about
4x smaller and faster, and usually slightly less accurate:

| File | Size | SHA-256 |
|---|---|---|
| `onnx/vision_model.onnx` | 371.7 MB | `a7e773846b27a699c45ba7e3978514b7fca420662d7e69e3b9226982f09f4a13` |
| `onnx/vision_model_quantized.onnx` | 94.0 MB | `f1009cfce0eedd409f601e8351eedab72e8529641105eee6517821a9a634a2f4` |

```bash
mkdir -p data/models
curl -fL -o data/models/fashion-siglip-vision.onnx \
  https://huggingface.co/Marqo/marqo-fashionSigLIP/resolve/c56244cc94f92419e8369fa71efdaf403b124ce8/onnx/vision_model.onnx
sha256sum data/models/fashion-siglip-vision.onnx
.venv/bin/python -m pip install -e '.[dev,image-model]'
.venv/bin/fashion-finder --data-dir data index-images \
  data/images/manifest.json --policy data/source-policies.json \
  --model-path data/models/fashion-siglip-vision.onnx
```

The encoder recognises the file by its size and checks the digest. It squashes
each photo to 224×224 (bicubic) and scales pixels to [-1, 1], which is the
preprocessing Marqo publishes for this model, then L2-normalises `image_embeds`.
Point the API at the same file with `FASHION_FINDER_IMAGE_MODEL_PATH`, as in the
Docker settings below.

## Text-to-photo search ("match by look")

With a FashionSigLIP photo index, the model's text tower can rank product photos
by a description such as `black puffer jacket`, instead of matching words in the
Hungarian titles. It needs the text model and its tokenizer from the same revision:

| File | Size | SHA-256 |
|---|---|---|
| `onnx/text_model.onnx` | 441.4 MB | `c1d501b23bddf27ba828c037b0780e44fdf47ca4c0b925ef190ab5bcf7aaf6e6` |
| `onnx/text_model_quantized.onnx` | 111.1 MB | `f5a7995e029c6ee9346fa46857661c4a171d28b164fa7703b85a527d73adf170` |
| `tokenizer.json` | 2.4 MB | `5bc9b16db0e7adf5c9a2bd57f8f9c8e6ebdaabe75679cc956d007f78036d5b4a` |

```bash
REV=c56244cc94f92419e8369fa71efdaf403b124ce8
curl -fL -o data/models/fashion-siglip-text.onnx \
  https://huggingface.co/Marqo/marqo-fashionSigLIP/resolve/$REV/onnx/text_model.onnx
curl -fL -o data/models/fashion-siglip-tokenizer.json \
  https://huggingface.co/Marqo/marqo-fashionSigLIP/resolve/$REV/tokenizer.json
sha256sum data/models/fashion-siglip-text.onnx data/models/fashion-siglip-tokenizer.json
.venv/bin/fashion-finder --data-dir data search-look "black puffer jacket" \
  --policy data/source-policies.json \
  --model-path data/models/fashion-siglip-vision.onnx \
  --text-model-path data/models/fashion-siglip-text.onnx \
  --tokenizer-path data/models/fashion-siglip-tokenizer.json
```

The `image-model` extra now installs `tokenizers`. The query is cleaned the way
open_clip trained the model (lowercase, no punctuation), padded to 64 tokens, and
embedded as `text_embeds`. English descriptions work best; the model card lists
English only. For the API and shopper page, also set:

```dotenv
FASHION_FINDER_TEXT_MODEL_PATH=/data/models/fashion-siglip-text.onnx
FASHION_FINDER_TOKENIZER_PATH=/data/models/fashion-siglip-tokenizer.json
```

`GET /api/v1/health` then reports `"text_to_image": "available"`, the search panel
shows a **Match by look** checkbox, and `GET /api/v1/search/look?q=…` accepts the
same filters as `/api/v1/search`. Text search refuses an index built with another
model, because the two embeddings would not be comparable.

## Cropping and the category vote

On the shopper page, drag across the garment in the photo preview. The browser
crops the photo before upload, so a person or background no longer dominates the
match. **Clear crop** sends the whole photo again.

When the shopper picks no category, the 20 closest offers vote on one, with weights
1, 1/2, 1/3, and so on, so the nearest matches count most. If one category holds a
majority, its offers move to the top in their visual order, and the response
includes `preferred_category`. A red-dress photo then lists dresses before a red
bag. Without a clear majority, the order stays purely visual.

## Measure before trusting a model

`evaluate-rankings` replays private tasks with relevance labels; see the
[task guide](task-evaluation-guide.md). Run it once per model, then compare:

```bash
.venv/bin/fashion-finder --data-dir data evaluate-rankings tasks/jackets.json \
  --category jackets --policy data/source-policies.json \
  --model-path data/models/fashion-siglip-vision.onnx \
  --text-model-path data/models/fashion-siglip-text.onnx \
  --tokenizer-path data/models/fashion-siglip-tokenizer.json
```

| Field | What it compares |
|---|---|
| `photo_top5_relevant_mean` | Photo search with the task's category filter |
| `photo_any_category_top5_relevant_mean` | Photo search with no category, with the vote |
| `photo_any_category_without_vote_top5_relevant_mean` | The same without the vote |
| `look_top5_relevant_mean` | Text-to-photo search for each task's query |
| `text_top5_relevant_mean_on_look_tasks` | Word search for the same queries |

Rebuild the index with each model you compare (`--model-path` changes the index),
and use the same tasks for every run.

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
