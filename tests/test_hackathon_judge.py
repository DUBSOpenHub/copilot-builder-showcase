"""
Test suite for Hackathon Judge
Tests all layers: tone safety, hash/seal, write-once, freshness gate,
shadow score, eval engine, command flows, registry, exit codes.

Run with: python -m pytest tests/test_hackathon_judge.py -v
"""

import copy
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# Add parent dir to sys.path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent))
import hackathon_judge as cbp

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


def make_run(tmp_path: Path, run_id: str = "test-run", mode: str = "workshop",
             gateway: Optional[MockGateway] = None) -> Path:
    """Initialize a run and return bundle_path."""
    bundle_path = tmp_path / run_id
    cbp.init_bundle(run_id, mode, dict(cbp.DEFAULT_RUBRIC), bundle_path, fixed_clock)
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
    selected = gate["selected_model"]

    scored = cbp.score_submissions(submissions, rubric, selected, bundle_path, gw, fixed_clock)
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

    def test_assert_tone_raises_on_failure(self):
        tone_result = {"passed": False, "banned_phrases": ["weak"], "missing_required": []}
        with pytest.raises(cbp.ToneSafetyFailure):
            cbp.assert_tone(tone_result, "test context")

    def test_assert_tone_passes_silently(self):
        tone_result = {"passed": True, "banned_phrases": [], "missing_required": []}
        cbp.assert_tone(tone_result)  # should not raise


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

    def test_workshop_yes_runs_full_guided_flow(self, tmp_path):
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
            awards = cbp.load_json(bundle / "winner" / "awards.json")
            assert [a["award_name"] for a in awards["awards"]] == [
                "Audience Choice",
                "Demo Day Grand Prize",
            ]
            assert (bundle / "recap.md").exists()
            assert (bundle / "HASHES").exists()
            assert (bundle / "SEAL").exists()
            assert len(list((bundle / "inputs").glob("*.json"))) == 2

    def test_workshop_showtime_defaults_to_audience_autopilot(self, tmp_path, capsys):
        env = {
            "HJ_RUNS_DIR": str(tmp_path / "runs"),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry" / "log.ndjson"),
        }
        args = build_args(
            "workshop",
            run_id="show-room",
            urls=["DUBSOpenHub/copilot-cli-agent-pulse"],
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
        assert "Innovation Award" in output
        assert "Build Quality Award" in output
        assert "Hackathon Grand Prize" in output
        assert "Workshop Recap" in output
        assert "ACT I — PROJECTS ENTER" in output
        assert "Sealing the Night" in output
        assert "SHARE THIS MOMENT" in output

    def test_gate_result_immutably_recorded(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        gw = MockGateway()
        cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        gate_path = bundle_path / "freshness_gate.json"
        assert gate_path.exists()
        # Second call should return existing result without re-running
        result2 = cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)
        assert result2["status"] in ("pass", "fallback", "blocked")

    def test_gate_blocks_missing_model_strict(self, tmp_path):
        bundle_path = make_run(tmp_path)
        rubric = cbp.load_rubric(bundle_path)
        rubric["freshness_gate"]["policy_mode"] = "strict"
        rubric["freshness_gate"]["preferred_model"] = "nonexistent-model"
        gw = MockGateway()
        with pytest.raises(cbp.FreshnessGateBlock):
            cbp.run_freshness_gate(bundle_path, rubric, gw, fixed_clock)


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
        assert init_args.event == "event.json"
        assert tui_args.operator is True

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

            bundle_path = tmp_path / run_id
            files_before = set(str(p) for p in bundle_path.rglob("*") if p.is_file())

            rc = cbp.cmd_feedback(build_args("feedback", run_id=run_id), gw, fixed_clock)
            assert rc == 0

            files_after = set(str(p) for p in bundle_path.rglob("*") if p.is_file())
            assert files_before == files_after  # bundle unchanged

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
        assert card["tone_checked"] is True

    def test_doctor_passes_clean_env(self, tmp_path):
        """DF-04: doctor self-test passes on clean environment."""
        env = self._env(tmp_path)
        with patch.dict(os.environ, env):
            args = build_args("doctor", run_id=None)
            rc = cbp.cmd_doctor(args, None, fixed_clock)
        assert rc == 0

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
        "operator": False,
    }
    defaults.update(kwargs)
    return cbp.argparse.Namespace(**defaults)


if __name__ == "__main__":
    # Generate fixture when run directly
    fixture_path = Path(__file__).parent / "fixtures"
    generate_sample_fixture(fixture_path)
    print("Fixture generated. Run tests with: python -m pytest tests/ -v")
