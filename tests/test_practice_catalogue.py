"""The practice catalogue runs the real storage, policy and index path, and never mixes with
real retailer data."""

from __future__ import annotations

import io
import json
import zipfile
from io import BytesIO

import pytest
from django.conf import settings
from django.test import Client, override_settings
from PIL import Image, ImageOps

from fashion_finder_data.api.views import api_context
from fashion_finder_data.cli import main
from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_index import INDEX_FILE, image_index_ready, search_indexed_image
from fashion_finder_data.practice.build import _abo_product, remote_member
from fashion_finder_data.practice.loader import (
    MANIFEST_FILE,
    MARKER_FILE,
    POLICY_FILE,
    build_practice_index,
    load_practice_catalogue,
)
from fashion_finder_data.practice.normalise import (
    PRICE_BANDS,
    PracticeProduct,
    abo_title,
    colour_in,
    glami_category,
    synthetic_price,
    synthetic_sizes,
)
from fashion_finder_data.public_catalogue import search_public_catalogue
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer

ABO = "https://amazon-berkeley-objects.s3.amazonaws.com/images/small"
MIDDLEWARE = "fashion_finder_data.practice.web.PracticeImageMiddleware"


def _product(practice_id, category, title, photos, **extra):
    dataset = practice_id.split("-", 1)[0]
    return {
        "practice_id": practice_id,
        "dataset": dataset,
        "group_id": f"{dataset}:{practice_id}",
        "title": title,
        "language": "en" if dataset == "abo" else "hu",
        "category": category,
        "source_category": "test",
        "price_huf": "9990",
        "images": [
            {
                "url": f"{ABO}/{name}" if dataset == "abo" else f"https://practice.invalid/{name}",
                "path": name,
                "view": "main" if number == 0 else "other",
            }
            for number, name in enumerate(photos)
        ],
        "credit": "Practice data: test",
        **extra,
    }


def _practice_dir(tmp_path):
    """Five products with CC0 demo photos; the ABO shoe has a front and a mirrored view."""
    root = tmp_path / "practice"
    (root / "images" / "abo").mkdir(parents=True)
    (root / "images" / "glami").mkdir(parents=True)
    samples = {
        "images/glami/1.jpg": demo_image_bytes("demo-008"),
        "images/glami/2.jpg": demo_image_bytes("demo-003"),
        "images/glami/3.jpg": demo_image_bytes("demo-009"),
        "images/glami/4.jpg": demo_image_bytes("demo-011"),
        "images/abo/shoe-a.jpg": demo_image_bytes("demo-012"),
    }
    for name, data in samples.items():
        (root / name).write_bytes(data)
    with Image.open(BytesIO(samples["images/abo/shoe-a.jpg"])) as image:
        ImageOps.mirror(image).save(root / "images" / "abo" / "shoe-b.jpg", "JPEG")
    products = [
        _product("glami-1", "dresses", "Piros női ruha", ["images/glami/1.jpg"], colour="red"),
        _product("glami-2", "jackets", "Barna férfi dzseki", ["images/glami/2.jpg"]),
        _product("glami-3", "sweaters", "Szürke pulóver", ["images/glami/3.jpg"]),
        _product("glami-4", "jeans", "Kék farmer", ["images/glami/4.jpg"]),
        _product(
            "abo-B0SHOE",
            "shoes",
            "Canvas Sneaker, Tan",
            ["images/abo/shoe-a.jpg", "images/abo/shoe-b.jpg"],
            colour_names=["Brown"],
            sizes=["38", "39"],
        ),
    ]
    with (root / "catalogue.jsonl").open("w", encoding="utf-8") as stream:
        for product in products:
            stream.write(json.dumps(product, ensure_ascii=False) + "\n")
    return root


def _search_photo(data_dir, name):
    database = open_database(data_dir / "fashion_finder.sqlite")
    policies = load_policies(data_dir / POLICY_FILE)
    with Image.open(data_dir / "practice-images" / name) as image:
        return search_indexed_image(database, policies, image)["items"]


