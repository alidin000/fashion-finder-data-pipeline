"""Photo search: garment focus, zero-shot reading, variants and the response contract.

The encoders are small stand-ins: a photo becomes its mean colour, and each prompt
becomes the colour of the thing it names. A red jacket is red, trousers are blue and
a person (or a busy background) is green.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image, ImageDraw

from fashion_finder_data import image_index, query_understanding
from fashion_finder_data.api.views import api_context
from fashion_finder_data.image_index import (
    INDEX_FILE,
    _nearest_eligible,
    _rank,
    build_image_index,
    consensus_category,
    pack_features,
    search_indexed_image,
    unpack_features,
)
from fashion_finder_data.query_understanding import COLOURS, Box, understand_photo
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer
from fashion_finder_data.taxonomy import CATEGORIES
from fashion_finder_data.text_match import TRANSLATIONS
from fashion_finder_data.visual_encoder import (
    FASHION_SIGLIP_SPACE,
    OnnxImageEncoder,
    preprocessing_suffix,
    split_feature_version,
    square_image,
)

RED, GREEN, BLUE = (220, 20, 20), (30, 160, 40), (20, 40, 220)
COLOUR_WORDS = {"red": (1, 0, 0, 0), "green": (0, 1, 0, 0), "blue": (0, 0, 1, 0)}
POLICY = SourcePolicy(
    source_key="reserved",
    approval_status="approved",
    evidence_ref="local test fixture only",
    allow_public_listing=True,
    allow_image_display=True,
    allow_image_processing=True,
)
POLICIES = {("reserved", "HU"): POLICY}


def unit(values) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class ColourVision:
    """Vision stand-in with the ONNX encoder's batch API; counts photos embedded."""

    length = 4
    cosine = True
    space = FASHION_SIGLIP_SPACE

    def __init__(self, version: str = "stand-in-vision-v1", flip_tta: bool = False):
        self.base_version = "stand-in-vision-v1"
        self.version = version
        self.flip_tta = flip_tta
        self.embedded = 0

    def with_options(self, *, aspect: str = "squash", flip_tta: bool = False):
        return ColourVision(self.base_version + preprocessing_suffix(aspect, flip_tta), flip_tta)

    def _vector(self, photo: Image.Image) -> list[float]:
        red, green, blue = (
            photo.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        )
        return unit([red / 255, green / 255, blue / 255, 0.1])

    def embed(self, photos):
        import numpy as np

        self.embedded += len(photos)
        return np.asarray([self._vector(photo) for photo in photos])

    def query_vector(self, rgb, embedded=None):
        return embedded if embedded is not None else self.embed([rgb])[0]

    def encode(self, image: Image.Image) -> list[float]:
        return self._vector(image)

    def distance(self, query, indexed):
        return 1.0 - sum(a * b for a, b in zip(query, indexed))


class PromptColours:
    """Text stand-in: jackets are red, trousers blue, people green, the rest grey-ish."""

    space = FASHION_SIGLIP_SPACE
    length = 4

    def encode(self, text: str) -> list[float]:
        words = text.lower().split()
        if "person" in words:
            base = [0.0, 1.0, 0.0, 0.0]
        elif "jacket" in words:
            base = [1.0, 0.0, 0.0, 0.0]
        elif "trousers" in words or "pants" in words:
            base = [0.0, 0.0, 1.0, 0.0]
        else:
            base = [0.1, 0.1, 0.1, 1.0]
        # Red, green and blue are themselves; every other colour word is grey-ish.
        colour = next(
            (COLOUR_WORDS.get(word, (0, 0, 0, 1)) for word in words if word in COLOURS), None
        )
        # A colour word dominates its prompt: "a photo of a red jacket" is mostly red.
        return unit([0.3 * a + b for a, b in zip(base, colour)] if colour else base)


JACKET = Box(0.1, 0.05, 0.8, 0.35)
TROUSERS = Box(0.3, 0.62, 0.4, 0.33)


