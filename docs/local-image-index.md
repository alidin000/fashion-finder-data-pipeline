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
back) for each offer the index would accept, then writes `data/images/manifest.json`
in the format `index-images` expects. It needs `--policy`: a source must be approved
with image display and processing allowed, and the offer must be current and in
stock. The report lists every skipped photo by reason under `skipped_not_eligible`.

Every download:
- uses only HTTPS photos on the retailer's own domains, such as `static.sinsay.com`,
  and refuses a redirect that leaves the site;
- follows the image host's `robots.txt`, parsed with Protego as Scrapy does
  (RFC 9309 wildcards and longest match);
- honours text-and-data-mining opt-outs under EU DSM Directive Article 4: a
  `tdm-reservation: 1` response header, or a reserving rule in
  `/.well-known/tdmrep.json` on the image host or the shop's own site;
- waits `--delay` seconds (default 2) between requests.

A rerun reuses files it already has and drops manifest entries the index would no
longer accept. The run stops before any download if the manifest would pass 20,000
photos.

```bash
.venv/bin/fashion-finder --data-dir data collect sinsay --limit 500
.venv/bin/fashion-finder --data-dir data fetch-images --source sinsay --limit 500 \
  --policy data/source-policies.json
```

Collect shortly before indexing: offers older than the source policy's
`freshness_hours` (default 72) are skipped.

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

