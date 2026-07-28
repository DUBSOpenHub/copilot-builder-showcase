"""The practice screen must score the way the engine scores.

``docs/index.html`` is a single self-contained file served from GitHub Pages, so
it cannot read ``event_spec.py`` or ``config/rubric.json`` at runtime — it embeds
its own copy of the rubric. These tests fail the moment that copy drifts from the
shipped default, so a practice showcase always previews what an Official Panel
computes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

import builder_showcase as cbp

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "docs" / "index.html"
SAMPLE_RUBRIC = REPO_ROOT / "config" / "rubric.json"


def _extract_js_array(source: str, name: str) -> List[Dict[str, Any]]:
    """Parse a ``var NAME = [ {...}, ... ];`` literal out of the page.

    The literals are plain data with bare keys, so quoting the keys is enough to
    make them JSON. Anything more exotic should fail loudly rather than silently
    weaken the check.
    """
    match = re.search(
        r"var\s+" + re.escape(name) + r"\s*=\s*(\[.*?\]);", source, re.DOTALL
    )
    assert match, f"{name} not found in docs/index.html"
    literal = re.sub(r"(?<=[{,])\s*([A-Za-z_]\w*)\s*:", r' "\1":', match.group(1))
    try:
        return json.loads(literal)
    except json.JSONDecodeError as exc:  # pragma: no cover - guard rail
        pytest.fail(f"Could not parse {name} from docs/index.html: {exc}")


@pytest.fixture(scope="module")
def page_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_web_dimensions_match_the_shipped_rubric(page_source: str) -> None:
    embedded = _extract_js_array(page_source, "RUBRIC_DIMENSIONS")
    expected = cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
    assert len(embedded) == len(expected)
    for got, want in zip(embedded, expected):
        assert got["id"] == want["id"]
        assert got["name"] == want["name"]
        assert got["max_score"] == want["max_score"]
        assert got["description"] == want["description"]
        assert got["weight"] == pytest.approx(want["weight"])


def test_web_archetypes_match_the_shipped_rubric(page_source: str) -> None:
    embedded = _extract_js_array(page_source, "JUDGE_ARCHETYPES")
    expected = cbp.DEFAULT_RUBRIC["judge_archetypes"]
    assert len(embedded) == len(expected)
    for got, want in zip(embedded, expected):
        assert got["id"] == want["id"]
        assert got["name"] == want["name"]
        assert got["focus"] == want["focus"]


def test_web_weights_sum_to_one(page_source: str) -> None:
    embedded = _extract_js_array(page_source, "RUBRIC_DIMENSIONS")
    assert sum(d["weight"] for d in embedded) == pytest.approx(1.0)


def test_web_totals_are_out_of_ten(page_source: str) -> None:
    """A weighted rubric totals 10, never the sum of its dimensions."""
    assert "/30" not in page_source, "the practice screen still shows a /30 total"
    for shown in ('"/10"', "/10 · weighted median"):
        assert shown in page_source


def test_sample_rubric_matches_the_shipped_rubric() -> None:
    """config/rubric.json is the --config example; it must not contradict the default."""
    sample = json.loads(SAMPLE_RUBRIC.read_text(encoding="utf-8"))
    assert sample["rubric"]["dimensions"] == cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
    assert sample["judge_archetypes"] == cbp.DEFAULT_RUBRIC["judge_archetypes"]


def test_web_scoring_reproduces_the_engine_total() -> None:
    """The page's weighted total must equal compute_shadow_score for the same input.

    Guards the formula itself, not just the data: dimension scores are normalized
    against ``max_score`` and weighted, rather than averaged or summed.
    """
    per = {"innovation": 8.4, "impact": 7.1, "execution": 9.0, "presentation": 6.5}
    scored = [
        {
            "submission_id": "sub-1",
            "dimension_scores": {
                dim_id: {"score": value, "max_score": 10}
                for dim_id, value in per.items()
            },
        }
    ]
    engine_total = cbp.compute_shadow_score(scored, cbp.DEFAULT_RUBRIC)["scores"]["sub-1"]

    web_total = 0.0
    for dim in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]:
        web_total += (per[dim["id"]] / dim["max_score"]) * 10 * dim["weight"]

    assert web_total == pytest.approx(engine_total)
    assert 0 <= engine_total <= 10


def test_displayed_dimensions_add_up_to_the_displayed_total(page_source: str) -> None:
    """The four numbers on a card must sum to the total printed beside them.

    Dimension scores are shown to 1dp and the weights carry 2dp, so a weighted
    total lands on an exact half-cent (8.295) regularly. JavaScript floats put
    such a value a hair below the half, and ``toFixed(2)`` would round it *down*
    to 8.29 while anyone in the room adding the column gets 8.30. ``scoreEntry``
    must therefore hold dimensions at the displayed 1dp and round the total with
    the half-up helper rather than raw float truncation.
    """
    body = re.search(
        r"function scoreEntry\(e\) \{(.+?)\n  \}", page_source, re.DOTALL
    )
    assert body, "scoreEntry not found in the practice screen"
    scoring = body.group(1)

    assert "function roundTo(" in page_source, "the half-up rounding helper is gone"
    assert "roundTo(vals[1], 1)" in scoring, "dimensions must be held at the 1dp shown"
    assert "roundTo(total, 2)" in scoring, "the total must round half-up to the 2dp shown"

    # A 1dp dimension weighted by a 2dp weight yields at most 3 decimals, which is
    # exactly why half-cent totals occur often enough to be worth guarding.
    from decimal import Decimal

    per = {"innovation": "8.1", "impact": "7.9", "execution": "8.2", "presentation": "9.2"}
    exact = sum(
        (
            Decimal(per[dim["id"]]) * Decimal(str(dim["weight"]))
            for dim in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
        ),
        Decimal(0),
    )
    assert exact == Decimal("8.295"), "sample lands on a half-cent, as the room will see"
