# Public fashion datasets for testing image search

Researched 2026-10-13 from dataset cards, licence pages and papers; no dataset was
downloaded. Code references are to `9a554f5`.

**Summary.** Only two sets have a permissive licence that plausibly covers the
photos as well as the labels: **ABO** (Amazon Berkeley Objects, CC BY 4.0, images
credited to Amazon.com) and **GLAMI-1M** (Apache-2.0, the only set with Hungarian
product titles; it says nothing separate about who owns its e-shop photos). Most
others are non-commercial, or carry a permissive tag over scraped photos the
uploader did not own. They are fine for private measurement, never for shipping.

`evaluate-rankings` cannot read a public dataset directly: it needs offers in the
database, an approved source policy, HUF prices and storefront URLs. Use a
temporary-database harness or a standalone benchmark script (below).

## Datasets

| Dataset | Content | Annotation licence | Image licence | Commercial | Size | Best use here |
|---|---|---|---|---|---|---|
| GLAMI-1M | 1.11M items, 968k product photos; name and description in 13 languages incl. `hu`; 191 classes; no pairs | Apache-2.0 [G] | Not stated separately; e-shop photos (inferred) | Yes per licence; images unclear | 11 GB (228×298), 54 GB (800×800) | Hungarian text and look search |
| ABO | 147,702 products, 398,212 catalogue photos; mostly household, some apparel [Ap] | CC BY 4.0 [A] | CC BY 4.0, credit Amazon.com [A] | Yes | 3 GB small photos + 83 MB metadata | Photo and title-to-photo sanity checks |
| LookBench (2026) | 4 subsets, 2,363 queries vs ~60k gallery; studio, street, AI-generated | Apache-2.0 card [L] | Real photos from Google Images plus Fashion200K filler [Lp] | Unclear | 2.2 GB | Model comparison (private) |
| Fashionpedia | 48.8k street/model photos; 46 classes, 294 attributes; no pairs | CC BY 4.0 [F] | "does not own the copyright of the images"; source terms apply [F]; Kaggle copy non-commercial [K7] | Per image | 3.5 GB | Category and attribute photo queries |
| Open Images V7 | ~9M scene photos with apparel boxes | CC BY 4.0 [O] | "listed as having a CC BY 2.0 license", not verified by Google [O] | Likely; check per image | by class [FO] | Street-photo queries |
| Fashion-MNIST | 70k 28×28 greyscale | MIT [M] | MIT | Yes | ~30 MB | None (too low resolution) |
| ZooClaw eval | 2,000 text queries, 12k products, answer key | CC BY-NC 4.0 [Z] | same | No | 1.8 GB | Private text-to-photo comparison |
| Fashion Product Images / `Marqo/KAGL` | 44k packshots, attributes, names | MIT tag [KP][MK] | scraped [KP] | Unclear | 15.7 GB (572 MB small) | Private only |
| `Marqo/atlas` | 78k Indian e-commerce packshots | MIT [MA][AT] | crawled from shops [AT] | Unclear | 2.7 GB | Private only |
| Fashion200k | 200k photos plus descriptions | Apache-2.0 [F2] | "crawled from Lyst.com" [F2] | Unclear | 3.5 GB | Private only |
| Polyvore | 68k outfits, 261k items | Apache-2.0 / CC BY 4.0 tags | gated "solely for academic and non-profit research" [PV] | No | 2.5–4.3 GB | Avoid |
| DeepFashion (+ Marqo copies) | 800k photos; in-shop and consumer-to-shop pairs | "non-commercial research purposes only" [DF][DFM] | same | No | inshop 216 MB | Avoid |
| DeepFashion2 | 491,895 photos; 873k pairs | none public; password by form [DF2] | — | No (inferred) | — | Avoid |
| H&M (Kaggle) | article photos, names, descriptions | "non-commercial purposes only" [HM] | same | No | — | Avoid |
| iMaterialist 2018–2020 | ~1M Wish-hosted photos [IM] | research, education, non-commercial [K7] | Wish URLs | No | 71.5 GB | Avoid |
| Fashion IQ | relative captions, 3 categories | CDLA [FIQ] | from an Amazon review dataset [FIQp] | Unclear | — | Avoid |
| ModaNet | Chictopia street photos | CC BY-NC 4.0 [MN] | PaperDoll set | No | 101 MB labels | Avoid |
| Street2Shop | 20,357 street, 404,683 shop, 39,479 matches [S2S] | none found | 25 retailers | Unclear | — | Avoid |
| Marqo-GS-10M | 10M Google Shopping rows | Apache-2.0 card [GS] | not stated | Unclear | 212 GB | Avoid |

