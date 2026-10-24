# Benchmark results: photo and look search on the practice catalogue

Measured 2026-10-24 on the `improve/benchmark` branch (base `6d0b3d7`, uncommitted). Machine:
6 CPUs and 15 GB RAM shared with three other agents encoding at the same time (load average
9–12), so every latency here is pessimistic, roughly 3× an idle machine.

## Dataset

The [practice catalogue](practice-catalogue.md): 3,597 products and 4,653 photos from ABO
(shoes, bags, accessories; English titles; 2–3 photos each) and GLAMI-1M Hungarian rows
(all 14 categories; one photo each). `build_split` holds the queries out of the gallery
deterministically:

| | count |
|---|---|
| Gallery products / photos | 3,387 / 3,843 |
| Held-out GLAMI products (15 per category, never in the gallery) | 210 |
| Held-out ABO photos (each ABO product's last view) | 600 |

| Task | Queries | Query | Relevant |
|---|---|---|---|
| `a` photo → same product | 600 | held-out ABO view | that product (its other 1–2 views are indexed) |
| `b` photo → same category | 255 | 15 held-out ABO views per ABO category + the 210 held-out GLAMI products | every gallery product of that category |
| `c` look, English name | 600 | ABO title, e.g. "206 Collective Women's Hannah Leather Slip-on Sneaker" | that product |
| `d_name` look, Hungarian name | 210 | GLAMI title, 15 per category, e.g. "Női Diesel Body Body Fekete" | that product |
| `d_phrase` look, Hungarian phrase | 150 | e.g. "fekete női kabát", "zöld nadrág" | gallery products of that category, colour and gender |
| `d_phrase_en` | 150 | the same phrases in English, e.g. "black women's coat" | same as `d_phrase` |

`d_phrase` relevance needs a detected colour, and 39 % of products have none, so its
precision is a lower bound; compare variants with each other, not with 1.0.

Metrics per query, averaged: Hit@1, Hit@5, MRR@10 and nDCG@10 (the last three from
`ranking_evaluation.ranking_metrics`, with unranked relevant items counted in the ideal
DCG), Recall@10 (relevant found in the top 10 / all relevant; tiny for category tasks),
Precision@10, and per-query latency p50/p95. Real photo search returns only 5 results, so
its @10 numbers are really @5 (P@10 is at most 0.5).

## How to run

```bash
PRACTICE=/path/to/practice; WORK=/path/to/bench-work; MODELS=/path/to/models
# Script B: raw embeddings, any pinned encoder; rerun until "status: complete"
# (each run encodes at most 2,000 photos and caches them per encoder version)
.venv/bin/python scripts/benchmark_public_retrieval.py --mode embedding \
  --practice-dir "$PRACTICE" --work-dir "$WORK" \
  --model-path "$MODELS/fashion-siglip-vision-int8.onnx" \
  --text-model-path "$MODELS/fashion-siglip-text-int8.onnx" \
  --tokenizer-path "$MODELS/fashion-siglip-tokenizer.json" --output results.json
# Harness A: the shipped search functions on a temporary practice data dir; options are
# passed straight through as keyword arguments, and unknown ones are refused
.venv/bin/python scripts/benchmark_public_retrieval.py --mode real ... \
  --photo-options '{"consensus": false}' --look-options '{"blend": false}'
```

Omit `--model-path` for the colour-layout encoder; omit the text model to run only `a`
and `b`. The script exits 3 while an embedding cache is incomplete.

## Photo search: encoders compared (embedding mode)

Raw embeddings, each product ranked by its nearest photo, no category vote.

| Encoder | Task | Hit@1 | Hit@5 | MRR@10 | nDCG@10 | R@10 | P@10 |
|---|---|---|---|---|---|---|---|
| colour layout (built in) | a photo → product | 0.087 | 0.145 | 0.113 | 0.127 | 0.172 | 0.017 |
| ViT-B/32 INT8 | a photo → product | 0.237 | 0.397 | 0.303 | 0.340 | 0.457 | 0.046 |
| FashionSigLIP INT8 | a photo → product | 0.512 | 0.703 | 0.597 | 0.637 | 0.765 | 0.077 |
| FashionSigLIP fp32 | a photo → product | 0.525 | 0.718 | 0.610 | 0.652 | 0.785 | 0.079 |
| colour layout | b photo → category | 0.533 | 0.769 | 0.642 | 0.370 | 0.016 | 0.331 |
| ViT-B/32 INT8 | b photo → category | 0.816 | 0.953 | 0.873 | 0.757 | 0.034 | 0.742 |
| FashionSigLIP INT8 | b photo → category | 0.894 | 0.996 | 0.933 | 0.827 | 0.039 | 0.807 |
| FashionSigLIP fp32 | b photo → category | 0.910 | 0.996 | 0.942 | 0.826 | 0.038 | 0.803 |

Zero-shot category from the photo alone (FashionSigLIP text prompts such as "a photo of a
coat", top-1 of 14): 0.898 on the task-a queries and 0.859 on the task-b queries with the
INT8 towers (`zero_shot_category` in the report). The neighbour vote below is more
accurate when it speaks.

Speed, measured separately in one process with both models, 40 photos and 30 phrases,
three interleaved rounds at load average 5: FashionSigLIP vision INT8 130 ms per photo
(p95 141), fp32 169 ms (p95 212); text INT8 21 ms per query, fp32 57 ms. ViT INT8 encoded
at 64 ms and colour layout at 0.4 ms per photo during their cache runs. Peak RSS of the
benchmark process: colour 149 MB, ViT 298 MB, FashionSigLIP INT8 vision + text 420 MB,
fp32 vision + text 1,066 MB. INT8 and fp32 photo embeddings agree closely (mean cosine
0.986, minimum 0.943 over the 40 photos).

## Look search: text towers compared (embedding mode)

Raw text embedding against the photo gallery: no translation, no word blending.

| Vision / text | c English name, Hit@1 / Hit@5 | d_name Hungarian name, Hit@1 / Hit@5 | d_phrase Hungarian, Hit@1 / P@10 | d_phrase_en English, Hit@1 / P@10 |
|---|---|---|---|---|
| INT8 / INT8 | 0.613 / 0.855 | 0.219 / 0.371 | 0.060 / 0.063 | 0.467 / 0.373 |
| fp32 / fp32 | 0.643 / 0.872 | 0.443 / 0.652 | 0.400 / 0.276 | 0.460 / 0.372 |
| fp32 / INT8 | 0.603 / 0.857 | 0.210 / 0.371 | 0.060 / 0.057 | 0.420 / 0.376 |
| INT8 / fp32 | 0.642 / 0.867 | 0.443 / 0.638 | 0.387 / 0.282 | 0.473 / 0.367 |

The INT8 **text** tower is what loses Hungarian: with the fp32 text tower, raw Hungarian
phrases reach P@10 0.28 whichever vision tower is used; with the INT8 text tower they fall
to 0.06, while English phrases stay at 0.37 either way. INT8 and fp32 text embeddings
agree at cosine 0.993–0.996 for English phrases but only 0.82–0.83 for "fekete női kabát"
and "barna férfi cipő": quantisation error lands on the rare, non-English tokens.

## The shipped search functions (real mode, FashionSigLIP INT8)

`search_indexed_image` with no category filter (as a shopper who picks none) and
`search_indexed_text` with its defaults: `english_query` translation and reciprocal-rank
fusion with word relevance (`blend=True`). The "fp32 text tower" rows keep the INT8 photo
index and swap only `--text-model-path` to the fp32 file. Latencies are end to end per
call, including encoding and `_eligible`, under load.

| Options | Task | Hit@1 | Hit@5 | MRR@10 | nDCG@10 | R@10 | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| defaults (vote on) | a | 0.512 | 0.702 | 0.588 | 0.617 | 0.702 | 0.070 | 447 | 709 |
| `{"consensus": false}` | a | 0.512 | 0.703 | 0.589 | 0.618 | 0.703 | 0.070 | 640 | 945 |
| defaults (vote on) | b | 0.894 | 0.937 | 0.912 | 0.582 | 0.022 | 0.449 | 562 | 865 |
| `{"consensus": false}` | b | 0.894 | 0.996 | 0.932 | 0.557 | 0.021 | 0.425 | 787 | 1036 |
| defaults (blend on) | c | 0.965 | 0.998 | 0.980 | 0.985 | 1.000 | 0.100 | 337 | 612 |
| `{"blend": false}` | c | 0.613 | 0.853 | 0.717 | 0.762 | 0.898 | 0.090 | 266 | 355 |
| defaults (blend on) | d_name | 0.991 | 0.995 | 0.993 | 0.994 | 0.995 | 0.100 | 322 | 405 |
| `{"blend": false}` | d_name | 0.281 | 0.452 | 0.355 | 0.397 | 0.529 | 0.053 | 312 | 403 |
| defaults (blend on) | d_phrase | 0.827 | 0.933 | 0.876 | 0.717 | 0.577 | 0.539 | 346 | 455 |
| `{"blend": false}` | d_phrase | 0.367 | 0.680 | 0.501 | 0.336 | 0.291 | 0.271 | 304 | 476 |
| defaults (blend on) | d_phrase_en | 0.687 | 0.947 | 0.791 | 0.686 | 0.595 | 0.541 | 322 | 418 |
| `{"blend": false}` | d_phrase_en | 0.487 | 0.907 | 0.659 | 0.476 | 0.420 | 0.367 | 285 | 371 |
| colour layout, vote on | a | 0.087 | 0.155 | 0.112 | 0.123 | 0.155 | 0.015 | 627 | 939 |
| colour layout, vote on | b | 0.533 | 0.702 | 0.605 | 0.305 | 0.010 | 0.228 | 429 | 663 |
| colour layout, `{"consensus": false}` | b | 0.533 | 0.769 | 0.628 | 0.277 | 0.009 | 0.199 | 642 | 879 |
| fp32 text tower, `{"blend": false}` | d_name | 0.400 | 0.638 | 0.502 | 0.551 | 0.705 | 0.070 | 236 | 304 |
| fp32 text tower, `{"blend": false}` | d_phrase | 0.393 | 0.733 | 0.546 | 0.370 | 0.318 | 0.296 | 246 | 297 |
| fp32 text tower, `{"blend": false}` | d_phrase_en | 0.460 | 0.887 | 0.642 | 0.470 | 0.419 | 0.365 | 248 | 292 |
| fp32 text tower, defaults | d_name | 0.986 | 0.995 | 0.991 | 0.992 | 0.995 | 0.100 | 270 | 310 |
| fp32 text tower, defaults | d_phrase | 0.820 | 0.940 | 0.878 | 0.737 | 0.603 | 0.557 | 283 | 329 |
| fp32 text tower, defaults | d_phrase_en | 0.667 | 0.927 | 0.779 | 0.682 | 0.595 | 0.541 | 289 | 347 |

Category guesses on photo queries (share of queries that showed one, and how often it was
right): the vote's `preferred_category` appeared on 97.7 % of task-a queries (right 97.8 %
of those times) and 95.3 % of task-b queries (right 92.2 %). No branch returns
`query.category` yet; once agent 2's field lands, the same run reports it as
`query_category_shown` and `query_category_precision`.

Colour layout through the real path matches its embedding numbers (a Hit@1 0.087 both
ways), which cross-checks the two rankers. It is slow: its distance is pure Python, so
one photo query over 3,843 photos took 0.43–0.64 s at p50.

## Memory on Render's free plan (512 MB)

Measured with `gunicorn --workers N --threads 2` (as `scripts/render-api-start.sh` runs
it), the INT8 vision and text models, and the full practice index (4,653 photos, 20 MB
file); totals are after serving image, look and health requests on every worker. PSS
counts pages shared between the gunicorn master and workers once.

| Setup | RSS total | PSS total |
|---|---|---|
| 1 worker, vision + text INT8 | 463 MB (worker 433 MB) | 432 MB |
| 2 workers, vision + text INT8 (the current start script) | 865 MB | 781 MB |
| 1 worker, vision INT8 only (look search off) | 320 MB | 288 MB |
| `fashion-finder-worker` refresh loop, idle (runs beside the API outside demo mode) | 61 MB | 46 MB |

Verdict: the current start script (two workers, both models) does not fit 512 MB. One
worker with both models fits only on paper: 432 MB PSS plus the refresh worker's 46 MB
leaves about 30 MB, before Python's per-request peaks (an upload decode is up to 12 MB)
and index growth (the embedding matrix is 3 KB per photo; 20,000 photos add about 45 MB
over this index, plus the per-entry Python objects). Plausible free-plan setups: one
worker with the vision model only (320 MB), or one worker with both models and no
refresh worker in the same instance. The health check loads both models, so the first
probe after a deploy pays the load time.

## Private probe: street photos (task e, LookBench, not committed)

[LookBench](https://huggingface.co/datasets/srpone/look-bench) (card Apache-2.0, but its
real photos come from an image search engine and its distractors from Fashion200K) was
used **privately only**: nothing from it is committed, shipped or indexed for the page. To
keep it to about 13,500 photos we used each subset's own gallery **without** the 58,275
Fashion200K distractors, FashionSigLIP INT8, and counted a hit only when the top result
shares the query's `item_ID`. The paper's headline "fine Recall@1" (62.77 overall for
FashionSigLIP, with distractors) instead credits any result with the same category and
attributes, so these numbers are stricter, not comparable, and say nothing about
preprocessing. Each query ran twice: the dataset's detector crop, and the full photo.

| Subset | Queries / gallery | R@1 crop | R@1 full photo | R@10 crop | R@10 full photo |
|---|---|---|---|---|---|
| RealStudioFlat (packshots) | 1,011 / 3,951 | 0.574 | 0.548 | 0.886 | 0.853 |
| RealStreetLook (street) | 981 / 3,278 | 0.431 | 0.316 | 0.686 | 0.527 |
| AIGen-Studio | 193 / 979 | 0.679 | 0.632 | 0.953 | 0.928 |
| AIGen-StreetLook | 160 / 571 | 0.650 | 0.344 | 0.956 | 0.744 |

Street photos are where the practice catalogue's packshot-to-packshot tasks would mislead:
real street queries lose 14 points of R@1 against studio queries even when cropped, and
the full street photo loses another 11 (real) to 31 (generated) points. Cropping to the
garment matters far more for street photos than for packshots (2.6 points). The probe
script is `fashion-tools/lookbench_probe.py` in the agent's scratch folder; it is not part
of the repository.

## Findings

1. **FashionSigLIP is the right default.** On photo → same product it finds the product
   first in 51 % of queries (Hit@5 70 %), against 24 % for ViT and 9 % for colour layout;
   on photo → category it is right first time in 89 %. fp32 gains little on photos (+1.3
   points Hit@1 on both photo tasks) for 1.3× the encode time and about 2.5× the memory.
2. **Look search needs English; translation does most of the work.** With the INT8 towers,
   raw Hungarian phrases ("fekete női kabát") score P@10 0.063 against 0.373 for the same
   phrases in English. The shipped `english_query` lifts Hungarian phrases to 0.271 with
   blending off (74 % of English). The rest of the gap is words outside
   `text_match.TRANSLATIONS`, such as női, férfi and gyerek, which reach the model as "noi",
   "ferfi" and "gyerek".
3. **The INT8 text tower is the Hungarian bottleneck, not the model.** The fp32 text tower
   alone scores raw Hungarian phrases at P@10 0.28 and Hungarian names at Hit@1 0.44; INT8
   drops them to 0.06 and 0.22 while English stays level. With INT8 (the dev default and the
   only text tower that fits a small instance), every Hungarian word that `english_query`
   does not translate is close to noise; with fp32, untranslated words still carry signal.
   Through the shipped path with translation and blending off, swapping in the fp32 text
   tower raises Hungarian phrases from P@10 0.271 to 0.296 (English: 0.365) and Hungarian
   titles from Hit@1 0.281 to 0.400, at 57 ms instead of 21 ms per query and a 441 MB
   instead of 111 MB model file.
4. **Blending inflates the name tasks.** With `blend=True`, `c` and `d_name` (the query is
   the product's exact title) become a title lookup: Hit@1 0.965 and 0.991, against 0.613
   and 0.281 without blending. `d_phrase` also gains (P@10 0.271 → 0.539), but its colour
   and gender labels come from the same title words the blend matches, so part of that
   gain is by construction. Judge visual look-search changes on `{"blend": false}` and
   `d_phrase`, and judge blending itself only on real shopper queries.
5. **The category vote trades recall for precision.** With the vote, photo → category
   precision among the 5 results rises from 0.425 to 0.449, but Hit@5 falls from 0.996 to
   0.937: when the vote is wrong (7.8 % of the times it speaks), all four results after
   the first switch to the wrong category. Photo → product is unaffected. The neighbour
   vote names the right category more often (92 %) than zero-shot prompts do (86 %), so a
   recognised `query.category` should lean on neighbours, not prompts alone.
6. **`_eligible` dominates search latency after encoding.** At 4,653 photos the
   per-request offer and policy filter took 0.24–0.41 s and the vector ranking 0.019 s
   (measured separately). It reloads every candidate offer from SQLite on each request,
   so it grows with the index; at the 20,000-photo cap expect over a second.
7. **Photo → product is held down by the ABO views, not by preprocessing.** By category
   (INT8, embedding mode) shoes reach Hit@1 0.70 and Hit@5 0.86, bags 0.56 / 0.83, but
   accessories only 0.28 / 0.43. A look at a sample of products shows why: the held-out
   last view is often a size reference (an earring on a grey head silhouette, a ring on a
   grey hand, a watch on a wrist), a worn-on-model shot, an infographic or a bag's
   interior, which shares little with the indexed packshot. Colour layout through the
   real path matches its embedding numbers exactly, the real path reproduces the embedding
   ranking for FashionSigLIP (a Hit@1 0.512 both ways), and INT8 and fp32 photo embeddings
   agree (cosine 0.986), so neither the preprocessing nor the quantisation explains the gap.
8. **Shopper photos will be harder than these packshots, and cropping is the big lever.**
   The private LookBench probe loses 11–31 points of R@1 when a street photo is not cropped
   to the garment, against 2.6 points for packshots. The practice tasks use clean catalogue
   photos, so they understate the value of auto-crop; measure crop variants on street
   photos as well.

## Not measured

- The public benchmark task (e) is not part of the script: see the private probe above.
- Browser and network latency: every latency above is local processing on a loaded
  machine.
- Any GPU, any model other than the pinned ViT and FashionSigLIP files.
