"""Product text sidecar, its version checks, and look-search blending and hints."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client
from PIL import Image

from fashion_finder_data import image_index
from fashion_finder_data.api.views import api_context
from fashion_finder_data.cli import main
from fashion_finder_data.image_index import build_image_index, search_indexed_text
from fashion_finder_data.product_text import PRODUCT_TEXT_FILE, describe
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer
from fashion_finder_data.visual_encoder import FASHION_SIGLIP_SPACE, canonical_text

POLICY = SourcePolicy(
    source_key="reserved",
    approval_status="approved",
    evidence_ref="local test fixture only",
    allow_public_listing=True,
    allow_image_display=True,
    allow_image_processing=True,
)
POLICIES = {("reserved", "HU"): POLICY}
AXES = {"red": 0, "blue": 1, "green": 2}


def unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class Photo:
    """Stand-in vision tower: every photo is grey, so photos alone cannot decide."""

    version = "stand-in-photo-v1"
    length = 3
    cosine = True
    space = FASHION_SIGLIP_SPACE

    def encode(self, image: Image.Image) -> list[float]:
        return unit([1.0, 1.0, 1.0])

    def distance(self, query, indexed):
        return 1.0 - sum(a * b for a, b in zip(query, indexed))


class Words:
    """Stand-in text tower: red, blue and green words are axes; the rest is grey."""

    space = FASHION_SIGLIP_SPACE
    length = 3

    def __init__(self, version: str = "stand-in-text-v1") -> None:
        self.version = version
        self.seen: list[str] = []

    def encode(self, text: str) -> list[float]:
        self.seen.append(text)
        vector = [0.05, 0.05, 0.05]
        for word in canonical_text(text).split():
            if word in AXES:
                vector[AXES[word]] += 1.0
        return unit(vector)


@pytest.fixture
def shop(tmp_path, monkeypatch):
    text = Words()
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: Photo())
    monkeypatch.setattr(image_index, "get_text_encoder", lambda model, tokenizer: text)
    database = open_database(tmp_path / "fashion_finder.sqlite")
    manifest = []
    for sku, title, category, colour, price in (
        ("dress", "Női piros ruha", "dresses", "piros", 9995),
        ("coat", "Kék gyapjúkabát", "coats", "kék", 29995),
        ("bag", "Zöld táska", "bags", "zöld", 12995),
    ):
        url = f"https://www.reserved.com/hu/hu/{sku}-123ab-45c"
        image_url = f"https://static.reserved.com/{sku}.png"
        upsert_offer(
            database,
            RetailerOffer(
                source_key="reserved",
                source_url=url,
                canonical_url=url,
                source_product_id=sku,
                title=title,
                category=category,
                colour_names=[colour],
                price_amount=Decimal(price),
                currency="HUF",
                image_urls=[image_url],
                source_fetched_at=utc_now(),
            ),
        )
        Image.new("RGB", (8, 8), (128, 128, 128)).save(tmp_path / f"{sku}.png")
        offer_id = next(
            row["id"] for row in offer_rows(database) if row["source_product_id"] == sku
        )
        manifest.append({"offer_id": offer_id, "image_url": image_url, "image_path": f"{sku}.png"})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    build_image_index(database, POLICIES, path, model_path=Path("m"))
    return database, text


def paths() -> dict[str, Path]:
    return {"model_path": Path("m"), "text_model_path": Path("t"), "tokenizer_path": Path("k")}


def ids(result: dict) -> list[str]:
    return [item["source_product_id"] for item in result["items"]]


def build_sidecar(database) -> dict:
    from fashion_finder_data.product_text import build_product_text_index

    return build_product_text_index(database, POLICIES, **paths())


def test_descriptions_are_short_english_from_structured_fields() -> None:
    row = {
        "title": "Női steppelt kapucnis kabát XYZ",
        "category": "coats",
        "colour_names": json.dumps(["fekete"]),
        "source_category": "Kabátok",
        "description": "Pamut anyagú, csíkos bélés",
    }
    # Gender, colour, title words, then fabric and pattern; the unknown word is left out.
    assert describe(row) == "women's black quilted hooded coat cotton striped"
    assert describe({**row, "title": "XYZ 123", "colour_names": "[]"}) == "cotton striped coat"


def test_product_text_breaks_ties_between_ambiguous_photos(shop) -> None:
    database, text = shop
    report = build_sidecar(database)
    assert report["described_offers"] == 3
    assert "women's red dress" in text.seen
    # Grey photos: by look alone the order is arbitrary (offer id).
    plain = search_indexed_text(
        database, POLICIES, "blue", blend=False, product_text_weight=0, **paths()
    )
    assert ids(plain)[0] == "dress"
    blended = search_indexed_text(
        database, POLICIES, "blue", blend=False, product_text_weight=0.5, **paths()
    )
    assert ids(blended)[0] == "coat"
    # The default weight (0.5) uses a compatible sidecar automatically.
    assert ids(search_indexed_text(database, POLICIES, "green", blend=False, **paths()))[0] == "bag"


def test_sidecar_from_another_model_is_refused_not_mixed(shop, monkeypatch) -> None:
    database, _ = shop
    build_sidecar(database)
    newer = Words(version="stand-in-text-v2")
    monkeypatch.setattr(image_index, "get_text_encoder", lambda model, tokenizer: newer)
    with pytest.raises(ValueError, match="incompatible; rebuild"):
        search_indexed_text(database, POLICIES, "blue", product_text_weight=0.5, **paths())
    # Shoppers still get photo results when the weight is left to the default.
    assert len(search_indexed_text(database, POLICIES, "blue", **paths())["items"]) == 3
    (database.path.parent / PRODUCT_TEXT_FILE).unlink()
    with pytest.raises(ValueError, match="needs a product text index"):
        search_indexed_text(database, POLICIES, "blue", product_text_weight=0.5, **paths())


def test_a_changed_offer_loses_its_stale_description(shop) -> None:
    database, _ = shop
    build_sidecar(database)
    url = "https://www.reserved.com/hu/hu/coat-123ab-45c"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            source_product_id="coat",
            title="Szürke kabát",  # no longer blue
            category="coats",
            colour_names=["szürke"],
            price_amount=Decimal(29995),
            currency="HUF",
            image_urls=["https://static.reserved.com/coat.png"],
            source_fetched_at=utc_now(),
        ),
    )
    result = search_indexed_text(
        database, POLICIES, "blue", blend=False, product_text_weight=1.0, **paths()
    )
    assert ids(result)[0] == "dress"  # the stale "blue coat" text no longer counts


def test_look_search_reads_hungarian_prices_and_hints(shop) -> None:
    database, text = shop
    result = search_indexed_text(database, POLICIES, "kék kabátot 20000 Ft alatt", **paths())
    # The price phrase is a limit, not words for the model; the coat costs 29,995 Ft.
    assert result["interpreted_query"] == "blue coat"
    assert text.seen[-1] == "a photo of blue coat"  # the template wraps only the embedding
    assert result["interpretation"]["max_price"] == "20000"
    assert "coat" not in ids(result)
    # Category and colour hints lift the matching offer over identical photos.
    hinted = search_indexed_text(database, POLICIES, "zöld táska", **paths())
    assert ids(hinted)[0] == "bag"
    assert ids(search_indexed_text(database, POLICIES, "táska", hints="filter", **paths())) == [
        "bag"
    ]
    over = search_indexed_text(database, POLICIES, "ruha 10000 Ft felett", **paths())
    assert "dress" not in ids(over)  # 9,995 Ft is below the typed minimum
    with pytest.raises(ValueError, match="describe"):
        search_indexed_text(database, POLICIES, "10000 Ft alatt", **paths())


def test_look_route_returns_the_interpreted_query(shop, tmp_path, monkeypatch) -> None:
    database, _ = shop
    policy_path = tmp_path / "policies.json"
    policy_path.write_text(f"[{POLICY.model_dump_json()}]", encoding="utf-8")
    for key, value in {
        "FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT": "1",
        "FASHION_FINDER_IMAGE_MODEL_PATH": "m",
        "FASHION_FINDER_TEXT_MODEL_PATH": "t",
        "FASHION_FINDER_TOKENIZER_PATH": "k",
        # A translation folder without the pinned files leaves the dictionary in charge.
        "FASHION_FINDER_TRANSLATION_MODEL_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    with api_context(database, policy_path):
        response = Client().get("/api/v1/search/look", {"q": "piros női ruhát"})
    assert response.status_code == 200
    body = response.json()
    assert body["interpreted_query"] == "red women's dress"
    assert body["interpretation"]["translated_by"] == "dictionary"
    assert body["interpretation"]["gender"] == "women"
    assert body["items"][0]["source_product_id"] == "dress"


def test_cli_builds_the_product_text_sidecar(shop, tmp_path, capsys) -> None:
    database, _ = shop
    policy_path = tmp_path / "policies.json"
    policy_path.write_text(f"[{POLICY.model_dump_json()}]", encoding="utf-8")
    argv = ["--data-dir", str(database.path.parent), "index-product-text"]
    argv += ["--policy", str(policy_path), "--model-path", "m"]
    argv += ["--text-model-path", "t", "--tokenizer-path", "k"]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["described_offers"] == 3
    assert (database.path.parent / PRODUCT_TEXT_FILE).is_file()
