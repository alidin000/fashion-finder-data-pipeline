"""Licence-safe practice catalogue built from public fashion datasets.

Practice rows let the real API, shopper page and benchmarks run before any
retailer data exists. They use the ``practice`` source key and the reserved
``practice.invalid`` host, carry synthetic HUF prices, and must never share a
data directory with a real catalogue. Dataset photos stay outside the repository.
"""

SOURCE_KEY = "practice"
HOST = "practice.invalid"
# Display-only photos (GLAMI) live under this prefix; the practice API route serves them.
LOCAL_IMAGE_PREFIX = f"https://{HOST}/images/"
CATALOGUE_FILE = "catalogue.jsonl"
