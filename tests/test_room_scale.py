"""A full room has to survive being judged.

Rooms above roughly ten projects used to fail at the moment of judging. Every
project in the room was scored in a single model call, and that call was capped
at a flat 45 seconds, so the work grew with the size of the room while the
budget did not. Measured against the shipped panel model, four projects took
17s, eight took 30s, and twelve blew the cap and killed the whole showcase.

These pin the three things that fixed it: bounded batches, a ceiling that scales
with the batch, and a retry so one flaky judge response cannot end a live event.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import builder_showcase as cbp

from test_builder_showcase import add_submission, fixed_clock, make_run


def scorecard_payload(submission_ids: list[str]) -> str:
    return json.dumps(
        {
            "projects": [
                {
                    "submission_id": submission_id,
                    "scores": {
                        dimension["id"]: 8
                        for dimension in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
                    },
                    "reactions": {
                        "innovation": "Strong concept with a memorable hook.",
                        "craft": "Clear builder utility and focused scope.",
                        "impact": "Useful payoff for the target audience.",
                    },
                    "panel_favorite": "Top Pick",
                    "next_commit": "Show one complete workflow end-to-end.",
                    "copilot_next_move": "Use GitHub Copilot to draft a regression test.",
                    "frontier_experiment": "Prototype one bounded automation.",
                    "grounding_refs": ["submission.project_description"],
                }
                for submission_id in submission_ids
            ]
        }
    )


class RecordingGateway:
    """Records the size of every room call and can be told to flake."""

    supports_showcase_scorecards = True

    def __init__(self, fail_times: int = 0, failure: Exception | None = None):
        self.batch_sizes: list[int] = []
        self.timeouts: list[int] = []
        self.calls = 0
        self._fail_times = fail_times
        self._failure = failure or cbp.ModelAPIError("unparseable scorecard")

    def call_showcase_scorecard(self, prompt, model_id, project_count=1):
        self.calls += 1
        self.timeouts.append(cbp._scorecard_timeout_seconds(project_count))
        if self.calls <= self._fail_times:
            raise self._failure
        submission_ids = re.findall(r"^Submission ID: (.+)$", prompt, re.MULTILINE)
        self.batch_sizes.append(len(submission_ids))
        return scorecard_payload(submission_ids)

    def call_model(self, prompt, model_id):  # shadow spec and other passes
        return scorecard_payload(
            re.findall(r"^Submission ID: (.+)$", prompt, re.MULTILINE)
        )


def score_room(tmp_path, project_count: int, gateway) -> list[dict]:
    bundle_path = make_run(tmp_path, f"room-{project_count}")
    for index in range(1, project_count + 1):
        add_submission(bundle_path, project_name=f"Project {index}")
    rubric = cbp.load_rubric(bundle_path)
    panel = ["model-a", "model-b", "model-c"]
    shadow_spec = cbp.generate_shadow_spec(
        bundle_path, rubric, panel, gateway, fixed_clock, deterministic=True
    )
    return cbp.score_submissions(
        cbp._load_submissions(bundle_path),
        rubric,
        panel,
        bundle_path,
        gateway,
        fixed_clock,
        shadow_spec=shadow_spec,
    )


class TestRoomBatching:
    def test_a_big_room_is_split_into_bounded_batches(self, tmp_path):
        """The regression: 20 projects used to go out as one call per model."""
        gateway = RecordingGateway()
        scored = score_room(tmp_path, 20, gateway)

        assert len(scored) == 20, "every project must come back scored"
        limit = cbp._showcase_room_batch_size(cbp.DEFAULT_RUBRIC)
        assert gateway.batch_sizes, "no room scorecard calls were made"
        assert max(gateway.batch_sizes) <= limit, (
            f"a single call carried {max(gateway.batch_sizes)} projects; "
            f"the batch limit is {limit}"
        )
        # Three models, each covering all 20 projects exactly once.
        assert sum(gateway.batch_sizes) == 60

    def test_every_project_is_scored_exactly_once_per_model(self, tmp_path):
        gateway = RecordingGateway()
        scored = score_room(tmp_path, 14, gateway)
        assert len({item["submission_id"] for item in scored}) == 14

    def test_batches_cover_a_room_that_does_not_divide_evenly(self):
        submissions = [{"submission_id": f"s{i}"} for i in range(13)]
        batches = cbp._showcase_room_batches(submissions, cbp.DEFAULT_RUBRIC)
        assert [len(batch) for batch in batches] == [6, 6, 1]
        assert [s for batch in batches for s in batch] == submissions

    def test_batch_size_is_clamped_to_something_survivable(self):
        def size(value):
            return cbp._showcase_room_batch_size(
                {"freshness_gate": {"showcase_room_batch_size": value}}
            )

        assert size(200) <= 12, "an unbounded batch reintroduces the timeout"
        assert size(0) >= 1 and size(-4) >= 1
        assert size("six") == 6, "junk config must fall back, not crash the room"


class TestScorecardTimeout:
    def test_the_ceiling_grows_with_the_batch(self):
        """A flat ceiling is what killed big rooms."""
        assert cbp._scorecard_timeout_seconds(6) > cbp._scorecard_timeout_seconds(1)

    def test_a_full_batch_clears_the_measured_worst_case(self):
        """
        Six-project batches were measured at 18-48s across a full 15-call room,
        with an 83s outlier. The old flat 45s ceiling sat underneath that.
        """
        assert cbp._scorecard_timeout_seconds(6) >= 100

    def test_a_single_project_keeps_the_original_budget(self):
        assert cbp._scorecard_timeout_seconds(1) == 45

    def test_a_missing_count_does_not_produce_a_zero_timeout(self):
        assert cbp._scorecard_timeout_seconds(0) == 45
        assert cbp._scorecard_timeout_seconds(None) == 45

    def test_the_gateway_asks_for_a_budget_that_matches_the_batch(self, tmp_path):
        gateway = RecordingGateway()
        score_room(tmp_path, 12, gateway)
        assert max(gateway.timeouts) > 45, (
            "batched calls were still given the flat single-project budget"
        )


class TestFlakyJudgeRetry:
    def test_one_unparseable_response_does_not_end_the_showcase(self, tmp_path):
        """
        Seen live: a judge returned prose instead of JSON on one call out of
        fifteen and the entire 30-project run failed with no recourse.
        """
        gateway = RecordingGateway(fail_times=1)
        scored = score_room(tmp_path, 8, gateway)
        assert len(scored) == 8
        assert gateway.calls > len(gateway.batch_sizes), "the flake was not retried"

    def test_retries_are_bounded_and_the_failure_still_surfaces(self, tmp_path):
        gateway = RecordingGateway(fail_times=999)
        with pytest.raises(cbp.ModelAPIError) as excinfo:
            score_room(tmp_path, 6, gateway)
        assert "attempts" in str(excinfo.value)
        attempts = cbp._model_call_attempts(cbp.DEFAULT_RUBRIC)
        assert gateway.calls <= attempts * 3 + 1, "retries were not bounded"

    def test_attempt_count_is_clamped(self):
        def attempts(value):
            return cbp._model_call_attempts(
                {"freshness_gate": {"model_call_attempts": value}}
            )

        assert attempts(0) >= 1, "a room must always get at least one attempt"
        assert attempts(500) <= 5, "unbounded retries would stall a live room"
        assert attempts("lots") == 3