def outfit_photo(*, trousers: bool = True) -> Image.Image:
    """A red jacket (and blue trousers) on a large green street-like background."""
    photo = Image.new("RGB", (400, 600), GREEN)
    draw = ImageDraw.Draw(photo)
    draw.rectangle(JACKET.pixels(400, 600), fill=RED)
    if trousers:
        draw.rectangle(TROUSERS.pixels(400, 600), fill=BLUE)
    return photo


def as_upload(photo: Image.Image) -> bytes:
    buffer = BytesIO()
    photo.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def fresh_prompt_cache():
    query_understanding._category_bank.cache_clear()
    query_understanding._colour_bank.cache_clear()
    yield
    query_understanding._category_bank.cache_clear()
    query_understanding._colour_bank.cache_clear()


def test_auto_crop_focuses_on_the_garment_and_offers_the_other_one() -> None:
    understood = understand_photo(ColourVision(), PromptColours(), outfit_photo()).as_json()
    assert understood["crop_source"] == "auto"
    crop = Box(**understood["crop"])
    # The chosen region holds the jacket and leaves most of the background out.
    assert crop.area <= 0.5
    assert JACKET.overlap(crop) > 0.5
    assert crop.y <= JACKET.y + 0.06 and crop.y + crop.height >= JACKET.y + JACKET.height
    assert understood["category"]["value"] == "jackets"
    assert understood["category"]["confidence"] >= query_understanding.CATEGORY_CONFIDENCE
    assert understood["colour"]["value"] == "red"
    # The trousers are offered as another garment, in a region below the jacket.
    assert [item["category"] for item in understood["alternatives"]] == ["trousers"]
    trousers = Box(**understood["alternatives"][0]["crop"])
    assert trousers.y >= 0.4 and trousers.overlap(TROUSERS) > 0.2


def test_shopper_crop_and_missing_text_model_turn_auto_crop_off() -> None:
    vision = ColourVision()
    manual = understand_photo(vision, PromptColours(), outfit_photo(), manual_crop=True)
    assert manual.as_json()["crop_source"] == "manual"
    assert manual.crop is None and manual.alternatives == []
    assert vision.embedded == 1  # the shopper's crop is embedded as sent, nothing else
    # Without a text model nothing is claimed about the photo.
    plain = understand_photo(ColourVision(), None, outfit_photo()).as_json()
    assert plain == {
        "crop_source": "none",
        "crop": None,
        "category": None,
        "colour": None,
        "alternatives": [],
    }


def test_photo_close_to_a_catalogue_photo_is_searched_whole() -> None:
    vision = ColourVision()
    understood = understand_photo(
        vision, PromptColours(), outfit_photo(), nearest=lambda vector: 0.97
    )
    assert understood.crop_source == "none" and understood.crop is None
    assert vision.embedded == 1  # no regions were tried


def test_colour_vocabulary_matches_hungarian_offer_colours() -> None:
    assert set(query_understanding.CATEGORY_PHRASES) == CATEGORIES
    # Every predicted colour must also find "fekete", "piros" and so on in offers.
    assert set(COLOURS) <= set(TRANSLATIONS)


def test_preprocessing_choice_is_part_of_the_feature_version() -> None:
    encoder = OnnxImageEncoder.__new__(OnnxImageEncoder)  # no model file needed
    encoder.base_version = encoder.version = "model-v1:abc"
    assert encoder.with_options() is encoder  # the published squash keeps old indexes valid
    variants = {
        encoder.with_options(aspect=aspect, flip_tta=flip).version
        for aspect in ("squash", "pad", "center-crop")
        for flip in (False, True)
    }
    assert len(variants) == 6
    assert split_feature_version("model-v1:abc+aspect-pad+flip-tta") == (
        "model-v1:abc",
        "pad",
        True,
    )
    with pytest.raises(ValueError, match="unknown preprocessing"):
        split_feature_version("model-v1:abc+aspect-squash")
    with pytest.raises(ValueError, match="aspect must be one of"):
        preprocessing_suffix("stretch")


