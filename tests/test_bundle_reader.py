"""
Test suite for bundle_reader.BundleReader.

Exercises the reader against real bundles produced by the current
builder_showcase.py artifact writers (manifest/bundle.json,
inputs/*.json, verdicts/*.json, feedback/*.json, sealed/shadow_score.json,
winner/awards.json, freshness_gate.json) — not the legacy root
manifest.json / NDJSON layout.

Run with: python -m pytest tests/test_bundle_reader.py -v
"""

import copy
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

# Add parent dir to sys.path so we can import the modules under test.
sys.path.insert(0, str(Path(__file__).parent.parent))

import builder_showcase as cbp
from bundle_reader import BundleReader, REVEALED_STATUSES, redact_audience_narrative

FIXED_TS = "2026-06-04T22:44:37+00:00"
fixed_clock = lambda: datetime(2026, 6, 4, 22, 44, 37, tzinfo=timezone.utc)


class MockGateway:
    """Deterministic mock model gateway, mirroring the one used by the main
    test suite so scoring output is reproducible."""

    def __init__(self, models=None):
        self.models = models or cbp.APPROVED_MODELS

    def query_available_models(self) -> List[Dict]:
        return self.models

    def call_model(self, prompt: str, model_id: str) -> str:
        return cbp._synthetic_model_response(prompt, model_id)


def _make_submission(builder_name: str, project_name: str, description: str) -> Dict:
    return {
        "submission_id": str(uuid.uuid4()),
        "builder_name": builder_name,
        "project_name": project_name,
        "description": description,
        "artifacts": [],
        "submitted_at": FIXED_TS,
        "file_size_bytes": 0,
    }


def _init_collecting_bundle(bundle_path: Path, run_id: str, submissions: List[Dict]) -> Dict:
    """Create a bundle at 'collecting' status with the given submissions
    written to inputs/*.json, matching the real submit/import-urls path."""
    rubric = copy.deepcopy(cbp.DEFAULT_RUBRIC)
    rubric["freshness_gate"]["policy_mode"] = "permissive"
    cbp.init_bundle(run_id, "workshop", rubric, bundle_path, fixed_clock)
    for sub in submissions:
        cbp.write_once_json(bundle_path / "inputs" / f"{sub['submission_id']}.json", sub)
    cbp.update_status(bundle_path, "collecting", fixed_clock)
    return rubric


def _run_full_judge(bundle_path: Path, rubric: Dict, submissions: List[Dict],
                    gateway: Optional[MockGateway] = None) -> Dict:
    """Drive the bundle through freshness gate, scoring, sealing, verdicts,
    and feedback — leaving status at 'sealed', mirroring `judge`."""
    gw = gateway or MockGateway()
    gate = cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
    selected = gate["selected_models"]
    shadow_spec = cbp.generate_shadow_spec(bundle_path, rubric, selected, gw, fixed_clock)
    scored = cbp.score_submissions(submissions, rubric, selected, bundle_path, gw, fixed_clock)
    cbp.assess_shadow_spec(
        scored, submissions, shadow_spec, selected, bundle_path, gw, fixed_clock
    )
    shadow = cbp.compute_shadow_score(scored, rubric, fixed_clock)
    cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)
    cbp.build_panel_verdicts(scored, submissions, rubric, selected, bundle_path, gw, fixed_clock)
    cbp.build_feedback_cards(scored, submissions, rubric, selected, bundle_path, gw, fixed_clock)
    cbp.update_status(bundle_path, "sealed", fixed_clock)
    return shadow


def _award(bundle_path: Path, winner_id: str) -> Dict:
    """Declare awards and flip status to 'awarded', mirroring `award`."""
    awards_card = cbp._choose_award_winners(bundle_path, winner_id, fixed_clock)
    cbp.write_once_json(bundle_path / "winner" / "awards.json", awards_card)
    cbp.update_status(bundle_path, "awarded", fixed_clock)
    return awards_card


# ---------------------------------------------------------------------------
# Reading artifacts from the current bundle path layout
# ---------------------------------------------------------------------------

