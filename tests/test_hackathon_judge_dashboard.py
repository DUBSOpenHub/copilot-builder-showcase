"""Regression tests for audience-facing dashboard row states."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("textual")

from hackathon_judge_dashboard import (
    AwardReveal,
    BuilderDashboard,
    BundleState,
    _progress_label,
    _submission_table_rows,
    _verdict_cell,
)


def test_revealed_audience_rows_show_completion_without_scores():
    cell = _verdict_cell(
        {"total_score": 9.5},
        show_scores=False,
        revealed=True,
    )

    assert cell.plain == "✓ reviewed"


def test_unrevealed_audience_rows_remain_in_review():
    cell = _verdict_cell(
        {"total_score": 9.5},
        show_scores=False,
        revealed=False,
    )

    assert cell.plain == "🕒 in review"


def test_operator_rows_do_not_show_scores_before_reveal():
    cell = _verdict_cell(
        {"total_score": 9.5},
        show_scores=True,
        revealed=False,
    )

    assert "9.5" not in cell.plain


def test_submissions_appear_before_verdicts_exist():
    rows = _submission_table_rows(
        [
            {
                "submission_id": "sub-1",
                "project_name": "Project Aurora",
                "builder_name": "Team Aurora",
            }
        ],
        {},
        show_scores=False,
        revealed=False,
    )

    assert len(rows) == 1
    assert rows[0][1:3] == ("Project Aurora", "Team Aurora")
    assert rows[0][3].plain == "✓ entered"


def test_dashboard_surfaces_are_unambiguous():
    audience = type("Surface", (), {"operator": False})()
    operator = type("Surface", (), {"operator": True})()

    assert "NOT PART OF THE LIVE SHOW" in BuilderDashboard.surface_label.fget(audience)
    assert "KEEP PRIVATE" in BuilderDashboard.surface_label.fget(operator)


def test_operator_state_uses_audience_projection_before_reveal():
    audience_view = object()

    class Reader:
        def is_revealed(self):
            return False

        def audience_view(self):
            return audience_view

        def operator_view(self):
            raise AssertionError("operator view must stay unavailable before reveal")

    state = object.__new__(BundleState)
    state.operator = True
    state.reader = Reader()
    state.refresh()

    assert state.view is audience_view


def test_failed_progress_has_a_safe_pause_label():
    assert _progress_label(
        {
            "status": "failed",
            "stage": "ranking-seal",
            "submissions": {"completed": 3, "total": 3},
        },
        3,
    ) == "⚠ evaluation paused"


def test_award_reveal_ignores_malformed_shared_podium_recipients():
    reveal = AwardReveal()
    reveal._awards = {
        "tie_ceremony": {
            "award_tie_resolutions": [
                {
                    "resolution": "shared-podium",
                    "selected_submission_ids": None,
                    "award_name": "Gold",
                }
            ]
        },
        "awards": [],
    }

    assert reveal.render() is not None