def test_pad_keeps_a_tall_garment_whole_with_its_own_background() -> None:
    tall = Image.new("RGB", (100, 300), (255, 255, 255))
    ImageDraw.Draw(tall).rectangle((20, 10, 80, 290), fill=RED)
    padded = square_image(tall, "pad")
    assert padded.size == (224, 224)
    assert padded.getpixel((112, 4)) == RED or padded.getpixel((112, 10)) == RED  # top kept
    assert padded.getpixel((5, 112)) == (255, 255, 255)  # side bars use the white border
    cropped = square_image(tall, "center-crop")
    assert cropped.getpixel((112, 112)) == RED and cropped.size == (224, 224)


def add_offer(database, sku: str, *, title: str, colour: str, price: int = 9995, source="reserved"):
    url = f"https://www.reserved.com/hu/hu/{sku}-123ab-45c"
    image_url = f"https://static.reserved.com/{sku}.png"
    upsert_offer(
        database,
        RetailerOffer(
            source_key=source,
            source_url=url,
            canonical_url=url,
            source_product_id=sku,
            title=title,
            category="jackets",
            price_amount=Decimal(price),
            currency="HUF",
            image_urls=[image_url],
            colour_names=[colour],
            source_fetched_at=utc_now(),
        ),
    )
    offer_id = next(row["id"] for row in offer_rows(database) if row["source_product_id"] == sku)
    return {"offer_id": offer_id, "image_url": image_url, "image_path": f"{sku}.png"}


@pytest.fixture
def jackets(tmp_path, monkeypatch):
    """Red jackets in an index built with flip TTA by the stand-in encoder."""
    vision = ColourVision()
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: vision)
    monkeypatch.setattr(image_index, "get_text_encoder", lambda model, tokenizer: PromptColours())
    database = open_database(tmp_path / "fashion_finder.sqlite")
    manifest = []
    for sku, colour, shade in (("one", "Piros", RED), ("two", "Bordó", (200, 30, 40))):
        manifest.append(add_offer(database, sku, title=f"Bőrdzseki {sku}", colour=colour))
        Image.new("RGB", (16, 16), shade).save(tmp_path / f"{sku}.png")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    build_image_index(database, POLICIES, tmp_path / "manifest.json", flip_tta=True)
    return database


def test_search_follows_the_index_preprocessing_and_refuses_a_mismatch(jackets, tmp_path):
    stored = json.loads((tmp_path / INDEX_FILE).read_text(encoding="utf-8"))
    assert stored["feature_version"] == "stand-in-vision-v1+flip-tta"
    paths = {"model_path": "m", "text_model_path": "t", "tokenizer_path": "k"}
    result = search_indexed_image(jackets, POLICIES, outfit_photo(trousers=False), **paths)
    assert result["items"] and result["query"]["crop_source"] == "auto"
    with pytest.raises(ValueError, match="rebuild it"):
        search_indexed_image(jackets, POLICIES, outfit_photo(), flip_tta=False, **paths)


def test_photo_route_reports_what_it_understood(jackets, tmp_path, monkeypatch) -> None:
    policy_path = tmp_path / "policies.json"
    policy_path.write_text(f"[{POLICY.model_dump_json()}]", encoding="utf-8")
    for name, value in {
        "FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT": "1",
        "FASHION_FINDER_IMAGE_MODEL_PATH": "m",
        "FASHION_FINDER_TEXT_MODEL_PATH": "t",
        "FASHION_FINDER_TOKENIZER_PATH": "k",
    }.items():
        monkeypatch.setenv(name, value)
    photo = as_upload(outfit_photo())

    def post(**fields):
        upload = SimpleUploadedFile("look.jpg", photo, content_type="image/jpeg")
        return Client().post("/api/v1/search/image", {"file": upload, **fields})

    with api_context(jackets, policy_path):
        found = post().json()
        query = found["query"]
        assert set(query) == {"crop_source", "crop", "category", "colour", "alternatives"}
        assert query["crop_source"] == "auto"
        assert set(query["crop"]) == {"x", "y", "width", "height"}
        assert all(0 <= value <= 1 for value in query["crop"].values())
        assert query["category"] == {
            "value": "jackets",
            "confidence": query["category"]["confidence"],
        }
        assert query["alternatives"][0]["category"] == "trousers"
        assert len(found["items"]) == 2
        # A shopper's own crop is never cropped again.
        manual = post(crop="manual").json()["query"]
        assert manual["crop_source"] == "manual" and manual["crop"] is None
        assert len(post(limit="1").json()["items"]) == 1
        assert post(limit="0").status_code == 400
        assert post(limit="21").status_code == 400
        assert post(crop="everything").status_code == 400


