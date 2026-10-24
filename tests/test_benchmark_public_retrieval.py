"""The public-retrieval benchmark scores rankings correctly and ranks like the product."""

from __future__ import annotations

import importlib.util
import json
import math
import random
from pathlib import Path

import pytest
from test_practice_catalogue import _practice_dir

from fashion_finder_data.image_index import search_indexed_image, search_indexed_text
from fashion_finder_data.practice.benchmark import (
    LOOK_RESERVED,
    PHOTO_RESERVED,
    build_split,
    category_guesses,
    checked_options,
    distance_matrix,
    percentile,
    query_metrics,
    rank_products,
    summarise,
)
from fashion_finder_data.practice.normalise import PracticeProduct
from fashion_finder_data.ranking_evaluation import ranking_metrics
from fashion_finder_data.visual_encoder import COLOUR_LAYOUT
from fashion_finder_data.visual_features import feature_distance

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_public_retrieval.py"


def _script():
    spec = importlib.util.spec_from_file_location("benchmark_public_retrieval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_metrics_score_ranks_and_count_unranked_relevant_items():
    one = query_metrics(["x", "p", "y"], frozenset({"p"}))
    assert one["hit_at_1"] == 0.0 and one["hit_at_5"] == 1.0
    assert one["mrr_at_10"] == 0.5 and one["recall_at_10"] == 1.0
    assert one["precision_at_10"] == pytest.approx(0.1)
    assert one["ndcg_at_10"] == pytest.approx(1 / math.log2(3))

    # Two of four relevant products ranked: the ideal DCG still counts all four.
    many = query_metrics(["a", "x", "b"], frozenset({"a", "b", "c", "d"}))
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, 5))
    assert many["ndcg_at_10"] == pytest.approx((1 + 1 / math.log2(4)) / ideal)
    assert many["recall_at_10"] == 0.5 and many["hit_at_1"] == 1.0
    # When every relevant item is ranked, the scores equal ranking_evaluation's.
    shared = ranking_metrics([7, 3, 9], {3, 9})
    mine = query_metrics(["g", "c", "i"], frozenset({"c", "i"}))
    assert {key: mine[key] for key in shared} == pytest.approx(shared)
    assert query_metrics([], frozenset({"a"}))["hit_at_5"] == 0.0


def test_category_guesses_score_the_vote_and_the_recognised_category():
    voted = category_guesses(
        {"items": [], "preferred_category": "shoes", "query": {"category": {"value": "bags"}}},
        "shoes",
    )
    assert voted == {
        "preferred_category_shown": 1.0,
        "preferred_category_correct": 1.0,
        "query_category_shown": 1.0,
        "query_category_correct": 0.0,
    }
    silent = category_guesses({"items": [], "query": {"category": None}}, "shoes")
    summary = summarise([voted, silent], [5.0, 7.0])
    assert summary["preferred_category_shown"] == 0.5
    assert summary["preferred_category_precision"] == 1.0
    assert summary["query_category_precision"] == 0.0
    assert summary["latency_ms_p50"] == 5.0


def test_percentile_uses_nearest_rank():
    assert percentile(list(range(1, 21)), 0.95) == 19
    assert percentile([5.0], 0.5) == 5.0
    assert percentile([], 0.5) is None


def test_vectorised_colour_layout_distance_matches_the_product():
    import numpy as np

    generator = random.Random(4)
    vectors = [[generator.random() for _ in range(792)] for _ in range(4)]
    matrix = distance_matrix(COLOUR_LAYOUT, np.asarray(vectors[:1]), np.asarray(vectors[1:]))
    expected = [feature_distance(vectors[0], other) for other in vectors[1:]]
    assert matrix[0].tolist() == pytest.approx(expected)


def test_products_rank_by_their_nearest_photo():
    import numpy as np

    distances = np.asarray([0.4, 0.1, 0.3, 0.1])
    assert rank_products(distances, ["a", "b", "a", "c"]) == ["b", "c", "a"]