Every Marqo card says "We do not own this dataset"; its tags cannot override the
original terms (inferred). Newer 2025–26 sets are not permissive either: FashionMV
builds on DeepFashion, Fashion200K and FashionGen [FMV]; FACap states no dataset
licence [FC].

## Use these, in order

1. **ABO, to sanity-check the pipeline.** Keep apparel product types and map them to
   `taxonomy.CATEGORIES`. Photo search: the query is one product photo; the relevant
   items are that product's other photos. Look search: the query is the English item
   name; the relevant item is that product.
2. **GLAMI-1M rows with `geo == "hu"`, for Hungarian.** Store each item with its
   Hungarian `name` as the title. Category tasks: a short Hungarian class phrase
   (e.g. "női blúz") with every item in that class relevant. Instance tasks: an
   item's name, with items sharing its `image_id` relevant. These exercise text
   search, look search (including `text_match.english_query`) and the category vote.
3. **LookBench, to compare models (private).** `pip install look-bench`. The paper
   reports FashionSigLIP at 62.77 overall fine Recall@1 [Lp]. Wrapping our ONNX
   encoder and landing near that would confirm the 224 squash preprocessing and the
   INT8 loss (inferred). Then compare FashionCLIP, SigLIP2 and
   ZooClaw-FashionSigLIP2.
4. **Marqo `eval.py`** (Apache-2.0 code) on KAGL, atlas and fashion200k, private only.
5. **Fashionpedia (CVDF download, not Kaggle) and Open Images apparel.** Garment
   crops as street-photo queries with category-level relevance; tests cropping and
   the vote. Keep only images whose per-image licence allows reuse (inferred).

## Harness A: the real code path

Copy the `_catalogue()` pattern in `tests/test_image_index.py`:
- In a temporary directory, call `storage.open_database` and
  `storage.upsert_offer(schema.RetailerOffer(...))` with `source_key="reserved"`,
  reserved.com-shaped HTTPS URLs, HUF prices and `utc_now()`, as the tests do.
- Add an in-memory approved `SourcePolicy`, write a manifest, then run
  `image_index.build_image_index(model_path=...)` and
  `ranking_evaluation.evaluate_rankings(...)`.
- `RankingTask` fields: `task_id`, `consent_ref` (e.g. `"public:abo"`), a unique
  `input_ref`, `category`, `query`, a relative `photo_path`, and 1–100 unique
  `relevant_offer_ids`; leave colour, size and price null.
- Limits: at most 100 tasks and one category per run; at most 20,000 photos; each
  photo ≤5 MB and ≤20 MP, JPEG/PNG/WebP. Photo search returns 5 results, so its @10
  metrics are really @5.
- Never point it at `data/`: fake retailer rows must stay out of the shopper
  catalogue.

## Script B: benchmark scale, any model

A proposed `scripts/benchmark_public_retrieval.py`:
- Load models with `visual_encoder.get_encoder()` / `get_text_encoder()`.
- Rank with `1 - M @ q` as `image_index._distances` does; score with
  `ranking_evaluation.ranking_metrics()` plus Recall@1 and Recall@10.
- The 20k-photo and 100-task limits do not apply.
- The encoders accept only the pinned FashionSigLIP and ViT files; other models
  need open_clip inside the script, or a new `OnnxArtifact` in `visual_encoder.py`.

## Fine-tuning

- Full or LoRA fine-tuning is GPU work: ZooClaw trained on 200K–800K+ pairs at an
  effective batch of 1,024, and its best LoRA (r=16 + LwF) scored below unmodified
  FashionSigLIP on long-query R@10 (0.749 vs 0.765) [ZP].