def test_practice_catalogue_runs_the_real_storage_policy_and_index_path(tmp_path):
    practice = _practice_dir(tmp_path)
    data = tmp_path / "data"
    report = load_practice_catalogue(data, practice)
    assert report["products"] == 5
    assert report["manifest_photos"] == 6

    policies = load_policies(data / POLICY_FILE)
    policy = policies[("practice", "HU")]
    assert policy.approval_status == "approved" and policy.allow_image_processing
    assert "CC BY 4.0" in policy.evidence_ref and "GLAMI-1M" in policy.evidence_ref
    assert "ABO © Amazon.com (CC BY 4.0)" in policy.attribution

    database = open_database(data / "fashion_finder.sqlite")
    items = search_public_catalogue(database, policies, query="ruha")["items"]
    assert [item["title"] for item in items] == ["Piros női ruha"]
    assert items[0]["source_key"] == "practice"
    assert items[0]["source_url"] == "https://practice.invalid/hu/glami/glami-1"
    assert items[0]["attribution"] == policy.attribution
    shoe = search_public_catalogue(database, policies, category="shoes")["items"][0]
    assert shoe["price_amount"] == "9990" and shoe["size_options"] == ["38", "39"]
    assert shoe["image_urls"] == [f"{ABO}/images/abo/shoe-a.jpg", f"{ABO}/images/abo/shoe-b.jpg"]

    manifest = json.loads((data / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert all((data / entry["image_path"]).is_file() for entry in manifest)
    assert all(entry["image_path"].startswith("practice-images/") for entry in manifest)

    assert build_practice_index(data)["index_status"] == "complete"
    assert image_index_ready(database, policies)
    assert _search_photo(data, "abo/shoe-b.jpg")[0]["title"] == "Canvas Sneaker, Tan"


def test_practice_loader_refuses_a_data_dir_with_real_or_demo_data(tmp_path):
    practice = _practice_dir(tmp_path)

    real = tmp_path / "real"
    database = open_database(real / "fashion_finder.sqlite")
    url = "https://www.reserved.com/hu/hu/coat-abc-12"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            title="Real coat",
            source_fetched_at=utc_now(),
        ),
    )
    with pytest.raises(ValueError, match="real sources"):
        load_practice_catalogue(real, practice)
    assert not (real / MARKER_FILE).exists()
    assert [row["source_key"] for row in offer_rows(database)] == ["reserved"]

    policy_only = tmp_path / "policy-only"
    policy_only.mkdir()
    (policy_only / POLICY_FILE).write_text(
        json.dumps([{"source_key": "sinsay", "market_code": "HU"}]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="sinsay"):
        load_practice_catalogue(policy_only, practice)

    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / ".demo-catalogue").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="demo"):
        load_practice_catalogue(demo, practice)

    with pytest.raises(ValueError, match="outside the shared practice folder"):
        load_practice_catalogue(practice / "data", practice)


def test_real_collection_commands_refuse_a_practice_data_dir(tmp_path, capsys):
    data = tmp_path / "data"
    load_practice_catalogue(data, _practice_dir(tmp_path))
    for command in (["collect", "reserved"], ["import-feed", "reserved", "feed.csv"]):
        with pytest.raises(SystemExit) as stopped:
            main(["--data-dir", str(data), *command])
        assert stopped.value.code == 2
    assert "practice catalogue" in capsys.readouterr().err


