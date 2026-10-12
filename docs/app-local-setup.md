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

For PowerShell on Windows, use `py -3.12 -m venv .venv`, then `.venv\Scripts\python -m pip install -e ".[dev]"`. The [pipeline setup guide](local-setup-windows.md) has collection commands.

## Source policy

Copy `config/source-policies.example.json` to a local path outside the repository or under ignored `data/`. Add one entry per source and market. Keep the approval evidence itself private; `evidence_ref` is a pointer to it, not the agreement text. The policy parser rejects duplicate entries and any permissions claimed for a pending or revoked source. An absent policy is a denial.

Set `approval_status` to `approved` only after access, publication, deep-link, attribution, and image rights have been checked. Set `allow_public_listing`, `allow_image_display`, and `allow_image_processing` separately. `freshness_hours` controls when an offer disappears from the public API after its last successful check. An HTTP error never refreshes that timestamp or implies out of stock. Edits to the policy file take effect on the next request, including revocation.

## Run the API and web app

Terminal 1, from the repository root:

```bash
export FASHION_FINDER_DB=data/fashion_finder.sqlite
export FASHION_FINDER_SOURCE_POLICIES=/absolute/path/to/your-reviewed-policies.json
export FASHION_FINDER_CORS_ORIGINS=http://localhost:3000
.venv/bin/uvicorn fashion_finder_data.api.app:app --host 127.0.0.1 --port 8000
```

Omit `FASHION_FINDER_SOURCE_POLICIES` to test the default-deny empty catalogue. The health endpoint is `http://127.0.0.1:8000/api/v1/health`; OpenAPI is at `/docs`.

Terminal 2:

```bash
cd web
npm ci
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Open `http://localhost:3000`. The search form calls the API without crawling retailers. A phrase such as `black jacket under 15000 Ft` fills the HUF budget and filters higher-priced offers. Images are displayed only if the active source policy permits display. Photo upload shows a local preview; image matching returns HTTP 503 until approved image sources, a versioned index, and the 30-task evaluation exist. The server validates uploaded type, bytes, and dimensions and does not retain the upload. Put a request-size cap at the reverse proxy before Internet deployment.

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

The local API uses SQLite and scans eligible rows in memory for search. That is suitable for the initial bounded catalogue, not a large production catalogue. Measure query latency and add database-side filtering/pagination when scale requires it. The web project currently has no persistent analytics or accounts.