def _product(practice_id, category, colour=None, gender=None, photos=1, language="hu"):
    dataset = "abo" if language == "en" else "glami"
    return PracticeProduct(
        practice_id=practice_id,
        dataset=dataset,
        group_id=practice_id,
        title=f"{category} {practice_id}",
        language=language,
        category=category,
        source_category="test",
        gender=gender,
        colour=colour,
        price_huf=1990,
        images=[
            {
                "url": f"https://practice.invalid/{practice_id}/{n}.jpg",
                "path": f"images/{dataset}/{practice_id.split('-')[1]}x{n}.jpg",
                "view": "main" if n == 0 else "other",
            }
            for n in range(photos)
        ],
        credit="test",
    )


def test_split_holds_out_every_query_and_labels_phrases_by_attributes():
    products = [
        _product(f"glami-{n}", "coats", colour="black", gender="women" if n % 2 else "men")
        for n in range(40)
    ] + [_product(f"abo-S{n}", "shoes", colour="white", photos=3, language="en") for n in range(6)]
    split = build_split(products)
    gallery_paths = {path for path, _ in split.gallery_photos}
    assert not gallery_paths & split.held_out_photos
    assert not set(split.gallery) & split.held_out_products
    assert len(split.held_out_products) == 15  # coats have enough GLAMI items to hold out
    for task, queries in split.queries.items():
        for query in queries:
            assert query.relevant <= set(split.gallery), task
            if query.photo:
                assert query.photo not in gallery_paths
    assert {query.relevant for query in split.queries["a"]} == {
        frozenset({f"abo-S{n}"}) for n in range(6)
    }
    phrases = {query.text: query.relevant for query in split.queries["d_phrase"]}
    women = {pid for pid in split.gallery if pid.startswith("glami-") and int(pid[6:]) % 2}
    assert phrases["fekete női kabát"] == women
    assert {query.text for query in split.queries["d_phrase_en"]} >= {"black women's coat"}


def test_search_options_must_be_keyword_arguments_the_function_accepts():
    assert checked_options(search_indexed_image, '{"consensus": false}', PHOTO_RESERVED) == {
        "consensus": False
    }
    assert checked_options(search_indexed_text, '{"blend": false}', LOOK_RESERVED) == {
        "blend": False
    }
    assert checked_options(search_indexed_image, '{"flip_tta": true}', PHOTO_RESERVED) == {
        "flip_tta": True
    }
    with pytest.raises(ValueError, match="does not accept no_such_option; options it accepts: .*"):
        checked_options(search_indexed_image, '{"no_such_option": true}', PHOTO_RESERVED)
    with pytest.raises(ValueError, match="does not accept model_path"):
        checked_options(search_indexed_image, '{"model_path": "x"}', PHOTO_RESERVED)
    with pytest.raises(TypeError, match="JSON object"):
        checked_options(search_indexed_text, "[1]", LOOK_RESERVED)


def test_benchmark_runs_both_modes_on_a_practice_folder(tmp_path, capsys):
    practice = _practice_dir(tmp_path)
    script = _script()
    common = ["--practice-dir", str(practice), "--work-dir", str(tmp_path / "work")]

    assert script.main([*common, "--output", str(tmp_path / "embedding.json")]) == 0
    embedding = json.loads((tmp_path / "embedding.json").read_text(encoding="utf-8"))
    assert embedding["skipped_tasks"] == {
        task: "no text model" for task in ("c", "d_name", "d_phrase", "d_phrase_en")
    }
    assert embedding["tasks"]["a"]["queries"] == 1
    assert embedding["tasks"]["a"]["hit_at_1"] == 1.0  # the mirrored view finds its shoe

    output = tmp_path / "real.json"
    code = script.main(
        [
            *common,
            "--mode",
            "real",
            "--photo-options",
            '{"consensus": false}',
            "--output",
            str(output),
        ]
    )
    assert code == 0
    real = json.loads(output.read_text(encoding="utf-8"))
    assert real["photo_options"] == {"consensus": False}
    assert real["prepare"]["index"]["index_status"] == "complete"
    assert real["tasks"]["a"]["hit_at_1"] == 1.0
    assert "| a | 1 |" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        script.main([*common, "--mode", "real", "--photo-options", '{"no_such_option": true}'])
    assert "does not accept no_such_option" in capsys.readouterr().err