def test_reload_is_idempotent_and_withdraws_products_left_out(tmp_path):
    practice = _practice_dir(tmp_path)
    data = tmp_path / "data"
    assert load_practice_catalogue(data, practice)["changed_offers"] == 5
    manifest = (data / MANIFEST_FILE).read_text(encoding="utf-8")
    again = load_practice_catalogue(data, practice)
    assert again["changed_offers"] == 0 and again["withdrawn_offers"] == 0
    assert (data / MANIFEST_FILE).read_text(encoding="utf-8") == manifest

    smaller = load_practice_catalogue(data, practice, limit=2)
    assert smaller["withdrawn_offers"] == 3
    # The limit takes categories in turn rather than the first rows of one category.
    assert len(smaller["products_by_category"]) == 2
    database = open_database(data / "fashion_finder.sqlite")
    visible = search_public_catalogue(database, load_policies(data / POLICY_FILE))["items"]
    assert len(visible) == 2

    held_out = load_practice_catalogue(
        data,
        practice,
        exclude_products=frozenset({"glami-1"}),
        exclude_photos=frozenset({"images/abo/shoe-b.jpg"}),
    )
    assert held_out["products"] == 4 and held_out["manifest_photos"] == 4


def test_index_builds_in_parts_and_never_serves_a_partial_index(tmp_path):
    practice = _practice_dir(tmp_path)
    data = tmp_path / "data"
    load_practice_catalogue(data, practice)
    database = open_database(data / "fashion_finder.sqlite")
    policies = load_policies(data / POLICY_FILE)

    first = build_practice_index(data, chunk_photos=2)
    assert first == {
        "index_status": "partial",
        "parts_done": 1,
        "parts": 3,
        "encoded_photos": 2,
        "next_step": "run the same command again to encode the next part",
    }
    assert not (data / INDEX_FILE).exists() and not image_index_ready(database, policies)
    assert build_practice_index(data, chunk_photos=2)["parts_done"] == 2
    done = build_practice_index(data, chunk_photos=2)
    assert done["index_status"] == "complete" and done["indexed_photos"] == 6
    assert image_index_ready(database, policies)
    assert build_practice_index(data, chunk_photos=2)["encoded_photos"] == 0

    # Holding out one photo changes only the chunks after it.
    load_practice_catalogue(data, practice, exclude_photos=frozenset({"images/abo/shoe-b.jpg"}))
    rebuilt = build_practice_index(data, chunk_photos=2)
    assert rebuilt["index_status"] == "complete" and rebuilt["indexed_photos"] == 5
    assert rebuilt["encoded_photos"] == 1


def test_practice_photos_are_served_only_in_practice_mode(tmp_path, monkeypatch):
    data = tmp_path / "data"
    load_practice_catalogue(data, _practice_dir(tmp_path))
    database = open_database(data / "fashion_finder.sqlite")
    photo = (data / "practice-images" / "glami" / "1.jpg").read_bytes()

    monkeypatch.setenv("FASHION_FINDER_PRACTICE_MODE", "1")
    with (
        override_settings(MIDDLEWARE=[*settings.MIDDLEWARE, MIDDLEWARE]),
        api_context(database, data / POLICY_FILE),
    ):
        client = Client()
        item = client.get("/api/v1/search", {"q": "ruha"}).json()["items"][0]
        assert item["image_urls"] == ["/api/v1/practice/images/glami/1.jpg"]
        response = client.get(item["image_urls"][0])
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == photo
        assert client.get("/api/v1/practice/images/glami/..%2F..%2Fsecret.jpg").status_code == 404

    monkeypatch.delenv("FASHION_FINDER_PRACTICE_MODE")
    with (
        override_settings(MIDDLEWARE=[*settings.MIDDLEWARE, MIDDLEWARE]),
        api_context(database, data / POLICY_FILE),
    ):
        client = Client()
        item = client.get("/api/v1/search", {"q": "ruha"}).json()["items"][0]
        assert item["image_urls"] == ["https://practice.invalid/images/glami/1.jpg"]
        assert client.get("/api/v1/practice/images/glami/1.jpg").status_code == 404


