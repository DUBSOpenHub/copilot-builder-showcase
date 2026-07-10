"""Regression coverage for neutral events and projector-safe bundle behavior."""

from __future__ import annotations

import argparse
import copy
import io
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
    scored = cbp.score_submissions(
        submissions, rubric, gate["selected_model"], bundle_path, None, fixed_clock
    )
    cbp.seal_shadow_score(bundle_path, cbp.compute_shadow_score(scored, rubric, fixed_clock), fixed_clock)
    cbp.build_panel_verdicts(
        scored, submissions, rubric, gate["selected_model"], bundle_path, None, fixed_clock
    )
    cbp.build_feedback_cards(
        scored, submissions, rubric, gate["selected_model"], bundle_path, None, fixed_clock
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


def test_freshness_gate_marks_synthetic_evaluation(tmp_path: Path):
    bundle_path = make_bundle(tmp_path)

    result = cbp.run_freshness_gate(bundle_path, cbp.load_rubric(bundle_path), None, fixed_clock)

    assert result["evaluation_provenance"]["mode"] == "simulated"
    assert "deterministic synthetic" in result["evaluation_provenance"]["detail"]


def test_audience_presenter_hides_scores_until_awards(tmp_path: Path):
    run_id = "safe-audience"
    bundle_path = make_bundle(tmp_path, run_id)
    submission_id = seal_submission(bundle_path)
    args = argparse.Namespace(run_id=run_id, showtime=False, projector=True, operator=False)

    with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
        with patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            assert cbp.cmd_present(args, None, fixed_clock) == 0
    assert "Score:" not in output.getvalue()

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
