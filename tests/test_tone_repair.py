"""The tone gate must protect builders without killing the event.

Two faults lived here. The gate matched banned phrases as bare substrings, so
"BadgeForge" tripped "bad", "Blacksmith" tripped "lacks", and "Weakly
Supervised" tripped "weak" — real project names that aborted the award. And a
genuine violation anywhere in generated prose raised, with no repair and no
retry, so one adjective from one judge threw away a finished showcase.

The guarantee that matters is unchanged: no builder is ever shown teardown
language. These pin that it still holds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import builder_showcase as cbp

from test_builder_showcase import (
    MockGateway,
    add_submission,
    build_args,
    fixed_clock,
    full_judge_run,
    make_run,
)


class TestBannedPhraseMatching:
    """Innocent words that used to abort a live event."""

    @pytest.mark.parametrize(
        "text",
        [
            "BadgeForge wins the Builder Award for project BadgeForge.",
            "Badger Analytics shares the award.",
            "Blacksmith wins the award for project Blacksmith.",
            "Weakly Supervised Labs wins the award.",
            "Poorvi's Planner wins the award.",
            "The team shipped a slackbot integration.",
            "An embedded systems project.",
        ],
    )
    def test_a_name_that_merely_contains_a_banned_word_is_fine(self, text):
        result = cbp.check_tone(text)
        assert result["passed"], (
            f"{result['banned_phrases']} matched inside a word; "
            "a project name must not be able to end the showcase"
        )

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("The project is bad.", "bad"),
            ("This lacks a clear use case.", "lacks"),
            ("A weak submission overall.", "weak"),
            ("Poor execution throughout.", "poor"),
            ("The build failed to deliver.", "failed to"),
            ("It is just a wrapper.", "just a"),
            ("Unfortunately, this missed the mark.", "unfortunately,"),
        ],
    )
    def test_real_teardown_language_is_still_caught(self, text, expected):
        """Loosening the match must not loosen the gate."""
        result = cbp.check_tone(text)
        assert not result["passed"]
        assert expected in result["banned_phrases"]

    def test_matching_is_case_insensitive(self):
        assert not cbp.check_tone("This Is BAD.")["passed"]

    def test_configured_phrases_are_matched_the_same_way(self):
        rubric = {"tone_policy": {"banned_phrases": ["clunky"]}}
        assert not cbp.check_tone("A clunky flow.", rubric)["passed"]
        assert cbp.check_tone("The clunkiness index.", rubric)["passed"]


class TestToneRepair:
    def test_clean_text_is_returned_untouched(self):
        repairs = []
        out = cbp._tone_repaired_text(
            "A genuinely strong build.", "bright_spot", None, fixed_clock, repairs
        )
        assert out == "A genuinely strong build."
        assert repairs == []

    def test_a_violation_is_replaced_and_recorded(self):
        repairs = []
        out = cbp._tone_repaired_text(
            "This lacks polish.", "bright_spot", None, fixed_clock, repairs
        )
        assert "lacks" not in out.lower()
        assert out == cbp.SAFE_TONE_FALLBACKS["bright_spot"]
        assert len(repairs) == 1
        assert repairs[0]["banned_phrases"] == ["lacks"]

    def test_the_replacement_never_needs_replacing(self):
        """A fallback that itself trips the gate would loop or fail."""
        for field, text in cbp.SAFE_TONE_FALLBACKS.items():
            assert cbp.check_tone(text)["passed"], f"{field} fallback trips the gate"

    def test_fallbacks_satisfy_the_extra_rules_their_fields_carry(self):
        """bright_spot needs positive framing; next_commit needs a forward verb."""
        card = {
            "bright_spot": cbp.SAFE_TONE_FALLBACKS["bright_spot"],
            "next_commit": cbp.SAFE_TONE_FALLBACKS["next_commit"],
            "panel_notes": cbp.SAFE_TONE_FALLBACKS["panel_notes"],
        }
        result = cbp.check_feedback_card_tone(card)
        assert result["passed"], result["missing_required"]

    def test_the_original_wording_is_fingerprinted_not_stored(self):
        """Keeping the sentence would carry teardown language into the bundle."""
        repairs = []
        cbp._tone_repaired_text(
            "This lacks polish and the demo is weak.",
            "bright_spot", None, fixed_clock, repairs,
        )
        record = json.dumps(repairs)
        assert "This lacks polish" not in record, "the original sentence was stored"
        # The phrase that tripped is still named — that is the audit value.
        assert set(repairs[0]["banned_phrases"]) == {"lacks", "weak"}
        assert len(repairs[0]["original_sha256"]) == 64


class TestFeedbackCardRepair:
    def test_only_the_offending_field_is_replaced(self):
        card = {
            "bright_spot": "A genuinely strong and creative build.",
            "next_commit": "Consider extending the core flow.",
            "panel_notes": "This lacks a clear use case.",
        }
        repairs = cbp.repair_feedback_card_tone(card, None, fixed_clock)
        assert card["bright_spot"] == "A genuinely strong and creative build."
        assert card["next_commit"] == "Consider extending the core flow."
        assert card["panel_notes"] == cbp.SAFE_TONE_FALLBACKS["panel_notes"]
        assert [r["field"] for r in repairs] == ["panel_notes"]

    def test_a_bad_judges_liked_highlight_is_recoverable(self):
        """
        The old blanket fallback never touched judges_liked, so a banned phrase
        there survived the retry and failed the card for good.
        """
        card = {
            "bright_spot": "A strong, creative build.",
            "next_commit": "Consider extending the core flow.",
            "judges_liked": [{"highlight": "The demo was weak but promising."}],
        }
        cbp.repair_feedback_card_tone(card, None, fixed_clock)
        assert cbp.check_feedback_card_tone(card)["passed"]

    def test_suggestion_lists_are_repaired_in_place(self):
        card = {
            "bright_spot": "A strong, creative build.",
            "next_commit": "Consider extending the core flow.",
            "copilot_next_moves": ["This is just a wrapper."],
        }
        cbp.repair_feedback_card_tone(card, None, fixed_clock)
        assert cbp.check_feedback_card_tone(card)["passed"]


class HarshGateway(MockGateway):
    """A judge that writes one banned word into every verdict."""

    def call_model(self, prompt: str, model_id: str) -> str:
        raw = super().call_model(prompt, model_id)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if isinstance(payload, dict) and "panel_notes" in payload:
            payload["panel_notes"] = "The execution lacks polish in places."
        return json.dumps(payload)


class TestShowcaseSurvivesAHarshJudge:
    def test_a_banned_word_no_longer_ends_the_run(self, tmp_path):
        """This raised ToneSafetyFailure and lost the whole event."""
        bundle_path = make_run(tmp_path, "harsh-judge")
        for index in range(1, 4):
            add_submission(bundle_path, project_name=f"Project {index}")

        full_judge_run(bundle_path, HarshGateway())

        verdicts = list((bundle_path / "verdicts").glob("*.json"))
        assert len(verdicts) == 3

    def test_no_builder_facing_field_carries_the_banned_word(self, tmp_path):
        """The point of the gate. Repairing must not weaken it."""
        bundle_path = make_run(tmp_path, "harsh-judge-tone")
        for index in range(1, 4):
            add_submission(bundle_path, project_name=f"Project {index}")
        full_judge_run(bundle_path, HarshGateway())

        for path in (bundle_path / "verdicts").glob("*.json"):
            verdict = json.loads(path.read_text())
            for reaction in verdict["archetype_verdicts"]:
                for field in ("perspective", "bright_spot"):
                    assert cbp.check_tone(reaction[field])["passed"], (
                        f"{field} still reaches the builder with teardown language"
                    )

    def test_the_substitution_is_recorded_in_the_sealed_bundle(self, tmp_path):
        """A silent rewrite would be worse than the crash."""
        bundle_path = make_run(tmp_path, "harsh-judge-audit")
        add_submission(bundle_path, project_name="Project 1")
        full_judge_run(bundle_path, HarshGateway())

        verdicts = [
            json.loads(path.read_text())
            for path in (bundle_path / "verdicts").glob("*.json")
        ]
        repaired = [v for v in verdicts if v.get("tone_repairs")]
        assert repaired, "the swap left no audit trail"
        assert repaired[0]["tone_repairs"][0]["banned_phrases"] == ["lacks"]

    def test_a_clean_run_records_no_repairs(self, tmp_path):
        bundle_path = make_run(tmp_path, "clean-judge")
        add_submission(bundle_path, project_name="Project 1")
        full_judge_run(bundle_path, MockGateway())
        for path in (bundle_path / "verdicts").glob("*.json"):
            assert "tone_repairs" not in json.loads(path.read_text())


class TestWinnerCardNaming:
    def test_the_gate_reads_the_framing_not_the_entrants_name(self):
        """
        The winner card is a fixed template plus a builder name, an award name,
        and a project name. Gating the whole string let an entrant abort their
        own ceremony by being called "Bad Idea Labs", and there is no safe
        substitution for someone's project name.
        """
        assert cbp.check_tone("wins the GitHub Copilot Builder Award")["passed"]
        # The award framing itself is still gated.
        assert not cbp.check_tone("wins the Least Bad Project Award")["passed"]

    def test_a_project_named_for_a_banned_word_can_still_win(self, tmp_path):
        """End to end: the ceremony must not abort on the entrant's own name."""
        import os
        from unittest.mock import patch

        run_id = "banned-name-award"
        gw = MockGateway()
        env = {
            "HJ_RUNS_DIR": str(tmp_path),
            "HJ_REGISTRY_PATH": str(tmp_path / "registry.ndjson"),
            "SHOWCASE_SKIP_UPDATE_CHECK": "1",
        }
        with patch.dict(os.environ, env):
            cbp.cmd_init(build_args("init", run_id=run_id), gw, fixed_clock)
            cbp.cmd_submit(
                build_args(
                    "submit",
                    run_id=run_id,
                    builder_name="Poorvi Shah",
                    project_name="Bad Idea Labs",
                    description="A weakly supervised idea triage tool.",
                ),
                gw,
                fixed_clock,
            )
            cbp.cmd_judge(build_args("judge", run_id=run_id), gw, fixed_clock)
            winner = cbp.load_shadow_score(tmp_path / run_id)["ranking"][0]
            rc = cbp.cmd_award(
                build_args("award", run_id=run_id, winner=winner), gw, fixed_clock
            )
        assert rc == 0, "an entrant's project name aborted their own award"
