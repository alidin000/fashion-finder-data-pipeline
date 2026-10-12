# Local application setup

The shopper app reads the existing SQLite catalogue. The API publishes **no offers** until a source policy grants that use. The [example policy](../config/source-policies.example.json) is deliberately pending. Do not mark a source approved without permission evidence for the intended use.

## Requirements

- Python 3.12
- Node.js 20.9 or newer (Node 24 was used for the first build)
- A local catalogue populated through the existing `fashion-finder collect`, `ingest`, or `import-feed` commands

From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

For an existing catalogue, stop collector/API processes and back up the SQLite
file before migration. From the repository root:

```bash
cp data/fashion_finder.sqlite data/fashion_finder.sqlite.before-django
.venv/bin/fashion-finder migrate
```

The migration keeps the existing four catalogue tables and rows. It adds
Django's migration and admin tables. For a new installation, omit the copy and
run `fashion-finder migrate` to create the database. To roll back a local
migration, stop the processes and restore the backed-up SQLite file. Take a
fresh backup if new collection runs happened after migration; restoring the old
file discards those newer runs.

For PowerShell on Windows, use `py -3.12 -m venv .venv`, then `.venv\Scripts\python -m pip install -e ".[dev]"`. The [pipeline setup guide](local-setup-windows.md) has collection commands.

On Windows, the migration commands are:

```powershell
Copy-Item data\fashion_finder.sqlite data\fashion_finder.sqlite.before-django
.\.venv\Scripts\fashion-finder.exe migrate
```

Skip `Copy-Item` on a new installation. For the local API, set the same four
environment variables below with `$env:NAME = "value"` and run
`.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000`.

## Source policy

Copy `config/source-policies.example.json` to a local path outside the repository or under ignored `data/`. Add one entry per source and market. Keep the approval evidence itself private; `evidence_ref` is a pointer to it, not the agreement text. The policy parser rejects duplicate entries and any permissions claimed for a pending or revoked source. An absent policy is a denial.

Set `approval_status` to `approved` only after access, publication, deep-link, attribution, and image rights have been checked. Set `allow_public_listing`, `allow_image_display`, and `allow_image_processing` separately. `freshness_hours` controls when an offer disappears from the public API after its last successful check. An HTTP error never refreshes that timestamp or implies out of stock. Edits to the policy file take effect on the next request, including revocation.

## Run the API and web app

Terminal 1, from the repository root:

```bash
export FASHION_FINDER_DB=data/fashion_finder.sqlite
export FASHION_FINDER_SOURCE_POLICIES=/absolute/path/to/your-reviewed-policies.json
export FASHION_FINDER_CORS_ORIGINS=http://localhost:3000
export FASHION_FINDER_SECRET_KEY='set-a-long-random-local-secret'
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Omit `FASHION_FINDER_SOURCE_POLICIES` to test the default-deny empty catalogue. The health endpoint is `http://127.0.0.1:8000/api/v1/health`. Django admin is at `/admin/`; create a local staff account with `.venv/bin/python manage.py createsuperuser`. Admin catalogue records are read-only. Store the secret key outside Git; set `FASHION_FINDER_ALLOWED_HOSTS` and HTTPS settings before any non-local deployment. Django's `runserver` is for local development only.

Terminal 2:

```bash
cd web
npm ci
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Open `http://localhost:3000`. The search form calls the API without crawling retailers. A phrase such as `black jacket under 15000 Ft` fills the HUF budget and filters higher-priced offers. Images are displayed only if the active source policy permits display. Photo upload shows a local preview; image matching returns HTTP 503 until approved image sources, a versioned index, and the 30-task evaluation exist. The server validates uploaded type, bytes, and dimensions and does not retain the upload. Put a request-size cap at the reverse proxy before Internet deployment.

## Measure launch-category coverage

Run the local release-readiness report after choosing a launch category. It counts
stored offers and offers currently eligible for the public API under the supplied
policy and each source's freshness window. The four supported source keys appear
even when they have no offers. The text threshold is two approved sources with
at least 100 eligible offers each. The photo threshold additionally requires
declared display and processing permission and 100 HTTPS image candidates per
source. An image candidate count does not prove image quality or actual rights.

```bash
.venv/bin/fashion-finder readiness --category jackets --policy data/source-policies.json
```

Omit `--policy` to use `FASHION_FINDER_SOURCE_POLICIES`. If neither is set,
the report treats every source as unapproved. The report does not inspect
agreements or make a beta-ready decision. Confirm rights against the private
evidence, audit offers, evaluate real shopping tasks, and run a deployed smoke
test before calling the release gate met.

Create one review CSV per launch source. By default, rows must currently be
eligible for the public API. This keeps the 30-offer audit focused on offers
the shopper could see. The sample is reproducible on the same catalogue and
cannot overwrite an existing review file.

```bash
.venv/bin/fashion-finder audit-sample --category jackets --source reserved \
  --policy data/source-policies.json --count 30 \
  --output data/audits/reserved-jackets.csv
```

Before approval, use `--all-stored` for an **internal diagnostic sample**.
Its `public_eligible` column marks rows that cannot count toward the release
audit. Open each product page and fill in the four verification columns and
notes. A CSV with fewer than 30 eligible rows cannot satisfy the 30-offer
audit gate. Keep the completed review and approval evidence private; `data/audits/`
is ignored by Git.

## Checks

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
cd web
npm run typecheck
npm run lint
npm run test
npm run build
```

The local API filters approved sources, category, active state, HUF, priced offers, and stock in SQLite. It keeps exact Decimal budgets, Unicode text matching, colour/size checks, freshness, sorting, and cursor pages in memory over those candidates. Measure query latency and move the remaining work to a suitable index when scale requires it. The web project currently has no persistent analytics or shopper accounts.
