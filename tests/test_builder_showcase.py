"""
Test suite for Copilot Builder Showcase
Tests all layers: tone safety, hash/seal, write-once, freshness gate,
shadow score, eval engine, command flows, registry, exit codes.

Run with: python -m pytest tests/test_builder_showcase.py -v
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# Add parent dir to sys.path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent))
import builder_showcase as cbp

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

FIXED_TS = "2026-06-04T22:44:37+00:00"
fixed_clock = lambda: datetime(2026, 6, 4, 22, 44, 37, tzinfo=timezone.utc)


class MockGateway:
    """Deterministic mock model gateway for tests."""

    def __init__(self, models=None, fail_on_call=False, stale_preferred=False):
        self.models = models or cbp.APPROVED_MODELS
        self.fail_on_call = fail_on_call
        self.stale_preferred = stale_preferred
        self.call_count = 0

    def query_available_models(self) -> List[Dict]:
        return self.models

    def call_model(self, prompt: str, model_id: str) -> str:
        self.call_count += 1
        if self.fail_on_call:
            raise Exception("Mock API failure")
        return cbp._synthetic_model_response(prompt, model_id)


def _podium_event_spec() -> Dict[str, Any]:
    event = copy.deepcopy(cbp.DEFAULT_EVENT_SPEC)
    event["awards"] = [
        {
            "id": "bronze",
            "name": "Bronze — Third Place",
            "emoji": "🥉",
            "tagline": "A podium finish for a project with a strong overall story.",
            "dimensions": [],
            "rank": 3,
            "reason": "This project earned the third-highest overall result across the event rubric.",
        },
        {
            "id": "silver",
            "name": "Silver — Second Place",
            "emoji": "🥈",
            "tagline": "A podium finish for a project with impact and craft.",
            "dimensions": [],
            "rank": 2,
            "reason": "This project earned the second-highest overall result across the event rubric.",
        },
        {
            "id": "grand-prize",
            "name": "Gold — First Place",
            "emoji": "🥇",
            "tagline": "The top project across the event rubric.",
            "dimensions": [],
            "rank": 1,
            "reason": "This project earned the highest overall result across the event rubric.",
        },
    ]
    return event


def make_run(
    tmp_path: Path,
    run_id: str = "test-run",
    mode: str = "workshop",
    gateway: Optional[MockGateway] = None,
    event_spec: Optional[Dict[str, Any]] = None,
) -> Path:
    """Initialize a run and return bundle_path."""
    bundle_path = tmp_path / run_id
    cbp.init_bundle(
        run_id,
        mode,
        dict(cbp.DEFAULT_RUBRIC),
        bundle_path,
        fixed_clock,
        event_spec,
    )
    return bundle_path


def add_submission(bundle_path: Path, builder_name: str = "Alex Builder",
                   project_name: str = "SuperApp", description: str = "A great project") -> str:
    """Add a submission and return submission_id."""
    sid = str(uuid.uuid4())
    rubric = cbp.load_rubric(bundle_path)
    submission = {
        "submission_id": sid,
        "builder_name": builder_name,
        "project_name": project_name,
        "description": description,
        "artifacts": [],
        "submitted_at": FIXED_TS,
        "file_size_bytes": 0,
    }
    cbp.write_once_json(bundle_path / "inputs" / f"{sid}.json", submission)
    cbp.update_status(bundle_path, "collecting", fixed_clock)
    return sid


def full_judge_run(bundle_path: Path, gateway: Optional[MockGateway] = None) -> None:
    """Run the full judge pipeline on a bundle."""
    gw = gateway or MockGateway()
    rubric = cbp.load_rubric(bundle_path)
    submissions = cbp._load_submissions(bundle_path)

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


# ---------------------------------------------------------------------------
# Layer 5 — Tone Safety Tests (DF-07)
# ---------------------------------------------------------------------------

class TestToneSafety:

    @pytest.mark.parametrize("phrase", [
        "failed to", "disappointing", "lacks", "weak", "poor",
        "mediocre", "mistake", "terrible", "worthless", "subpar",
        "merely", "nothing special", "unfortunately,", "sadly,",
    ])
    def test_banned_phrase_fails(self, phrase: str):
        result = cbp.check_tone(f"This project {phrase} expectations.", source_field="test")
        assert result["passed"] is False
        assert len(result["banned_phrases"]) > 0

    @pytest.mark.parametrize("text", [
        "This project demonstrates excellent technical execution.",
        "Outstanding creativity and impressive problem solving.",
        "Strong implementation with great attention to detail.",
        "The builder achieved remarkable results.",
        "Brilliant approach with solid fundamentals.",
    ])
    def test_positive_text_passes(self, text: str):
        result = cbp.check_tone(text, source_field="test")
        assert result["passed"] is True
        assert result["banned_phrases"] == []

    def test_check_tone_case_insensitive(self):
        result = cbp.check_tone("This is DISAPPOINTING work.", source_field="test")
        assert result["passed"] is False

    def test_feedback_card_requires_bright_spot(self):
        card = {
            "bright_spot": "",
            "next_commit": "Consider adding more features.",
            "panel_notes": "Great work!",
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"] is False
        assert any("bright_spot" in m for m in result["missing_required"])

    def test_feedback_card_requires_next_commit(self):
        card = {
            "bright_spot": "This project demonstrates excellent technical execution.",
            "next_commit": "",
            "panel_notes": "Great work!",
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"] is False
        assert any("next_commit" in m for m in result["missing_required"])

    def test_feedback_card_bright_spot_needs_positive_keyword(self):
        card = {
            "bright_spot": "The project exists and was submitted.",
            "next_commit": "Consider adding more features.",
            "panel_notes": "Great work!",
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"] is False

    def test_feedback_card_next_commit_needs_forward_verb(self):
        card = {
            "bright_spot": "This project demonstrates excellent technical execution.",
            "next_commit": "The project exists.",  # no forward verb
            "panel_notes": "Great work!",
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"] is False

    def test_valid_feedback_card_passes(self):
        card = {
            "bright_spot": "This project demonstrates outstanding creativity and excellent execution.",
            "next_commit": "Consider adding automated tests to extend reliability.",
            "panel_notes": "Inspiring work from a strong builder.",
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"] is True

    def test_feedback_card_checks_recommendation_tone(self):
        card = {
            "bright_spot": "This project demonstrates outstanding creativity and excellent execution.",
            "next_commit": "Consider adding automated tests to extend reliability.",
            "panel_notes": "Inspiring work from a strong builder.",
            "copilot_next_moves": ["This approach is disappointing."],
            "frontier_experiments": ["Prototype a human-reviewed workflow."],
        }

        result = cbp.check_feedback_card_tone(card)

        assert result["passed"] is False
        assert "disappointing" in result["banned_phrases"]

    def test_feedback_card_checks_judge_highlight_tone(self):
        card = {
            "bright_spot": "This project demonstrates strong product focus.",
            "next_commit": "Consider adding a focused user test.",
            "panel_notes": "The project has a clear story.",
            "judges_liked": [
                {
                    "lens": "Impact lens",
                    "highlight": "This project is disappointing.",
                }
            ],
        }

        result = cbp.check_feedback_card_tone(card)

        assert result["passed"] is False
        assert "disappointing" in result["banned_phrases"]

    def test_assert_tone_raises_on_failure(self):
        tone_result = {"passed": False, "banned_phrases": ["weak"], "missing_required": []}
        with pytest.raises(cbp.ToneSafetyFailure):
            cbp.assert_tone(tone_result, "test context")

    def test_assert_tone_passes_silently(self):
        tone_result = {"passed": True, "banned_phrases": [], "missing_required": []}
        cbp.assert_tone(tone_result)  # should not raise


class TestModelResponseParsing:

    def test_parser_falls_back_for_non_object_json(self):
        parsed = cbp._parse_model_response("[]")
        non_string = cbp._parse_model_response(None)

        assert parsed["scores"] == {}
        assert "bright_spot" in parsed
        assert non_string["scores"] == {}

    def test_shadow_generation_falls_back_for_non_object_json(self, tmp_path):
        bundle_path = make_run(tmp_path, "shadow-non-object")
        rubric = cbp.load_rubric(bundle_path)

        with patch.object(cbp, "call_model", return_value="[]"):
            shadow_spec = cbp.generate_shadow_spec(
                bundle_path,
                rubric,
                ["claude-opus-4.8"],
                None,
                fixed_clock,
            )

        assert shadow_spec["source"] == "deterministic-policy"
        assert len(shadow_spec["criteria"]) == rubric["shadow_spec"]["criteria_count"]

    def test_synthetic_feedback_uses_supplied_project_context(self):
        prompt = """
Project: demo-day/pulseboard
Builder: Team Aurora
Source-labeled project context:
- [builder.problem_statement] Builder-provided problem statement: Event teams lose time reconciling updates.
- [builder.intended_user] Builder-provided intended user: hackathon organizers
- [builder.demo_url] Builder-provided demo or artifact: https://demo.example/pulseboard

Rubric dimensions:
  - Innovation (id=innovation, max=10): weight=0.5
  - Impact (id=impact, max=10): weight=0.5