@pytest.mark.parametrize(
    ("category_name", "name", "expected"),
    [
        ("mens-pants", "Férfi farmernadrág slim", "jeans"),
        ("womens-pants", "Női chino nadrág", "trousers"),
        ("mens-denim-jackets", "Farmerdzseki", "jackets"),
        ("bikinis", "Bikini felső", None),
    ],
)
def test_glami_classes_map_to_the_shared_taxonomy(category_name, name, expected):
    assert glami_category(category_name, name) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Női világoskék blúz", "blue"),
        ("ESPRIT Ing sötétkék", "navy"),
        ("Fekete-fehér csíkos ruha", "black"),
        ("Gyerekek cipője", None),  # "kék" inside another word is not a colour
        ("Sneaker, Off-White", "white"),
    ],
)
def test_colours_map_to_the_repository_vocabulary(text, expected):
    assert colour_in(text) == expected


def test_abo_rows_need_two_photos_and_lose_sizes_from_titles():
    images = {name: {"path": f"ab/{name}.jpg"} for name in ("m1", "o1", "o2")}
    row = {
        "item_id": "B0TEST",
        "domain_name": "amazon.com",
        "product_type": [{"value": "SANDAL"}],
        "item_name": [
            {"language_tag": "de_DE", "value": "Sandale"},
            {
                "language_tag": "en_US",
                "value": "Amazon Brand - The Fix Women's Sandal, Taupe, 6.5 B US",
            },
        ],
        "brand": [{"language_tag": "en_US", "value": "The Fix"}],
        "color": [{"language_tag": "en_US", "value": "Taupe", "standardized_values": ["Beige"]}],
        "main_image_id": "m1",
        "other_image_id": ["o1", "o2", "missing"],
    }
    product = _abo_product(row, images)
    assert product.title == "The Fix Women's Sandal, Taupe"
    assert (product.category, product.colour, product.gender) == ("shoes", "beige", "women")
    assert [image.view for image in product.images] == ["main", "other", "other"]
    assert _abo_product({**row, "other_image_id": ["missing"]}, images) is None
    assert _abo_product({**row, "product_type": [{"value": "SOFA"}]}, images) is None
    assert abo_title("Stanton Men's Brown Loafers-8 UK (42 EU)") == "Stanton Men's Brown Loafers"


def test_synthetic_prices_and_sizes_are_stable_and_plausible():
    for category, (low, high) in PRICE_BANDS.items():
        price = synthetic_price("glami-123", category)
        assert low <= price <= high and price % 1000 == 990
        assert price == synthetic_price("glami-123", category)
    assert synthetic_sizes("abo-1", "bags") == []
    sizes = synthetic_sizes("abo-1", "shoes")
    assert len(sizes) >= 3 and all(36 <= int(size) <= 45 for size in sizes)


def test_practice_records_reject_unknown_labels_and_unsafe_photo_paths():
    good = _product("glami-9", "skirts", "Szoknya", ["images/glami/9.jpg"])
    assert PracticeProduct.model_validate(good).category == "skirts"
    for bad in (
        {**good, "category": "swimwear"},
        {**good, "colour": "gold"},
        {**good, "images": [{**good["images"][0], "path": "images/glami/../../x.jpg"}]},
    ):
        with pytest.raises(ValueError):
            PracticeProduct.model_validate(bad)


def test_remote_zip_members_are_read_with_one_range_request():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/stored.jpg", b"\xff\xd8 stored", compress_type=zipfile.ZIP_STORED)
        archive.writestr("a/deflated.csv", b"x,y\n" * 500, compress_type=zipfile.ZIP_DEFLATED)
    data = buffer.getvalue()

    class Remote:
        calls = 0

        def read_range(self, start, length):
            Remote.calls += 1
            return data[start : start + length]

    infos = {info.filename: info for info in zipfile.ZipFile(io.BytesIO(data)).infolist()}
    assert remote_member(Remote(), infos["a/stored.jpg"]) == b"\xff\xd8 stored"
    assert remote_member(Remote(), infos["a/deflated.csv"]) == b"x,y\n" * 500
    assert Remote.calls == 2