def scored_candidates(rows):
    """(entry, offer) pairs whose photos sit at the given similarity to [1, 0, 0, 0]."""
    pairs = []
    for offer_id, similarity, offer in rows:
        vector = [similarity, math.sqrt(max(0.0, 1 - similarity**2)), 0.0, 0.0]
        entry = {"features": unpack_features(pack_features(vector)), "sha256": f"photo-{offer_id}"}
        pairs.append((entry, {"id": offer_id, "category": "coats", **offer}))
    return pairs


def coat(title: str, colour: str, *, price="25995", source="reserved") -> dict:
    return {"title": title, "colour_names": [colour], "price_amount": price, "source_key": source}


def test_colour_variants_of_one_product_collapse_into_one_result() -> None:
    query = [1.0, 0.0, 0.0, 0.0]
    candidates = scored_candidates(
        [
            (1, 0.99, coat("Gyapjúkabát - fekete", "Fekete")),
            (2, 0.97, coat("Gyapjúkabát - bézs", "Bézs")),  # same coat, other colour
            (3, 0.96, coat("Gyapjúkabát - szürke", "Szürke", price="19995")),  # cheaper
            (4, 0.95, coat("Gyapjúkabát - fekete", "Fekete", source="zara")),  # other shop
            (5, 0.94, coat("Szövet nadrág", "Fekete")),  # another product, similar photo
            (6, 0.93, coat("Gyapjúkabát - zöld", "Zöld")),
        ]
    )
    ranked = _rank(ColourVision(), query, candidates, consensus=False, diversify=True, limit=10)
    items = {item["id"]: item for item in ranked["items"]}
    assert list(items) == [1, 3, 4, 5]
    assert items[1]["variant_count"] == 2
    assert items[1]["variant_colours"] == ["Bézs", "Zöld"]
    assert all("variant_count" not in items[key] for key in (3, 4, 5))
    plain = _rank(ColourVision(), query, candidates, consensus=False, diversify=False, limit=10)
    assert [item["id"] for item in plain["items"]] == [1, 2, 3, 4, 5, 6]


def test_confident_colour_moves_matching_offers_up_but_not_far_ones() -> None:
    query = [1.0, 0.0, 0.0, 0.0]
    candidates = scored_candidates(
        [
            (1, 0.950, coat("Kabát A", "Bézs")),
            (2, 0.940, coat("Kabát B", "Fekete")),  # black, just behind
            (3, 0.700, coat("Kabát C", "Fekete")),  # black, far away
        ]
    )
    ranked = _rank(ColourVision(), query, candidates, consensus=False, colour="black")
    assert [item["id"] for item in ranked["items"]] == [2, 1, 3]


def test_zero_shot_reading_tips_a_split_vote() -> None:
    # Weights 1, 1/2, 1/3, 1/4, 1/5: dresses 44 %, jackets 33 %, coats 23 %; no majority.
    split = [{"category": name} for name in ("dresses", "jackets", "coats", "jackets", "coats")]
    assert consensus_category(split) is None
    reading = {"jackets": 0.9, "coats": 0.05, "dresses": 0.05}
    assert consensus_category(split, reading, weight=0.75) == "jackets"
    # A confident vote of close neighbours is not overturned by a weak reading.
    vote = [{"category": "coats"}] * 5
    assert consensus_category(vote, {"jackets": 0.6, "coats": 0.4}, weight=0.25) == "coats"
    assert image_index.zero_shot_weight(0.9) < image_index.zero_shot_weight(0.6)


