"""Text-to-photo search and the category vote, with stand-in encoders."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from django.test import Client
from PIL import Image

from fashion_finder_data import image_index
from fashion_finder_data.api.views import api_context
from fashion_finder_data.image_index import (
    _rank,
    build_image_index,
    pack_features,
    search_indexed_image,
    search_indexed_text,
    unpack_features,
)
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer
from fashion_finder_data.text_match import english_query
from fashion_finder_data.visual_encoder import (
    COLOUR_LAYOUT,
    FASHION_SIGLIP_SPACE,
    canonical_text,
    get_text_encoder,
)

COLOURS = {"red": (230, 20, 20), "green": (20, 200, 40), "blue": (20, 40, 220)}
POLICY = SourcePolicy(
    source_key="reserved",
    approval_status="approved",
    evidence_ref="local test fixture only",
    allow_public_listing=True,
    allow_image_display=True,
    allow_image_processing=True,
)


def unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class MeanColour:
    """Stand-in for FashionSigLIP's vision tower: a photo becomes its mean colour."""

    version = "stand-in-fashion-siglip-v1"
    length = 3
    cosine = True
    space = FASHION_SIGLIP_SPACE

    def encode(self, image: Image.Image) -> list[float]:
        mean = image.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        return unit([float(channel) for channel in mean])

    def distance(self, query, indexed):
        return 1.0 - sum(a * b for a, b in zip(query, indexed))


class ColourWords:
    """Stand-in for the text tower: a colour word maps to that colour."""

    space = FASHION_SIGLIP_SPACE
    length = 3

    def encode(self, text: str) -> list[float]:
        colour = COLOURS.get(canonical_text(text).split()[0], COLOURS["red"])
        return unit([float(channel) for channel in colour])


def stand_in_text_encoder(model_path, tokenizer_path) -> ColourWords:
    if model_path is None or tokenizer_path is None:
        raise ValueError("text-to-photo search needs a text model and tokenizer path")
    return ColourWords()


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: MeanColour())
    monkeypatch.setattr(image_index, "get_text_encoder", stand_in_text_encoder)
    database = open_database(tmp_path / "fashion_finder.sqlite")
    manifest = []
    for name, category in (("red", "dresses"), ("green", "jackets"), ("blue", "bags")):
        url = f"https://www.reserved.com/hu/hu/{name}-item-123ab-45c"
        image_url = f"https://static.reserved.com/{name}.png"
        upsert_offer(
            database,
            RetailerOffer(
                source_key="reserved",
                source_url=url,
                canonical_url=url,
                source_product_id=name,
                title=f"Sample {name}",
                category=category,
                price_amount=Decimal(9995),
                currency="HUF",
                image_urls=[image_url],
                source_fetched_at=utc_now(),
            ),
        )
        Image.new("RGB", (8, 8), COLOURS[name]).save(tmp_path / f"{name}.png")
        offer_id = next(
            row["id"] for row in offer_rows(database) if row["source_product_id"] == name
        )
        manifest.append({"offer_id": offer_id, "image_url": image_url, "image_path": f"{name}.png"})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    build_image_index(database, {("reserved", "HU"): POLICY}, path, model_path=Path("model"))
    return database


def paths() -> dict[str, Path]:
    return {"model_path": Path("m"), "text_model_path": Path("t"), "tokenizer_path": Path("k")}


def test_description_ranks_indexed_photos_by_look(catalogue) -> None:
    policies = {("reserved", "HU"): POLICY}
    ranked = search_indexed_text(catalogue, policies, "Blue!", **paths())
    assert ranked["items"][0]["source_product_id"] == "blue"
    filtered = search_indexed_text(catalogue, policies, "blue", category="dresses", **paths())
    assert [item["source_product_id"] for item in filtered["items"]] == ["red"]
    with pytest.raises(ValueError, match="describe"):
        search_indexed_text(catalogue, policies, "  ", **paths())


