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
python -m playwright install chromium
```

If PowerShell blocks activation, invoke the virtual environment's Python directly rather than changing the machine-wide execution policy.

## Planned commands

The intended interface (not implemented yet):

```powershell
fashion-finder sources list
fashion-finder collect zara --market HU --category jackets --limit 25
fashion-finder normalize data/raw/zara/<run-id>.jsonl
fashion-finder report <run-id>
```

Do not run these commands until the corresponding CLI and collectors are implemented.

## Local outputs
- data/raw/ — ignored by Git; raw collection runs
- data/exports/ — ignored by Git; generated exports
- data/fashion_finder.sqlite — ignored by Git; local database

Only small synthetic fixtures should be committed under tests/fixtures/. Do not commit large live catalogues or API credentials.