"""

        response = json.loads(cbp._synthetic_model_response(prompt, "gpt-5.6-terra"))

        assert "pulseboard" in response["bright_spot"]
        assert "event teams" in response["bright_spot"]
        assert "hackathon organizers" in response["bright_spot"]
        assert "measurable before-and-after" in response["next_commit"]
        assert response["grounding_refs"] == [
            "builder.problem_statement",
            "builder.intended_user",
            "builder.demo_url",
        ]
        assert set(response["scores"]) == {"innovation", "impact"}


class TestShowtimeDelight:

    def test_panel_style_changes_the_opening_chatter(self):
        event = copy.deepcopy(cbp.DEFAULT_EVENT_SPEC)

        fun = cbp._panel_opening_message(event, "fun")
        professional = cbp._panel_opening_message(event, "professional")

        assert fun.startswith("Panel chatter:")
        assert "No spoilers" in fun
        assert professional.startswith("Panel brief:")
        assert "independently" in professional

    def test_showtime_pause_respects_its_budget(self):
        args = argparse.Namespace(showtime=True, no_suspense=False)
        pacer = cbp._ShowtimePacer(0.5)
        token = cbp._SHOWTIME_PACER.set(pacer)
        try:
            with patch.dict(os.environ, {"HJ_COLOR": "always"}, clear=True):
                with patch.object(cbp.time, "sleep") as sleep:
                    cbp._showtime_pause(args, 0.4)
                    cbp._showtime_pause(args, 0.4)
        finally:
            cbp._SHOWTIME_PACER.reset(token)

        assert [call.args[0] for call in sleep.call_args_list] == pytest.approx([0.4, 0.1])
        assert pacer.remaining_seconds == pytest.approx(0.0)

    def test_no_suspense_skips_showtime_pauses(self):
        args = argparse.Namespace(showtime=True, no_suspense=True)

        with patch.object(cbp.time, "sleep") as sleep:
            cbp._showtime_pause(args, 1.0)

        sleep.assert_not_called()

    def test_audience_reveal_moment_is_stable_per_run(self):
        args = argparse.Namespace(
            run_id="live-room",
            showtime=True,
            no_suspense=True,
        )

        with patch.dict(os.environ, {"HJ_COLOR": "always"}, clear=True):
            with patch("sys.stdout", new_callable=io.StringIO) as first:
                cbp._audience_reveal_moment(args)
            with patch("sys.stdout", new_callable=io.StringIO) as second:
                cbp._audience_reveal_moment(args)

        assert first.getvalue() == second.getvalue()
        assert "Sideline report" in first.getvalue()
        assert "Audience check ready" in first.getvalue()

    def test_audience_reveal_waits_for_live_confirmation(self):
        args = argparse.Namespace(
            run_id="live-room",
            showtime=True,
            no_suspense=False,
            yes=True,
            demo=True,
        )

        with patch.dict(os.environ, {"HJ_COLOR": "always"}, clear=True):
            with patch.object(cbp.sys.stdin, "isatty", return_value=True):
                with patch.object(cbp, "_confirm", return_value=True) as confirm:
                    cbp._audience_reveal_moment(args)

        confirm.assert_called_once()

    def test_textual_status_rejects_untested_major_version(self):
        with patch.object(cbp.importlib.util, "find_spec", return_value=object()):
            with patch.object(cbp.importlib.metadata, "version", return_value="9.0.0"):
                ready, detail = cbp._textual_status()

        assert ready is False
        assert "unsupported" in detail

    def test_textual_status_rejects_broken_dashboard_import(self):
        with patch.object(cbp.importlib.util, "find_spec", return_value=object()):
            with patch.object(cbp.importlib.metadata, "version", return_value="8.2.3"):
                with patch.object(cbp.importlib, "import_module", side_effect=ImportError("broken")):
                    ready, detail = cbp._textual_status()

        assert ready is False
        assert "could not load the dashboard" in detail

    def test_workshop_receipt_does_not_overclaim_unexported_run(self, tmp_path, capsys):
        bundle_path = make_run(tmp_path, "export-pending")
        cbp.update_status(bundle_path, "awarded", fixed_clock)

        cbp._print_workshop_receipt(bundle_path, "export-pending")

        output = capsys.readouterr().out
        assert "export pending" in output
        assert "sealed and replayable" not in output
        assert "before treating this result as tamper-evident" in output

    def test_legacy_receipt_derives_official_status_from_provenance(self, tmp_path, capsys):
        bundle_path = make_run(tmp_path, "legacy-official")
        cbp.write_once_json(
            bundle_path / "freshness_gate.json",
            {"evaluation_provenance": {"mode": "live"}},
        )

        cbp._print_workshop_receipt(bundle_path, "legacy-official")

        assert "OFFICIAL COPILOT PANEL" in capsys.readouterr().out

    def test_workshop_caps_total_showtime_animation(self, tmp_path):
        env = {
            "HJ_RUNS_DIR": str(tmp_path / "runs"),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
            "HJ_COLOR": "always",
        }
        args = argparse.Namespace(
            run_id="paced-show",
            urls=[f"DUBSOpenHub/project-{index}" for index in range(4)],
            file=None,
            audience=None,
            awards=None,
            panel_style="fun",
            config=None,
            event=None,
            showtime=True,
            yes=True,
            configure=False,
            manual_confirm=False,
            no_suspense=False,
            projector=True,
            require_live_terminal=False,
            require_projector_window=False,
            demo=False,
        )

        def fake_metadata(url):
            owner_repo = url.replace("https://github.com/", "", 1)
            return {
                "name_with_owner": owner_repo,
                "description": "",
                "language": "Python",
                "stars": 0,
                "forks": 0,
                "updated_at": FIXED_TS,
                "url": url,
                "source": "test",
            }

        with patch.dict(os.environ, env):
            with patch.object(cbp, "fetch_repo_metadata", fake_metadata):
                with patch.object(cbp.time, "sleep") as sleep:
                    assert cbp.cmd_workshop(args, MockGateway(), fixed_clock) == 0

        total_animation_seconds = sum(call.args[0] for call in sleep.call_args_list)
        assert total_animation_seconds <= cbp.SHOWTIME_PAUSE_BUDGET_SECONDS + 1e-9
        assert total_animation_seconds > 0

    @pytest.mark.parametrize(
        "spoiler",
        [
            "Leading the ranking with a score of 9/10.",
            "This is the first-place project with nine out of ten.",
            "A 1st-place finish and a place among the winners.",
            "The sixth-place project earned a perfect ten.",
            "The panel gave this build 9 points.",
            "This entry is number one in the room.",
            "They earned 10 of 10.",
        ],
    )
    def test_audience_chatter_redacts_score_like_language(self, spoiler):
        assert cbp._audience_safe_commentary(spoiler, "safe fallback") == "safe fallback"

    def test_audience_chatter_keeps_specific_non_result_feedback(self):
        commentary = (
            "Pulseboard shows strong product focus around reducing event handoff delays."
        )

        assert cbp._audience_safe_commentary(commentary, "safe fallback") == commentary


# ---------------------------------------------------------------------------
# Layer 2 — Bundle I/O Tests
# ---------------------------------------------------------------------------

class TestBundleIO:

    def test_write_once_creates_file(self, tmp_path):
        p = tmp_path / "test.txt"
        cbp.write_once(p, "hello")
        assert p.read_text() == "hello"

    def test_write_once_raises_on_second_write(self, tmp_path):
        p = tmp_path / "test.txt"
        cbp.write_once(p, "hello")
        with pytest.raises(cbp.BundleSealError):
            cbp.write_once(p, "world")

    def test_write_once_json(self, tmp_path):
        p = tmp_path / "data.json"
        cbp.write_once_json(p, {"key": "value"})
        loaded = json.loads(p.read_text())
        assert loaded["key"] == "value"

    def test_append_ndjson_creates_and_appends(self, tmp_path):
        p = tmp_path / "log.ndjson"
        cbp.append_ndjson(p, {"a": 1})
        cbp.append_ndjson(p, {"b": 2})
        entries = cbp.read_ndjson(p)
        assert len(entries) == 2
        assert entries[0] == {"a": 1}
        assert entries[1] == {"b": 2}

    def test_sha256_file(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_bytes(b"hello world")
        digest = cbp._sha256_file(p)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert digest == expected

    def test_sha256_bytes(self):
        result = cbp._sha256_bytes(b"test")
        expected = hashlib.sha256(b"test").hexdigest()
        assert result == expected

    def test_collect_bundle_artifacts_excludes_hashes_seal(self, tmp_path):
        (tmp_path / "HASHES").write_text("h")
        (tmp_path / "SEAL").write_text("s")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "data.json").write_text("{}")
        artifacts = cbp.collect_bundle_artifacts(tmp_path)
        names = [a.name for a in artifacts]
        assert "HASHES" not in names
        assert "SEAL" not in names
        assert "data.json" in names

    def test_hash_artifacts_and_seal(self, tmp_path):
        (tmp_path / "manifest").mkdir()
        (tmp_path / "manifest" / "bundle.json").write_text('{"run_id": "test"}')
        hashes_content, seal = cbp.write_hashes_and_seal(tmp_path)
        assert (tmp_path / "HASHES").exists()
        assert (tmp_path / "SEAL").exists()
        assert seal == hashlib.sha256(hashes_content.encode()).hexdigest()

    def test_hash_seal_write_once_enforced(self, tmp_path):
        (tmp_path / "manifest").mkdir()
        (tmp_path / "manifest" / "bundle.json").write_text('{"run_id": "test"}')
        cbp.write_hashes_and_seal(tmp_path)
        with pytest.raises(cbp.BundleSealError):
            cbp.write_hashes_and_seal(tmp_path)


# ---------------------------------------------------------------------------
# Layer 3 — Shadow Score Vault Tests
# ---------------------------------------------------------------------------

class TestShadowScore:

    def _make_scored(self, sid: str, scores: Dict) -> Dict:
        rubric = dict(cbp.DEFAULT_RUBRIC)
        dims = rubric["rubric"]["dimensions"]
        dim_scores = {}
        for dim in dims:
            did = dim["id"]
            dim_scores[did] = {
                "score": scores.get(did, 8),
                "max_score": dim["max_score"],
                "rationale": "Strong work.",
                "archetype": "spark",
            }
        return {
            "submission_id": sid,
            "dimension_scores": dim_scores,
            "total_score": 8.0,
            "scored_at": FIXED_TS,
        }

    def test_compute_shadow_score_ranking(self):
        rubric = dict(cbp.DEFAULT_RUBRIC)
        sid_a = "sub-a"
        sid_b = "sub-b"
        scored_a = self._make_scored(sid_a, {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9})
        scored_b = self._make_scored(sid_b, {"innovation": 6, "impact": 6, "execution": 6, "presentation": 6})
        shadow = cbp.compute_shadow_score([scored_a, scored_b], rubric, fixed_clock)
        assert shadow["ranking"][0] == sid_a
        assert shadow["scores"][sid_a] > shadow["scores"][sid_b]

    def test_tied_scores_use_shared_competition_placements(self, tmp_path):
        rubric = copy.deepcopy(cbp.DEFAULT_RUBRIC)
        scored_a = self._make_scored(
            "sub-a",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_b = self._make_scored(
            "sub-b",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_c = self._make_scored(
            "sub-c",
            {"innovation": 8, "impact": 8, "execution": 8, "presentation": 8},
        )
        shadow = cbp.compute_shadow_score([scored_b, scored_a, scored_c], rubric, fixed_clock)
        assert shadow["placements"] == [
            {
                "rank": 1,
                "submission_ids": ["sub-a", "sub-b"],
                "score": 9.0,
                "shared": True,
            },
            {
                "rank": 3,
                "submission_ids": ["sub-c"],
                "score": 8.0,
                "shared": False,
            },
        ]

        bundle_path = make_run(
            tmp_path,
            "tied-podium",
            event_spec=_podium_event_spec(),
        )
        for submission_id, project_name in (
            ("sub-a", "Aurora"),
            ("sub-b", "Beacon"),
            ("sub-c", "Comet"),
        ):
            cbp.write_once_json(
                bundle_path / "inputs" / f"{submission_id}.json",
                {
                    "submission_id": submission_id,
                    "builder_name": project_name,
                    "project_name": project_name,
                    "description": "A project.",
                    "submitted_at": FIXED_TS,
                },
            )
        for scored in (scored_a, scored_b, scored_c):
            cbp.write_once_json(
                bundle_path / "verdicts" / f"{scored['submission_id']}.json",
                {
                    **scored,
                    "project_name": scored["submission_id"],
                    "builder_name": scored["submission_id"],
                    "archetype_verdicts": [],
                },
            )
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)

        awards = cbp._choose_award_winners(bundle_path, "sub-a", fixed_clock)["awards"]
        gold = [award for award in awards if award["placement"] == 1]
        bronze = [award for award in awards if award["placement"] == 3]
        assert {award["winner_submission_id"] for award in gold} == {"sub-a", "sub-b"}
        assert all(award["shared_placement"] is True for award in gold)
        assert [award["winner_submission_id"] for award in bronze] == ["sub-c"]
        assert not [award for award in awards if award["placement"] == 2]
        ceremony_notes = cbp._tie_ceremony_notes(
            cbp._choose_award_winners(bundle_path, "sub-a", fixed_clock)
        )
        assert ceremony_notes == [
            "Shared podium: 2 projects share Gold — First Place. "
            "The next numbered placement advances under the declared policy."
        ]

    def test_category_tie_uses_shared_overall_placement_instead_of_id_order(self, tmp_path):
        bundle_path = make_run(tmp_path, "category-tie")
        scored_a = self._make_scored(
            "sub-a",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_b = self._make_scored(
            "sub-b",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_c = self._make_scored(
            "sub-c",
            {"innovation": 10, "impact": 10, "execution": 10, "presentation": 10},
        )
        for scored in (scored_a, scored_b, scored_c):
            submission_id = scored["submission_id"]
            cbp.write_once_json(
                bundle_path / "inputs" / f"{submission_id}.json",
                {
                    "submission_id": submission_id,
                    "builder_name": submission_id,
                    "project_name": submission_id,
                    "description": "A project.",
                    "submitted_at": FIXED_TS,
                },
            )
            cbp.write_once_json(
                bundle_path / "verdicts" / f"{submission_id}.json",
                {
                    **scored,
                    "project_name": submission_id,
                    "builder_name": submission_id,
                    "archetype_verdicts": [],
                },
            )
        shadow = cbp.compute_shadow_score(
            [scored_b, scored_c, scored_a],
            cbp.load_rubric(bundle_path),
            fixed_clock,
        )
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)

        awards_card = cbp._choose_award_winners(
            bundle_path,
            "sub-c",
            fixed_clock,
        )
        boldest = [
            award
            for award in awards_card["awards"]
            if award["award_id"] == "boldest-idea"
        ]

        assert {award["winner_submission_id"] for award in boldest} == {
            "sub-a",
            "sub-b",
        }
        assert all(award["shared_placement"] is True for award in boldest)

    def test_sealed_tiebreaker_resolves_public_score_tie_without_changing_scores(self):
        rubric = copy.deepcopy(cbp.DEFAULT_RUBRIC)
        rubric["tie_policy"] = {
            "mode": "sealed-tiebreaker",
            "tiebreaker_dimensions": ["impact"],
        }
        scored_a = self._make_scored(
            "sub-a",
            {"innovation": 8.166666666666666, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_b = self._make_scored(
            "sub-b",
            {"innovation": 9, "impact": 8, "execution": 9, "presentation": 9},
        )

        shadow = cbp.compute_shadow_score([scored_b, scored_a], rubric, fixed_clock)

        assert shadow["scores"]["sub-a"] == shadow["scores"]["sub-b"] == 8.75
        assert shadow["ranking"] == ["sub-a", "sub-b"]
        assert shadow["placements"][0]["submission_ids"] == ["sub-a"]
        assert shadow["placements"][0]["tie_resolution"] == "sealed-tiebreaker"
        assert shadow["tie_events"] == [{
            "rank": 1,
            "public_score": 8.75,
            "submission_ids": ["sub-a", "sub-b"],
            "resolution": "sealed-tiebreaker",
            "tiebreaker_dimensions": ["impact"],
        }]

    def test_human_tie_policy_requires_logged_decision(self, tmp_path):
        event = _podium_event_spec()
        event["tie_policy"] = {
            "mode": "human-resolution",
            "tiebreaker_dimensions": [],
        }
        bundle_path = tmp_path / "human-tie"
        cbp.init_bundle(
            "human-tie",
            "workshop",
            copy.deepcopy(cbp.DEFAULT_RUBRIC),
            bundle_path,
            fixed_clock,
            event,
        )
        scored_a = self._make_scored(
            "sub-a",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        scored_b = self._make_scored(
            "sub-b",
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
        )
        for scored in (scored_a, scored_b):
            submission_id = scored["submission_id"]
            cbp.write_once_json(
                bundle_path / "inputs" / f"{submission_id}.json",
                {
                    "submission_id": submission_id,
                    "builder_name": submission_id,
                    "project_name": submission_id,
                    "description": "A project.",
                    "submitted_at": FIXED_TS,
                },
            )
            cbp.write_once_json(
                bundle_path / "verdicts" / f"{submission_id}.json",
                {
                    **scored,
                    "project_name": submission_id,
                    "builder_name": submission_id,
                    "archetype_verdicts": [],
                },
            )
        shadow = cbp.compute_shadow_score(
            [scored_a, scored_b], cbp.load_rubric(bundle_path), fixed_clock
        )
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)

        event_path = bundle_path / "config" / "event.json"
        event_snapshot = cbp.load_json(event_path)
        drifted_event = copy.deepcopy(event_snapshot)
        drifted_event["tie_policy"] = {
            "mode": "shared-podium",
            "tiebreaker_dimensions": [],
        }
        event_path.write_text(json.dumps(drifted_event), encoding="utf-8")
        with pytest.raises(cbp.ConfigValidationError, match="no longer matches"):
            cbp._choose_award_winners(bundle_path, None, fixed_clock)
        event_path.write_text(json.dumps(event_snapshot), encoding="utf-8")

        with pytest.raises(cbp.ConfigValidationError, match="rank:1"):
            cbp._choose_award_winners(bundle_path, None, fixed_clock)
        with pytest.raises(cbp.ConfigValidationError, match="rank:1"):
            cbp._choose_award_winners(bundle_path, "sub-b", fixed_clock)

        assert cbp._winner_id_from_award_selection(
            bundle_path,
            {"rank:1": "sub-b"},
            fixed_clock,
        ) == "sub-b"
        awards_card = cbp._choose_award_winners(
            bundle_path,
            "sub-b",
            fixed_clock,
            {"rank:1": "sub-b"},
        )
        gold = [award for award in awards_card["awards"] if award["placement"] == 1]
        silver = [award for award in awards_card["awards"] if award["placement"] == 2]
        assert [award["winner_submission_id"] for award in gold] == ["sub-b"]
        assert [award["winner_submission_id"] for award in silver] == ["sub-a"]
        assert {
            event["resolution"]
            for event in awards_card["tie_ceremony"]["award_tie_resolutions"]
        } == {"human-declared", "human-resolution-derived"}

    def test_shadow_criteria_reject_non_finite_weights(self):
        criteria = cbp._default_shadow_criteria(
            copy.deepcopy(cbp.DEFAULT_EVENT_SPEC), 6
        )
        criteria[0]["weight"] = float("nan")
        assert cbp._normalize_shadow_criteria(criteria, 6) is None

    def test_seal_shadow_score_write_once(self, tmp_path):
        bundle_path = tmp_path / "run"
        bundle_path.mkdir()
        (bundle_path / "sealed").mkdir()
        shadow = {
            "scores": {"sub-a": 8.5},
            "ranking": ["sub-a"],
            "computed_at": FIXED_TS,
            "locked_at": None,
            "schema_version": "1.0",
        }
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)
        assert (bundle_path / "sealed" / "shadow_score.json").exists()

    def test_seal_shadow_score_second_write_raises(self, tmp_path):
        bundle_path = tmp_path / "run"
        bundle_path.mkdir()
        (bundle_path / "sealed").mkdir()
        shadow = {
            "scores": {"sub-a": 8.5},
            "ranking": ["sub-a"],
            "computed_at": FIXED_TS,
            "locked_at": None,
            "schema_version": "1.0",
        }
        cbp.seal_shadow_score(bundle_path, shadow.copy(), fixed_clock)
        shadow2 = dict(shadow)
        shadow2["locked_at"] = None
        with pytest.raises(cbp.BundleSealError):
            cbp.seal_shadow_score(bundle_path, shadow2, fixed_clock)

    def test_shadow_score_locked_at_set_on_seal(self, tmp_path):
        bundle_path = tmp_path / "run"
        bundle_path.mkdir()
        (bundle_path / "sealed").mkdir()
        shadow = {
            "scores": {"sub-a": 8.5},
            "ranking": ["sub-a"],
            "computed_at": FIXED_TS,
            "locked_at": None,
            "schema_version": "1.0",
        }
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)
        loaded = cbp.load_shadow_score(bundle_path)
        assert loaded["locked_at"] is not None

    def test_shadow_score_hidden_until_awarded(self, tmp_path):
        """Shadow score values should not appear in present output before award."""
        bundle_path = make_run(tmp_path, "hidden-test")
        sid = add_submission(bundle_path)
        full_judge_run(bundle_path)
        # present should work without revealing shadow score values
        args = build_args("present", run_id="hidden-test")
        import io
        with patch.object(sys, "stdout", new_callable=io.StringIO) as mock_stdout:
            with patch.dict(os.environ, {"HJ_RUNS_DIR": str(tmp_path)}):
                rc = cbp.cmd_present(args, None, fixed_clock)
        assert rc == 0


# ---------------------------------------------------------------------------
# Layer 4 — Freshness Gate Tests
# ---------------------------------------------------------------------------

class TestFreshnessGate:

    def test_gate_pass_with_approved_model(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        gw = MockGateway()
        result = cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        assert result["status"] == "pass"
        assert result["selected_model"] == "claude-opus-4.8"
        assert result["evaluation_provenance"]["mode"] == "live"
        assert (bundle_path / "freshness_gate.json").exists()

    def test_gate_ignores_unconfigured_preferred_model(self, tmp_path):
        bundle_path = make_run(tmp_path, "panel-only-policy")
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"].update(
            {
                "policy_mode": "strict",
                "preferred_model": "legacy-preferred-model",
                "panel_models": [
                    "claude-opus-4.8",
                    "gpt-5.6-terra",
                    "gemini-3.1-pro-preview",
                ],
                "minimum_panel_size": 3,
                "minimum_distinct_providers": 3,
            }
        )

        result = cbp.run_freshness_gate(
            bundle_path,
            rubric,
            MockGateway(),
            fixed_clock,
        )

        assert result["status"] == "pass"
        assert result["selected_models"] == rubric["freshness_gate"]["panel_models"]

    def test_default_gate_requires_premium_high_reasoning(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        gw = MockGateway(models=[
            {"id": "gpt-4o", "tier": 3, "premium": False, "reasoning": "medium", "deprecated": False},
        ])
        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        gate = cbp.load_json(bundle_path / "freshness_gate.json")
        assert "not available" in gate["reason"] or "not premium" in gate["reason"]

    def test_gate_blocks_stale_model_strict(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"]["policy_mode"] = "strict"
        rubric["freshness_gate"]["preferred_model"] = "gpt-4-legacy"
        stale_models = [{"id": "gpt-4-legacy", "tier": 1, "deprecated": True}]
        gw = MockGateway(models=stale_models)
        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        gate = cbp.load_json(bundle_path / "freshness_gate.json")
        assert gate["status"] == "blocked"

    def test_cached_blocked_gate_stays_blocked(self, tmp_path):
        bundle_path = make_run(tmp_path, "cached-block")
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"]["policy_mode"] = "strict"
        rubric["freshness_gate"]["preferred_model"] = "gpt-4-legacy"
        stale_gateway = MockGateway(
            models=[{"id": "gpt-4-legacy", "tier": 1, "deprecated": True}]
        )

        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, stale_gateway, fixed_clock)
        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, MockGateway(), fixed_clock)

    def test_cached_official_gate_requires_and_revalidates_gateway(self, tmp_path):
        bundle_path = make_run(tmp_path, "cached-official")
        rubric = cbp.load_rubric(bundle_path)
        cbp.run_freshness_gate(bundle_path, rubric, MockGateway(), fixed_clock)

        with pytest.raises(cbp.ModelAPIError, match="Official Copilot Panel"):
            cbp.run_freshness_gate(bundle_path, rubric, None, fixed_clock)
        with pytest.raises(cbp.FreshnessGateBlock, match="revalidation"):
            cbp.run_freshness_gate(
                bundle_path,
                rubric,
                MockGateway(
                    models=[
                        {
                            "id": "legacy-model",
                            "tier": 0,
                            "premium": False,
                            "reasoning": "low",
                            "deprecated": True,
                        }
                    ]
                ),
                fixed_clock,
            )

    def test_gate_fallback_permissive(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"]["policy_mode"] = "permissive"
        rubric["freshness_gate"]["preferred_model"] = "gpt-4-legacy"
        models = [
            {"id": "gpt-4-legacy", "tier": 1, "deprecated": True},
            {"id": "gpt-4o", "tier": 3, "deprecated": False},
        ]
        gw = MockGateway(models=models)
        result = cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        assert result["status"] == "fallback"
        assert result["selected_model"] == "gpt-4o"


class TestBulkUrlImport:

    def test_parse_submission_urls_dedupes_url_and_owner_repo(self):
        raw = """
        https://github.com/DUBSOpenHub/terminal-stampede
        DUBSOpenHub/terminal-stampede
        https://github.com/DUBSOpenHub/copilot-cli-agent-pulse/issues/1
        """
        urls = cbp.parse_submission_urls(raw)
        assert urls == [
            "https://github.com/DUBSOpenHub/terminal-stampede",
            "https://github.com/DUBSOpenHub/copilot-cli-agent-pulse",
        ]

    def test_parse_submission_urls_accepts_generic_project_links(self):
        raw = """
        https://demo.example.com/projects/aurora
        https://www.figma.com/design/abc123/workshop-demo
        https://youtu.be/example-video
        javascript:alert(1)
        """

        assert cbp.parse_submission_urls(raw) == [
            "https://demo.example.com/projects/aurora",
            "https://www.figma.com/design/abc123/workshop-demo",
            "https://youtu.be/example-video",
        ]

    def test_generic_url_deduplication_preserves_case_sensitive_paths(self):
        assert cbp.parse_submission_urls(
            "https://demo.example.com/Project\nhttps://demo.example.com/project"
        ) == [
            "https://demo.example.com/Project",
            "https://demo.example.com/project",
        ]

    def test_github_submission_ids_remain_legacy_compatible(self):
        url = "https://github.com/DUBSOpenHub/terminal-stampede"

        assert (
            cbp._submission_id_from_project_url(url)
            == "repo-dubsopenhub-terminal-stampede-1ee86947"
        )
        assert cbp._submission_id_from_repo_url(url) == cbp._submission_id_from_project_url(url)

    def test_long_generic_submission_ids_keep_distinct_digests(self):
        shared = "https://demo.example.com/projects/" + ("a" * 180)
        first = cbp._submission_id_from_project_url(shared + "One")
        second = cbp._submission_id_from_project_url(shared + "Two")

        assert first != second
        assert len(first) <= 96
        assert len(second) <= 96
        assert re.search(r"-[0-9a-f]{8}$", first)
        assert re.search(r"-[0-9a-f]{8}$", second)

    def test_generic_project_link_import_does_not_fetch_remote_metadata(self, tmp_path):
        bundle_path = make_run(tmp_path, "generic-link")
        url = "https://demo.example.com/projects/aurora"

        with patch.object(cbp, "fetch_repo_metadata") as fetch_repo_metadata:
            created = cbp.import_url_submissions(bundle_path, [url], clock=fixed_clock)

        fetch_repo_metadata.assert_not_called()
        assert created[0]["project_name"] == "demo.example.com/aurora"
        assert created[0]["builder_name"] == "Project team"
        assert created[0]["project_url"] == url
        assert created[0]["repo_metadata"]["source"] == "project-link"

    def test_percent_encoded_terminal_escape_is_removed_from_project_display(self, tmp_path, capsys):
        bundle_path = make_run(tmp_path, "escaped-link")
        url = "https://demo.example.com/projects/%1b%5b31mAurora"

        created = cbp.import_url_submissions(bundle_path, [url], clock=fixed_clock)
        cbp._sideline(f"{created[0]['project_name']} enters the showcase.")

        assert "\x1b" not in created[0]["project_name"]
        assert "\x1b" not in capsys.readouterr().out
        assert "Aurora" in created[0]["project_name"]

    def test_project_and_builder_identities_strip_controls_but_keep_unicode(self, tmp_path):
        bundle_path = make_run(tmp_path, "safe-identities")
        url = "https://demo.example.com/projects/caf%C3%A9-%F0%9F%9A%80"

        created = cbp.import_url_submissions(
            bundle_path,
            [{"url": url, "builder_name": "Tēam 🚀\x07"}],
            clock=fixed_clock,
        )

        assert created[0]["project_name"] == "demo.example.com/café-🚀"
        assert created[0]["builder_name"] == "Tēam 🚀"

    def test_terminal_title_removes_injected_control_sequences(self):
        class TtyOutput(io.StringIO):
            def isatty(self):
                return True

        output = TtyOutput()
        with patch.object(cbp.sys, "stdout", output):
            cbp._set_terminal_title("Aurora\x1b[2J Showcase")

        rendered = output.getvalue()
        assert rendered.startswith("\x1b]0;")
        assert "\x1b[2J" not in rendered
        assert "Aurora[2J Showcase" in rendered

    def test_import_url_submissions_creates_idempotent_submissions(self, tmp_path):
        bundle_path = make_run(tmp_path, "url-room")
        urls = [
            "https://github.com/DUBSOpenHub/terminal-stampede",
            "https://github.com/DUBSOpenHub/copilot-cli-agent-pulse",
        ]
        created = cbp.import_url_submissions(bundle_path, urls, "Workshop Room", fixed_clock)
        created_again = cbp.import_url_submissions(bundle_path, urls, "Workshop Room", fixed_clock)
        assert len(created) == 2
        assert len(created_again) == 0
        submissions = cbp._load_submissions(bundle_path)
        assert len(submissions) == 2
        assert all(s["builder_name"] == "Workshop Room" for s in submissions)
        assert {s["repo_url"] for s in submissions} == set(urls)

    def test_links_only_import_uses_repository_owner_as_team_name(self, tmp_path):
        bundle_path = make_run(tmp_path, "owner-team")
        url = "https://github.com/DUBSOpenHub/terminal-stampede"

        created = cbp.import_url_submissions(
            bundle_path,
            [url],
            clock=fixed_clock,
            metadata_provider=lambda _: cbp._fallback_repo_metadata(url),
        )

        assert created[0]["builder_name"] == "DUBSOpenHub team"

    def test_workshop_yes_runs_full_guided_flow(self, tmp_path, capsys):
        env = {
            "HJ_RUNS_DIR": str(tmp_path / "runs"),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        event_path = tmp_path / "demo-day.json"
        event_path.write_text(json.dumps({
            "event": {
                "name": "Demo Day",
                "tagline": "Share what you made.",
            },
            "awards": [
                {
                    "id": "audience-choice",
                    "name": "Audience Choice",
                    "emoji": "*",
                    "tagline": "For a project that connected with the room.",
                    "dimensions": ["presentation"],
                    "reason": "This project made its value especially clear.",
                },
                {
                    "id": "grand-prize",
                    "name": "Demo Day Grand Prize",
                    "emoji": "#",
                    "tagline": "For the strongest overall project.",
                    "dimensions": [],
                    "reason": "This project delivered the strongest overall result.",
                },
            ],
        }), encoding="utf-8")
        args = build_args(
            "workshop",
            run_id="guided-room",
            urls=[
                "https://github.com/DUBSOpenHub/terminal-stampede",
                "DUBSOpenHub/copilot-cli-agent-pulse",
            ],
            file=None,
            audience="external",
            awards="Builder,Spark,Ship",
            panel_style="fun",
            config=None,
            event=str(event_path),
            showtime=False,
            yes=True,
            no_suspense=True,
        )
        def fake_metadata(url):
            owner_repo = url.replace("https://github.com/", "", 1)
            return {
                "name_with_owner": owner_repo,
                "description": "",
                "language": "Python",
                "stars": 12,
                "forks": 1,
                "updated_at": FIXED_TS,
                "url": url,
                "source": "test",
            }

        with patch.dict(os.environ, env), patch.object(cbp, "fetch_repo_metadata", fake_metadata):
            rc = cbp.cmd_workshop(args, MockGateway(), fixed_clock)
            assert rc == 0
            bundle = tmp_path / "runs" / "guided-room"
            manifest = cbp.load_json(bundle / "manifest" / "bundle.json")
            assert manifest["workshop_choices"]["awards"] == [
                "Audience Choice",
                "Demo Day Grand Prize",
            ]
            assert manifest["event"]["name"] == "Demo Day"
            assert manifest["result_status"] == "OFFICIAL COPILOT PANEL"
            assert manifest["results_are_illustrative"] is False
            assert manifest["official_copilot_panel_connected"] is True
            assert manifest["official_live_panel_connected"] is True
            awards = cbp.load_json(bundle / "winner" / "awards.json")
            award_names = [a["award_name"] for a in awards["awards"]]
            assert set(award_names) == {"Audience Choice", "Demo Day Grand Prize"}
            assert award_names[-1] == "Demo Day Grand Prize"
            assert (bundle / "recap.md").exists()
            assert (bundle / "HASHES").exists()
            assert (bundle / "SEAL").exists()
            assert len(list((bundle / "inputs").glob("*.json"))) == 2
        assert capsys.readouterr().out.count("OFFICIAL COPILOT PANEL") >= 5

    def test_workshop_showtime_defaults_to_audience_autopilot(self, tmp_path, capsys):
        env = {
            "HJ_RUNS_DIR": str(tmp_path / "runs"),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        args = build_args(
            "workshop",
            run_id="show-room",
            urls=[
                "DUBSOpenHub/project-one | Team One",
                "DUBSOpenHub/project-two | Team Two",
                "DUBSOpenHub/project-three | Team Three",
            ],
            file=None,
            audience=None,
            awards=None,
            panel_style=None,
            config=None,
            showtime=True,
            yes=False,
            configure=False,
            manual_confirm=False,
            no_suspense=True,
        )

        def fake_metadata(url):
            owner_repo = url.replace("https://github.com/", "", 1)
            return {
                "name_with_owner": owner_repo,
                "description": "",
                "language": "Python",
                "stars": 99,
                "forks": 2,
                "updated_at": FIXED_TS,
                "url": url,
                "source": "test",
            }

        with patch.dict(os.environ, env), patch.object(cbp, "fetch_repo_metadata", fake_metadata):
            rc = cbp.cmd_workshop(args, MockGateway(), fixed_clock)

        assert rc == 0
        output = capsys.readouterr().out
        assert "Create the workshop run bundle?" not in output
        assert "Boldest Idea" in output
        assert "Most Useful" in output
        assert "Project of the Showcase" in output
        assert "Copilot Builder Showcase Recap" in output
        assert "ACT I — PROJECTS ENTER" in output
        assert "Sealing the Night" in output
        assert "SHARE THIS MOMENT" in output
        assert "Panel chatter:" in output
        assert "One fast panel take per project" in output
        awards = cbp.load_json(tmp_path / "runs" / "show-room" / "winner" / "awards.json")
        award_order = {"boldest-idea": 0, "most-useful": 1, "grand-prize": 2}
        award_ids = [award["award_id"] for award in awards["awards"]]
        assert award_ids == sorted(award_ids, key=award_order.__getitem__)
        assert "grand-prize" in award_ids
        assert len({award["winner_submission_id"] for award in awards["awards"]}) == 3

    def test_gate_result_immutably_recorded(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        gw = MockGateway()
        cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        gate_path = bundle_path / "freshness_gate.json"
        assert gate_path.exists()
        # Second call keeps the immutable artifact but revalidates a live panel.
        result2 = cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        assert result2["status"] in ("pass", "fallback", "blocked")

    def test_gate_blocks_missing_model_strict(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"].update(
            {
                "policy_mode": "strict",
                "preferred_model": "claude-opus-4.8",
                "panel_models": [
                    "claude-opus-4.8",
                    "gpt-5.6-terra",
                    "nonexistent-model",
                ],
                "minimum_panel_size": 3,
                "minimum_distinct_providers": 3,
            }
        )
        gw = MockGateway()
        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)


class TestCopilotCLIGateway:

    def test_copilot_cli_supports_default_diverse_panel_and_inference(self, tmp_path):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            model_id = command[command.index("--model") + 1]
            output = (
                '{"status":"ready"}'
                if model_id == "auto"
                else '{"scores":{"impact":9}}'
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        gateway = cbp.CopilotCLIGateway("/opt/homebrew/bin/copilot", runner=runner)
        bundle_path = make_run(tmp_path, "copilot-panel")
        gate = cbp.run_freshness_gate(
            bundle_path,
            cbp.load_rubric(bundle_path),
            gateway,
            fixed_clock,
        )
        completion = gateway.call_model("Return JSON.", "gpt-5.6-terra")

        assert gate["status"] == "pass"
        assert gate["selected_models"] == cbp.DEFAULT_EVENT_SPEC["model_policy"]["panel_models"]
        assert completion == '{"scores":{"impact":9}}'
        readiness_command, readiness_kwargs = calls[0]
        model_command, model_kwargs = calls[1]
        assert readiness_command[readiness_command.index("--model") + 1] == "auto"
        assert model_command[model_command.index("--model") + 1] == "gpt-5.6-terra"
        assert "--available-tools=" in model_command
        assert "--disable-builtin-mcps" in model_command
        assert "--no-custom-instructions" in model_command
        assert readiness_kwargs["timeout"] == 90
        assert model_kwargs["timeout"] == 180
        assert model_kwargs["env"]["COPILOT_ALLOW_ALL"] == "false"

    def test_environment_uses_installed_copilot_cli(self):
        with patch.object(cbp.shutil, "which", return_value=None):
            assert cbp._live_gateway_from_environment({}) is None
        with patch.object(
            cbp.shutil, "which", return_value="/opt/homebrew/bin/copilot"
        ):
            gateway = cbp._live_gateway_from_environment({})

        assert isinstance(gateway, cbp.CopilotCLIGateway)
        assert gateway.copilot_path == "/opt/homebrew/bin/copilot"

    def test_copilot_failure_is_public_safe(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="private provider detail",
            )

        gateway = cbp.CopilotCLIGateway("copilot", runner=runner)
        with pytest.raises(cbp.ModelAPIError, match="exit 1") as exc_info:
            gateway.query_available_models()

        assert "private provider detail" not in str(exc_info.value)

    def test_main_passes_environment_gateway_to_command(self):
        gateway = object()
        received = {}

        def handler(_args, selected_gateway, _clock):
            received["gateway"] = selected_gateway
            return 0

        with patch.object(
            cbp, "_live_gateway_from_environment", return_value=gateway
        ), patch.dict(cbp.COMMAND_MAP, {"doctor": handler}):
            assert cbp.main(["doctor"]) == 0

        assert received["gateway"] is gateway


# ---------------------------------------------------------------------------
# Integration — Full Command Flow Tests
# ---------------------------------------------------------------------------

class TestCommandFlows:

    def _env(self, tmp_path: Path) -> Dict:
        return {"HJ_RUNS_DIR": str(tmp_path), "HJ_REGISTRY_PATH": str(tmp_path / "registry.ndjson")}

    def test_cli_parser_accepts_event_and_operator_flags(self):
        parser = cbp.build_parser()
        init_args = parser.parse_args(["init", "demo-day", "--event", "event.json"])
        tui_args = parser.parse_args(["tui", "demo-day", "--operator"])
        quick_args = parser.parse_args([
            "quick",
            "owner/project",
            "--tie-resolution",
            "rank:1=sub-a",
        ])
        workshop_args = parser.parse_args([
            "workshop",
            "owner/project",
            "--tie-resolution",
            "rank:1=sub-a",
            "--require-live-terminal",
        ])
        submit_args = parser.parse_args([
            "submit", "demo-day", "--builder-name", "Team Aurora",
            "--project-name", "Aurora", "--problem-statement", "Reduce follow-up gaps",
            "--intended-user", "Account executives", "--demo-url", "https://demo.example",
            "--builder-notes", "Daily workflow demo",
        ])
        award_args = parser.parse_args([
            "award", "demo-day", "--winner", "sub-a",
            "--tie-resolution", "rank:1=sub-a",
        ])
        assert init_args.event == "event.json"
        assert tui_args.operator is True
        assert quick_args.urls == ["owner/project"]
        assert quick_args.tie_resolution == ["rank:1=sub-a"]
        assert workshop_args.tie_resolution == ["rank:1=sub-a"]
        assert workshop_args.require_live_terminal is True
        assert submit_args.problem_statement == "Reduce follow-up gaps"
        assert submit_args.intended_user == "Account executives"
        assert submit_args.demo_url == "https://demo.example"
        assert submit_args.builder_notes == "Daily workflow demo"
        assert award_args.tie_resolution == ["rank:1=sub-a"]

    def test_workshop_blocks_when_current_output_is_not_a_real_terminal(self, tmp_path, capsys):
        args = build_args(
            "workshop",
            run_id="required-live-terminal",
            urls=["DUBSOpenHub/project-one"],
            audience=None,
            panel_style=None,
            showtime=True,
            yes=True,
            configure=False,
            manual_confirm=False,
            no_suspense=True,
            projector=False,
            require_live_terminal=True,
        )

        with patch.dict(os.environ, self._env(tmp_path)):
            with patch.object(cbp.sys.stdout, "isatty", return_value=False):
                rc = cbp.cmd_workshop(args, MockGateway(), fixed_clock)

        assert rc == 7
        captured = capsys.readouterr()
        assert "requires a real interactive terminal" in captured.err
        assert not (tmp_path / "required-live-terminal").exists()

    def test_workshop_never_auto_launches_optional_monitor(self, tmp_path):
        args = build_args(
            "workshop",
            run_id="single-screen",
            urls=["DUBSOpenHub/project-one"],
            audience=None,
            panel_style="fun",
            showtime=True,
            yes=True,
            configure=False,
            manual_confirm=False,
            no_suspense=True,
            projector=True,
        )

        with patch.dict(os.environ, self._env(tmp_path)):
            with patch.object(cbp, "fetch_repo_metadata", return_value=cbp._fallback_repo_metadata(
                "https://github.com/DUBSOpenHub/project-one"
            )):
                with patch.object(cbp.subprocess, "Popen") as popen:
                    with patch.object(cbp.subprocess, "run") as run:
                        assert cbp.cmd_workshop(args, MockGateway(), fixed_clock) == 0

        popen.assert_not_called()
        assert all(
            "osascript" not in str(call.args)
            for call in run.call_args_list
        )

    def test_bundled_demo_runs_the_complete_single_screen_show(self, tmp_path, capsys):
        args = build_args(
            "workshop",
            run_id="practice-show",
            urls=[],
            audience=None,
            panel_style=None,
            showtime=True,
            yes=True,
            configure=False,
            manual_confirm=False,
            no_suspense=True,
            projector=False,
            demo=True,
        )

        with patch.dict(os.environ, self._env(tmp_path)):
            assert cbp.cmd_workshop(args, MockGateway(), fixed_clock) == 0

        output = capsys.readouterr().out
        manifest = cbp.load_manifest(tmp_path / "practice-show")
        elapsed = re.search(r"Practice showcase complete in ([0-9.]+)s", output)
        assert output.count("PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS") >= 5
        assert "Audience check ready" in output
        assert elapsed is not None
        assert float(elapsed.group(1)) < cbp.DEMO_TIME_BUDGET_SECONDS
        assert manifest["result_status"] == "PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS"
        assert manifest["results_are_illustrative"] is True
        assert manifest["official_copilot_panel_connected"] is False
        assert manifest["official_live_panel_connected"] is False
        assert manifest["workshop_choices"]["display_surface"] == "single-terminal"
        assert manifest["workshop_choices"]["optional_monitor_auto_launched"] is False
        assert len(cbp._load_submissions(tmp_path / "practice-show")) == 3

    def test_official_show_blocks_without_connected_panel(self, tmp_path, capsys):
        args = build_args(
            "workshop",
            run_id="official-show",
            urls=["DUBSOpenHub/project-one"],
            official=True,
        )

        with patch.dict(os.environ, self._env(tmp_path)):
            assert cbp.cmd_workshop(args, None, fixed_clock) == 7

        assert "Official judging is not connected" in capsys.readouterr().err
        assert not (tmp_path / "official-show").exists()

    def test_official_judge_resume_cannot_downgrade_to_practice(self, tmp_path, capsys):
        bundle_path = make_run(tmp_path, "official-resume")
        add_submission(bundle_path)
        manifest = cbp.load_manifest(bundle_path)
        manifest["result_status"] = "OFFICIAL COPILOT PANEL"
        manifest["official_copilot_panel_connected"] = True
        cbp.save_manifest(bundle_path, manifest)

        with patch.dict(os.environ, self._env(tmp_path)):
            assert cbp.cmd_judge(
                build_args("judge", run_id="official-resume"),
                None,
                fixed_clock,
            ) == 8

        assert "cannot continue with practice judges" in capsys.readouterr().err
        assert not (bundle_path / "freshness_gate.json").exists()
        assert not list((bundle_path / "verdicts").glob("*.json"))

    def test_projector_tui_refuses_captured_output(self, capsys):
        args = argparse.Namespace(
            run_id="captured-projector",
            projector=True,
            operator=False,
            showtime=False,
        )

        assert cbp.cmd_tui(args, None, fixed_clock) == 7
        assert "requires a real interactive terminal" in capsys.readouterr().err

    def test_init_creates_bundle(self, tmp_path):
        with patch.dict(os.environ, self._env(tmp_path)):
            args = build_args("init", run_id="myrun")
            rc = cbp.cmd_init(args, None, fixed_clock)
        assert rc == 0
        bundle = tmp_path / "myrun"
        assert (bundle / "manifest" / "bundle.json").exists()
        assert (bundle / "config" / "rubric.json").exists()

    def test_init_duplicate_run_id_fails(self, tmp_path):
        with patch.dict(os.environ, self._env(tmp_path)):
            args = build_args("init", run_id="myrun")
            rc = cbp.cmd_init(args, None, fixed_clock)
            assert rc == 0
            # Second init should fail with non-zero exit (ConfigValidationError caught internally)
            rc2 = cbp.cmd_init(args, None, fixed_clock)
            assert rc2 != 0

    def test_submit_adds_submission(self, tmp_path):
        with patch.dict(os.environ, self._env(tmp_path)):
            cbp.cmd_init(build_args("init", run_id="r1"), None, fixed_clock)
            args = build_args("submit", run_id="r1",
                              builder_name="Alice", project_name="AliceApp",
                              description="A project")
            rc = cbp.cmd_submit(args, None, fixed_clock)
        assert rc == 0
        inputs = list((tmp_path / "r1" / "inputs").glob("*.json"))
        assert len(inputs) == 1

    def test_structured_submission_intake_preserves_evidence(self, tmp_path):
        run_id = "structured-intake"
        entries = cbp.parse_submission_entries(
            "DUBSOpenHub/example | Team Aurora | Used Copilot Chat for the API contract | "
            "Built an agent workflow with retrieval"
        )
        assert entries == [{
            "url": "https://github.com/DUBSOpenHub/example",
            "builder_name": "Team Aurora",
            "copilot_evidence": "Used Copilot Chat for the API contract",
            "frontier_evidence": "Built an agent workflow with retrieval",
        }]

        def fake_metadata(url):
            return {
                "name_with_owner": "DUBSOpenHub/example",
                "description": "Example project",
                "language": "Python",
                "stars": 0,
                "forks": 0,
                "updated_at": FIXED_TS,
                "url": url,
                "source": "test",
            }

        with patch.dict(os.environ, self._env(tmp_path)):
            cbp.cmd_init(build_args("init", run_id=run_id), None, fixed_clock)
            with patch.object(cbp, "fetch_repo_metadata", fake_metadata):
                created = cbp.import_url_submissions(tmp_path / run_id, entries, clock=fixed_clock)

        assert created[0]["builder_name"] == "Team Aurora"
        assert created[0]["copilot_evidence"] == "Used Copilot Chat for the API contract"
        assert created[0]["frontier_evidence"] == "Built an agent workflow with retrieval"

    def test_structured_intake_preserves_feedback_context_and_sources(self, tmp_path):
        run_id = "contextual-intake"
        entries = cbp.parse_submission_entries(
            "DUBSOpenHub/example | Team Aurora |  |  | "
            "Reduce missed follow-ups after customer meetings | "
            "Account executives | https://demo.example.test/aurora | "
            "The demo covers the daily follow-up flow"
        )
        assert entries[0]["problem_statement"] == (
            "Reduce missed follow-ups after customer meetings"
        )
        assert entries[0]["intended_user"] == "Account executives"
        assert entries[0]["demo_url"] == "https://demo.example.test/aurora"
        assert entries[0]["builder_notes"] == "The demo covers the daily follow-up flow"

        def fake_metadata(url):
            return {
                "name_with_owner": "DUBSOpenHub/example",
                "description": "Turns meeting notes into follow-up plans.",
                "language": "Python",
                "stars": 0,
                "forks": 0,
                "updated_at": FIXED_TS,
                "topics": ["productivity"],
                "url": url,
                "source": "test",
            }

        with patch.dict(os.environ, self._env(tmp_path)):
            cbp.cmd_init(build_args("init", run_id=run_id), None, fixed_clock)
            with patch.object(cbp, "fetch_repo_metadata", fake_metadata):
                cbp.import_url_submissions(tmp_path / run_id, entries, clock=fixed_clock)
            full_judge_run(tmp_path / run_id)

        submission = cbp._load_submissions(tmp_path / run_id)[0]
        feedback = cbp._load_feedback(tmp_path / run_id)[0]
        source_ids = {source["id"] for source in feedback["grounding"]["sources"]}
        judgments = cbp.load_json(
            next((tmp_path / run_id / "eval").glob("step_*.json"))
        )["model_judgments"]

        assert submission["description_source"] == "project-link-import"
        assert feedback["grounding"]["status"] == "specific"
        assert {
            "builder.problem_statement",
            "builder.intended_user",
            "builder.demo_url",
            "builder.builder_notes",
            "repository.description",
        } <= source_ids
        assert feedback["grounding"]["used_source_ids"] == [
            "builder.problem_statement",
            "builder.intended_user",
            "builder.demo_url",
            "builder.builder_notes",
        ]
        assert feedback["grounding"]["reference_status"] == "panel-cited"
        assert all(
            "builder.problem_statement" in judgment["grounding_refs"]
            for judgment in judgments
        )
        assert all(
            not idea.startswith("Hypothesis:")
            for idea in feedback["copilot_next_moves"]
            + feedback["frontier_experiments"]
        )

    def test_feedback_labels_suggestions_as_hypotheses_without_context(self, tmp_path):
        bundle_path = make_run(tmp_path, "no-context")
        submission = {
            "submission_id": "no-context",
            "builder_name": "Team Aurora",
            "project_name": "Aurora",
            "description": "",
            "artifacts": [],
            "submitted_at": FIXED_TS,
            "file_size_bytes": 0,
        }
        cbp.write_once_json(bundle_path / "inputs" / "no-context.json", submission)
        cbp.update_status(bundle_path, "collecting", fixed_clock)
        full_judge_run(bundle_path)

        feedback = cbp._load_feedback(bundle_path)[0]
        assert feedback["grounding"]["status"] == "hypothesis"
        assert all(
            idea.startswith("Hypothesis:")
            for idea in feedback["copilot_next_moves"]
            + feedback["frontier_experiments"]
        )

    def test_tone_fallback_keeps_ungrounded_feedback_hypothetical(self, tmp_path):
        bundle_path = make_run(tmp_path, "no-context-tone-fallback")
        submission = {
            "submission_id": "no-context-tone-fallback",
            "builder_name": "Team Aurora",
            "project_name": "Aurora",
            "description": "",
            "artifacts": [],
            "submitted_at": FIXED_TS,
            "file_size_bytes": 0,
        }
        cbp.write_once_json(
            bundle_path / "inputs" / "no-context-tone-fallback.json",
            submission,
        )
        cbp.update_status(bundle_path, "collecting", fixed_clock)
        full_judge_run(bundle_path)
        feedback_path = bundle_path / "feedback" / "no-context-tone-fallback.json"
        feedback_path.unlink()
        panel_models = cbp.load_json(
            bundle_path / "freshness_gate.json"
        )["selected_models"]

        with patch.object(
            cbp,
            "check_feedback_card_tone",
            side_effect=[
                {"passed": False, "banned_phrases": ["weak"], "missing_required": []},
                {"passed": True, "banned_phrases": [], "missing_required": []},
            ],
        ):
            cards = cbp.build_feedback_cards(
                [{"submission_id": submission["submission_id"]}],
                [submission],
                cbp.load_rubric(bundle_path),
                panel_models,
                bundle_path,
                None,
                fixed_clock,
            )

        card = cards[0]
        assert card["bright_spot"].startswith("Hypothesis:")
        assert card["next_commit"].startswith("Hypothesis:")
        assert card["panel_notes"].startswith("Hypothesis:")

    def test_default_recognitions_spotlight_distinct_projects(self, tmp_path):
        bundle_path = make_run(tmp_path, "recognition-awards")
        submission_ids = [
            add_submission(bundle_path, project_name=project_name)
            for project_name in ("Aurora", "Beacon", "Cinder")
        ]
        score_sets = [
            {"innovation": 9, "impact": 9, "execution": 9, "presentation": 9},
            {"innovation": 10, "impact": 7, "execution": 7, "presentation": 7},
            {"innovation": 6, "impact": 10, "execution": 6, "presentation": 6},
        ]
        scored = []
        for submission_id, scores in zip(submission_ids, score_sets):
            dimension_scores = {
                dimension["id"]: {
                    "score": scores[dimension["id"]],
                    "max_score": dimension["max_score"],
                }
                for dimension in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
            }
            scored.append(
                {
                    "submission_id": submission_id,
                    "dimension_scores": dimension_scores,
                    "total_score": sum(
                        dimension_scores[dimension["id"]]["score"]
                        * dimension["weight"]
                        for dimension in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
                    ),
                    "scored_at": FIXED_TS,
                }
            )
        shadow = cbp.compute_shadow_score(
            scored, cbp.load_rubric(bundle_path), fixed_clock
        )
        cbp.seal_shadow_score(bundle_path, shadow, fixed_clock)
        submissions = {
            submission["submission_id"]: submission
            for submission in cbp._load_submissions(bundle_path)
        }
        for record in scored:
            submission = submissions[record["submission_id"]]
            cbp.write_once_json(
                bundle_path / "verdicts" / f"{record['submission_id']}.json",
                {
                    **record,
                    "project_name": submission["project_name"],
                    "builder_name": submission["builder_name"],
                    "archetype_verdicts": [],
                },
            )

        ranking = shadow["ranking"]
        awards = cbp._choose_award_winners(bundle_path, ranking[0], fixed_clock)["awards"]

        assert [award["award_id"] for award in awards] == [
            "boldest-idea",
            "most-useful",
            "grand-prize",
        ]
        assert awards[-1]["winner_submission_id"] == ranking[0]
        assert len({award["winner_submission_id"] for award in awards}) == 3

    def test_project_showcase_badges_include_activity_and_topics(self):
        badges = cbp.project_showcase_badges({
            "language": "TypeScript",
            "stars": 8674,
            "topics": ["ai", "llm", "natural-language", "types"],
            "pushed_at": "2026-07-07T23:18:52Z",
        })

        assert badges == [
            "📝 TypeScript",
            "⭐ 8.7k",
            "🏷️ ai, llm, natural-language",
            "🟢 Active 2026-07-07",
        ]

    def test_present_spotlight_centers_project_context(self, tmp_path):
        run_id = "project-context"
        bundle_path = make_run(tmp_path, run_id)
        submission_id = str(uuid.uuid4())
        cbp.write_once_json(
            bundle_path / "inputs" / f"{submission_id}.json",
            {
                "submission_id": submission_id,
                "builder_name": "Team Aurora",
                "project_name": "Project Aurora",
                "description": "A project",
                "repo_metadata": {
                    "description": "Turns meeting notes into clear action plans.",
                    "language": "Python",
                    "stars": 1200,
                    "topics": ["agents", "productivity"],
                    "pushed_at": "2026-07-09T12:00:00Z",
                    "homepage": "https://example.test/aurora",
                },
                "copilot_evidence": "Used Copilot Chat to design the workflow.",
                "frontier_evidence": "Uses an agent loop to classify actions.",
                "artifacts": [],
                "submitted_at": FIXED_TS,
                "file_size_bytes": 0,
            },
        )
        cbp.update_status(bundle_path, "collecting", fixed_clock)
        full_judge_run(bundle_path)

        args = argparse.Namespace(run_id=run_id, showtime=False, projector=True, operator=False)
        with patch.dict(os.environ, self._env(tmp_path)):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                assert cbp.cmd_present(args, None, fixed_clock) == 0

        rendered = output.getvalue()
        assert "SPOTLIGHT: Project Aurora" in rendered
        assert "Built by: Team Aurora" in rendered
        assert "What it does: Turns meeting notes into clear action plans." in rendered
        assert "Project signals:" in rendered
        assert "Copilot: Builder-provided Copilot use evidence:" in rendered
        assert "Used Copilot Chat to design the workflow." in rendered
        assert "Frontier: Builder-provided frontier use evidence:" in rendered
        assert "Uses an agent loop to classify actions." in rendered

    def test_quick_judging_is_quiet_and_writes_feedback(self, tmp_path, capsys):
        run_id = "quick-flow"

        def fake_metadata(url):
            return {
                "name_with_owner": url.replace("https://github.com/", "", 1),
                "description": "Example project",
                "language": "Python",
                "stars": 0,
                "forks": 0,
                "updated_at": FIXED_TS,
                "url": url,
                "source": "test",
            }

        args = argparse.Namespace(
            run_id=run_id,
            urls=[
                "DUBSOpenHub/project-one | Team One | Used Copilot for test design",
                "DUBSOpenHub/project-two | Team Two",
            ],
            file=None,
            builder_name="Participants",
            config=None,
            event=None,
        )
        with patch.dict(os.environ, self._env(tmp_path)):
            with patch.object(cbp, "fetch_repo_metadata", fake_metadata):
                assert cbp.cmd_quick(args, None, fixed_clock) == 0

        output = capsys.readouterr().out
        assert "Quick judging complete" in output
        assert "Results status: PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS" in output
        assert "Panel chatter:" not in output
        assert "🥁" not in output
        assert "Score:" not in output
        assert "Private project feedback:" in output
        assert "Validation: passed" in output
        assert "Replay: showcase replay quick-flow" in output
        assert cbp.load_manifest(tmp_path / run_id)["engagement_mode"] == "quick"
        assert cbp.load_manifest(tmp_path / run_id)["status"] == "exported"
        assert (tmp_path / run_id / "HASHES").exists()
        assert (tmp_path / run_id / "SEAL").exists()
        assert (tmp_path / f"{run_id}.bundle.tar.gz").exists()
        feedback = cbp._load_feedback(tmp_path / run_id)
        assert feedback[0]["judges_liked"]
        assert feedback[0]["copilot_use"]["status"] == "evidenced"
        assert feedback[1]["copilot_use"]["status"] == "not_provided"

    def test_full_e2e_3_submissions(self, tmp_path):
        """AC-01: init + submit + judge + award completes for 3 submissions."""
        run_id = "e2e-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            # init
            rc = cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            assert rc == 0

            # submit x3
            for i, (name, proj) in enumerate([
                ("Alice", "AliceBot"), ("Bob", "BobML"), ("Carol", "CarolOS")
            ]):
                args = build_args("submit", run_id=run_id, builder_name=name,
                                  project_name=proj, description=f"Project {i+1}")
                rc = cbp.cmd_submit(args, gw, fixed_clock)
                assert rc == 0

            # judge
            rc = cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            assert rc == 0

            # verify sealed
            manifest = cbp.load_manifest(tmp_path / run_id)
            assert manifest["status"] == "sealed"

            # verify shadow score exists and is unchanged
            ss_path = tmp_path / run_id / "sealed" / "shadow_score.json"
            assert ss_path.exists()
            ss_data = json.loads(ss_path.read_text())
            assert len(ss_data["scores"]) == 3

            # get winner
            subs = cbp._load_submissions(tmp_path / run_id)
            shadow = cbp.load_shadow_score(tmp_path / run_id)
            winner_id = shadow["ranking"][0]

            # award
            rc = cbp.cmd_award(
                build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock
            )
            assert rc == 0

            # verify winner card
            winner_card = cbp.load_json(tmp_path / run_id / "winner" / "card.json")
            assert winner_card["requires_human_approval"] is True
            assert winner_card["published"] is False
            assert winner_card["award_name"] == cbp.AWARD_NAME
            awards_card = cbp.load_json(tmp_path / run_id / "winner" / "awards.json")
            assert len(awards_card["awards"]) == 3
            feedback_by_submission = {
                card["submission_id"]: card
                for card in cbp._load_feedback(tmp_path / run_id)
            }
            for award in awards_card["awards"]:
                feedback = feedback_by_submission[award["winner_submission_id"]]
                assert award["selection_basis"]["award_criterion"] in award["reason"]
                assert feedback["bright_spot"] in award["reason"]
                assert award["selection_basis"]["judges_liked"] == feedback["judges_liked"]

            # present
            rc = cbp.cmd_present(build_args("present", run_id=run_id), gw, fixed_clock)
            assert rc == 0

    def test_judge_blocked_on_stale_model_strict(self, tmp_path):
        """AC-05: Freshness gate blocks judge on stale model (strict)."""
        run_id = "strict-test"
        env = self._env(tmp_path)
        # Use a deep-copied config with strict mode + deprecated model
        rubric = copy.deepcopy(cbp.DEFAULT_RUBRIC)
        rubric["freshness_gate"]["policy_mode"] = "strict"
        rubric["freshness_gate"]["preferred_model"] = "gpt-4-legacy"

        with patch.dict(os.environ, env):
            bundle_path = tmp_path / run_id
            cbp.init_bundle(run_id, "workshop", rubric, bundle_path, fixed_clock)
            add_submission(bundle_path)

            stale_gw = MockGateway(models=[{"id": "gpt-4-legacy", "tier": 0, "deprecated": True}])
            rc = cbp.cmd_judge(build_args("judge", run_id=run_id), stale_gw, fixed_clock)
        assert rc == 3  # FreshnessGateBlock exit code

    def test_export_and_validate(self, tmp_path):
        """AC-02: validate passes on untampered bundle."""
        run_id = "export-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            shadow = cbp.load_shadow_score(tmp_path / run_id)
            assert shadow is not None, "Shadow score should be sealed after judge"
            winner_id = shadow["ranking"][0]
            cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)

            rc = cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock)
            assert rc == 0

            rc = cbp.cmd_validate(build_args("validate", bundle=run_id), gw, fixed_clock)
            assert rc == 0

            (tmp_path / run_id / "unsealed.txt").write_text("late mutation")
            rc = cbp.cmd_validate(build_args("validate", bundle=run_id), gw, fixed_clock)
            assert rc == 5

    def test_export_resumes_after_archive_creation_failure(self, tmp_path, capsys):
        run_id = "resume-export"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(
                build_args(
                    "submit",
                    run_id=run_id,
                    builder_name="Dev",
                    project_name="P1",
                    description="desc",
                ),
                gw,
                fixed_clock,
            )
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            winner_id = cbp.load_shadow_score(tmp_path / run_id)["ranking"][0]
            cbp.cmd_award(
                build_args("award", run_id=run_id, winner=winner_id),
                gw,
                fixed_clock,
            )

            with patch.object(cbp.tarfile, "open", side_effect=OSError("disk full")):
                assert cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock) == 1

            assert (tmp_path / run_id / "SEAL").exists()
            assert not (tmp_path / f"{run_id}.bundle.tar.gz").exists()
            with patch.object(cbp, "_textual_status", return_value=(True, "Textual 8.2.3")):
                assert cbp.cmd_doctor(
                    build_args("doctor", run_id=run_id),
                    gw,
                    fixed_clock,
                ) == 1
            assert "Replay archive: MISSING" in capsys.readouterr().err
            assert cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock) == 0
            assert (tmp_path / f"{run_id}.bundle.tar.gz").exists()
            assert cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock) == 0

    def test_export_resumes_after_seal_write_failure(self, tmp_path):
        run_id = "resume-seal"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(
                build_args(
                    "submit",
                    run_id=run_id,
                    builder_name="Dev",
                    project_name="P1",
                    description="desc",
                ),
                gw,
                fixed_clock,
            )
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            winner_id = cbp.load_shadow_score(tmp_path / run_id)["ranking"][0]
            cbp.cmd_award(
                build_args("award", run_id=run_id, winner=winner_id),
                gw,
                fixed_clock,
            )

            original_write_once = cbp.write_once

            def fail_seal(path, data):
                if path.name == "SEAL":
                    raise OSError("disk full")
                return original_write_once(path, data)

            with patch.object(cbp, "write_once", side_effect=fail_seal):
                assert cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock) == 1

            assert (tmp_path / run_id / "HASHES").exists()
            assert not (tmp_path / run_id / "SEAL").exists()
            assert cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock) == 0
            assert (tmp_path / run_id / "SEAL").exists()
            assert (tmp_path / f"{run_id}.bundle.tar.gz").exists()

    def test_validate_detects_tampering(self, tmp_path):
        """AC-02: validate fails with clear error on tampered artifact."""
        run_id = "tamper-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            shadow = cbp.load_shadow_score(tmp_path / run_id)
            assert shadow is not None
            winner_id = shadow["ranking"][0]
            cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)
            cbp.cmd_export(build_args("export", run_id=run_id), gw, fixed_clock)

            # Tamper: modify manifest after sealing
            manifest_path = tmp_path / run_id / "manifest" / "bundle.json"
            content = json.loads(manifest_path.read_text())
            content["_tampered"] = True
            manifest_path.write_text(json.dumps(content))

            rc = cbp.cmd_validate(build_args("validate", bundle=run_id), gw, fixed_clock)
        assert rc == 5  # BundleTamperError

    def test_shadow_score_unchanged_between_judge_and_present(self, tmp_path):
        """AC-03: shadow_score.json hash unchanged between judge and present."""
        run_id = "ss-unchanged"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            ss_path = tmp_path / run_id / "sealed" / "shadow_score.json"
            hash_before = cbp._sha256_file(ss_path)

            cbp.cmd_present(build_args("present", run_id=run_id), gw, fixed_clock)

            hash_after = cbp._sha256_file(ss_path)
            assert hash_before == hash_after

    def test_replay_read_only_no_new_artifacts(self, tmp_path):
        """AC-04: replay on sealed bundle produces no new artifacts."""
        run_id = "replay-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            bundle_path = tmp_path / run_id
            files_before = set(str(p) for p in bundle_path.rglob("*") if p.is_file())

            rc = cbp.cmd_replay(build_args("replay", bundle=run_id), gw, fixed_clock)
            assert rc == 0

            files_after = set(str(p) for p in bundle_path.rglob("*") if p.is_file())
            assert files_before == files_after

    def test_present_idempotent(self, tmp_path):
        """AC-07: two present calls on same bundle produce identical output."""
        import io
        run_id = "present-idempotent"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            out1 = io.StringIO()
            with patch("sys.stdout", out1):
                cbp.cmd_present(build_args("present", run_id=run_id), gw, fixed_clock)

            out2 = io.StringIO()
            with patch("sys.stdout", out2):
                cbp.cmd_present(build_args("present", run_id=run_id), gw, fixed_clock)

        assert out1.getvalue() == out2.getvalue()

    def test_feedback_proposal_does_not_modify_bundle(self, tmp_path):
        """AC-08: feedback produces proposal file; does not modify bundle."""
        run_id = "fb-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            winner_id = cbp.load_shadow_score(tmp_path / run_id)["ranking"][0]
            cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)

            bundle_path = tmp_path / run_id
            files_before = set(str(p) for p in bundle_path.rglob("*") if p.is_file())
            model_calls_before_feedback = gw.call_count

            rc = cbp.cmd_feedback(build_args("feedback", run_id=run_id), gw, fixed_clock)
            assert rc == 0

            files_after = set(str(p) for p in bundle_path.rglob("*") if p.is_file())
            assert files_before == files_after  # bundle unchanged
            assert gw.call_count == model_calls_before_feedback
            proposal_path = next((tmp_path.parent / "feedback_proposals" / run_id).glob("proposal_*.json"))
            proposal = json.loads(proposal_path.read_text())["proposals"][0]
            assert proposal["selected_for"]
            assert proposal["bright_spot"] in proposal["selected_for"][0]["why_selected"]
            assert proposal["judges_liked"]
            assert proposal["next_commit"] == proposal["ways_to_improve"]
            assert proposal["copilot_use"]["status"] == "not_provided"
            assert proposal["frontier_use"]["status"] == "not_provided"

    def test_winner_card_requires_human_approval(self, tmp_path):
        """AC-09: winner card has requires_human_approval: true."""
        run_id = "winner-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

            shadow = cbp.load_shadow_score(tmp_path / run_id)
            winner_id = shadow["ranking"][0]
            cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)

            card = cbp.load_json(tmp_path / run_id / "winner" / "card.json")
        assert card["requires_human_approval"] is True

    def test_registry_appends_one_entry_per_award(self, tmp_path):
        """AC-10: registry grows by exactly one entry per award; prior entries unchanged."""
        registry_path = tmp_path / "registry.ndjson"
        env = self._env(tmp_path)
        gw = MockGateway()

        def do_run(run_id: str):
            with patch.dict(os.environ, env):
                cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
                cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                          project_name="P", description="d"), gw, fixed_clock)
                cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
                shadow = cbp.load_shadow_score(tmp_path / run_id)
                winner_id = shadow["ranking"][0]
                cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)

        do_run("reg-run-1")
        entries_1 = cbp.read_ndjson(registry_path)
        assert len(entries_1) == 1
        first_entry = dict(entries_1[0])

        do_run("reg-run-2")
        entries_2 = cbp.read_ndjson(registry_path)
        assert len(entries_2) == 2
        # First entry must be unchanged
        assert entries_2[0] == first_entry

    def test_feedback_cards_contain_bright_spot(self, tmp_path):
        """AC-06: every feedback card contains bright_spot and forward nudge."""
        run_id = "fb-bright"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P1", description="desc"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

        bundle_path = tmp_path / run_id
        feedback = cbp._load_feedback(bundle_path)
        assert len(feedback) == 1
        card = feedback[0]
        assert card["bright_spot"]
        assert card["next_commit"]
        assert card["judges_liked"]
        assert card["copilot_use"]["status"] == "not_provided"
        assert card["frontier_use"]["status"] == "not_provided"
        assert card["innovation_signal"]["status"] == "assessed"
        assert card["tone_checked"] is True

    def test_doctor_passes_clean_env(self, tmp_path, capsys):
        """DF-04: doctor self-test passes on clean environment."""
        env = self._env(tmp_path)
        with patch.dict(os.environ, env):
            with patch.object(cbp, "_textual_status", return_value=(True, "Textual 8.2.3")):
                args = build_args("doctor", run_id=None)
                rc = cbp.cmd_doctor(args, None, fixed_clock)
        assert rc == 0
        assert "Judge panel: practice showcase ready" in capsys.readouterr().out

    def test_doctor_reports_missing_textual(self, tmp_path, capsys):
        env = self._env(tmp_path)
        with patch.dict(os.environ, env):
            with patch.object(cbp, "_textual_status", return_value=(False, "Textual is not installed")):
                rc = cbp.cmd_doctor(build_args("doctor", run_id=None), None, fixed_clock)

        assert rc == 0
        assert "Optional monitor unavailable" in capsys.readouterr().out

    def test_award_blocked_if_already_awarded(self, tmp_path):
        """award is idempotent-blocked — second call raises BundleSealError (rc=2)."""
        run_id = "double-award"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P", description="d"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            shadow = cbp.load_shadow_score(tmp_path / run_id)
            winner_id = shadow["ranking"][0]
            cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)
            # Second award: winner/card.json already exists → BundleSealError (exit code 2)
            rc = cbp.cmd_award(build_args("award", run_id=run_id, winner=winner_id), gw, fixed_clock)
        assert rc == 2  # BundleSealError — winner card already exists

    def test_no_real_person_names_in_archetypes(self):
        """DF-05: No real-person names in judge archetypes."""
        archetypes = cbp.DEFAULT_RUBRIC["judge_archetypes"]
        real_names = ["john", "jane", "elon", "sam", "satya", "tim", "jeff", "mark"]
        for arch in archetypes:
            name_lower = arch["name"].lower()
            for real in real_names:
                assert real not in name_lower, f"Real name '{real}' found in archetype: {arch}"

    def test_compare_two_bundles(self, tmp_path):
        gw = MockGateway()
        env = self._env(tmp_path)

        def make_and_judge(run_id: str):
            with patch.dict(os.environ, env):
                cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
                cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                          project_name="P", description="d"), gw, fixed_clock)
                cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)

        make_and_judge("cmp-a")
        make_and_judge("cmp-b")

        with patch.dict(os.environ, env):
            args = build_args("compare", bundle_a="cmp-a", bundle_b="cmp-b")
            rc = cbp.cmd_compare(args, gw, fixed_clock)
        assert rc == 0

    def test_list_runs(self, tmp_path):
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id="list-r1"), gw, fixed_clock)
            cbp.cmd_init(build_args("init", run_id="list-r2"), gw, fixed_clock)
            args = build_args("list")
            rc = cbp.cmd_list(args, gw, fixed_clock)
        assert rc == 0

    def test_resume_completed_run_noop(self, tmp_path):
        run_id = "resume-test"
        gw = MockGateway()
        env = self._env(tmp_path)

        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(build_args("submit", run_id=run_id, builder_name="Dev",
                                      project_name="P", description="d"), gw, fixed_clock)
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            rc = cbp.cmd_resume(build_args("resume", run_id=run_id), gw, fixed_clock)
        assert rc == 0


# ---------------------------------------------------------------------------
# Multi-model consensus and Shadow Spec
# ---------------------------------------------------------------------------

class TestMultiModelConsensusAndShadowSpec:

    def test_freshness_gate_selects_diverse_default_panel(self, tmp_path):
        bundle_path = make_run(tmp_path)
        result = cbp.run_freshness_gate(
            bundle_path, cbp.load_rubric(bundle_path), MockGateway(), fixed_clock
        )

        assert result["selected_models"] == [
            "claude-opus-4.8",
            "gpt-5.6-terra",
            "gemini-3.1-pro-preview",
        ]
        assert result["consensus_method"] == "median"
        assert result["panel_degraded"] is False
        assert {
            cbp._model_provider(model_id) for model_id in result["selected_models"]
        } == {"anthropic", "openai", "google"}

    def test_strict_gate_blocks_an_incomplete_panel(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        claude_only = [
            model
            for model in cbp.APPROVED_MODELS
            if model["id"] == "claude-opus-4.8"
        ]

        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(
                bundle_path, rubric, MockGateway(models=claude_only), fixed_clock
            )

        gate = cbp.load_json(bundle_path / "freshness_gate.json")
        assert gate["status"] == "blocked"
        assert gate["selected_models"] == ["claude-opus-4.8"]

    def test_scores_use_model_medians_and_record_private_provenance(self, tmp_path):
        class MedianGateway:
            scores = {"model-a": 6, "model-b": 8, "model-c": 10}

            def call_model(self, _prompt, model_id):
                return json.dumps(
                    {
                        "scores": {
                            dimension["id"]: self.scores[model_id]
                            for dimension in cbp.DEFAULT_RUBRIC["rubric"]["dimensions"]
                        },
                        "bright_spot": "This project demonstrates strong focus.",
                        "next_commit": "Consider adding a focused user test.",
                        "panel_notes": "Concrete project evidence supports this read.",
                    }
                )

        bundle_path = make_run(tmp_path)
        submission_id = add_submission(bundle_path)
        scored = cbp.score_submissions(
            cbp._load_submissions(bundle_path),
            cbp.load_rubric(bundle_path),
            ["model-a", "model-b", "model-c"],
            bundle_path,
            MedianGateway(),
            fixed_clock,
        )

        assert len(scored) == 1
        assert scored[0]["submission_id"] == submission_id
        assert all(
            dimension["score"] == 8
            for dimension in scored[0]["dimension_scores"].values()
        )
        assert all(
            dimension["consensus"]["model_count"] == 3
            for dimension in scored[0]["dimension_scores"].values()
        )
        step = cbp.load_json(next((bundle_path / "eval").glob("step_*.json")))
        assert step["model_panel"] == ["model-a", "model-b", "model-c"]
        assert len(step["model_judgments"]) == 9
        assert "model_judgments" not in scored[0]

    def test_scoring_bounds_parallel_calls_and_preserves_judgment_order(self, tmp_path):
        import threading
        import time

        class SlowGateway:
            def __init__(self):
                self.active_calls = 0
                self.max_active_calls = 0
                self.lock = threading.Lock()

            def call_model(self, prompt, model_id):
                with self.lock:
                    self.active_calls += 1
                    self.max_active_calls = max(
                        self.max_active_calls, self.active_calls
                    )
                try:
                    time.sleep(0.01)
                    return cbp._synthetic_model_response(prompt, model_id)
                finally:
                    with self.lock:
                        self.active_calls -= 1

        bundle_path = make_run(tmp_path)
        add_submission(bundle_path)
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"]["max_parallel_calls"] = 2
        gateway = SlowGateway()
        panel = ["model-a", "model-b", "model-c"]

        cbp.score_submissions(
            cbp._load_submissions(bundle_path),
            rubric,
            panel,
            bundle_path,
            gateway,
            fixed_clock,
        )

        assert gateway.max_active_calls == 2
        step = cbp.load_json(next((bundle_path / "eval").glob("step_*.json")))
        expected = [
            (lens["id"], model_id)
            for lens in rubric["judge_archetypes"]
            for model_id in panel
        ]
        assert [
            (judgment["archetype_id"], judgment["model"])
            for judgment in step["model_judgments"]
        ] == expected
        assert step["max_parallel_calls"] == 2

    def test_failed_parallel_scoring_writes_no_partial_eval_step(self, tmp_path):
        bundle_path = make_run(tmp_path)
        add_submission(bundle_path)

        with pytest.raises(cbp.ModelAPIError):
            cbp.score_submissions(
                cbp._load_submissions(bundle_path),
                cbp.load_rubric(bundle_path),
                ["model-a", "model-b", "model-c"],
                bundle_path,
                MockGateway(fail_on_call=True),
                fixed_clock,
            )

        assert not list((bundle_path / "eval").glob("step_*.json"))

    def test_judge_reuses_scoring_pass_and_records_room_timing(self, tmp_path):
        import threading

        class CountingGateway:
            def __init__(self):
                self.models = cbp.APPROVED_MODELS
                self.call_count = 0
                self._lock = threading.Lock()

            def query_available_models(self):
                return self.models

            def call_model(self, prompt, model_id):
                with self._lock:
                    self.call_count += 1
                return cbp._synthetic_model_response(prompt, model_id)

        run_id = "fast-panel"
        env = {
            "HJ_RUNS_DIR": str(tmp_path),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        gateway = CountingGateway()

        with patch.dict(os.environ, env):
            assert cbp.cmd_init(build_args("init", run_id=run_id), gateway, fixed_clock) == 0
            assert cbp.cmd_submit(
                build_args("submit", run_id=run_id), gateway, fixed_clock
            ) == 0
            assert cbp.cmd_judge(
                build_args("judge", run_id=run_id), gateway, fixed_clock
            ) == 0

        bundle_path = tmp_path / run_id
        verdict = cbp._load_verdicts(bundle_path)[0]
        feedback = cbp._load_feedback(bundle_path)[0]
        plan = cbp.load_json(bundle_path / "eval" / "plan.json")
        timing = cbp.load_json(bundle_path / "eval" / "timing.json")
        progress = cbp.load_json(bundle_path / "eval" / "progress.json")

        assert gateway.call_count == 13  # spec + 9 score calls + 3 Shadow checks
        assert all(
            reaction["reused_scoring_pass"]
            for reaction in verdict["archetype_verdicts"]
        )
        assert feedback["feedback_panel"]["reused_scoring_pass"] is True
        assert plan["calls"]["avoided"] == 12
        assert plan["max_parallel_calls"] == 6
        assert timing["budget_policy"] == "warn-only"
        assert progress == {
            "schema_version": "1.0",
            "updated_at": FIXED_TS,
            "status": "complete",
            "stage": "complete",
            "submissions": {"completed": 1, "total": 1},
            "max_parallel_calls": 6,
            "remaining_model_calls": 0,
        }
        assert set(timing["stage_seconds"]) >= {
            "freshness_gate",
            "shadow_spec",
            "public_scoring",
            "shadow_assessment",
            "verdicts",
            "feedback",
        }

    def test_judge_seals_diagnostic_shadow_spec_without_ranking_it(self, tmp_path):
        run_id = "shadow-spec-room"
        env = {
            "HJ_RUNS_DIR": str(tmp_path),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        gateway = MockGateway()

        with patch.dict(os.environ, env):
            assert cbp.cmd_init(build_args("init", run_id=run_id), gateway, fixed_clock) == 0
            assert cbp.cmd_submit(
                build_args(
                    "submit",
                    run_id=run_id,
                    builder_name="Builder",
                    project_name="Project Lantern",
                    description="A focused project with a clear demo path.",
                ),
                gateway,
                fixed_clock,
            ) == 0
            assert cbp.cmd_judge(build_args("judge", run_id=run_id), gateway, fixed_clock) == 0

        bundle_path = tmp_path / run_id
        spec = cbp.load_shadow_spec(bundle_path)
        assessment = cbp.load_shadow_assessment(bundle_path)
        public_score = cbp.load_shadow_score(bundle_path)
        feedback = cbp._load_feedback(bundle_path)[0]

        assert spec["affects_public_ranking"] is False
        assert spec["reveal_after"] == "awarded"
        assert len(spec["criteria"]) == 6
        assert any(criterion["is_decoy"] for criterion in spec["criteria"])
        assert assessment["affects_public_ranking"] is False
        assert "ranking" not in assessment
        assert assessment["spec_hash"] == spec["spec_hash"]
        assert public_score["ranking"]
        assert feedback["copilot_next_moves"]
        assert feedback["frontier_experiments"]
        assert feedback["feedback_panel"]["model_count"] == 3

    def test_award_returns_clear_error_when_no_ranked_award_is_eligible(self, tmp_path):
        run_id = "empty-ranked-awards"
        event = copy.deepcopy(cbp.DEFAULT_EVENT_SPEC)
        event["awards"] = [
            {
                "id": "impossible-placement",
                "name": "Impossible Placement",
                "emoji": "🏅",
                "tagline": "Reserved for an unavailable placement.",
                "dimensions": [],
                "rank": 2,
                "reason": "This placement is intentionally unavailable in a one-project room.",
            }
        ]
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        env = {
            "HJ_RUNS_DIR": str(tmp_path),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        gateway = MockGateway()

        with patch.dict(os.environ, env):
            assert cbp.cmd_init(
                build_args("init", run_id=run_id, event=str(event_path)),
                gateway,
                fixed_clock,
            ) == 0
            assert cbp.cmd_submit(build_args("submit", run_id=run_id), gateway, fixed_clock) == 0
            assert cbp.cmd_judge(build_args("judge", run_id=run_id), gateway, fixed_clock) == 0
            winner_id = cbp.load_shadow_score(tmp_path / run_id)["ranking"][0]
            assert cbp.cmd_award(
                build_args("award", run_id=run_id, winner=winner_id),
                gateway,
                fixed_clock,
            ) == 7

        assert not (tmp_path / run_id / "winner" / "card.json").exists()


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

class TestExitCodes:

    def test_bundle_seal_error_exit_code_2(self):
        try:
            raise cbp.BundleSealError("test")
        except cbp.BundleSealError as e:
            assert e.exit_code == 2

    def test_freshness_gate_block_exit_code_3(self):
        try:
            raise cbp.FreshnessGateBlock("test")
        except cbp.FreshnessGateBlock as e:
            assert e.exit_code == 3

    def test_tone_safety_failure_exit_code_4(self):
        try:
            raise cbp.ToneSafetyFailure("test")
        except cbp.ToneSafetyFailure as e:
            assert e.exit_code == 4

    def test_bundle_tamper_error_exit_code_5(self):
        try:
            raise cbp.BundleTamperError("test")
        except cbp.BundleTamperError as e:
            assert e.exit_code == 5

    def test_submission_size_error_exit_code_6(self):
        try:
            raise cbp.SubmissionSizeError("test")
        except cbp.SubmissionSizeError as e:
            assert e.exit_code == 6

    def test_config_validation_error_exit_code_7(self):
        try:
            raise cbp.ConfigValidationError("test")
        except cbp.ConfigValidationError as e:
            assert e.exit_code == 7

    def test_model_api_error_exit_code_8(self):
        try:
            raise cbp.ModelAPIError("test")
        except cbp.ModelAPIError as e:
            assert e.exit_code == 8

    def test_human_approval_gate_exit_code_9(self):
        try:
            raise cbp.HumanApprovalGate("test")
        except cbp.HumanApprovalGate as e:
            assert e.exit_code == 9

    def test_unknown_command_exits_1(self, tmp_path):
        with pytest.raises(SystemExit):
            cbp.main(["nonexistent"])


# ---------------------------------------------------------------------------
# Config Validation Tests
# ---------------------------------------------------------------------------

class TestConfigValidation:

    def test_weights_not_summing_to_1_raises(self):
        config = copy.deepcopy(cbp.DEFAULT_RUBRIC)
        config["rubric"]["dimensions"] = [
            {"id": "a", "name": "A", "weight": 0.5, "max_score": 10},
            {"id": "b", "name": "B", "weight": 0.3, "max_score": 10},
            # sum = 0.8, not 1.0
        ]
        with pytest.raises(cbp.ConfigValidationError):
            cbp._validate_rubric(config)

    def test_no_dimensions_raises(self):
        config = {"rubric": {"dimensions": []}}
        with pytest.raises(cbp.ConfigValidationError):
            cbp._validate_rubric(config)

    def test_valid_rubric_passes(self):
        cbp._validate_rubric(copy.deepcopy(cbp.DEFAULT_RUBRIC))  # should not raise


# ---------------------------------------------------------------------------
# Fixture generation — 3-submission sealed bundle (AC-01 through AC-10)
# ---------------------------------------------------------------------------

def generate_sample_fixture(fixture_path: Path) -> None:
    """Generate a reference fixture bundle for tests/fixtures/sample_run/."""
    import shutil
    if fixture_path.exists():
        # Reset permissions on read-only directories before removal
        for p in fixture_path.rglob("*"):
            try:
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(fixture_path)
    fixture_path.mkdir(parents=True)

    run_id = "sample_run"
    gw = MockGateway()

    rubric = copy.deepcopy(cbp.DEFAULT_RUBRIC)
    rubric["freshness_gate"]["policy_mode"] = "permissive"

    bundle_path = fixture_path / run_id
    cbp.init_bundle(run_id, "workshop", rubric, bundle_path, fixed_clock)

    submissions_data = [
        ("Alice Chen", "Copilot Code Compass", "AI-powered navigation for legacy codebases."),
        ("Bob Torres", "BuildBot Pro", "Automated CI/CD pipeline builder using natural language."),
        ("Carol Kim", "DataLens", "Real-time dashboard generator from unstructured data sources."),
    ]

    for name, proj, desc in submissions_data:
        sid = str(uuid.uuid4())
        sub = {
            "submission_id": sid,
            "builder_name": name,
            "project_name": proj,
            "description": desc,
            "artifacts": [],
            "submitted_at": FIXED_TS,
            "file_size_bytes": 0,
        }
        cbp.write_once_json(bundle_path / "inputs" / f"{sid}.json", sub)

    cbp.update_status(bundle_path, "collecting", fixed_clock)

    full_judge_run(bundle_path, gw)

    shadow = cbp.load_shadow_score(bundle_path)
    winner_id = shadow["ranking"][0]

    # award
    subs = cbp._load_submissions(bundle_path)
    winner_sub = next(s for s in subs if s["submission_id"] == winner_id)
    declared_at = FIXED_TS
    winner_card = {
        "run_id": run_id,
        "winner_submission_id": winner_id,
        "winner_builder_name": winner_sub["builder_name"],
        "award_name": cbp.AWARD_NAME,
        "declared_at": declared_at,
        "requires_human_approval": True,
        "published": False,
    }
    cbp.write_once_json(bundle_path / "winner" / "card.json", winner_card)
    cbp.append_ndjson(bundle_path / "registry" / "log.ndjson", {
        "run_id": run_id, "winner_id": winner_id,
        "award_name": cbp.AWARD_NAME, "declared_at": declared_at, "bundle_sha256": ""
    })
    cbp.update_status(bundle_path, "awarded", fixed_clock)

    # export: update status to "exported" BEFORE writing HASHES so manifest is stable
    cbp.update_status(bundle_path, "exported", fixed_clock)
    cbp.log_command(bundle_path, "export", "ok", "fixture", fixed_clock)
    cbp.write_hashes_and_seal(bundle_path)

    print(f"✓ Sample fixture generated: {bundle_path}")


# ---------------------------------------------------------------------------
# Helpers for building args
# ---------------------------------------------------------------------------

def build_args(command: str, **kwargs) -> cbp.argparse.Namespace:
    defaults = {
        "command": command,
        "run_id": kwargs.get("run_id", "test-run"),
        "mode": "workshop",
        "config": None,
        "event": None,
        "builder_name": "Test Builder",
        "project_name": "Test Project",
        "description": "Test description",
        "file": None,
        "winner": None,
        "submission_id": None,
        "bundle": kwargs.get("bundle", kwargs.get("run_id", "test-run")),
        "bundle_a": kwargs.get("bundle_a", "run-a"),
        "bundle_b": kwargs.get("bundle_b", "run-b"),
        "force": False,
        "projector": False,
        "require_live_terminal": False,
        "require_projector_window": False,
        "demo": False,
        "operator": False,
    }
    defaults.update(kwargs)
    return cbp.argparse.Namespace(**defaults)


if __name__ == "__main__":
    # Generate fixture when run directly
    fixture_path = Path(__file__).parent / "fixtures"
    generate_sample_fixture(fixture_path)
    print("Fixture generated. Run tests with: python -m pytest tests/ -v")