The image path must stay inside the manifest directory. A missing file, a duplicate,
an unsafe path or URL, or an undecodable photo stops the build. An entry whose offer
is no longer eligible (unapproved, stale, sold out, or with a changed photo URL) is
skipped and counted under `skipped_images`, and the status becomes `partial`. The
index is replaced only when at least one photo is indexed.

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
fields. Two more are optional:
- `crop=manual` says the photo is already the shopper's crop, so the server
  does not crop it again (see [Photo understanding](#photo-understanding)).
- `limit` (1–20) asks for more results. Without it the route returns five,
  the number the release gate measures.

The shopper page asks for 20 and shows five until the shopper presses **Show
more**, so the extra results need no second model run. Results are labeled
**Style inspiration**. The uploaded photo is decoded in memory and not stored
by this route. The API does not fetch retailer images during a shopper request.

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

`index-images` takes two optional preprocessing flags for ONNX models:
- `--aspect pad` letterboxes each photo with its own border colour, and
  `--aspect center-crop` keeps the middle square. The default stays `squash`.
- `--flip-tta` averages each photo's embedding with its mirror image's.

Both are added to the index's `feature_version` (for example
`…:f1009cfc…+aspect-pad+flip-tta`), and photo search embeds queries the way the
index was built. An index built without flags keeps its old version and needs no
rebuild. Look search only needs the same model, so it accepts any of these
indexes. Measured with INT8 on 2,216 practice photos (1,656 ABO same-product
queries, 560 GLAMI category queries):

| Index | Same product Hit@1 | Hit@5 | Category P@5 |
|---|---|---|---|
| squash (default) | 0.475 | 0.725 | 0.745 |
| pad | 0.465 | 0.714 | 0.752 |
| center-crop | 0.423 | 0.677 | 0.725 |
| squash + flip TTA | 0.486 | 0.725 | 0.755 |

Flip TTA gains about one point, costs twice the indexing time and one more model
run per photo search, so it stays opt-in. Centre-cropping cuts off sleeves and hems.

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
same filters as `/api/v1/search`, plus `limit` (1–50, default 20). Text search
refuses an index built with another model, because the two embeddings would not be
comparable.

### How a look query is read

The model only reads English, and most shoppers type Hungarian. Each query is read
first (`look_query.understand`, dictionary in `hungarian.py`):

- **Hungarian words are translated** by a fashion dictionary of about 480 words:
  garments, colours, fabrics, patterns, fits, lengths, sleeves, necklines,
  occasions and who it is for (`női`, `férfi`, `gyerek`). Accents are optional.
  Case and plural endings are stripped (`kabátokat` → coat), compounds are split
  (`bőrkabát` → leather coat), and two-word phrases are kept together
  (`hosszú ujjú` → long sleeve, `fekete-fehér` → black and white).
- **Unknown words are kept as typed**, so brands (`Tamaris`) still reach the model.
  Unknown words with Hungarian letters are listed in `interpretation.untranslated`.
- **Price and size phrases become limits**, not words for the model:
  `10000 Ft alatt`, `20 000 Ft-ig`, `10e Ft alatt`, `5000 és 10000 Ft között`,
  `15000 felett`, `under 15000 Ft`, `38-as`, `M-es`. A `max_price` filter that the
  caller sends wins over a typed one.
- **Colour, category and gender become soft hints.** An offer that matches them
  moves up the visual order; nothing is filtered out.
- The English text is wrapped as `a photo of {q}` before embedding (measured best;
  see below).

`piros nyári ruha 10000 Ft alatt` is embedded as `a photo of red summer dress`,
limited to 10,000 Ft, and hinted towards red dresses. The response says so:

```json
{
  "items": [...],
  "interpreted_query": "red summer dress",
  "interpretation": {
    "language": "hu", "translated_by": "dictionary",
    "colour": "red", "category": "dresses", "gender": null, "size": null,
    "max_price": "10000", "min_price": null, "untranslated": []
  }
}
```

Then the visual order is fused with word relevance by reciprocal rank fusion
(k = 60). Word relevance lists only the offers whose title, category and colours
contain the most query words (usually all of them), so a title that names the item
ranks above one that only looks similar, and a red dress does not compete with a
red Manzara parka for `manzara piros`.

### Look options

`search_indexed_text` takes these keyword arguments, each with a safe default, so
the benchmark can compare them (`--look-options '{json}'`):

| Option | Default | Values |
|---|---|---|
| `translate` | `"dictionary"` | `"none"`, `"dictionary"`, `"dictionary+mt"`, `"mt"` |
| `translation_model_dir` | None | folder with the pinned opus-mt files (below) |
| `price_hints` | True | read price and size phrases from the text |
| `prompt_template` | `"a photo of {q}"` | None, one template, or a list (averaged) |
| `product_text_weight` | None | 0–1; None uses the default when a sidecar exists |
| `hints` | `"boost"` | `"boost"`, `"filter"`, `"off"` |
| `hint_weight` | 0.5 | visual-score bonus, in standard deviations, for meeting every hint |
| `fusion` | `"rrf"` | `"rrf"`, `"weighted"` |
| `rrf_k`, `lexical_weight` | 60, 1.0 | fusion constants |
| `blend` | True | False ranks by look alone |

### Product text sidecar

`index-product-text` describes every indexed offer in a few English words from its
structured fields (`women's black quilted hooded coat cotton`), embeds the
descriptions with the text model, and writes `product-text-index.json` next to the
photo index. It never changes the photo index. Rebuild it after `index-images`:

```bash
.venv/bin/fashion-finder --data-dir data index-product-text \
  --policy data/source-policies.json \
  --model-path data/models/fashion-siglip-vision.onnx \
  --text-model-path data/models/fashion-siglip-text.onnx \
  --tokenizer-path data/models/fashion-siglip-tokenizer.json
```

The sidecar records its format, description, text-model and photo-index versions.
A sidecar from another model or photo index is refused: an explicit
`product_text_weight` raises an error, and the default quietly ranks without it.
An offer whose title, category or colours changed since the build loses its stale
description. With `product_text_weight = w`, the visual score is
`(1 − w) · photo + w · text`, each standardised over the eligible offers.

### Optional offline translation

The dictionary path needs no model. For words it misses, look search can call the
Apache-2.0 [`Helsinki-NLP/opus-mt-hu-en`](https://huggingface.co/Helsinki-NLP/opus-mt-hu-en)
model, as the INT8 ONNX export in
[`Xenova/opus-mt-hu-en`](https://huggingface.co/Xenova/opus-mt-hu-en) at revision
`c9e8a7617fb4cb4fca3785c5bb22cfe457cde00b`. It runs locally with greedy decoding.
Only unknown words with Hungarian letters reach it: dictionary words never do, and
neither do plain-ASCII words, which are usually brands the model would rewrite.
Numbers and brackets it invents are dropped.

| File | Size | SHA-256 |
|---|---|---|
| `onnx/encoder_model_quantized.onnx` | 51.6 MB | `f0dce998a12ce35d4880d5f6ac153807be88bb50691244e512e9605b2cabd6fe` |
| `onnx/decoder_model_quantized.onnx` | 58.6 MB | `bec30ea95c7331e4fb33ee61a9812d2a4a234fbcd116a21a86b114aa8b7fb2c5` |
| `tokenizer.json` | 6.1 MB | `05467c7d69c3755d66e02b34d58d9643d0b2e7c28d3e761ba6036d7c855326c5` |

```bash
REV=c9e8a7617fb4cb4fca3785c5bb22cfe457cde00b
mkdir -p data/models/opus-mt-hu-en
for f in onnx/encoder_model_quantized.onnx onnx/decoder_model_quantized.onnx tokenizer.json; do
  curl -fL -o "data/models/opus-mt-hu-en/$(basename $f)" \
    "https://huggingface.co/Xenova/opus-mt-hu-en/resolve/$REV/$f"
done
sha256sum data/models/opus-mt-hu-en/*
```

Set `FASHION_FINDER_TRANSLATION_MODEL_DIR=/data/models/opus-mt-hu-en` for the API,
or pass `--translation-model-dir` to `search-look`. A missing or unpinned folder
leaves the API on the dictionary.

`interpretation.translated_by` says what actually translated the query: `none`
(nothing needed it), `dictionary`, `dictionary+mt` or `mt`.

### Measured on the practice catalogue

Private measurement, 2026-10-24, INT8 FashionSigLIP vision and text models, on
agent 1's practice catalogue (3,597 ABO and GLAMI-1M `hu` offers, 4,747 photos)
loaded with `source_key="reserved"` into a scratch database. Two labelled query
sets, kept with the scratch data, not in the repository:

- **Main set**, 100 queries (52 Hungarian, 48 English): category, colour and
  category, material or pattern, price and product-level queries. Category and
  colour labels come from the catalogue's category, gender and colour fields;
  material and pattern labels from the offer's own text.
- **Title set**, 40 Hungarian queries made from random GLAMI titles (codes and
  capitalised brands removed); the one relevant item is that product. The
  dictionary was not extended for these words.

Hit@5 / MRR@10 / nDCG@10:

| Variant | Main, Hungarian | Main, English | Title set |
|---|---|---|---|
| Before (30-word dictionary, all-words RRF) | 0.865 / 0.800 / 0.700 | 0.958 / 0.917 / 0.865 | 1.000 / 0.958 / 0.969 |
| **Now (defaults)** | **1.000 / 0.978 / 0.942** | **1.000 / 0.974 / 0.935** | **1.000 / 0.971 / 0.978** |
| no translation | 0.923 / 0.902 / 0.858 | 1.000 / 0.974 / 0.935 | 1.000 / 0.926 / 0.944 |
| dictionary + translation model | 1.000 / 0.978 / 0.942 | 1.000 / 0.974 / 0.935 | 1.000 / 0.938 / 0.954 |
| translation model for the whole query | 1.000 / 0.955 / 0.914 | 1.000 / 0.974 / 0.904 | 1.000 / 0.971 / 0.978 |
| no prompt template | 1.000 / 0.968 / 0.940 | 1.000 / 0.974 / 0.937 | 1.000 / 0.971 / 0.978 |
| no product text | 1.000 / 0.957 / 0.922 | 1.000 / 0.974 / 0.946 | 1.000 / 0.945 / 0.958 |
| hints off | 1.000 / 0.968 / 0.927 | 1.000 / 0.964 / 0.914 | 1.000 / 0.971 / 0.978 |
| hints as filters | 0.962 / 0.929 / 0.901 | 0.979 / 0.953 / 0.934 | 0.775 / 0.733 / 0.744 |
| weighted fusion (lexical 3) | 1.000 / 0.978 / 0.942 | 1.000 / 0.974 / 0.935 | 1.000 / 0.971 / 0.978 |

By look alone (`blend=False`), where translation and product text matter most:

| Variant | Main, Hungarian | Main, English | Title set |
|---|---|---|---|
| before (30-word dictionary) | 0.558 / 0.467 / 0.424 | 0.917 / 0.785 / 0.727 | 0.375 / 0.268 / 0.327 |
| no translation | 0.269 / 0.188 / 0.152 | 0.917 / 0.845 / 0.770 | 0.175 / 0.141 / 0.156 |
| dictionary | 0.827 / 0.752 / 0.685 | 0.917 / 0.845 / 0.770 | 0.550 / 0.429 / 0.469 |
| dictionary + translation model | 0.827 / 0.752 / 0.685 | 0.917 / 0.845 / 0.770 | 0.575 / 0.484 / 0.528 |
| dictionary + product text (0.5) | 0.942 / 0.868 / 0.818 | 0.979 / 0.950 / 0.881 | 0.825 / 0.700 / 0.743 |

What decided the defaults:
- **Dictionary on by default; translation model opt-in.** The model helps by look
  alone on unseen words (title set MRR 0.429 → 0.484) but lowers the fused title
  set (0.971 → 0.938), rewrites brands, and adds up to 258 ms. Whole-query
  translation hurt English queries and costs 127 ms at the median.
- **`a photo of {q}`**: +0.045 MRR by look alone on the main set; neutral fused.
  `a product photo of {q}` and averaging three templates were not better.
- **Product text weight 0.5**: the best balance of both sets (0.7 was best by
  look alone, but lost Hit@5 on English queries).
- **Hint weight 0.5** as a visual-score bonus; as filters, hints cost 0.24 MRR on
  the title set.
- **RRF k = 60, lexical weight 1**: the best RRF settings (k 10–60, lexical
  weight 1–2) and the best weighted fusion (lexical weight 3) were within 0.01 MRR
  of each other, so RRF stays; lower lexical weights were worse.

[SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-224) (Apache-2.0,
multilingual; INT8 ONNX from `onnx-community/siglip2-base-patch16-224-ONNX`) was
measured by look alone with its own photo index. It reads some Hungarian (main
Hungarian MRR 0.381 untranslated, against 0.188 for FashionSigLIP), but with the
dictionary FashionSigLIP is better (0.752 against 0.715; English 0.845 against
0.782; title set 0.429 against 0.326), and its text model is 2.5 times larger.
It was not added.

Latency, 100 main-set queries, measured while other model jobs kept the 6-CPU
machine at a load average of about 9–10: median 378 ms, p95 438 ms. Of a median
query, `_eligible` (the per-offer policy check shared with photo search) took
about 240 ms, ranking 50 ms, the text embedding 25 ms and reading the query under
0.1 ms. With the translation model on, 11 of 140 queries used it: reading took
p95 60 ms and at most 258 ms. Whole-query translation took a median 127 ms and
p95 412 ms. The model loads in about 0.7 s and needs about 0.5 GB more memory.

## Photo understanding

With Marqo-FashionSigLIP **and** its text model configured
(`FASHION_FINDER_TEXT_MODEL_PATH`, `FASHION_FINDER_TOKENIZER_PATH`), photo search
reads the photo before ranking (`src/fashion_finder_data/query_understanding.py`).
With the colour-layout descriptor, the ViT, or no text model, none of this runs:
the whole photo is embedded as before and `query` reports nothing.

**Garment focus (`auto_crop`).** A shopper's photo often shows a person, a street
or a whole outfit, while catalogue photos show one product. The search embeds the
whole photo first. A photo already very close to an indexed photo (cosine ≥ 0.85)
is a product photo and is searched whole: one model run. Otherwise seven regions
follow in one batched run: halves, the centre, and bands for torso, legs and feet
(or columns for a wide photo). The photo's main category is read with prompts such
as "a photo of a jacket". The search then uses the region that looks most like that
category and least like "a photo of a person". A region must beat the whole photo
by a margin, so product photos stay whole.

**Other garments (`query.alternatives`).** Regions that clearly read as another
category, and overlap the searched area little, are offered as alternatives. The
page shows them as "Also in this photo: Jeans". Choosing one re-runs the search on
that region, sent as `crop=manual`.

**Category and colour (`zero_shot`).** The searched region is classified against
the 14 categories and, for that category, the repository's 15 colour words.
`query.category` appears from probability 0.6 and `query.colour` from 0.7. The
category is blended into the neighbours' vote (below). Offers whose
`colour_names` match a confidently seen colour move up by 0.02 cosine distance,
unless the shopper set a colour filter.

The response adds, next to `items` and `preferred_category`:

```json
"query": {
  "crop_source": "auto",
  "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.5},
  "category": {"value": "jackets", "confidence": 0.94},
  "colour": {"value": "brown", "confidence": 1.0},
  "alternatives": [{"category": "jeans", "crop": {"x": 0.0, "y": 0.5, "width": 1.0, "height": 0.5}}]
}
```

`crop` is a fraction of the uploaded photo and is null when the whole photo was
searched. `crop_source` is `auto`, `manual` (the shopper cropped) or `none`.

On the shopper page, the automatic box is drawn on the preview. Chips such as
"Jacket · brown" show what was searched, marked "?" when the reading is weak.
**Use whole photo** turns the focus off for that search. The shopper can still
drag a crop, use the presets or the arrow keys; **Clear crop** sends the whole
photo again.

## The category vote

When the shopper picks no category, the 20 closest offers vote on one, with weights
1, 1/2, 1/3, and so on, so the nearest matches count most. With zero-shot on, the
vote is blended with the photo's own category probabilities first. Their share
rises from 0.25 to 0.75 as the nearest offer's similarity falls from 0.80 to 0.70:
product photos sit close to catalogue photos and their neighbours vote well,
while a street photo is far from all of them. If one category then holds a
majority, the single nearest match stays first and the majority category follows
in visual order; the response includes `preferred_category`. A red-dress photo then
lists dresses before a red bag. Without a clear majority, the order stays purely
visual. Look search does not use the vote.

## Colour variants (`diversify`)

Colour variants and duplicate listings of one product are folded into its nearest
offer, which gains `variant_count` (how many were folded) and `variant_colours`
(their other colour names). The page shows a "+2 colours" badge. Offers merge when
they share a product-group id (a field offers do not carry yet). Otherwise they must
come from the same shop at the same price, have the same title once colour words
are dropped, and have best photos with cosine similarity ≥ 0.80. Photo similarity
alone never merges: shops reuse one outfit photo on several product pages. A
cheaper colour, or the same item at another shop, stays a separate result. On the
practice catalogue's ABO colour-variant groups, the rule was 97 % precise and found
25 % of variant pairs (45 % at similarity ≥ 0.70, still 97 % precise).

## Photo search options

`search_indexed_image` takes each behaviour as a keyword argument, so a benchmark
can switch them: `auto_crop=True`, `zero_shot=True`, `diversify=True`,
`consensus=True`, `manual_crop=False`, `limit=5`, `aspect=None` and
`flip_tta=None`. For `aspect` and `flip_tta`, None follows how the index was built;
a different value raises an error rather than mixing embeddings.
`text_model_path` and `tokenizer_path` enable understanding.

Measured with INT8 models on the practice catalogue (4,653 photos) and on 325
licence-safe Fashionpedia street photos (CC BY, CC BY-SA, Unsplash, Pexels and
freestocks images; private measurement only), offline with cached embeddings:

| Query set | Measure | Before | After |
|---|---|---|---|
| ABO product photos (200) | same product Hit@5 | 0.66 | 0.66 (auto-crop) |
| GLAMI product photos (150) | exact photo Hit@5 | 1.00 | 1.00 (auto-crop) |
| ABO product pasted in a street photo (200) | same product Hit@5 | 0.29 | 0.34 (auto-crop) |
| GLAMI garment pasted in a street photo (150) | exact photo Hit@5 | 0.50 | 0.69 (auto-crop) |
| Street photos (324) | top 5 in the main garment's category | 0.554 | 0.591 (zero-shot) |
| GLAMI leave-one-out (2,997) | category P@5 | 0.864 | 0.869 (zero-shot) |
| GLAMI with a labelled colour (1,767) | top 5 in the photo's colour | 0.289 | 0.353 (colour boost; category P@5 0.867 → 0.862) |

Through the real `search_indexed_image` with 40 held-out queries per set, defaults
against every option off gave: pasted ABO products Hit@1 0.23 → 0.38 and Hit@5
0.38 → 0.45; pasted GLAMI garments Hit@5 0.50 → 0.68; product photos unchanged
within noise.

Known limits:
- On real street photos, 75 % get an automatic crop. Of those crops, 69 % keep the
  main garment whole, and the garment fills 17 points more of the frame. The rest
  cut it: the regions are fixed bands, not a garment detector. **Use whole photo**
  and a manual crop recover.
- Alternatives are right 78 % of the time on street photos. They appear on 35 % of
  street photos and, falsely, on 12 % of single-product photos.
- Same-product matching for a person wearing the item was not measured: no
  licence-safe set pairs worn photos with catalogue photos.

## Speed

Each photo search runs the vision model once, or eight times when it tries
regions. On this 6-core WSL machine, one INT8 run takes about 120 ms when idle.
Other jobs shared the CPU during every measurement. Measured on 60 photos (30
street, 30 product) against the 4,653-photo practice index:

| Setting | Load average | p50 | p95 |
|---|---|---|---|
| everything off | 11.5 | 553 ms | 649 ms |
| defaults (auto-crop on) | 10.9 | 2.4 s | 4.6 s |
| defaults, `FASHION_FINDER_ONNX_THREADS=2` | 12.5 | 1.8 s | 4.1 s |
| everything off | 6.3 | 309 ms | 391 ms |
| defaults (auto-crop on) | 7.5 | 1.4 s | 2.7 s |
| defaults, `FASHION_FINDER_ONNX_THREADS=2` | 6.2 | 1.7 s | 3.1 s |

Product photos take the one-run path. The slow tail is street photos, which try
all eight regions. At 120 ms per run an idle machine needs about one second for
them, but the 1.5 s p95 target was not met under shared load. Pass
`auto_crop=False` to trade focus for speed.

`FASHION_FINDER_ONNX_THREADS` caps ONNX Runtime threads per model (default: all
cores). It helped only under heavy contention (load 12) and hurt at load 6, so
leave it unset unless the CPU is oversubscribed. Policy checks for photo search
run nearest first and stop after 200 eligible offers, so they no longer grow with
the index (60–85 ms here instead of about 300 ms).

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

The report's `metrics` block gives, for each search mode, the mean **Hit@5** (a
relevant offer in the top 5), **MRR@10** (1 / rank of the first relevant offer) and
**nDCG@10** (rewards several relevant offers near the top):

| `metrics` key | What it measures |
|---|---|
| `text` | Word search, sorted by best match |
| `photo` | Photo search with the task's category filter (5 results) |
| `photo_any_category` | Photo search with no category, with the vote |
| `photo_any_category_without_vote` | The same without the vote |
| `look` | Text-to-photo search for each task's query |
| `text_on_look_tasks` | Word search on the same tasks as `look` |

The release gate still needs 30 tasks with 80 % successful. For comparing two
models or settings, use 50–100 tasks (up to 100 are accepted): with 30, only a
large difference is distinguishable from noise. Rebuild the index with each model
you compare (`--model-path` changes the index), and use the same tasks every run.

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