- Small precedent: one community model tuned only the image tower on 13,557 triplets
  on an Apple M-series GPU and gained +2.7 fine R@1 on LookBench [HA].
- CPU only (inferred): a linear adapter is feasible. Embed 5–20k pairs once with the
  ONNX towers, then train a 768×768 map in minutes; it needs a new embedding version
  and an index rebuild. Not worth trying until Harness A shows a gap.
- If you publish, only ABO and arguably GLAMI are licence-safe training data.

## Hungarian queries

- GLAMI-1M is the only set with Hungarian product names (e.g. "Női blúz ONLY") [Gp].
- XM3600 has Hungarian captions under CC BY 4.0, but general photos [XM].
- Inferred: translate ABO or Fashionpedia English phrases with
  `Helsinki-NLP/opus-mt-en-hu` (Apache-2.0) [OP], then compare look search on
  Hungarian vs English queries to measure `text_match.TRANSLATIONS` coverage.
  Hand-check about 50 translations.

## Avoid if you might publish

DeepFashion (including Marqo inshop/multimodal and FashionMV), DeepFashion2, H&M,
iMaterialist (including Fashionpedia's Kaggle copy), ModaNet, Polyvore, ZooClaw,
Fashion IQ, Street2Shop, Fashion200k, KAGL, Atlas, Marqo-GS-10M, and LookBench's
real-photo subsets (inferred). Private measurement is fine; never ship their
photos, their embeddings, or a model trained on them.

## Sources

- [G] https://github.com/glami/glami-1m · https://huggingface.co/datasets/glami/glami-1m · https://zenodo.org/record/7326406
- [Gp] https://arxiv.org/abs/2211.14451
- [A] https://amazon-berkeley-objects.s3.amazonaws.com/index.html
- [Ap] https://arxiv.org/abs/2110.06199
- [L] https://huggingface.co/datasets/srpone/look-bench
- [Lp] https://arxiv.org/html/2601.14706v2
- [F] https://fashionpedia.github.io/home/data_license.html
- [K7] https://www.kaggle.com/competitions/imaterialist-fashion-2020-fgvc7/rules
- [O] https://storage.googleapis.com/openimages/web/factsfigures_v7.html
- [FO] https://docs.voxel51.com/dataset_zoo/datasets/open_images_v7.html
- [M] https://github.com/zalandoresearch/fashion-mnist
- [Z] https://huggingface.co/datasets/srpone/zooclaw-fashion-eval
- [KP] https://www.kaggle.com/datasets/paramaggarwal/fashion-product-images-dataset
- [MK] https://huggingface.co/datasets/Marqo/KAGL
- [MA] https://huggingface.co/datasets/Marqo/atlas
- [AT] https://github.com/vumaasha/Atlas
- [F2] https://github.com/xthan/fashion-200k
- [PV] https://huggingface.co/datasets/mvasil/polyvore-outfits
- [DF] https://mmlab.ie.cuhk.edu.hk/projects/DeepFashion.html
- [DFM] https://github.com/yumingj/DeepFashion-MultiModal
- [DF2] https://github.com/switchablenorms/DeepFashion2
- [HM] https://www.kaggle.com/competitions/h-and-m-personalized-fashion-recommendations/rules
- [IM] https://github.com/visipedia/imat_fashion_comp
- [FIQ] https://github.com/XiaoxiaoGuo/fashion-iq
- [FIQp] https://arxiv.org/abs/1905.12794
- [MN] https://github.com/eBay/modanet
- [S2S] https://openaccess.thecvf.com/content_iccv_2015/html/Kiapour_Where_to_Buy_ICCV_2015_paper.html
- [GS] https://huggingface.co/datasets/Marqo/marqo-GS-10M
- [FMV] https://github.com/yuandaxia2001/FashionMV
- [FC] https://arxiv.org/abs/2507.07135
- [ZP] https://arxiv.org/html/2606.27708
- [HA] https://huggingface.co/HopitAI/moda-fashion-crossdomain
- [XM] https://huggingface.co/datasets/floschne/xm3600
- [OP] https://huggingface.co/Helsinki-NLP/opus-mt-en-hu
