# Practice catalogue

A local catalogue of about 3,600 real-looking fashion products from two public datasets, so
the real API, the shopper page and the benchmarks run before any retailer data exists. It
is **practice data, not a shop**: prices and sizes are synthetic, product links use the
reserved `practice.invalid` host, and no photo, embedding or index of it is committed or
deployed.

## Sources and licences

| Dataset | Licence | What we take |
|---|---|---|
| [Amazon Berkeley Objects](https://amazon-berkeley-objects.s3.amazonaws.com/index.html) (ABO) | CC BY 4.0; credit **Amazon.com** for data and photos | Shoes, bags and accessories (ABO has no garments), English titles, up to three catalogue photos each |
| [GLAMI-1M](https://github.com/glami/glami-1m) | Apache-2.0, © Inspigroup s.r.o.; photos come from e-shops and have no separate licence statement | Hungarian (`geo == "hu"`), human-labelled rows for every garment category, one photo each |

Changes made, as CC BY asks: ABO titles lose the "Amazon Brand -" prefix and size suffixes;
categories and colours are mapped to `taxonomy.CATEGORIES` and the colour words of
`text_match`; prices in HUF and sizes are invented. Every offer description ends with its
dataset credit, and the source policy's attribution, shown on every product card, reads
"Practice data, not for sale: ABO © Amazon.com (CC BY 4.0); GLAMI-1M © Inspigroup s.r.o.
(Apache-2.0)". Keep GLAMI photos private: the research in
[public-datasets.md](data-research/public-datasets.md) found no statement about who owns them.

## Build the practice folder

The build downloads about 2 GB (ABO metadata, the GLAMI CSVs read from the 11 GB archive with
HTTP range requests, and roughly 4,700 photos). Choose a folder outside the repository:

```bash
.venv/bin/python -m fashion_finder_data.practice.build --out /path/to/practice
```

It writes `catalogue.jsonl` (one product per line: practice ID, product-group ID for colour
variants, title, category, colour, gender, synthetic price and sizes, photos and credit),
`images/abo/` and `images/glami/`, the licence files, and `build-info.json` with counts.
Each category gets up to 300 products, taken in turn from the dataset's subclasses so one
subclass cannot dominate. Shoes, bags and accessories mix 200 ABO and 100 GLAMI products.
ABO photos shown for several products (gift boxes, size cards) are dropped.

## Load it into a data directory

```bash
.venv/bin/fashion-finder --data-dir /path/to/practice-data \
  load-practice-catalogue /path/to/practice [--limit 1000] \
  --build-index --model-path /path/to/fashion-siglip-vision-int8.onnx
```

The loader stores offers through `storage.upsert_offer` with `source_key="practice"`, writes
an approved practice-only `source-policies.json`, places the photos under
`practice-images/` and writes `practice-image-manifest.json`. It refuses a data directory
that holds any other source, a policy for real sources, or the demo catalogue, and the
`collect`, `ingest`, `import-feed` and `fetch-images` commands refuse a practice data
directory. Rerunning is safe; with a smaller `--limit` the products left out are withdrawn.

`--build-index` runs the real `build_image_index` on at most 2,000 photos per run and stops;
rerun the same command until it reports `"index_status": "complete"` (three runs for the
whole catalogue). Parts are cached by encoder version, so reruns only encode what is new.
`python -m fashion_finder_data.practice.loader` takes the same arguments.

## Run the API and shopper page on it

```bash
export FASHION_FINDER_DB=/path/to/practice-data/fashion_finder.sqlite
export FASHION_FINDER_SOURCE_POLICIES=/path/to/practice-data/source-policies.json
export FASHION_FINDER_PRACTICE_MODE=1
export FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT=1
export FASHION_FINDER_IMAGE_MODEL_PATH=/path/to/fashion-siglip-vision-int8.onnx
export FASHION_FINDER_TEXT_MODEL_PATH=/path/to/fashion-siglip-text-int8.onnx
export FASHION_FINDER_TOKENIZER_PATH=/path/to/fashion-siglip-tokenizer.json
export FASHION_FINDER_CORS_ORIGINS=http://localhost:3000
export FASHION_FINDER_SECRET_KEY=local-practice-only
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

ABO photos load from the dataset's public S3 bucket. GLAMI photos have no public URL, so
practice offers store them as `https://practice.invalid/images/glami/<id>.jpg`; with
`FASHION_FINDER_PRACTICE_MODE=1` and a practice data directory, the API serves them from
`/api/v1/practice/images/` and rewrites that prefix in its JSON responses. Outside practice
mode the route answers 404 and nothing is rewritten. Product links point at
`https://practice.invalid/...`, which does not resolve; the page should label practice
items like demo items rather than offer a shop link.

## Measure search on it

See [benchmark-results.md](benchmark-results.md) for `scripts/benchmark_public_retrieval.py`,
its tasks and the baseline numbers.