def test_nearest_first_eligibility_matches_a_full_scan(jackets, monkeypatch) -> None:
    monkeypatch.setattr(image_index, "ELIGIBLE_BATCH", 1)  # one photo per round trip
    vision = ColourVision()
    entries, matrix = image_index._load_index(jackets, vision)
    query = vision.encode(Image.new("RGB", (4, 4), RED))
    lazy = _nearest_eligible(
        jackets, POLICIES, vision, query, entries, matrix, offers_needed=1, colour="bordó"
    )
    assert [offer["source_product_id"] for _, offer in lazy] == ["two"]
    every = _nearest_eligible(jackets, POLICIES, vision, query, entries, matrix, offers_needed=5)
    assert [offer["source_product_id"] for _, offer in every] == ["one", "two"]


def test_onnx_thread_setting_is_validated(monkeypatch) -> None:
    pytest.importorskip("onnxruntime")
    from fashion_finder_data.visual_encoder import THREADS_ENV, session_options

    monkeypatch.setenv(THREADS_ENV, "2")
    assert session_options().intra_op_num_threads == 2
    monkeypatch.setenv(THREADS_ENV, "many")
    with pytest.raises(ValueError, match="whole number"):
        session_options()


def test_index_built_without_options_still_loads(tmp_path, monkeypatch) -> None:
    """An index from before this change (no preprocessing suffix) keeps working."""
    vision = ColourVision()
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: vision)
    database = open_database(tmp_path / "fashion_finder.sqlite")
    entry = add_offer(database, "old", title="Régi dzseki", colour="Piros")
    Image.new("RGB", (8, 8), RED).save(tmp_path / "old.png")
    (tmp_path / "manifest.json").write_text(json.dumps([entry]), encoding="utf-8")
    build_image_index(database, POLICIES, tmp_path / "manifest.json")
    stored = json.loads((tmp_path / INDEX_FILE).read_text(encoding="utf-8"))
    assert stored["feature_version"] == "stand-in-vision-v1"
    result = search_indexed_image(
        database, POLICIES, Image.new("RGB", (8, 8), RED), model_path=Path("m")
    )
    assert [item["source_product_id"] for item in result["items"]] == ["old"]
    assert result["query"]["crop_source"] == "none"  # no text model configured


def test_each_photo_is_still_checked_when_its_offer_is_shared(tmp_path, monkeypatch) -> None:
    """One offer's photos share its checks, but each photo's own URL and policy count."""
    vision = ColourVision()
    monkeypatch.setattr(image_index, "get_encoder", lambda model_path=None: vision)
    database = open_database(tmp_path / "fashion_finder.sqlite")
    url = "https://www.reserved.com/hu/hu/two-photos-123ab-45c"
    front, back = "https://static.reserved.com/front.png", "https://static.reserved.com/back.png"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            source_product_id="two-photos",
            title="Bőrdzseki",
            category="jackets",
            price_amount=Decimal(19995),
            currency="HUF",
            image_urls=[front, back],
            colour_names=["Fekete"],
            source_fetched_at=utc_now(),
        ),
    )
    offer_id = offer_rows(database)[0]["id"]
    manifest = []
    for name, image_url in (("front", front), ("back", back)):
        Image.new("RGB", (8, 8), RED).save(tmp_path / f"{name}.png")
        manifest.append({"offer_id": offer_id, "image_url": image_url, "image_path": f"{name}.png"})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    build_image_index(database, POLICIES, tmp_path / "manifest.json")
    entries, _ = image_index._load_index(database, vision)
    assert len(image_index._eligible(database, POLICIES, list(entries))) == 2
    # The shop dropped the back photo: only the front may still be matched.
    from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer

    StoredOffer.objects.using(database.alias).filter(pk=offer_id).update(
        image_urls=json.dumps([front])
    )
    kept = image_index._eligible(database, POLICIES, list(entries))
    assert [entry["image_url"] for entry, _ in kept] == [front]
    # A photo indexed under an older policy version is refused on its own.
    outdated = [
        {**entry, "policy_version": "old"} if entry["image_url"] == front else entry
        for entry in entries
    ]
    assert image_index._eligible(database, POLICIES, outdated) == []