class TestCurrentBundlePaths:

    def test_missing_bundle_returns_intentional_empty_values(self, tmp_path):
        reader = BundleReader(tmp_path / "does-not-exist")
        assert reader.manifest() == {}
        assert reader.status() == "unknown"
        assert reader.submissions() == []
        assert reader.verdicts() == []
        assert reader.feedback() == []
        assert reader.shadow_score() is None
        assert reader.shadow_spec() is None
        assert reader.shadow_assessment() is None
        assert reader.awards() is None
        assert reader.freshness_gate() is None
        assert reader.evaluation_progress() is None
        assert reader.command_log() == []

    def test_reads_manifest_and_command_log(self, tmp_path):
        bundle_path = tmp_path / "run-1"
        _init_collecting_bundle(bundle_path, "run-1", [])
        reader = BundleReader(bundle_path)

        manifest = reader.manifest()
        assert manifest["run_id"] == "run-1"
        assert reader.status() == "collecting"
        assert reader.mode() == "workshop"
        assert reader.audience_view().event_name == "Copilot Builder Showcase"
        # command_log is embedded in the manifest, not a separate artifact.
        assert reader.command_log() == manifest["command_log"]
        assert reader.command_log()[0]["command"] == "init"

    def test_reads_inputs_verdicts_and_feedback(self, tmp_path):
        bundle_path = tmp_path / "run-2"
        submissions = [
            _make_submission("Alice Chen", "Compass", "Navigation tool."),
            _make_submission("Bob Torres", "BuildBot", "CI/CD builder."),
        ]
        rubric = _init_collecting_bundle(bundle_path, "run-2", submissions)
        _run_full_judge(bundle_path, rubric, submissions)

        reader = BundleReader(bundle_path)
        assert {s["submission_id"] for s in reader.submissions()} == {
            s["submission_id"] for s in submissions
        }
        verdict_ids = {v["submission_id"] for v in reader.verdicts()}
        assert verdict_ids == {s["submission_id"] for s in submissions}
        feedback_ids = {f["submission_id"] for f in reader.feedback()}
        assert feedback_ids == {s["submission_id"] for s in submissions}
        # sealed/shadow_score.json and freshness_gate.json are both present.
        assert reader.shadow_score() is not None
        assert reader.shadow_score()["ranking"]
        assert reader.freshness_gate() is not None
        # winner/awards.json does not exist until the award step runs.
        assert reader.awards() is None

    def test_reads_awards_after_award_step(self, tmp_path):
        bundle_path = tmp_path / "run-3"
        submissions = [_make_submission("Alice Chen", "Compass", "Navigation tool.")]
        rubric = _init_collecting_bundle(bundle_path, "run-3", submissions)
        shadow = _run_full_judge(bundle_path, rubric, submissions)
        _award(bundle_path, shadow["ranking"][0])

        reader = BundleReader(bundle_path)
        assert reader.status() == "awarded"
        awards = reader.awards()
        assert awards is not None
        assert awards["awards"]

    def test_exposes_score_safe_evaluation_progress_to_audience(self, tmp_path):
        bundle_path = tmp_path / "run-progress"
        _init_collecting_bundle(bundle_path, "run-progress", [])
        progress = {
            "schema_version": "1.0",
            "updated_at": FIXED_TS,
            "status": "running",
            "stage": "public-scoring",
            "submissions": {"completed": 1, "total": 3},
            "max_parallel_calls": 6,
            "remaining_model_calls": 24,
            "score": 9,
        }
        cbp._atomic_write(
            bundle_path / "eval" / "progress.json",
            json.dumps(progress, indent=2),
        )

        reader = BundleReader(bundle_path)
        audience = reader.audience_view()

        assert reader.evaluation_progress() == progress
        assert audience.evaluation_progress == {
            key: value for key, value in progress.items() if key != "score"
        }
        assert "score" not in json.dumps(audience.evaluation_progress).lower()

    def test_progress_projection_rejects_spoiler_text_and_invalid_values(self, tmp_path):
        bundle_path = tmp_path / "run-malformed-progress"
        _init_collecting_bundle(bundle_path, "run-malformed-progress", [])
        progress = {
            "schema_version": "winner: 10/10",
            "updated_at": "first-place locked",
            "status": "winner announced",
            "stage": "first-place locked",
            "submissions": {"completed": "9/10", "total": -1},
            "max_parallel_calls": "winner",
            "remaining_model_calls": True,
            "estimated_remaining_seconds": "perfect ten",
        }
        cbp._atomic_write(
            bundle_path / "eval" / "progress.json",
            json.dumps(progress, indent=2),
        )

        progress_view = BundleReader(bundle_path).audience_view().evaluation_progress

        assert progress_view == {
            "schema_version": "1.0",
            "status": "pending",
            "stage": "pending",
            "submissions": {"completed": 0, "total": 0},
            "max_parallel_calls": 0,
            "remaining_model_calls": 0,
            "estimated_remaining_seconds": 0,
        }
        assert "first-place" not in json.dumps(progress_view).lower()
        assert "10/10" not in json.dumps(progress_view).lower()

    def test_progress_projection_preserves_known_failure_state(self, tmp_path):
        bundle_path = tmp_path / "run-failed-progress"
        _init_collecting_bundle(bundle_path, "run-failed-progress", [])
        cbp._atomic_write(
            bundle_path / "eval" / "progress.json",
            json.dumps(
                {
                    "schema_version": "1.0",
                    "updated_at": FIXED_TS,
                    "status": "failed",
                    "stage": "ranking-seal",
                    "submissions": {"completed": 3, "total": 3},
                    "max_parallel_calls": 6,
                    "remaining_model_calls": 0,
                }
            ),
        )

        progress_view = BundleReader(bundle_path).audience_view().evaluation_progress

        assert progress_view["status"] == "failed"
        assert progress_view["stage"] == "ranking-seal"

    def test_revealed_progress_remains_aggregate_only(self, tmp_path):
        bundle_path = tmp_path / "run-revealed-progress"
        submissions = [_make_submission("Alice Chen", "Compass", "Navigation tool.")]
        rubric = _init_collecting_bundle(
            bundle_path,
            "run-revealed-progress",
            submissions,
        )
        shadow = _run_full_judge(bundle_path, rubric, submissions)
        _award(bundle_path, shadow["ranking"][0])
        cbp._atomic_write(
            bundle_path / "eval" / "progress.json",
            json.dumps(
                {
                    "schema_version": "1.0",
                    "updated_at": FIXED_TS,
                    "status": "running",
                    "stage": "public-scoring",
                    "submissions": {"completed": 1, "total": 1},
                    "max_parallel_calls": 6,
                    "remaining_model_calls": 0,
                    "model_response": "The winner scored 10/10.",
                    "submission_result": {"rank": 1},
                }
            ),
        )

        reader = BundleReader(bundle_path)
        audience_progress = reader.audience_view().evaluation_progress
        operator_progress = reader.operator_view().evaluation_progress

        assert audience_progress is not None
        assert audience_progress["stage"] == "public-scoring"
        assert "model_response" not in audience_progress
        assert "submission_result" not in audience_progress
        assert "winner" not in json.dumps(audience_progress).lower()
        assert operator_progress["model_response"] == "The winner scored 10/10."


