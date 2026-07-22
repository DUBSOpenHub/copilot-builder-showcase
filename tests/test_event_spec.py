"""Regression coverage for neutral events and projector-safe bundle behavior."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import hackathon_judge as cbp
from event_spec import DEFAULT_EVENT_SPEC


def fixed_clock() -> datetime:
    return datetime(2026, 6, 4, 22, 44, 37, tzinfo=timezone.utc)


def make_bundle(tmp_path: Path, run_id: str = "event-test") -> Path:
    bundle_path = tmp_path / run_id
    cbp.init_bundle(run_id, "workshop", copy.deepcopy(cbp.DEFAULT_RUBRIC), bundle_path, fixed_clock)
    return bundle_path


def add_submission(bundle_path: Path) -> str:
    submission_id = str(uuid.uuid4())
    cbp.write_once_json(
        bundle_path / "inputs" / f"{submission_id}.json",
        {
            "submission_id": submission_id,
            "builder_name": "Participant",
            "project_name": "Project Aurora",
            "description": "A complete project submission.",
            "artifacts": [],
            "submitted_at": fixed_clock().isoformat(),
            "file_size_bytes": 0,
        },
    )
    cbp.update_status(bundle_path, "collecting", fixed_clock)
    return submission_id


def seal_submission(bundle_path: Path) -> str:
    submission_id = add_submission(bundle_path)
    rubric = cbp.load_rubric(bundle_path)
    submissions = cbp._load_submissions(bundle_path)
    gate = cbp.run_freshness_gate(bundle_path, rubric, None, fixed_clock)
    selected_models = gate["selected_models"]
    shadow_spec = cbp.generate_shadow_spec(
        bundle_path, rubric, selected_models, None, fixed_clock
    )
    scored = cbp.score_submissions(
        submissions, rubric, selected_models, bundle_path, None, fixed_clock
    )
    cbp.assess_shadow_spec(
        scored,
        submissions,
        shadow_spec,
        selected_models,
        bundle_path,
        None,
        fixed_clock,
    )
    cbp.seal_shadow_score(bundle_path, cbp.compute_shadow_score(scored, rubric, fixed_clock), fixed_clock)
    cbp.build_panel_verdicts(
        scored, submissions, rubric, selected_models, bundle_path, None, fixed_clock
    )
    cbp.build_feedback_cards(
        scored, submissions, rubric, selected_models, bundle_path, None, fixed_clock
    )
    cbp.update_status(bundle_path, "sealed", fixed_clock)
    return submission_id


def test_event_spec_is_resolved_and_snapshotted(tmp_path: Path):
    event = {
        "event": {
            "name": "Accessibility Hackday",
            "tagline": "Build access for everyone.",
        },
        "awards": [
            {
                "id": "impact",
                "name": "Accessibility Impact Award",
                "emoji": "*",
                "tagline": "For a project that removes a real barrier.",
                "dimensions": ["impact"],
                "reason": "This project made access meaningfully easier.",
            },
            {
                "id": "grand",
                "name": "Accessibility Hackday Grand Prize",
                "emoji": "#",
                "tagline": "For the strongest overall result.",
                "dimensions": [],
                "reason": "This project best met the event goals.",
            },
        ],
    }
    bundle_path = tmp_path / "accessibility-hackday"

    cbp.init_bundle(
        "accessibility-hackday",
        "workshop",
        copy.deepcopy(cbp.DEFAULT_RUBRIC),
        bundle_path,
        fixed_clock,
        event,
    )

    snapshot = cbp.load_event_spec(bundle_path)
    manifest = cbp.load_manifest(bundle_path)
    rubric = cbp.load_rubric(bundle_path)
    assert snapshot["event"]["name"] == "Accessibility Hackday"
    assert [award["name"] for award in snapshot["awards"]] == [
        "Accessibility Impact Award",
        "Accessibility Hackday Grand Prize",
    ]
    assert manifest["event"]["name"] == "Accessibility Hackday"
    assert rubric["judge_archetypes"] == snapshot["review_lenses"]


def test_legacy_rubric_bundle_has_a_neutral_event_adapter(tmp_path: Path):
    bundle_path = make_bundle(tmp_path)
    (bundle_path / "config" / "event.json").unlink()

    event = cbp.load_event_spec(bundle_path)

    assert event["event"]["name"] == DEFAULT_EVENT_SPEC["event"]["name"]
    assert event["rubric"] == cbp.load_rubric(bundle_path)["rubric"]


def test_legacy_rubric_preserves_declared_tie_policy(tmp_path: Path):
    legacy = copy.deepcopy(cbp.DEFAULT_RUBRIC)
    legacy["tie_policy"] = {
        "mode": "human-resolution",
        "tiebreaker_dimensions": [],
    }
    bundle_path = tmp_path / "legacy-human-tie"

    cbp.init_bundle("legacy-human-tie", "workshop", legacy, bundle_path, fixed_clock)

    assert cbp.load_event_spec(bundle_path)["tie_policy"] == legacy["tie_policy"]


def test_event_spec_rejects_invalid_podium_rank(tmp_path: Path):
    event = copy.deepcopy(DEFAULT_EVENT_SPEC)
    event["awards"][0]["rank"] = 0

    with pytest.raises(cbp.ConfigValidationError):
        cbp.init_bundle(
            "invalid-podium",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            tmp_path / "invalid-podium",
            fixed_clock,
            event,
        )


def test_event_spec_rejects_shadow_ranking_influence(tmp_path: Path):
    event = copy.deepcopy(DEFAULT_EVENT_SPEC)
    event["shadow_spec"]["ranking_effect"] = "weighted"

    with pytest.raises(cbp.ConfigValidationError):
        cbp.init_bundle(
            "invalid-shadow-policy",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            tmp_path / "invalid-shadow-policy",
            fixed_clock,
            event,
        )


@pytest.mark.parametrize(
    ("mode", "dimensions"),
    [
        ("unknown", []),
        ("sealed-tiebreaker", []),
        ("shared-podium", ["impact"]),
        ("human-resolution", ["impact"]),
        ("sealed-tiebreaker", ["missing-dimension"]),
    ],
)
def test_event_spec_rejects_invalid_tie_policy(
    tmp_path: Path, mode: str, dimensions: list[str]
):
    event = copy.deepcopy(DEFAULT_EVENT_SPEC)
    event["tie_policy"] = {
        "mode": mode,
        "tiebreaker_dimensions": dimensions,
    }

    with pytest.raises(cbp.ConfigValidationError):
        cbp.init_bundle(
            "invalid-tie-policy",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            tmp_path / "invalid-tie-policy",
            fixed_clock,
            event,
        )


def test_legacy_model_policy_receives_consensus_defaults(tmp_path: Path):
    legacy = copy.deepcopy(cbp.DEFAULT_RUBRIC)
    legacy["freshness_gate"] = {
        "policy_mode": "strict",
        "preferred_model": "claude-opus-4.8",
        "required_tier": "premium",
        "required_reasoning": "high",
    }

    bundle_path = tmp_path / "legacy-policy"
    cbp.init_bundle("legacy-policy", "workshop", legacy, bundle_path, fixed_clock)

    event = cbp.load_event_spec(bundle_path)
    assert event["model_policy"]["minimum_panel_size"] == 3
    assert event["model_policy"]["panel_models"][0] == "claude-opus-4.8"


def test_event_spec_rejects_unachievable_provider_diversity(tmp_path: Path):
    event = copy.deepcopy(DEFAULT_EVENT_SPEC)
    event["model_policy"].update(
        {
            "panel_models": ["gpt-5.6-terra", "gpt-5.4", "gpt-5-mini"],
            "minimum_panel_size": 3,
            "minimum_distinct_providers": 3,
        }
    )

    with pytest.raises(cbp.ConfigValidationError):
        cbp.init_bundle(
            "invalid-provider-diversity",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            tmp_path / "invalid-provider-diversity",
            fixed_clock,
            event,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_parallel_calls", 0),
        ("max_parallel_calls", 33),
        ("max_parallel_calls", True),
        ("live_time_budget_seconds", 29),
        ("live_time_budget_seconds", 3601),
        ("live_time_budget_seconds", True),
        ("live_time_budget_policy", "drop-slowest"),
    ],
)
def test_event_spec_rejects_unsafe_live_panel_policy(
    tmp_path: Path, field: str, value: object
):
    event = copy.deepcopy(DEFAULT_EVENT_SPEC)
    event["model_policy"][field] = value

    with pytest.raises(cbp.ConfigValidationError):
        cbp.init_bundle(
            "invalid-live-policy",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            tmp_path / "invalid-live-policy",
            fixed_clock,
            event,
        )


def test_freshness_gate_marks_synthetic_evaluation(tmp_path: Path):
    bundle_path = make_bundle(tmp_path)

    result = cbp.run_freshness_gate(bundle_path, cbp.load_rubric(bundle_path), None, fixed_clock)

    assert result["evaluation_provenance"]["mode"] == "simulated"
    assert "deterministic synthetic" in result["evaluation_provenance"]["detail"]


def test_audience_presenter_hides_scores_until_awards(tmp_path: Path):
    run_id = "safe-audience"
    bundle_path = make_bundle(tmp_path, run_id)
    submission_id = seal_submission(bundle_path)
    private_context = "The confidential northstar renewal plan."
    verdict_path = next((bundle_path / "verdicts").glob("*.json"))
    verdict = json.loads(verdict_path.read_text())
    for reaction in verdict["archetype_verdicts"]:
        reaction["bright_spot"] = "Leading the ranking at 9/10."
        reaction["perspective"] = private_context
    verdict_path.write_text(json.dumps(verdict))
    feedback_path = next((bundle_path / "feedback").glob("*.json"))
    feedback = json.loads(feedback_path.read_text())
    feedback["bright_spot"] = "The winner scored 9/10."
    feedback["next_commit"] = private_context
    feedback["copilot_use"] = {
        "status": "evidenced",
        "summary": private_context,
    }
    feedback_path.write_text(json.dumps(feedback))

    args = argparse.Namespace(run_id=run_id, showtime=False, projector=True, operator=False)

    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_present(args, None, fixed_clock) == 0
    audience_output = output.getvalue().lower()
    assert "score:" not in audience_output
    assert "9/10" not in audience_output
    assert "leading" not in audience_output
    assert "winner" not in audience_output
    assert "confidential northstar renewal plan" not in audience_output

    award_args = argparse.Namespace(
        run_id=run_id,
        winner=submission_id,
        showtime=False,
        no_suspense=True,
    )
    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        assert cbp.cmd_award(award_args, None, fixed_clock) == 0

    args.operator = True
    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_present(args, None, fixed_clock) == 0
    assert "Score:" in output.getvalue()


def test_replay_hides_score_like_narrative_before_award(tmp_path: Path):
    run_id = "safe-replay"
    bundle_path = make_bundle(tmp_path, run_id)
    seal_submission(bundle_path)
    verdict_path = next((bundle_path / "verdicts").glob("*.json"))
    verdict = json.loads(verdict_path.read_text())
    for reaction in verdict["archetype_verdicts"]:
        reaction["bright_spot"] = "This is the first-place project with nine out of ten."
    verdict_path.write_text(json.dumps(verdict))
    feedback_path = next((bundle_path / "feedback").glob("*.json"))
    feedback = json.loads(feedback_path.read_text())
    feedback["bright_spot"] = "Leading the ranking at 9/10."
    feedback["next_commit"] = "Protect that winning score."
    feedback_path.write_text(json.dumps(feedback))
    args = argparse.Namespace(bundle=run_id, showtime=False)

    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_replay(args, None, fixed_clock) == 0

    replay_output = output.getvalue().lower()
    assert "9/10" not in replay_output
    assert "first-place" not in replay_output
    assert "nine out of ten" not in replay_output
    assert "leading" not in replay_output
    assert "winning score" not in replay_output


def test_present_and_replay_hide_partial_award_artifacts(tmp_path: Path):
    run_id = "partial-award"
    bundle_path = make_bundle(tmp_path, run_id)
    submission_id = seal_submission(bundle_path)
    cbp.write_once_json(
        bundle_path / "winner" / "card.json",
        {
            "winner_submission_id": submission_id,
            "winner_builder_name": "Premature Winner",
            "award_name": "Premature Prize",
        },
    )
    cbp.write_once_json(
        bundle_path / "winner" / "awards.json",
        {
            "awards": [
                {
                    "award_name": "Premature Prize",
                    "winner_builder_name": "Premature Winner",
                    "project_name": "Hidden Project",
                }
            ]
        },
    )

    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_present(
                argparse.Namespace(
                    run_id=run_id,
                    showtime=False,
                    projector=True,
                    operator=False,
                ),
                None,
                fixed_clock,
            ) == 0
    assert "Premature Prize" not in output.getvalue()
    assert "Premature Winner" not in output.getvalue()

    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_replay(
                argparse.Namespace(bundle=run_id, showtime=False),
                None,
                fixed_clock,
            ) == 0
    assert "Premature Prize" not in output.getvalue()
    assert "Premature Winner" not in output.getvalue()


def test_run_id_cannot_escape_runs_directory(tmp_path: Path):
    with pytest.raises(cbp.ConfigValidationError):
        cbp.get_bundle_path("../outside", tmp_path)
    with pytest.raises(cbp.ConfigValidationError):
        cbp.get_bundle_path("/tmp/outside", tmp_path)


def test_replay_extraction_rejects_path_traversal(tmp_path: Path):
    archive_path = tmp_path / "unsafe.bundle.tar.gz"
    member = tarfile.TarInfo("../outside.txt")
    payload = b"unsafe"
    member.size = len(payload)

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    destination = tmp_path / "extract"
    destination.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        with pytest.raises(cbp.ConfigValidationError):
            cbp._safe_extract_tar(archive, destination)
    assert not (tmp_path / "outside.txt").exists()
