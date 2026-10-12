# Local Setup — Windows

## Prerequisites
- Python 3.12
- Git
- PowerShell
- Network access for package installation and explicitly invoked test runs

## Environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
# Optional: only if investigating a page that needs browser rendering
python -m playwright install chromium
```

If PowerShell blocks activation, invoke the virtual environment's Python directly rather than changing the machine-wide execution policy.

## Commands

```powershell
fashion-finder sources list
fashion-finder collect reserved --limit 1
fashion-finder collect all --limit 1
fashion-finder ingest reserved data/raw/reserved/<run-id>.jsonl
fashion-finder report <run-id>
fashion-finder export data/exports/catalogue.csv
```

The command currently targets Hungarian storefronts only. A failed source is recorded in its own report.

## Local outputs
- data/raw/ — ignored by Git; raw collection runs
- data/exports/ — ignored by Git; generated exports
- data/fashion_finder.sqlite — ignored by Git; local database

Only small synthetic fixtures should be committed under tests/fixtures/. Do not commit large live catalogues or API credentials.