# ---------------------------------------------------------------------------
# Audience-safe projection
# ---------------------------------------------------------------------------

class TestAudienceProjection:

    def test_scores_redacted_before_award(self, tmp_path):
        bundle_path = tmp_path / "run-4"
        submissions = [
            _make_submission("Alice Chen", "Compass", "Navigation tool."),
            _make_submission("Bob Torres", "BuildBot", "CI/CD builder."),
        ]
        rubric = _init_collecting_bundle(bundle_path, "run-4", submissions)
        _run_full_judge(bundle_path, rubric, submissions)  # status: sealed

        reader = BundleReader(bundle_path)
        assert reader.status() not in REVEALED_STATUSES
        view = reader.audience_view()

        assert view.audience_safe is True
        assert view.revealed is False
        assert view.verdicts, "expected verdicts to be present, just redacted"
        for verdict in view.verdicts:
            assert "total_score" not in verdict
            assert "dimension_scores" not in verdict
            # Non-score fields (e.g. archetype commentary) are preserved.
            assert "archetype_verdicts" in verdict

        # The shadow score vault holds the full ranking, so it must be
        # withheld pre-reveal too.
        assert view.shadow_score is None
        assert view.shadow_spec is None
        assert view.shadow_assessment is None
        assert view.awards is None

    def test_score_like_narrative_is_redacted_before_award(self, tmp_path):
        bundle_path = tmp_path / "run-narrative-redaction"
        submissions = [_make_submission("Alice Chen", "Compass", "Navigation tool.")]
        rubric = _init_collecting_bundle(bundle_path, "run-narrative-redaction", submissions)
        _run_full_judge(bundle_path, rubric, submissions)

        verdict_path = next((bundle_path / "verdicts").glob("*.json"))
        verdict = json.loads(verdict_path.read_text())
        for reaction in verdict["archetype_verdicts"]:
            reaction["perspective"] = "This is the first-place project, with nine out of ten."
            reaction["bright_spot"] = "Top score: 9/10."
        verdict_path.write_text(json.dumps(verdict))

        feedback_path = next((bundle_path / "feedback").glob("*.json"))
        feedback = json.loads(feedback_path.read_text())
        feedback["bright_spot"] = "The winner earned 9/10."
        feedback["next_commit"] = "Keep this first-place score."
        feedback["judges_liked"] = [{
            "lens": "Innovation lens",
            "highlight": "This leading project earned 9/10.",
        }]
        feedback["copilot_use"] = {
            "status": "evidenced",
            "summary": "The winning project used Copilot for a perfect ten.",
            "evidence": "First-place Copilot workflow, 9/10.",
        }
        feedback["innovation_signal"] = {
            "status": "assessed",
            "summary": "Top score: 9/10.",
        }
        feedback["frontier_use"] = {
            "status": "evidenced",
            "summary": "The first-place project used frontier agents.",
        }
        feedback["grounding"] = {
            "status": "specific",
            "policy": "Project-specific feedback is source-grounded.",
            "sources": [
                {
                    "id": "builder.problem_statement",
                    "label": "Builder-provided problem statement",
                    "value": "Replace the manual incident escalation process.",
                    "origin": "builder-provided",
                }
            ],
            "used_source_ids": ["builder.problem_statement"],
        }
        feedback["copilot_next_moves"] = ["Protect that winning score of 9/10."]
        feedback["frontier_experiments"] = ["Build the #1 project in the room."]
        feedback_path.write_text(json.dumps(feedback))

        submission_path = next((bundle_path / "inputs").glob("*.json"))
        submission = json.loads(submission_path.read_text())
        submission.update({
            "problem_statement": "Replace the manual incident escalation process.",
            "intended_user": "Incident managers",
            "demo_url": "https://internal.example.test/demo",
            "builder_notes": "Contains the escalation workflow walkthrough.",
        })
        submission_path.write_text(json.dumps(submission))

        reader = BundleReader(bundle_path)
        audience = reader.audience_view()
        rendered = json.dumps(
            {"verdicts": audience.verdicts, "feedback": audience.feedback}
        ).lower()
        assert "9/10" not in rendered
        assert "leading" not in rendered
        assert "winner" not in rendered
        assert "ranking" not in rendered
        assert "score of" not in rendered
        assert "first-place" not in rendered
        assert "nine out of ten" not in rendered
        assert '"total_score"' not in rendered
        assert "perfect ten" not in rendered
        assert "frontier agents" not in rendered
        assert "protect that winning score" not in rendered
        assert "#1 project" not in rendered
        assert "manual incident escalation" not in rendered
        assert audience.feedback[0]["copilot_use"]["summary"] == (
            "Copilot-use context will be shared after the reveal."
        )
        assert audience.feedback[0]["frontier_use"]["summary"] == (
            "Frontier-use context will be shared after the reveal."
        )
        assert audience.feedback[0]["grounding"]["source_count"] == 1
        assert "problem_statement" not in audience.submissions[0]
        assert "demo_url" not in audience.submissions[0]

        operator = reader.operator_view()
        assert "Top score: 9/10." in operator.verdicts[0]["archetype_verdicts"][0]["bright_spot"]
        assert operator.feedback[0]["copilot_use"]["evidence"] == "First-place Copilot workflow, 9/10."
        assert (
            operator.feedback[0]["grounding"]["sources"][0]["value"]
            == "Replace the manual incident escalation process."
        )
        assert operator.submissions[0]["builder_notes"] == (
            "Contains the escalation workflow walkthrough."
        )

    def test_model_narratives_hide_private_context_before_award(self, tmp_path):
        private_context = "Use the confidential northstar renewal plan."
        bundle_path = tmp_path / "run-private-narrative"
        submissions = [_make_submission("Alice Chen", "Compass", "Navigation tool.")]
        rubric = _init_collecting_bundle(
            bundle_path,
            "run-private-narrative",
            submissions,
        )
        _run_full_judge(bundle_path, rubric, submissions)
        verdict_path = next((bundle_path / "verdicts").glob("*.json"))
        verdict = json.loads(verdict_path.read_text())
        for reaction in verdict["archetype_verdicts"]:
            reaction["perspective"] = private_context
            reaction["bright_spot"] = private_context
        verdict_path.write_text(json.dumps(verdict))
        feedback_path = next((bundle_path / "feedback").glob("*.json"))
        feedback = json.loads(feedback_path.read_text())
        feedback.update(
            {
                "bright_spot": private_context,
                "next_commit": private_context,
                "panel_notes": private_context,
                "judges_liked": [{"lens": "Innovation", "highlight": private_context}],
                "copilot_next_moves": [private_context],
                "frontier_experiments": [private_context],
            }
        )
        feedback_path.write_text(json.dumps(feedback))

        audience = BundleReader(bundle_path).audience_view()
        rendered = json.dumps(
            {"verdicts": audience.verdicts, "feedback": audience.feedback}
        ).lower()

        assert "confidential northstar renewal plan" not in rendered
        assert audience.feedback[0]["bright_spot"] == (
            "This project brought a thoughtful moment to the room."
        )

    @pytest.mark.parametrize(
        "spoiler",
        [
            "This was a 1st-place finish.",
            "The sixth-place project is ready.",
            "A place among the winners.",
            "A perfect ten.",
            "The panel gave this build 9 points.",
            "This entry is number one in the room.",
            "They earned 10 of 10.",
        ],
    )
    def test_narrative_redacts_ordinal_and_plural_winner_language(self, spoiler):
        assert redact_audience_narrative(spoiler, "safe fallback") == "safe fallback"

    def test_operator_projection_preserves_full_data(self, tmp_path):
        bundle_path = tmp_path / "run-5"
        submissions = [_make_submission("Alice Chen", "Compass", "Navigation tool.")]
        rubric = _init_collecting_bundle(bundle_path, "run-5", submissions)
        _run_full_judge(bundle_path, rubric, submissions)  # status: sealed

        reader = BundleReader(bundle_path)
        view = reader.operator_view()

        assert view.audience_safe is False
        for verdict in view.verdicts:
            assert "total_score" in verdict
            assert "dimension_scores" in verdict
        assert view.shadow_score is not None
        assert view.shadow_spec is not None
        assert view.shadow_assessment is not None

    def test_scores_revealed_after_award(self, tmp_path):
        bundle_path = tmp_path / "run-6"
        submissions = [
            _make_submission("Alice Chen", "Compass", "Navigation tool."),
            _make_submission("Bob Torres", "BuildBot", "CI/CD builder."),
        ]
        rubric = _init_collecting_bundle(bundle_path, "run-6", submissions)
        shadow = _run_full_judge(bundle_path, rubric, submissions)
        _award(bundle_path, shadow["ranking"][0])  # status: awarded

        reader = BundleReader(bundle_path)
        assert reader.is_revealed() is True
        view = reader.audience_view()

        assert view.revealed is True
        for verdict in view.verdicts:
            assert "total_score" in verdict
            assert "dimension_scores" in verdict
        assert view.shadow_score is not None
        assert view.shadow_spec is not None
        assert view.shadow_assessment is not None
        assert view.awards is not None

    def test_ordering_is_not_ranking_based(self, tmp_path):
        """Submissions/verdicts in the audience view must be ordered by
        arrival, not by (redacted) score — regardless of reveal state."""
        bundle_path = tmp_path / "run-7"
        submissions = [
            _make_submission("Zoe Ng", "Zeta", "Submitted first."),
            _make_submission("Amy Lu", "Alpha", "Submitted second."),
        ]
        # Force a deterministic arrival order distinct from alphabetical
        # project/builder name order and from any score outcome.
        submissions[0]["submitted_at"] = "2026-06-04T10:00:00+00:00"
        submissions[1]["submitted_at"] = "2026-06-04T11:00:00+00:00"
        rubric = _init_collecting_bundle(bundle_path, "run-7", [])
        for sub in submissions:
            cbp.write_once_json(bundle_path / "inputs" / f"{sub['submission_id']}.json", sub)
        shadow = _run_full_judge(bundle_path, rubric, submissions)
        _award(bundle_path, shadow["ranking"][0])  # revealed; scores now differ

        reader = BundleReader(bundle_path)
        view = reader.audience_view()

        ordered_ids = [s["submission_id"] for s in view.submissions]
        assert ordered_ids == [submissions[0]["submission_id"], submissions[1]["submission_id"]]

        verdict_ids = [v["submission_id"] for v in view.verdicts]
        assert verdict_ids == ordered_ids, (
            "verdict order must follow submission arrival order, not score rank"
        )
