# Local Docker runbook

This stack runs the Django API, Next.js web app, and a separate timestamped-feed
worker on one computer. Both published ports bind to `127.0.0.1`. It is a local
deployment, not evidence that a public beta or any retailer integration is ready.

## First start

From the repository root, with Docker Compose available:

```bash
cp .env.example .env
python3 -c 'import secrets; print("FASHION_FINDER_SECRET_KEY=" + secrets.token_urlsafe(48))' >> .env
mkdir -p data/feeds
cp config/source-policies.example.json data/source-policies.json
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:8000/api/v1/health
```

Open `http://localhost:3000`. The example source policy is pending, so search
returns no public offers. The `migrate` service completes before API and worker
start. The web server forwards `/api/v1/` to the API over Docker's internal
network, so the image works with different host ports. Keep `.env` and
`data/source-policies.json` private.
On Linux or WSL, set `FASHION_FINDER_UID` and `FASHION_FINDER_GID` in `.env` to
the output of `id -u` and `id -g` so the containers can write to `data/`.

The API image serves Django admin static files through WhiteNoise. Gunicorn
serves the API; Next.js serves the web app. The worker checks local feed files
each hour. No browser scraping runs inside the stack.

## Approved feed refresh

Only `data/feeds/<source>.csv` for `zara`, `hm`, `reserved`, `pullandbear`,
`aboutyou`, or `answear` is watched. ABOUT YOU and Answear are feed-only
candidates; [onboarding details](hungarian-source-candidates.md) cover the
required publisher access and CSV mapping. The source policy must be `approved`, include a private `evidence_ref`,
and set `access_method` to `feed-csv`. Approval in a JSON file is a declaration;
the software cannot verify the agreement. Public listing and each image use
must be granted separately.

The scheduled feed requires `product_url,name,price,currency,observed_at`.
`observed_at` is the source observation time in ISO 8601 with a timezone, for
example `2026-10-13T09:00:00+00:00`. Optional columns remain `sku,brand,category,
description,original_price,image_url,colour,size,availability`. A feed without
the timestamp, with future observations, or over 20 MB is rejected. Deliver a
complete new file with an atomic rename, for example write
`data/feeds/reserved.csv.tmp` and rename it to `reserved.csv` after closing it.
The worker imports changed file content once and does not refresh timestamps
from an unchanged file. A partial import stays visible as a failed run for
review; fix the feed and replace the file. Collector errors do not mark stock
as sold out.

Before delivery, run `fashion-finder validate-feed <source> <path>` on the
mapped CSV. This dry run requires timestamps, checks every row, and stores
nothing. Approval and image rights still require separate human evidence.

```bash
docker compose logs --tail=50 worker
docker compose exec worker fashion-finder-worker --data-dir /data --once
docker compose exec api fashion-finder --data-dir /data readiness --category jackets --policy /data/source-policies.json
```

For ongoing refreshes, let the worker process the next hourly interval. The
`--once` command is useful for a local check; avoid running two imports for the
same feed at once.

## Checks and monitoring

```bash
docker compose ps
docker compose logs --tail=100 api worker
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS 'http://127.0.0.1:8000/api/v1/search?category=jackets'
docker compose exec api fashion-finder --data-dir /data quality --stale-after-days 7
docker compose exec api fashion-finder --data-dir /data recheck --limit 20
```

After **an approved feed** is supplied, check one import report, one API
search, one browser search/upload flow, and one live Hungarian retailer link
from the environment that runs the stack. Record the command, location, and
observed result. A synthetic local offer does not satisfy that live smoke test.

For the optional pretrained photo-search experiment, follow the pinned model
download and checksum instructions in the [image-index guide](local-image-index.md).
Set `FASHION_FINDER_INSTALL_IMAGE_MODEL=1` and the in-container
`FASHION_FINDER_IMAGE_MODEL_PATH=/data/models/fashion-siglip-vision-int8.onnx`
(Marqo-FashionSigLIP, the recommended encoder; the ViT file also works) before
rebuilding. For **Match by look**, also set `FASHION_FINDER_TEXT_MODEL_PATH` and
`FASHION_FINDER_TOKENIZER_PATH` to the matching text model and tokenizer. The same
model path must be used to create the image index.
Keep the experiment flag at `0` until the index, source policy, and task labels
are ready.

To collect low-cardinality analytics, set `FASHION_FINDER_ANALYTICS_ENABLED=1`
in `.env` and recreate the services. The browser then sends only search type,
filter use, result count (stored as a bucket), source of a retailer click, and coarse error
code. It sends no query text, uploaded bytes, IP address, user agent, or
persistent visitor ID in the event body. Django stores daily counters, not
individual events. The worker removes counters older than 30 calendar days.
Use `docker compose exec api fashion-finder --data-dir /data analytics-report`
to inspect the last seven days. Keep analytics disabled until the local privacy
notice and participant process are reviewed. Server or proxy access logs must
also avoid full query strings if this stack is later exposed beyond localhost.

## Backup, restore, and rollback

Stop the processes before copying the SQLite database, so the backup includes
committed catalogue and metric rows:

```bash
docker compose stop api worker
mkdir -p backups
cp data/fashion_finder.sqlite backups/fashion_finder-$(date +%Y%m%d-%H%M%S).sqlite
docker compose start api worker
```

Store backups privately outside Git. Rehearse restore on a **copy** of `data/`
before relying on it. Restoring over the active database discards newer offers,
run reports, and metric counters; stop the stack and get an explicit decision
before doing that. A code rollback uses the previous Git revision and a rebuild.
If its schema is older, restore the matching database backup rather than
guessing a reverse migration. `docker compose down` stops containers but keeps
the bind-mounted `data/` directory.

## Release evidence still required

Local container health, CI, and synthetic tests do not establish source rights
or shopper usefulness. The first private beta still needs two approved sources
with 100 current offers each in the selected category, 30 manual offer reviews
per source, 30 consented shopping tasks with at least 24 successful, photo
relevance evidence, and a deployed live source-to-browser smoke test. Do not
expose this local stack publicly as a substitute for those gates.