def test_look_search_blends_word_relevance_and_translates_hungarian(catalogue) -> None:
    policies = {("reserved", "HU"): POLICY}
    # The stand-in model sees "jacket" as red, so by look alone the red dress wins;
    # the green offer is in the jackets category, so fusion puts it first.
    blended = search_indexed_text(catalogue, policies, "jacket", **paths())
    assert blended["items"][0]["source_product_id"] == "green"
    visual = search_indexed_text(catalogue, policies, "jacket", blend=False, **paths())
    assert visual["items"][0]["source_product_id"] == "red"
    # Hungarian colour words reach the English-only model translated.
    assert (
        search_indexed_text(catalogue, policies, "kék", **paths())["items"][0]["source_product_id"]
        == "blue"
    )
    assert english_query("Fekete kabátok") == "black coat"


def test_text_search_refuses_an_index_from_another_model(catalogue, monkeypatch) -> None:
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: COLOUR_LAYOUT)
    with pytest.raises(ValueError, match="Marqo-FashionSigLIP"):
        search_indexed_text(catalogue, {("reserved", "HU"): POLICY}, "red", **paths())


def test_look_route_reports_and_serves_text_to_photo_search(
    catalogue, tmp_path, monkeypatch
) -> None:
    policy_path = tmp_path / "policies.json"
    policy_path.write_text(f"[{POLICY.model_dump_json()}]", encoding="utf-8")
    monkeypatch.setenv("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT", "1")
    monkeypatch.setenv("FASHION_FINDER_IMAGE_MODEL_PATH", "m")
    with api_context(catalogue, policy_path):
        client = Client()
        assert client.get("/api/v1/health").json()["text_to_image"] == "unavailable"
        monkeypatch.setenv("FASHION_FINDER_TEXT_MODEL_PATH", "t")
        monkeypatch.setenv("FASHION_FINDER_TOKENIZER_PATH", "k")
        assert client.get("/api/v1/health").json()["text_to_image"] == "available"
        response = client.get("/api/v1/search/look", {"q": "green jacket"})
        assert response.status_code == 200
        assert response.json()["items"][0]["source_product_id"] == "green"
        assert client.get("/api/v1/search/look", {"q": ""}).status_code == 400


def offer(offer_id: int, category: str) -> dict:
    return {"id": offer_id, "category": category}


def test_category_vote_lists_the_majority_category_first() -> None:
    query = [1.0, 0.0, 0.0]
    # The closest photo is a bag; dresses hold the weighted majority of the rest.
    layout = ["bags", "dresses", "dresses", "shoes", "dresses", "dresses", "dresses"]
    candidates = [
        (
            {"features": unpack_features(pack_features(unit([1 - step / 100, step / 100, 0])))},
            offer(step + 1, category),
        )
        for step, category in enumerate(layout)
    ]
    voted = _rank(MeanColour(), query, candidates, consensus=True)
    assert voted["preferred_category"] == "dresses"
    # The nearest match stays first; dresses follow; the shoe drops out of the top 5.
    assert [item["category"] for item in voted["items"]] == ["bags"] + ["dresses"] * 4
    plain = _rank(MeanColour(), query, candidates, consensus=False)
    assert [item["category"] for item in plain["items"]] == layout[:5]
    assert "preferred_category" not in plain

    # Four different categories: weights 1, 1/2, 1/3, 1/4 leave no majority (1 < 2.08 / 2).
    split = [
        (entry, offer(item["id"], category))
        for (entry, item), category in zip(
            candidates, ["bags", "dresses", "shoes", "coats"], strict=False
        )
    ]
    assert "preferred_category" not in _rank(MeanColour(), query, split, consensus=True)


def test_photo_search_keeps_its_nearest_match_with_the_vote(catalogue) -> None:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), COLOURS["red"]).save(buffer, format="PNG")
    with Image.open(buffer) as photo:
        result = search_indexed_image(
            catalogue, {("reserved", "HU"): POLICY}, photo, model_path=Path("m")
        )
    assert result["items"][0]["source_product_id"] == "red"


def test_text_model_files_must_be_the_pinned_ones(tmp_path) -> None:
    model = tmp_path / "text_model.onnx"
    tokenizer = tmp_path / "tokenizer.json"
    model.write_bytes(b"not a model")
    tokenizer.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="text model does not match"):
        get_text_encoder(model, tokenizer)
    with pytest.raises(ValueError, match="needs a text model"):
        get_text_encoder(model, None)


def test_query_text_is_cleaned_as_the_model_was_trained() -> None:
    assert canonical_text("  Black  Puffer_Jacket &amp; Bag!! ") == "black puffer jacket bag"
